# SURVIVOR

Rules-only Polymarket BTC 5-min agent. Runs on GitHub Actions. Controlled from a phone.
Hard limits, enforced in `survivor.py` and verified by an offline harness: **floor $30** (agent stops permanently below it), **daily loss cap $12** (hibernates until the next UTC day), **max 10% of bankroll per trade**, **max 2 open positions**. If the exchange's 5-share minimum would exceed the 10% cap, the trade is skipped and logged as MIN_ORDER_BLOCK. Forced trading and arbitrage are removed from the code, not merely switched off.

## Setup (all from phone browser, github.com)

1. **Repo** — New repository → name `survivor` → Public → Create.
2. **Files** — Add file → Create new file, paste each of these (the workflow's filename is the full path `.github/workflows/survivor.yml`):
   `survivor.py`, `params.json`, `requirements.txt`, `.github/workflows/survivor.yml`, `README.md`
3. **Telegram** — message @BotFather → `/newbot` → copy the token. Send your new bot any message, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `chat.id`.
4. **Secrets** — Settings → Secrets and variables → Actions → New repository secret:
   `POLY_PRIVATE_KEY`, `POLY_FUNDER` (your Polymarket profile address), `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
   Variables tab (optional): `POLY_SIGNATURE_TYPE` = `1` email login / `2` browser wallet; `RUN_SECONDS` (default 21000 ≈ 5h50m per job).
   Paper mode needs only the two Telegram secrets.
5. **Start** — Actions tab → enable workflows → survivor → Run workflow. From then on the cron keeps it alive.

## Controls (create/delete empty files in repo root)

- `LIVE` — real orders. Absent = paper trading. Delete `state.json` when switching so bankroll re-reads from chain.
- `HALT` — stops trading and exits. Delete it to resume on the next run.

## Read

- `journal.md` — what it did and why. `trades.jsonl` — every closed trade. `state.json` — bankroll, mode, open positions.
- Telegram pings on every order, close, mode change, HALT, DEAD.

## Daily review (the adaptive part)

Paste into Claude: the last ~30 lines of `journal.md`, the last ~20 lines of `trades.jsonl`, and `params.json`.
You get back a new `params.json`; paste it over the old one. One parameter changes per review, inside these bounds:
min_edge 0.01–0.08, max_trade_pct 0.02–0.10, momentum_min_confidence 0.55–0.85, momentum_window_sec 10–45,
max_open_positions 1–3, min_liquidity_usd 20–200, fees 0–0.05, slippage 0–0.05, momentum_min_move_bps 3–30,
momentum_max_ask 0.6–0.9, min_order_usd 1–5.

## Known unknowns

- Set `fees` to whatever Polymarket charges on these markets before going live; the default 0 is optimistic.
- Momentum uses Coinbase spot as a proxy for the resolution price. Treat it as an experiment until the log says otherwise.
- GitHub Actions is meant for CI. Low-volume use is unlikely to be flagged, but that risk is yours.

## The hand: a GitHub Codespace (required for live)

GitHub's Actions machines are in the US and Polymarket rejects orders from there. The brain stays on Actions; a Codespace in
West Europe forwards its signed orders through an allowed IP. The private key never leaves GitHub Secrets.

One-time: https://github.com/settings/codespaces → **Region: West Europe** · **Default idle timeout: 240 minutes**.
Every day: repo → **Code → Codespaces → Open** (or *Create codespace on main* the first time). Leave the tab open.
The relay starts itself, publishes its URL to `RELAY`, and Telegram says "relay … | balance via relay …" then "LIVE resumed".
When the Codespace stops, Telegram says "Relay offline: open the Codespace" and the bot trades paper until you do.

## Decisions and lessons

- `decisions.jsonl` — one line per decision: ENTER, SKIP or EXIT, with the reason in plain words,
  plus ask, move, peers, confidence, edge, time left, stake and P&L.
- `lessons.json` — outcome counts by bucket (what we paid, how many coins agreed, how late we entered).

The loop is deliberately one-directional: a bucket with 25+ trades, a win rate under 45% and a negative
net gets **vetoed** — the agent stops taking that kind of trade. It can never loosen a rule, raise a size,
or change its own code. Set `lessons_enabled` to 0 in `params.json` to switch it off.
