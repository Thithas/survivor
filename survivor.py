"""
SURVIVOR — rules-only Polymarket BTC 5-min agent, built to run inside GitHub Actions.
Code is the law. Adaptation happens in params.json, reviewed via chat.

Control files in repo root (edit from your phone, picked up within ~2 min):
  LIVE         present -> real orders.  absent -> paper trading (default)
  HALT         present -> stop trading and exit the run
  params.json  tunables (bounded below). Hard limits live in HARD and never move.

Env (GitHub Secrets/Variables): POLY_PRIVATE_KEY, POLY_FUNDER, POLY_SIGNATURE_TYPE,
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, RUN_SECONDS
"""
import os, json, time, subprocess, datetime as dt
import requests

GAMMA, CLOB = "https://gamma-api.polymarket.com", "https://clob.polymarket.com"
SLUG_PREFIX, SERIES_SLUG = "btc-updown-5m-", "btc-up-or-down-5m"
MIN_SHARES = 5          # Polymarket engine minimum per order
STATE_FILE, TRADES_FILE, JOURNAL_FILE, WINDOWS_FILE = "state.json", "trades.jsonl", "journal.md", "windows.jsonl"
RUN_SECONDS = int(os.environ.get("RUN_SECONDS", "21000"))
IN_ACTIONS = bool(os.environ.get("GITHUB_ACTIONS"))
PULL_EVERY, COMMIT_EVERY = 120, 600

HARD = {"floor_usd": 30.0, "daily_loss_cap_usd": 5.0, "max_trade_pct": 0.10, "max_open_positions": 2}
BOUNDS = {"min_edge": (0.01, 0.08), "max_trade_pct": (0.02, 0.10), "momentum_min_confidence": (0.55, 0.85),
          "momentum_window_sec": (10, 45), "max_open_positions": (1, 3), "min_liquidity_usd": (20, 200),
          "fees": (0.0, 0.05), "slippage": (0.0, 0.05), "momentum_min_move_bps": (3, 30),
          "momentum_max_ask": (0.6, 0.9), "min_order_usd": (1.0, 5.0), "fee_rate": (0.0, 0.10),
          "take_profit_bid": (0.90, 1.0), "stop_loss_bid": (0.05, 0.50), "stop_loss_min_left_sec": (3, 60)}
DEFAULT_PARAMS = {"min_edge": 0.03, "max_trade_pct": 0.10, "momentum_min_confidence": 0.70,
                  "momentum_window_sec": 20, "max_open_positions": 2, "min_liquidity_usd": 50,
                  "fees": 0.0, "slippage": 0.01, "momentum_min_move_bps": 8, "momentum_max_ask": 0.85,
                  "min_order_usd": 1.0, "fee_rate": 0.07,
                  "take_profit_bid": 0.97, "stop_loss_bid": 0.25, "stop_loss_min_left_sec": 8}

# Paper-only exploration: loose thresholds so the log fills fast. Live ignores this entirely.
EXPLORE = {"momentum_min_move_bps": 3, "momentum_min_confidence": 0.55, "momentum_window_sec": 45,
           "momentum_max_ask": 0.90, "max_open_positions": 3, "min_liquidity_usd": 20}
def params():
    P = load_params()
    if not is_live(): P.update(EXPLORE)
    return P

def fee(p, P):
    """Polymarket crypto_fees_v2: taker pays rate * p * (1-p) per share; makers pay nothing."""
    return P["fee_rate"] * p * (1 - p)

def now(): return dt.datetime.now(dt.timezone.utc)
def today(): return now().date().isoformat()
def parse(s): return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
def log(*a): print(now().strftime("%H:%M:%S"), *a, flush=True)
def is_live(): return os.path.exists("LIVE")

# ---------- telegram ----------
def notify(msg):
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not tok or not chat: return
    try: requests.post(f"https://api.telegram.org/bot{tok}/sendMessage", json={"chat_id": chat, "text": msg}, timeout=10)
    except Exception as e: log("telegram fail", e)

# ---------- files & git ----------
def load_json(p, default):
    try: return json.load(open(p))
    except Exception: return default
def save_json(p, d): json.dump(d, open(p, "w"), indent=1)
def load_params():
    p = dict(DEFAULT_PARAMS); p.update(load_json("params.json", {}))
    for k, (lo, hi) in BOUNDS.items(): p[k] = min(hi, max(lo, p.get(k, DEFAULT_PARAMS[k])))
    return p
