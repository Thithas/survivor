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
SLUG_PREFIX = "btc-updown-5m-"
STATE_FILE, TRADES_FILE, JOURNAL_FILE = "state.json", "trades.jsonl", "journal.md"
RUN_SECONDS = int(os.environ.get("RUN_SECONDS", "21000"))
IN_ACTIONS = bool(os.environ.get("GITHUB_ACTIONS"))
PULL_EVERY, COMMIT_EVERY = 120, 600

HARD = {"floor_usd": 30.0, "daily_loss_cap_usd": 5.0, "max_trade_pct": 0.10, "max_open_positions": 2}
BOUNDS = {"min_edge": (0.01, 0.08), "max_trade_pct": (0.02, 0.10), "momentum_min_confidence": (0.55, 0.85),
          "momentum_window_sec": (10, 45), "max_open_positions": (1, 3), "min_liquidity_usd": (20, 200),
          "fees": (0.0, 0.05), "slippage": (0.0, 0.05), "momentum_min_move_bps": (3, 30),
          "momentum_max_ask": (0.6, 0.9), "min_order_usd": (1.0, 5.0)}
DEFAULT_PARAMS = {"min_edge": 0.03, "max_trade_pct": 0.10, "momentum_min_confidence": 0.70,
                  "momentum_window_sec": 20, "max_open_positions": 2, "min_liquidity_usd": 50,
                  "fees": 0.0, "slippage": 0.01, "momentum_min_move_bps": 8, "momentum_max_ask": 0.85,
                  "min_order_usd": 1.0}

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
def pull():
    if IN_ACTIONS: git("pull", "--rebase", "--autostash", "-q")
def commit(state, msg="survivor: state"):
    save_json(STATE_FILE, state)
    if not IN_ACTIONS: return
    git("add", STATE_FILE, TRADES_FILE, JOURNAL_FILE)
    if git("commit", "-q", "-m", msg).returncode == 0:
        pull(); r = git("push", "-q")
        if r.returncode: log("push failed", r.stderr[-200:])

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
    r = requests.get(f"{GAMMA}/markets", params={"limit": 40, "closed": "false", "order": "startDate", "ascending": "false"}, timeout=10).json()
    out = []
    for m in r:
        slug = m.get("slug", "")
        if not slug.startswith(SLUG_PREFIX): continue
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

def best_ask(token):
    b = requests.get(f"{CLOB}/book", params={"token_id": token}, timeout=5).json()
    asks = [(float(a["price"]), float(a["size"])) for a in b.get("asks", [])]
    if not asks: return None, 0.0
    p, s = min(asks); return p, round(p * s, 2)

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
    for m in markets():
        left = (m["end"] - t).total_seconds()
        if left <= 0 or left > 330: continue
        if m["slug"] not in state["opens"] and abs((t - m["start"]).total_seconds()) <= 6:
            state["opens"][m["slug"]] = px
        ua, ul = best_ask(m["up"]); da, dl = best_ask(m["down"])
        if ua is None or da is None: continue
        base = {"slug": m["slug"], "end": m["end"].isoformat(), "time_left_sec": round(left)}
        gross = round(1 - (ua + da), 4)
        if gross > 0:
            sigs.append({**base, "type": "ARB", "up": m["up"], "down": m["down"], "up_ask": ua, "down_ask": da,
                         "gross_edge": gross, "liquidity_usd": min(ul, dl), "confidence": 1.0})
        op = state["opens"].get(m["slug"])
        if op and left <= P["momentum_window_sec"]:
            mv = (px - op) / op * 1e4
            up_side = mv > 0
            ask, liq = (ua, ul) if up_side else (da, dl)
            conf = min(0.95, abs(mv) / (2 * P["momentum_min_move_bps"]))
            if abs(mv) >= P["momentum_min_move_bps"] and ask <= P["momentum_max_ask"]:
                sigs.append({**base, "type": "MOMENTUM", "token": m["up"] if up_side else m["down"],
                             "outcome": 0 if up_side else 1, "ask": ask, "gross_edge": round(conf - ask, 4),
                             "liquidity_usd": liq, "confidence": round(conf, 3), "move_bps": round(mv, 1)})
    state["opens"] = dict(list(state["opens"].items())[-30:])
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
        if state["bankroll_usd"] - stake < HARD["floor_usd"]: continue
        if stake < P["min_order_usd"] * (2 if s["type"] == "ARB" else 1): continue
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

