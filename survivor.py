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
import os, json, time, subprocess, datetime as dt, socket
import requests
socket.setdefaulttimeout(20)

GAMMA, CLOB = "https://gamma-api.polymarket.com", "https://clob.polymarket.com"
# Every 5-minute Up/Down series Polymarket runs. Each asset is its own series; slug pattern <asset>-updown-5m-<epoch>.
ASSETS = {"btc": "BTC", "eth": "ETH", "sol": "SOL", "xrp": "XRP", "doge": "DOGE"}
SERIES = {a: f"{a}-up-or-down-5m" for a in ASSETS}
PREFIX = {a: f"{a}-updown-5m-" for a in ASSETS}
MIN_SHARES = 5          # Polymarket engine minimum per order
STATE_FILE, TRADES_FILE, JOURNAL_FILE, WINDOWS_FILE = "state.json", "trades.jsonl", "journal.md", "windows.jsonl"
RUN_SECONDS = int(os.environ.get("RUN_SECONDS", "21000"))
IN_ACTIONS = bool(os.environ.get("GITHUB_ACTIONS"))
PULL_EVERY, COMMIT_EVERY = 120, 600

HARD = {"floor_usd": 1.0, "daily_loss_cap_usd": 999.0, "max_trade_pct": 0.25, "max_open_positions": 5}   # owner removed the daily cap and the floor: the bot may trade the account to zero. 25%/trade is the only pacing left.
# Sized for a ~$20 bankroll: the engine's 5-share minimum makes one trade ~$3-4.5, i.e. 15-25% of bankroll.
# Floor $10 = room for roughly three losing trades in total; daily cap $5 = about two in a day, then hibernate.
BOUNDS = {"min_edge": (0.01, 0.08), "max_trade_pct": (0.02, 0.25), "momentum_min_confidence": (0.55, 0.85),
          "momentum_window_sec": (10, 150), "max_open_positions": (1, 5), "min_liquidity_usd": (20, 200),
          "fees": (0.0, 0.05), "slippage": (0.0, 0.05), "momentum_min_move_bps": (3, 30),
          "momentum_max_ask": (0.6, 0.9), "min_order_usd": (1.0, 5.0), "fee_rate": (0.0, 0.10),
          "take_profit_bid": (0.90, 1.0), "stop_loss_bid": (0.05, 0.50), "stop_loss_min_left_sec": (3, 60),
          "momentum_min_ask": (0.10, 0.60), "forced_at_sec": (20, 120), "forced_max_ask": (0.60, 0.95),
          "lock_from_bid": (0.60, 0.95), "lock_giveback": (0.10, 0.50), "stop_frac_of_entry": (0.3, 0.9)}
DEFAULT_PARAMS = {"min_edge": 0.03, "max_trade_pct": 0.10, "momentum_min_confidence": 0.70,
                  "momentum_window_sec": 20, "max_open_positions": 2, "min_liquidity_usd": 50,
                  "fees": 0.0, "slippage": 0.01, "momentum_min_move_bps": 8, "momentum_max_ask": 0.85,
                  "min_order_usd": 1.0, "fee_rate": 0.07,
                  "take_profit_bid": 0.97, "stop_loss_bid": 0.25, "stop_loss_min_left_sec": 8, "momentum_min_ask": 0.40,
                  "forced_at_sec": 60, "forced_max_ask": 0.92, "lock_from_bid": 0.85, "lock_giveback": 0.25, "stop_frac_of_entry": 0.6}

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
LIVE_BLOCKED = False   # set when LIVE is requested but Polymarket auth/balance fails; falls back to paper and pings you
def is_live(): return os.path.exists("LIVE") and not LIVE_BLOCKED

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

# ---------- polymarket (unified SDK: Deposit Wallet / pUSD, V2 CLOB) ----------
_pm, _pm_relay = None, None
def _install_bypass():
    """Vercel's free plan keeps a login wall on the relay URL; VERCEL_BYPASS is its official automation key.
    Attach it to every httpx client that targets the relay (the SDK builds its own clients, so hook the constructor)."""
    key = os.environ.get("VERCEL_BYPASS")
    if getattr(_install_bypass, "done", False): return
    import httpx
    orig = httpx.Client.__init__
    def patched(self, *a, **kw):
        kw["timeout"] = httpx.Timeout(20.0)           # never let one relay call hang the whole loop
        orig(self, *a, **kw)
        base = str(kw.get("base_url") or "")
        if key and "vercel.app" in base: self.headers["x-vercel-protection-bypass"] = key
    httpx.Client.__init__ = patched; _install_bypass.done = True