def journal(line):
    with open(JOURNAL_FILE, "a") as f: f.write(f"- {now().isoformat(timespec='seconds')} {line}\n")
def git(*a): return subprocess.run(["git", *a], capture_output=True, text=True)
RESTART = False
def pull():
    """Fetch phone-side edits. If the code itself changed, flag a restart so the next cron run picks it up."""
    global RESTART
    if not IN_ACTIONS: return
    before = git("rev-parse", "HEAD").stdout.strip()
    git("pull", "--rebase", "--autostash", "-q")
    after = git("rev-parse", "HEAD").stdout.strip()
    if before and after and before != after:
        changed = git("diff", "--name-only", before, after).stdout
        if "survivor.py" in changed or "survivor.yml" in changed: RESTART = True
def commit(state, msg="survivor: state"):
    save_json(STATE_FILE, state)
    for f in (TRADES_FILE, JOURNAL_FILE, WINDOWS_FILE):
        if not os.path.exists(f): open(f, "a").close()   # git add fails outright on a missing path
    if not IN_ACTIONS: return
    a = git("add", "--", STATE_FILE, TRADES_FILE, JOURNAL_FILE, WINDOWS_FILE)
    if a.returncode: log("git add failed", a.stderr[-200:]); return
    c = git("commit", "-q", "-m", msg)
    if c.returncode: log("nothing to commit"); return
    pull(); r = git("push", "-q")
    if r.returncode: log("push failed", r.stderr[-300:])

# ---------- polymarket ----------
_clob = None
def clob():
    global _clob
    if _clob is None:
        from py_clob_client.client import ClobClient
        _clob = ClobClient(CLOB, key=os.environ["POLY_PRIVATE_KEY"], chain_id=137,
                           signature_type=int(os.environ.get("POLY_SIGNATURE_TYPE", "1")),
                           funder=os.environ["POLY_FUNDER"])
        _clob.set_api_creds(_clob.create_or_derive_api_creds())
    return _clob

def live_balance():
    from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
    r = clob().get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
    return float(r["balance"]) / 1e6

def buy(token_id, usd):
    """Market FOK buy of `usd` collateral. Returns (ok, response)."""
    if not is_live(): return True, "paper"
    from py_clob_client.clob_types import MarketOrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY
    try:
        o = clob().create_market_order(MarketOrderArgs(token_id=token_id, amount=round(usd, 2), side=BUY, order_type=OrderType.FOK))
        r = clob().post_order(o, OrderType.FOK)
        return bool(r.get("success")), str(r)[:160]
    except Exception as e:
        return False, str(e)[:160]

def spot():
    try: return float(requests.get("https://api.coinbase.com/v2/prices/BTC-USD/spot", timeout=5).json()["data"]["amount"])
    except Exception:
        r = requests.get("https://api.kraken.com/0/public/Ticker?pair=XBTUSD", timeout=5).json()["result"]
        return float(list(r.values())[0]["c"][0])

def markets():
    # /markets hides this series ("Hide From New"); the events endpoint with series_slug is the reliable path.
    r = requests.get(f"{GAMMA}/events", params={"series_slug": SERIES_SLUG, "closed": "false", "limit": 30,
                                               "order": "endDate", "ascending": "true"}, timeout=10).json()
    out = []
    for ev in r:
        slug = ev.get("slug", "")
        if not slug.startswith(SLUG_PREFIX): continue
        m = (ev.get("markets") or [None])[0]
        if not m: continue
        try:
            toks, outs = json.loads(m["clobTokenIds"]), json.loads(m["outcomes"])
            up, down = toks[outs.index("Up")], toks[outs.index("Down")]
        except Exception: continue
        tail = slug.rsplit("-", 1)[-1]
        if tail.isdigit():   # window start epoch is in the slug
            start = dt.datetime.fromtimestamp(int(tail), dt.timezone.utc); end = start + dt.timedelta(minutes=5)
        else:
            start, end = parse(m["startDate"]), parse(m["endDate"])
        out.append({"slug": slug, "start": start, "end": end, "up": up, "down": down})
    return out