def settle(state):
    t, keep = now(), []
    for p in state["open_positions"]:
        age = (t - parse(p["end"])).total_seconds()
        if age < 45: keep.append(p); continue
        winner = None
        try:
            m = requests.get(f"{GAMMA}/markets", params={"slug": p["slug"]}, timeout=10).json()[0]
            prices = [float(x) for x in json.loads(m.get("outcomePrices", "[]"))]
            if m.get("closed") and prices and max(prices) >= 0.99: winner = prices.index(max(prices))
        except Exception as e: log("settle err", e)
        if winner is None:
            if age < 7200: keep.append(p); continue
            pnl, winner = 0.0, -1   # gave up; flag it
        else:
            payout = sum(l["shares"] for l in p["legs"] if l["ok"] and l["outcome"] == winner)
            pnl = round(payout - p["stake"], 4)
        record(state, p, pnl, winner)
    state["open_positions"] = keep

def record(state, p, pnl, winner):
    if not p["live"]: state["bankroll_usd"] += p["stake"] + pnl
    state["today_pnl_usd"] = round(state["today_pnl_usd"] + pnl, 4)
    st = state["stats"]; st["pnl"] = round(st["pnl"] + pnl, 4); st[p["type"].lower()] += 1
    if pnl > 0: st["wins"] += 1; state["consecutive_wins"] += 1; state["consecutive_losses"] = 0
    else: st["losses"] += 1; state["consecutive_losses"] += 1; state["consecutive_wins"] = 0
    state["closed_trades"] += 1
    rec = {"ts": p["ts"], "slug": p["slug"], "type": p["type"], "stake": p["stake"], "pnl": pnl,
           "predicted_edge": p["predicted_edge"], "winner": winner, "live": p["live"]}
    with open(TRADES_FILE, "a") as f: f.write(json.dumps(rec) + "\n")
    msg = f"closed {p['type']} {p['slug']} pnl {pnl:+.2f} | today {state['today_pnl_usd']:+.2f} | bankroll {state['bankroll_usd']:.2f}"
    journal(msg); notify(msg)

# ---------- loop ----------
def main():
    P = load_params()
    state = load_json(STATE_FILE, None) or new_state(live_balance() if is_live() else 50.0)
    t0 = time.time(); last_pull = last_commit = last_bal = time.time(); scans = 0; dirty = False
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
        set_mode(state)
        if state["mode"] == "DEAD":
            journal(f"DEAD at bankroll {state['bankroll_usd']:.2f}. Post-mortem: stats {state['stats']}")
            notify("DEAD. Floor breached. Trading stopped permanently."); commit(state, "survivor: DEAD"); return
        try: sigs = scan(state, P)
        except Exception as e:
            log("scan err", e); time.sleep(5); continue
        scans += 1
        for s, stake, net in decide(state, P, sigs):
            execute(state, s, stake, net); dirty = True
        if time.time() - last_pull > PULL_EVERY:
            pull(); P = load_params(); last_pull = time.time()
        if dirty or time.time() - last_commit > COMMIT_EVERY:
            commit(state); last_commit = time.time(); dirty = False
        hot = any(s["time_left_sec"] <= P["momentum_window_sec"] + 15 for s in sigs) or bool(state["opens"])
        time.sleep(1 if hot else 3)
    journal(f"run end: {scans} scans, mode {state['mode']}, bankroll {state['bankroll_usd']:.2f}, "
            f"today {state['today_pnl_usd']:+.2f}, open {len(state['open_positions'])}, stats {state['stats']}")
    commit(state, "survivor: run end")

if __name__ == "__main__":
    main()