def _bypass_headers():
    key = os.environ.get("VERCEL_BYPASS"); return {"x-vercel-protection-bypass": key} if key else {}

def relay_url():
    """Public URL of the phone relay (file RELAY, written by phone/relay.py). GitHub runners are US IPs and
    Polymarket geoblocks order placement from there, so every CLOB call is routed through the phone."""
    try: return open("RELAY").read().strip() or None
    except Exception: return None

def pm():
    """Authenticated client, CLOB traffic via the phone relay. Rebuilt whenever the relay URL changes."""
    global _pm, _pm_relay
    relay = relay_url()
    if _pm is None or relay != _pm_relay:
        _install_bypass()
        import dataclasses
        from polymarket import SecureClient
        from polymarket.environments import PRODUCTION, _create_environment
        env = PRODUCTION
        if relay: env = _create_environment(name="relay", config=dataclasses.replace(PRODUCTION._config, clob_url=relay))
        _pm = SecureClient.create(private_key=os.environ["POLY_PRIVATE_KEY"], wallet=os.environ["POLY_FUNDER"], environment=env)
        _pm_relay = relay
        log("polymarket", _pm.wallet_type, str(_pm.wallet)[:10], "via relay" if relay else "DIRECT (orders will be geoblocked)")
    return _pm

def live_balance():
    """pUSD balance of the account wallet, in dollars."""
    r = pm().get_balance_allowance(asset_type="COLLATERAL")
    return r.balance / 1e6