def top(token):
    """(best ask, $ at ask, best bid) for a token."""
    b = requests.get(f"{CLOB}/book", params={"token_id": token}, timeout=5).json()
    asks = [(float(a["price"]), float(a["size"])) for a in b.get("asks", [])]
    bids = [float(a["price"]) for a in b.get("bids", [])]
    ask, liq = (min(asks)[0], round(min(asks)[0] * min(asks)[1], 2)) if asks else (None, 0.0)
    return ask, liq, (max(bids) if bids else None)

def sell(token_id, shares):
    """Market FOK sell of `shares`. Returns (ok, response)."""
    if not is_live(): return True, "paper"
    from py_clob_client.clob_types import MarketOrderArgs, OrderType
    from py_clob_client.order_builder.constants import SELL
    try:
        o = clob().create_market_order(MarketOrderArgs(token_id=token_id, amount=round(shares, 2), side=SELL, order_type=OrderType.FOK))
        r = clob().post_order(o, OrderType.FOK)
        return bool(r.get("success")), str(r)[:160]
    except Exception as e:
        return False, str(e)[:160]

# ---------- brain (rules) ----------
def new_state(bankroll):
    return {"bankroll_usd": bankroll, "start_bankroll_usd": bankroll, "peak_bankroll_usd": bankroll,
            "today": today(), "today_pnl_usd": 0.0, "consecutive_losses": 0, "consecutive_wins": 0,
            "mode": "NORMAL", "open_positions": [], "closed_trades": 0, "opens": {},
            "stats": {"wins": 0, "losses": 0, "pnl": 0.0, "arb": 0, "momentum": 0}}

def day_roll(state):
    if state["today"] != today():
        state["today"], state["today_pnl_usd"] = today(), 0.0
        state["consecutive_losses"] = min(state["consecutive_losses"], 2)
        journal("day rolled")

def set_mode(state):
    if state["mode"] == "DEAD": return
    old = state["mode"]
    if state["bankroll_usd"] <= HARD["floor_usd"]: state["mode"] = "DEAD"
    elif state["today_pnl_usd"] <= -HARD["daily_loss_cap_usd"] or state["consecutive_losses"] >= 4: state["mode"] = "HIBERNATE"
    elif state["consecutive_losses"] >= 2 or 1 - state["bankroll_usd"] / state["peak_bankroll_usd"] > 0.10: state["mode"] = "CAUTIOUS"
    elif state["consecutive_wins"] >= 3 or old == "NORMAL": state["mode"] = "NORMAL"
    else: state["mode"] = "CAUTIOUS"
    if state["mode"] != old:
        journal(f"mode {old} -> {state['mode']}"); notify(f"mode {old} -> {state['mode']}")

def scan(state, P):
    sigs, px, t = [], spot(), now()
    ms = markets()
    diag = {"markets": len(ms), "in_window": 0, "best_sum": None, "slug": "", "hot": False, "spot": px}
    for m in ms:
        left = (m["end"] - t).total_seconds()
        if left <= 0 or left > 330: continue
        diag["in_window"] += 1
        if left <= P["momentum_window_sec"] + 15 or left >= 290: diag["hot"] = True
        if m["slug"] not in state["opens"] and abs((t - m["start"]).total_seconds()) <= 6:
            state["opens"][m["slug"]] = px
        ua, ul, ub = top(m["up"]); da, dl, db = top(m["down"])
        state.setdefault("books", {})[m["slug"]] = {"up": [ua, ub], "down": [da, db], "left": round(left)}
        # Near the close one side's asks often vanish (the winner gets hoarded). Keep recording; only skip what needs both sides.
        if left <= 75:   # window dataset: one snapshot every ~5s in the final stretch
            snaps = state.setdefault("snaps", {}).setdefault(m["slug"], {"end": m["end"].isoformat(), "open": state["opens"].get(m["slug"]), "s": []})
            if not snaps["s"] or snaps["s"][-1]["t"] - left >= 5:
                op = snaps["open"]
                snaps["s"].append({"t": round(left), "up": ua, "down": da, "mv": round((px - op) / op * 1e4, 1) if op else None})
        base = {"slug": m["slug"], "end": m["end"].isoformat(), "time_left_sec": round(left)}
        if ua is not None and da is not None:
            if diag["best_sum"] is None or ua + da < diag["best_sum"]: diag["best_sum"], diag["slug"] = round(ua + da, 3), m["slug"]
            gross = round(1 - (ua + da) - fee(ua, P) - fee(da, P), 4)
            if gross > 0:
                sigs.append({**base, "type": "ARB", "up": m["up"], "down": m["down"], "up_ask": ua, "down_ask": da,
                             "gross_edge": gross, "liquidity_usd": min(ul, dl), "confidence": 1.0})
        op = state["opens"].get(m["slug"])
        if op and left <= P["momentum_window_sec"]:
            mv = (px - op) / op * 1e4
            up_side = mv > 0
            ask, liq = (ua, ul) if up_side else (da, dl)
            conf = min(0.95, abs(mv) / (2 * P["momentum_min_move_bps"]))
            if ask is not None and abs(mv) >= P["momentum_min_move_bps"] and ask <= P["momentum_max_ask"]:
                sigs.append({**base, "type": "MOMENTUM", "token": m["up"] if up_side else m["down"],
                             "outcome": 0 if up_side else 1, "ask": ask, "gross_edge": round(conf - ask - fee(ask, P), 4),
                             "liquidity_usd": liq, "confidence": round(conf, 3), "move_bps": round(mv, 1)})
    state["opens"] = dict(list(state["opens"].items())[-30:])
    state["books"] = dict(list(state.get("books", {}).items())[-3:])
    diag["hot"] = diag["hot"] or any(0 < (m["end"] - t).total_seconds() <= 75 for m in ms)
    state["diag"] = diag
    return sigs

def decide(state, P, sigs):
    if state["mode"] in ("DEAD", "HIBERNATE"): return []
    caut = state["mode"] == "CAUTIOUS"
    min_edge = P["min_edge"] * (1.5 if caut else 1.0)
    room = min(HARD["max_open_positions"], P["max_open_positions"]) - len(state["open_positions"])
    taken = {p["slug"] for p in state["open_positions"]}
    orders = []
    for s in sorted(sigs, key=lambda s: (s["type"] != "ARB", -s["gross_edge"])):
        if room <= 0: break
        if s["slug"] in taken: continue
        net = s["gross_edge"] - P["fees"] - P["slippage"]
        if net < min_edge or s["liquidity_usd"] < P["min_liquidity_usd"]: continue
        if s["type"] == "MOMENTUM" and s["confidence"] < P["momentum_min_confidence"]: continue
        stake = state["bankroll_usd"] * min(HARD["max_trade_pct"], P["max_trade_pct"]) * s["confidence"] * (0.5 if caut else 1.0)
        stake = min(stake, s["liquidity_usd"] * 0.8)
        unit = (s["up_ask"] + s["down_ask"]) if s["type"] == "ARB" else s["ask"]
        stake = max(stake, MIN_SHARES * unit)                       # engine rejects < 5 shares per leg
        if stake > state["bankroll_usd"] * HARD["max_trade_pct"] + 0.01: continue
        if state["bankroll_usd"] - stake < HARD["floor_usd"]: continue
        orders.append((s, round(stake, 2), round(net, 4))); room -= 1; taken.add(s["slug"])
    return orders

def execute(state, s, stake, net):
    legs = []
    if s["type"] == "ARB":
        shares = stake / (s["up_ask"] + s["down_ask"])
        for tok, ask, idx in ((s["up"], s["up_ask"], 0), (s["down"], s["down_ask"], 1)):
            usd = round(shares * ask, 2); ok, resp = buy(tok, usd)
            legs.append({"token": tok, "outcome": idx, "shares": round(shares, 4), "cost": usd, "ok": ok, "resp": resp})
    else:
        ok, resp = buy(s["token"], stake)
        legs.append({"token": s["token"], "outcome": s["outcome"], "shares": round(stake / s["ask"], 4), "cost": stake, "ok": ok, "resp": resp})
    filled = [l for l in legs if l["ok"]]
    if not filled:
        journal(f"order failed {s['type']} {s['slug']}: {legs[0]['resp']}"); return
    cost = round(sum(l["cost"] for l in filled), 2)
    pos = {"slug": s["slug"], "type": s["type"], "legs": legs, "stake": cost, "predicted_edge": net,
           "ts": now().isoformat(timespec="seconds"), "end": s["end"], "live": is_live()}
    state["open_positions"].append(pos)
    if not is_live(): state["bankroll_usd"] -= cost
    partial = " PARTIAL" if len(filled) < len(legs) else ""
    msg = f"{'LIVE' if is_live() else 'PAPER'} {s['type']}{partial} {s['slug']} ${cost} edge {net}"
    journal(msg); notify(msg)