def diagnose_funds():
    """Where is the money? SDK view of the wallet + on-chain pUSD/USDC.e of the account wallet."""
    out = []
    try:
        c = pm(); out.append(f"wallet_type={c.wallet_type} wallet={str(c.wallet)[:6]}…{str(c.wallet)[-4:]} signer={str(c.signer)[:6]}…{str(c.signer)[-4:]}")
        r = c.get_balance_allowance(asset_type="COLLATERAL"); out.append(f"clob pUSD balance={r.balance / 1e6:.2f} allowances={len(r.allowances)}")
        try: out.append(f"approvals={c.get_trading_approvals_state()}"[:120])
        except Exception as e: out.append(f"approvals err {str(e)[:60]}")
    except Exception as e: out.append(f"sdk err {str(e)[:100]}")
    funder = os.environ.get("POLY_FUNDER", "")
    tokens = {"pUSD": "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB", "USDC.e": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"}
    rpcs = ["https://polygon-bor-rpc.publicnode.com", "https://rpc.ankr.com/polygon", "https://polygon-rpc.com"]
    for tname, taddr in tokens.items():
        data = "0x70a08231" + funder[2:].lower().rjust(64, "0"); got = None; last = ""
        for rpc in rpcs:
            try:
                r = requests.post(rpc, json={"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": [{"to": taddr, "data": data}, "latest"]}, timeout=10).json()
                if "result" in r: got = int(r["result"], 16) / 1e6; break
                last = str(r.get("error", r))[:60]
            except Exception as e: last = str(e)[:60]
        out.append(f"onchain {tname}={got:.2f}" if got is not None else f"onchain {tname} err {last}")
    journal("funds check: " + " | ".join(out)); notify("funds check: " + " | ".join(out))

def _resp(r):
    ok = bool(getattr(r, "ok", False))
    return ok, (f"{r.order_id} {r.status}" if ok else f"{getattr(r, 'code', '?')}: {getattr(r, 'message', r)}")[:160]

def buy(token_id, usd):
    """Market FOK buy spending `usd` pUSD. Returns (ok, response)."""
    if not is_live(): return True, "paper"
    try: return _resp(pm().place_market_order(token_id=token_id, side="BUY", amount=str(round(usd, 2)), order_type="FOK"))
    except Exception as e: return False, str(e)[:160]

def sell(token_id, shares):
    """Market FOK sell of `shares`. Returns (ok, response)."""
    if not is_live(): return True, "paper"
    try: return _resp(pm().place_market_order(token_id=token_id, side="SELL", shares=str(round(shares, 2)), order_type="FOK"))
    except Exception as e: return False, str(e)[:160]

def spots():
    """Spot USD for every asset in one call (Coinbase exchange rates are USD->coin, so invert)."""
    out = {}
    try:
        rates = requests.get("https://api.coinbase.com/v2/exchange-rates", params={"currency": "USD"}, timeout=5).json()["data"]["rates"]
        for a, sym in ASSETS.items():
            if sym in rates and float(rates[sym]) > 0: out[a] = 1 / float(rates[sym])
    except Exception as e: log("spot err", e)
    if "btc" not in out:   # fallback so BTC never goes dark
        try: out["btc"] = float(requests.get("https://api.coinbase.com/v2/prices/BTC-USD/spot", timeout=5).json()["data"]["amount"])
        except Exception: pass
    return out
def spot(): return spots().get("btc")

_mcache = {}
def markets():
    """All 5-min Up/Down markets across assets. Gamma /markets hides these series; events?series_slug is reliable. Cached 30s."""
    out, t = [], time.time()
    for a in ASSETS:
        c = _mcache.get(a)
        if not c or t - c[0] > 30:
            try:
                r = requests.get(f"{GAMMA}/events", params={"series_slug": SERIES[a], "closed": "false", "limit": 20,
                                                           "order": "endDate", "ascending": "true"}, timeout=10).json()
            except Exception as e: log("markets err", a, e); r = []
            ms = []
            for ev in r or []:
                slug = ev.get("slug", "")
                if not slug.startswith(PREFIX[a]): continue
                m = (ev.get("markets") or [None])[0]
                if not m: continue
                try:
                    toks, outs = json.loads(m["clobTokenIds"]), json.loads(m["outcomes"])
                    up, down = toks[outs.index("Up")], toks[outs.index("Down")]
                except Exception: continue
                tail = slug.rsplit("-", 1)[-1]
                if tail.isdigit(): start = dt.datetime.fromtimestamp(int(tail), dt.timezone.utc); end = start + dt.timedelta(minutes=5)
                else: start, end = parse(m["startDate"]), parse(m["endDate"])
                ms.append({"asset": a, "slug": slug, "start": start, "end": end, "up": up, "down": down})
            _mcache[a] = (t, ms)
        out += _mcache[a][1]
    return out

def top(token):
    """(best ask, $ at ask, best bid) for a token."""
    b = requests.get(f"{CLOB}/book", params={"token_id": token}, timeout=5).json()
    asks = [(float(a["price"]), float(a["size"])) for a in b.get("asks", [])]
    bids = [float(a["price"]) for a in b.get("bids", [])]
    ask, liq, sz = (min(asks)[0], round(min(asks)[0] * min(asks)[1], 2), min(asks)[1]) if asks else (None, 0.0, 0.0)
    return ask, liq, (max(bids) if bids else None), sz

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

def equity(state):
    """Cash plus what's tied up in open positions — a buy moves cash into a position, it isn't a loss."""
    return state["bankroll_usd"] + sum(p["stake"] for p in state["open_positions"])

def set_mode(state):
    if state["mode"] == "DEAD": return
    old = state["mode"]; eq = equity(state)
    if eq <= HARD["floor_usd"]: state["mode"] = "DEAD"
    elif state["today_pnl_usd"] <= -HARD["daily_loss_cap_usd"]: state["mode"] = "HIBERNATE"
    elif state["consecutive_losses"] >= 2 or 1 - eq / state["peak_bankroll_usd"] > 0.10: state["mode"] = "CAUTIOUS"
    elif state["consecutive_wins"] >= 3 or old == "NORMAL": state["mode"] = "NORMAL"
    else: state["mode"] = "CAUTIOUS"
    if state["mode"] != old:
        journal(f"mode {old} -> {state['mode']}"); notify(f"mode {old} -> {state['mode']}")

def scan(state, P):
    sigs, sp, t = [], spots(), now()
    ms = markets()
    live_ms = [m for m in ms if 0 < (m["end"] - t).total_seconds() <= 330 and m["asset"] in sp]
    diag = {"markets": len(ms), "in_window": len(live_ms), "assets": sorted({m["asset"] for m in ms}), "best_sum": None, "slug": "", "hot": False, "spot": sp.get("btc")}
    # first pass: every asset's move this window, so each market can see whether its peers agree
    moves = {}
    for m in live_ms:
        if m["slug"] not in state["opens"] and abs((t - m["start"]).total_seconds()) <= 6: state["opens"][m["slug"]] = sp[m["asset"]]
        op = state["opens"].get(m["slug"])
        if op: moves[m["asset"]] = (sp[m["asset"]] - op) / op * 1e4
    for m in live_ms:
        left = (m["end"] - t).total_seconds(); px = sp[m["asset"]]
        if left <= P["momentum_window_sec"] + 15 or left >= 290: diag["hot"] = True
        try: ua, ul, ub, usz = top(m["up"]); da, dl, db, dsz = top(m["down"])
        except Exception as e: log("book err", m["slug"], e); continue
        state.setdefault("books", {})[m["slug"]] = {"up": [ua, ub], "down": [da, db], "left": round(left), "up_tok": m["up"], "down_tok": m["down"], "end": m["end"].isoformat()}
        # Near the close one side's asks often vanish (the winner gets hoarded). Keep recording; only skip what needs both sides.
        if left <= 300:  # window dataset: every ~10s, tightening to ~5s in the final 75s
            snaps = state.setdefault("snaps", {}).setdefault(m["slug"], {"asset": m["asset"], "end": m["end"].isoformat(), "open": state["opens"].get(m["slug"]), "s": []})
            if snaps["open"] is None and state["opens"].get(m["slug"]): snaps["open"] = state["opens"][m["slug"]]
            if not snaps["s"] or snaps["s"][-1]["t"] - left >= (5 if left <= 75 else 10):
                op = snaps["open"]
                snaps["s"].append({"t": round(left), "up": ua, "down": da, "mv": round((px - op) / op * 1e4, 1) if op else None})
        base = {"slug": m["slug"], "end": m["end"].isoformat(), "time_left_sec": round(left)}
        if ua is not None and da is not None:
            if diag["best_sum"] is None or ua + da < diag["best_sum"]: diag["best_sum"], diag["slug"] = round(ua + da, 3), m["slug"]
            gross = round(1 - (ua + da) - fee(ua, P) - fee(da, P), 4)
            if gross > 0:
                sigs.append({**base, "type": "ARB", "asset": m["asset"], "up": m["up"], "down": m["down"], "up_ask": ua, "down_ask": da,
                             "gross_edge": gross, "liquidity_usd": min(ul, dl), "liq_shares": min(usz, dsz), "confidence": 1.0})
        op = state["opens"].get(m["slug"])
        if op and left <= P["momentum_window_sec"]:
            mv = (px - op) / op * 1e4
            up_side = mv > 0
            ask, liq = (ua, ul) if up_side else (da, dl)
            # calibrated on recorded windows (spot move vs actual winner): the proxy feed is right ~55% under 10 bps,
            # ~70% at 10-20, ~80% above 20. Anything more optimistic than this lost paper money.
            a = abs(mv); conf = 0.50 if a < 3 else 0.55 if a < 10 else 0.70 if a < 20 else 0.80
            peers = [v for a2, v in moves.items() if a2 != m["asset"]]
            agree = sum(1 for v in peers if (v > 0) == up_side and abs(v) >= P["momentum_min_move_bps"] / 2)
            against = sum(1 for v in peers if (v > 0) != up_side and abs(v) >= P["momentum_min_move_bps"] / 2)
            conf = max(0.0, min(0.90, conf + min(0.05, 0.02 * agree) - 0.05 * against))
            if ask is not None and abs(mv) >= P["momentum_min_move_bps"] and P["momentum_min_ask"] <= ask <= P["momentum_max_ask"]:
                sigs.append({**base, "type": "MOMENTUM", "asset": m["asset"], "token": m["up"] if up_side else m["down"],
                             "outcome": 0 if up_side else 1, "ask": ask, "gross_edge": round(conf - ask - fee(ask, P), 4),
                             "liquidity_usd": liq, "confidence": round(conf, 3), "move_bps": round(mv, 1), "peers": f"{agree}/{against}"})
    state["opens"] = dict(list(state["opens"].items())[-60:])
    state["books"] = dict(list(state.get("books", {}).items())[-12:])
    diag["hot"] = diag["hot"] or any(0 < (m["end"] - t).total_seconds() <= 75 for m in live_ms)
    state["diag"] = diag
    return sigs

def decide(state, P, sigs):
    if state["mode"] in ("DEAD", "HIBERNATE"): return []
    caut = state["mode"] == "CAUTIOUS"
    min_edge = P["min_edge"] * (1.5 if caut else 1.0)
    room = min(HARD["max_open_positions"], P["max_open_positions"]) - sum(1 for p in state["open_positions"] if p["type"] != "ARB")
    arb_room = 3 - sum(1 for p in state["open_positions"] if p["type"] == "ARB")
    taken = {p["slug"] for p in state["open_positions"]} | set(state.get("traded", []))
    orders = []
    n_mom = max(1, sum(1 for s in sigs if s["type"] == "MOMENTUM" and s["confidence"] >= P["momentum_min_confidence"]))
    for s in sorted(sigs, key=lambda s: (s["type"] != "ARB", -s["gross_edge"])):
        if s["type"] == "ARB" and arb_room <= 0: continue
        if s["type"] != "ARB" and room <= 0: continue
        if s["slug"] in taken: continue
        net = s["gross_edge"] - P["fees"] - (0 if s["type"] == "ARB" else P["slippage"])   # arb is FOK at the quoted ask: no slippage term
        if net < (P["min_edge"] if s["type"] == "ARB" else min_edge): continue        # CAUTIOUS doesn't apply to riskless arb
        if s["type"] == "MOMENTUM" and (s["liquidity_usd"] < P["min_liquidity_usd"] or s["confidence"] < P["momentum_min_confidence"]): continue
        unit = (s["up_ask"] + s["down_ask"]) if s["type"] == "ARB" else s["ask"]
        if s["type"] == "ARB":
            shares = int(min(state["bankroll_usd"] * HARD["max_trade_pct"] / unit, s["liq_shares"] * 0.5))   # both legs must fill: never more than half the thinner book
            if shares < MIN_SHARES: continue
            stake = shares * unit
        else:
            stake = state["bankroll_usd"] * min(HARD["max_trade_pct"], P["max_trade_pct"]) * s["confidence"] * (0.5 if caut else 1.0)
            stake = min(stake / n_mom, s["liquidity_usd"] * 0.8)    # coins firing together are one bet split across them
            stake = max(stake, MIN_SHARES * unit)                   # engine rejects < 5 shares
            if stake > state["bankroll_usd"] * HARD["max_trade_pct"] + 0.01: continue
        if state["bankroll_usd"] - stake < HARD["floor_usd"]: continue
        orders.append((s, round(stake, 2), round(net, 4))); taken.add(s["slug"])
        if s["type"] == "ARB": arb_room -= 1
        else: room -= 1
    return orders

def forced_trade(state, P):
    """Owner's rule: one minimum-size trade per 5-minute cycle even without a signal. Least-bad version:
    at T-forced_at_sec buy 5 shares of the market's own favourite (highest ask <= forced_max_ask) — the side the market
    already expects to win, so the expected cost is just the fee. Skipped if a signal already traded this cycle."""
    if state["mode"] in ("DEAD", "HIBERNATE"): return None
    books = state.get("books", {})
    cyc = {slug: b for slug, b in books.items() if b["left"] <= P["forced_at_sec"] and b["left"] >= P["forced_at_sec"] - 12}
    if not cyc: return None
    epoch = list(cyc)[0].rsplit("-", 1)[-1]
    if state.get("forced_epoch") == epoch: return None
    if any(t.rsplit("-", 1)[-1] == epoch for t in state.get("traded", [])): state["forced_epoch"] = epoch; return None
    best = None
    for slug, b in cyc.items():
        for side, idx in (("up", 0), ("down", 1)):
            ask = b[side][0]
            if ask is None or ask > P["forced_max_ask"] or ask < 0.55: continue
            if best is None or ask > best[2]: best = (slug, side, ask, b[side + "_tok"], idx, b["end"])
    state["forced_epoch"] = epoch
    if not best: return None
    slug, side, ask, tok, idx, end = best
    return {"type": "MOMENTUM", "forced": True, "slug": slug, "end": end, "time_left_sec": cyc[slug]["left"], "token": tok,
            "outcome": idx, "ask": ask, "gross_edge": round(-fee(ask, P), 4), "liquidity_usd": 0, "confidence": ask, "move_bps": 0, "peers": "forced"}

def execute(state, s, stake, net):
    legs = []
    if s["type"] == "ARB":
        shares = int(stake / (s["up_ask"] + s["down_ask"]))
        order = sorted(((s["up"], s["up_ask"], 0), (s["down"], s["down_ask"], 1)), key=lambda x: -x[1])  # likely winner first
        for tok, ask, idx in order:
            usd = round(shares * ask, 2); ok, resp = buy(tok, usd)
            legs.append({"token": tok, "outcome": idx, "shares": float(shares), "cost": usd, "ok": ok, "resp": resp})
            if not ok: break
    else:
        ok, resp = buy(s["token"], stake)
        legs.append({"token": s["token"], "outcome": s["outcome"], "shares": round(stake / s["ask"], 4), "cost": stake, "ok": ok, "resp": resp})
    filled = [l for l in legs if l["ok"]]
    if not filled:
        journal(f"order failed {s['type']} {s['slug']}: {legs[0]['resp']}"); return
    cost = round(sum(l["cost"] for l in filled), 2)
    pos = {"slug": s["slug"], "type": s["type"], "legs": legs, "stake": cost, "predicted_edge": net,
           "entry_price": s.get("ask"),
           "ts": now().isoformat(timespec="seconds"), "end": s["end"], "live": is_live()}
    state["open_positions"].append(pos)
    state["traded"] = (state.get("traded", []) + [s["slug"]])[-40:]
    if not is_live(): state["bankroll_usd"] -= cost
    partial = " PARTIAL" if len(filled) < len(legs) else ""
    if partial:
        # half an arb is a naked bet. Unwind it this second; only if the sell fails do we keep it as a managed leg.
        leg = filled[0]; ok2, resp2 = sell(leg["token"], leg["shares"])
        if ok2:
            b = state.get("books", {}).get(s["slug"], {}); bid = (b.get("up") or [None, None])[1] if leg["outcome"] == 0 else (b.get("down") or [None, None])[1]
            proceeds = round(leg["shares"] * ((bid if bid else s["up_ask" if leg["outcome"] == 0 else "down_ask"]) - fee(bid or 0.5, P)), 4)
            journal(f"arb half-fill unwound {s['slug']}: sold {leg['shares']} back, ~{proceeds - cost:+.2f}"); notify(f"arb half-fill on {s['slug']} unwound (~{proceeds - cost:+.2f})")
            if not is_live(): state["bankroll_usd"] += proceeds - cost
            state["traded"] = (state.get("traded", []) + [s["slug"]])[-40:]
            return
        pos["type"] = "ARB_LEG"; pos["legs"] = filled          # unwind failed: manage it like a directional trade
    msg = f"{'LIVE' if is_live() else 'PAPER'} {'FORCED' if s.get('forced') else s['type']}{partial} {s['slug']} ${cost} edge {net}" + (f" move {s['move_bps']} bps peers {s['peers']}" if s["type"] == "MOMENTUM" and not s.get("forced") else "")
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
            f.write(json.dumps({"slug": slug, "asset": w.get("asset", "btc"), "end": w["end"], "open": w["open"], "winner": winner, "snaps": w["s"]}) + "\n")
        done.append(slug)
    for slug in done: state["snaps"].pop(slug, None)
    return bool(done)

def manage(state, P):
    """Close momentum positions early: lock in near-certain wins, cut clear losers while a bid still exists."""
    keep, changed = [], False
    for p in state["open_positions"]:
        b = state.get("books", {}).get(p["slug"])
        if p["type"] not in ("MOMENTUM", "ARB_LEG") or not b:
            keep.append(p); continue
        leg = p["legs"][0]; bid = b["up"][1] if leg["outcome"] == 0 else b["down"][1]; left = b["left"]
        if bid is None: keep.append(p); continue
        cyc = p["slug"].rsplit("-", 1)[-1]
        if state.get("bad_cycle") == cyc and bid < 0.6 and left >= P["stop_loss_min_left_sec"]:
            bid = min(bid, P["stop_loss_bid"])        # a sibling on this cycle already stopped: the whole tick was wrong
        p["peak_bid"] = max(p.get("peak_bid", 0.0), bid)          # trailing lock: once it was a near-certain win, don't ride it back down
        reason = "take profit" if bid >= P["take_profit_bid"] else \
                 "profit lock" if (p["peak_bid"] >= P["lock_from_bid"] and bid <= p["peak_bid"] - P["lock_giveback"] and left >= 3) else \
                 "stop loss" if (bid <= max(P["stop_loss_bid"], P["stop_frac_of_entry"] * (p.get("entry_price") or 1)) and left >= P["stop_loss_min_left_sec"]) else None
        if not reason: keep.append(p); continue
        ok, resp = sell(leg["token"], leg["shares"])
        if not ok:
            journal(f"sell failed ({reason}) {p['slug']}: {resp}"); keep.append(p); continue
        proceeds = round(leg["shares"] * (bid - fee(bid, P)), 4)
        record(state, p, round(proceeds - p["stake"], 4), -2, note=f"{reason} @ {bid:.2f} with {left:.0f}s left")
        if reason == "stop loss": state["bad_cycle"] = p["slug"].rsplit("-", 1)[-1]
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
    st = state["stats"]; st["pnl"] = round(st["pnl"] + pnl, 4); st[p["type"].lower()] = st.get(p["type"].lower(), 0) + 1
    if pnl > 0: st["wins"] += 1; state["consecutive_wins"] += 1; state["consecutive_losses"] = 0
    else: st["losses"] += 1; state["consecutive_losses"] += 1; state["consecutive_wins"] = 0
    state["closed_trades"] += 1
    state["peak_bankroll_usd"] = max(state.get("peak_bankroll_usd", 0), equity(state))
    rec = {"ts": p["ts"], "slug": p["slug"], "type": p["type"], "stake": p["stake"], "pnl": pnl,
           "predicted_edge": p["predicted_edge"], "winner": winner, "live": p["live"], "note": note}
    with open(TRADES_FILE, "a") as f: f.write(json.dumps(rec) + "\n")
    msg = f"{'sold' if winner == -2 else 'closed'} {p['type']} {p['slug']} pnl {pnl:+.2f}{' (' + note + ')' if note else ''} | today {state['today_pnl_usd']:+.2f} | bankroll {state['bankroll_usd']:.2f}"
    journal(msg); notify(msg)

_relay_fail = 0
def relay_alive():
    """True if the relay answers /health. Three misses in a row block live trading and ping once; recovery unblocks."""
    global _relay_fail, LIVE_BLOCKED
    url = relay_url()
    ok = False
    if url:
        try: ok = bool((requests.get(url + "/health", headers=_bypass_headers(), timeout=10).json() or {}).get("ok"))   # a login page is not "ok"
        except Exception: ok = False
    _relay_fail = 0 if ok else _relay_fail + 1
    if not ok and _relay_fail == 3 and not LIVE_BLOCKED and os.path.exists("LIVE"):
        LIVE_BLOCKED = True; journal("relay offline — live blocked"); notify("Relay offline: open the Codespace (github.com/Thithas/survivor → Code → Codespaces). Paper until it's back.")
    if ok and LIVE_BLOCKED and os.path.exists("LIVE"):
        LIVE_BLOCKED = False; journal("relay back — live resumed"); notify("Relay back. LIVE resumed.")
    return ok

def sweep_redeem(state):
    """Winnings on Polymarket sit as resolved shares until redeemed; the cash balance (and our sizing) ignores them.
    Every few minutes, claim everything the Data API marks redeemable."""
    if not is_live(): return
    try:
        r = requests.get("https://data-api.polymarket.com/positions", params={"user": os.environ["POLY_FUNDER"], "sizeThreshold": 0, "limit": 100}, timeout=15).json()
    except Exception as e: log("positions err", e); return
    claimed = 0
    for pos in r if isinstance(r, list) else []:
        if not pos.get("redeemable") or float(pos.get("size", 0)) <= 0: continue
        try:
            h = pm().redeem_positions(condition_id=pos["conditionId"]); h.wait()
            claimed += 1; journal(f"claimed {pos.get('title', pos['conditionId'][:10])}: {float(pos['size']):.2f} shares")
        except Exception as e: log("redeem err", pos.get("title", ""), str(e)[:100])
    if claimed: notify(f"claimed winnings on {claimed} market(s)")

def check_relay(state):
    """Prove the relay path end to end: /health (which region answers) and a balance read through it."""
    url = relay_url()
    if not url: journal("relay: none"); notify("relay: none — orders from GitHub are geoblocked"); return
    try:
        h = requests.get(url + "/health", headers=_bypass_headers(), timeout=15); region = (h.json() or {}).get("region", "?") if h.ok else f"HTTP {h.status_code}"
    except Exception as e: region = f"unreachable: {str(e)[:80]}"
    global _pm; _pm = None
    try: bal = f"balance via relay {live_balance():.2f}"
    except Exception as e: bal = f"balance via relay FAILED: {str(e)[:100]}"
    msg = f"relay {url.split('/')[2]} region {region} | {bal}"
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
    global LIVE_BLOCKED
    P = params()
    state = load_json(STATE_FILE, None)
    def fresh_state():
        global LIVE_BLOCKED
        if os.path.exists("LIVE") and not relay_url():
            LIVE_BLOCKED = True
            journal("LIVE requested but no phone relay (file RELAY) — orders from GitHub are geoblocked; paper until the phone is up")
            notify("LIVE waiting: phone relay is offline. Start it in Termux (bash ~/survivor/start.sh). Paper until then.")
        elif os.path.exists("LIVE"):
            try:
                b = live_balance()
                if b < HARD["floor_usd"] + 1:
                    LIVE_BLOCKED = True
                    try: diagnose_funds()
                    except Exception as e: journal(f"funds check failed: {str(e)[:100]}")
                    journal(f"LIVE requested but Polymarket balance is {b:.2f} (need > {HARD['floor_usd'] + 1:.0f}) — paper until funds land")
                    notify(f"LIVE waiting: Polymarket balance reads {b:.2f}. Paper until it's above {HARD['floor_usd'] + 1:.0f}. Rechecking every minute.")
                else:
                    st = new_state(b); st["live_mode"] = True
                    journal(f"LIVE mode on. Polymarket balance {b:.2f}"); notify(f"LIVE. Real money. Balance {b:.2f}. Floor {HARD['floor_usd']}, daily cap {HARD['daily_loss_cap_usd']}, max {int(HARD['max_trade_pct']*100)}%/trade.")
                    return st
            except Exception as e:
                LIVE_BLOCKED = True
                journal(f"LIVE requested but Polymarket auth/balance failed: {str(e)[:120]} — staying on paper"); notify(f"LIVE blocked: {str(e)[:120]}. Running paper until fixed.")
        st = new_state(50.0); st["live_mode"] = False; return st
    if state is None or (state.get("mode") == "DEAD" and state.get("bankroll_usd", 0) <= 0) or bool(state.get("live_mode")) != os.path.exists("LIVE"):
        state = fresh_state(); commit(state, "survivor: startup")
    if os.path.exists("LIVE"): state["relay_seen"] = relay_url(); check_relay(state)
    t0 = time.time(); last_pull = last_commit = last_bal = time.time(); last_sweep = 0; scans = 0; dirty = False
    scan_errs, last_err = 0, ""
    notify(f"run start {'LIVE' if is_live() else 'PAPER'} bankroll {state['bankroll_usd']:.2f} mode {state['mode']}")
    while time.time() - t0 < RUN_SECONDS:
        if os.path.exists("HALT"):
            journal("HALT found, exiting"); notify("HALT — stopped"); break
        day_roll(state)
        if is_live() and time.time() - last_sweep > 300:
            sweep_redeem(state); last_sweep = time.time()
        if os.path.exists("LIVE") and time.time() - last_bal > 60:
            try:
                if not relay_alive(): raise RuntimeError("relay offline")
                b = live_balance()
                if LIVE_BLOCKED and relay_url() and b >= HARD["floor_usd"] + 1 and not state["open_positions"]:
                    LIVE_BLOCKED = False; commit(state, "survivor: funds landed"); state = fresh_state(); P = params(); dirty = True
                elif is_live(): state["bankroll_usd"] = b
            except Exception as e: log("balance err", e)
            last_bal = time.time()
        pass
        before = state["closed_trades"]; settle(state); dirty |= state["closed_trades"] != before
        try: dirty |= settle_windows(state)
        except Exception as e: log("settle_windows err", e)
        set_mode(state)
        if state["mode"] == "DEAD":
            journal(f"DEAD at bankroll {state['bankroll_usd']:.2f}. Post-mortem: stats {state['stats']}")
            notify("DEAD. Floor breached. Trading stopped permanently."); commit(state, "survivor: DEAD"); return
        try:
            sigs = scan(state, P); scans += 1; scan_errs = 0
        except Exception as e:
            sigs, scan_errs, last_err = [], scan_errs + 1, f"{type(e).__name__}: {str(e)[:120]}"
            log("scan err", last_err)
            if scan_errs in (5, 100, 1000):
                journal(f"scan failing ({scan_errs} in a row): {last_err}"); notify(f"scan failing: {last_err}")
            time.sleep(5)
        try: dirty |= manage(state, P)          # runs even when the scan failed: a held position must never go unwatched
        except Exception as e: log("manage err", e)
        for s, stake, net in decide(state, P, sigs):
            execute(state, s, stake, net); dirty = True
        try:
            f = forced_trade(state, P)
            if f and f["slug"] not in {p["slug"] for p in state["open_positions"]}:
                stake = round(MIN_SHARES * f["ask"], 2)
                if stake <= state["bankroll_usd"]: execute(state, f, stake, f["gross_edge"]); dirty = True
        except Exception as e: log("forced err", e)
        if time.time() - last_pull > PULL_EVERY:
            pull(); P = params(); last_pull = time.time()
            if relay_url() != state.get("relay_seen"):
                state["relay_seen"] = relay_url(); check_relay(state)
            if bool(state.get("live_mode")) != is_live() and not state["open_positions"]:
                commit(state, "survivor: mode switch"); state = fresh_state(); P = params(); dirty = True
        if dirty or time.time() - last_commit > COMMIT_EVERY:
            d = state.get("diag", {})
            journal(f"heartbeat{'' if is_live() else ' [paper/explore]'}: {scans} scans, {len(state['open_positions'])} open, mode {state['mode']}, "
                    f"bankroll {state['bankroll_usd']:.2f}, markets {d.get('markets')}/{d.get('in_window')} in window across {d.get('assets')}, "
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