def resolve(slug):
    """Winner index (0=Up, 1=Down) once Gamma marks the market closed, else None."""
    ev = requests.get(f"{GAMMA}/events", params={"slug": slug}, timeout=10).json()
    m = (ev[0].get("markets") or [None])[0] if ev else None
    if not m: return None
    prices = [float(x) for x in json.loads(m.get("outcomePrices", "[]"))]
    return prices.index(max(prices)) if m.get("closed") and prices and max(prices) >= 0.99 else None

def settle_windows(state):
    """Write one line per finished window: open, snapshots, winner. This is the research dataset."""
    t, done = now(), []
    for slug, w in list(state.get("snaps", {}).items()):
        age = (t - parse(w["end"])).total_seconds()
        if age < 60: continue
        try: winner = resolve(slug)
        except Exception as e: log("window resolve err", e); continue
        if winner is None and age < 1800: continue
        with open(WINDOWS_FILE, "a") as f:
            f.write(json.dumps({"slug": slug, "end": w["end"], "open": w["open"], "winner": winner, "snaps": w["s"]}) + "\n")
        done.append(slug)
    for slug in done: state["snaps"].pop(slug, None)
    return bool(done)

def manage(state, P):
    """Close momentum positions early: lock in near-certain wins, cut clear losers while a bid still exists."""
    keep, changed = [], False
    for p in state["open_positions"]:
        b = state.get("books", {}).get(p["slug"])
        if p["type"] != "MOMENTUM" or not b:
            keep.append(p); continue
        leg = p["legs"][0]; bid = b["up"][1] if leg["outcome"] == 0 else b["down"][1]; left = b["left"]
        if bid is None: keep.append(p); continue
        reason = "take profit" if bid >= P["take_profit_bid"] else \
                 "stop loss" if (bid <= P["stop_loss_bid"] and left >= P["stop_loss_min_left_sec"]) else None
        if not reason: keep.append(p); continue
        ok, resp = sell(leg["token"], leg["shares"])
        if not ok:
            journal(f"sell failed ({reason}) {p['slug']}: {resp}"); keep.append(p); continue
        proceeds = round(leg["shares"] * (bid - fee(bid, P)), 4)
        record(state, p, round(proceeds - p["stake"], 4), -2, note=f"{reason} @ {bid:.2f} with {left}s left")
        changed = True
    state["open_positions"] = keep
    return changed

def settle(state):
    t, keep = now(), []
    for p in state["open_positions"]:
        age = (t - parse(p["end"])).total_seconds()
        if age < 45: keep.append(p); continue
        winner = None
        try: winner = resolve(p["slug"])
        except Exception as e: log("settle err", e)
        if winner is None:
            if age < 7200: keep.append(p); continue
            pnl, winner = 0.0, -1   # gave up; flag it
        else:
            payout = sum(l["shares"] for l in p["legs"] if l["ok"] and l["outcome"] == winner)
            pnl = round(payout - p["stake"], 4)
        record(state, p, pnl, winner)
    state["open_positions"] = keep

def record(state, p, pnl, winner, note=""):
    if not p["live"]: state["bankroll_usd"] += p["stake"] + pnl
    state["today_pnl_usd"] = round(state["today_pnl_usd"] + pnl, 4)
    st = state["stats"]; st["pnl"] = round(st["pnl"] + pnl, 4); st[p["type"].lower()] += 1
    if pnl > 0: st["wins"] += 1; state["consecutive_wins"] += 1; state["consecutive_losses"] = 0
    else: st["losses"] += 1; state["consecutive_losses"] += 1; state["consecutive_wins"] = 0
    state["closed_trades"] += 1
    rec = {"ts": p["ts"], "slug": p["slug"], "type": p["type"], "stake": p["stake"], "pnl": pnl,
           "predicted_edge": p["predicted_edge"], "winner": winner, "live": p["live"], "note": note}
    with open(TRADES_FILE, "a") as f: f.write(json.dumps(rec) + "\n")
    msg = f"{'sold' if winner == -2 else 'closed'} {p['type']} {p['slug']} pnl {pnl:+.2f}{' (' + note + ')' if note else ''} | today {state['today_pnl_usd']:+.2f} | bankroll {state['bankroll_usd']:.2f}"
    journal(msg); notify(msg)

def redispatch():
    """GitHub's cron is unreliable on quiet repos: start the next run ourselves when this one ends."""
    pat, repo = os.environ.get("GH_PAT"), os.environ.get("GITHUB_REPOSITORY")
    if not (pat and repo): return
    try:
        r = requests.post(f"https://api.github.com/repos/{repo}/actions/workflows/survivor.yml/dispatches",
                          headers={"Authorization": f"Bearer {pat}", "Accept": "application/vnd.github+json"},
                          json={"ref": "main"}, timeout=10)
        log("redispatch", r.status_code)
    except Exception as e: log("redispatch failed", e)

# ---------- loop ----------
def main():
    P = params()
    state = load_json(STATE_FILE, None) or new_state(live_balance() if is_live() else 50.0)
    t0 = time.time(); last_pull = last_commit = last_bal = time.time(); scans = 0; dirty = False
    scan_errs, last_err = 0, ""
    notify(f"run start {'LIVE' if is_live() else 'PAPER'} bankroll {state['bankroll_usd']:.2f} mode {state['mode']}")
    while time.time() - t0 < RUN_SECONDS:
        if os.path.exists("HALT"):
            journal("HALT found, exiting"); notify("HALT — stopped"); break
        day_roll(state)
        if is_live() and time.time() - last_bal > 60:
            try: state["bankroll_usd"] = live_balance()
            except Exception as e: log("balance err", e)
            last_bal = time.time()
        state["peak_bankroll_usd"] = max(state["peak_bankroll_usd"], state["bankroll_usd"])
        before = state["closed_trades"]; settle(state); dirty |= state["closed_trades"] != before
        try: dirty |= settle_windows(state)
        except Exception as e: log("settle_windows err", e)
        set_mode(state)
        if state["mode"] == "DEAD":
            journal(f"DEAD at bankroll {state['bankroll_usd']:.2f}. Post-mortem: stats {state['stats']}")
            notify("DEAD. Floor breached. Trading stopped permanently."); commit(state, "survivor: DEAD"); return
        try:
            sigs = scan(state, P); scans += 1; scan_errs = 0
            try: dirty |= manage(state, P)
            except Exception as e: log("manage err", e)
        except Exception as e:
            sigs, scan_errs, last_err = [], scan_errs + 1, f"{type(e).__name__}: {str(e)[:120]}"
            log("scan err", last_err)
            if scan_errs in (5, 100, 1000):
                journal(f"scan failing ({scan_errs} in a row): {last_err}"); notify(f"scan failing: {last_err}")
            time.sleep(5)
        for s, stake, net in decide(state, P, sigs):
            execute(state, s, stake, net); dirty = True
        if time.time() - last_pull > PULL_EVERY:
            pull(); P = params(); last_pull = time.time()
        if dirty or time.time() - last_commit > COMMIT_EVERY:
            d = state.get("diag", {})
            journal(f"heartbeat{'' if is_live() else ' [paper/explore]'}: {scans} scans, {len(state['open_positions'])} open, mode {state['mode']}, "
                    f"bankroll {state['bankroll_usd']:.2f}, markets {d.get('markets')}/{d.get('in_window')} in window, "
                    f"best up+down {d.get('best_sum')} on {d.get('slug')}, opens {len(state['opens'])}"
                    + (f", last error {last_err}" if scan_errs else ""))
            commit(state); last_commit = time.time(); dirty = False
        if RESTART:
            journal("code updated on main, restarting on next run"); notify("code updated, restarting")
            commit(state, "survivor: restart for new code"); redispatch(); return
        time.sleep(1 if state.get("diag", {}).get("hot") else 2)
    journal(f"run end: {scans} scans, mode {state['mode']}, bankroll {state['bankroll_usd']:.2f}, "
            f"today {state['today_pnl_usd']:+.2f}, open {len(state['open_positions'])}, stats {state['stats']}")
    commit(state, "survivor: run end")
    if state["mode"] != "DEAD" and not os.path.exists("HALT"): redispatch()

if __name__ == "__main__":
    main()
