"""
Daraz price & stock watcher.
Reads product URLs from watch.txt, checks each on a schedule, pings Telegram when price or stock changes,
keeps a full history in prices.jsonl. Runs on GitHub Actions. No API keys, no money at risk.

Env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""
import os, re, json, html, time, datetime as dt, subprocess
import requests

WATCH, STATE, HIST, LOG = "daraz/watch.txt", "daraz/state.json", "daraz/prices.jsonl", "daraz/log.md"
IN_ACTIONS = bool(os.environ.get("GITHUB_ACTIONS"))
UA = {"User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36",
      "Accept-Language": "en-US,en;q=0.9", "Accept": "text/html,application/xhtml+xml"}

def now(): return dt.datetime.now(dt.timezone.utc)
def log(*a): print(now().strftime("%H:%M:%S"), *a, flush=True)

def notify(msg):
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    log(msg)
    if not (tok and chat): return
    try: requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                       json={"chat_id": chat, "text": msg, "disable_web_page_preview": True}, timeout=15)
    except Exception as e: log("telegram fail", e)

# ---------- scraping ----------
def money(v):
    """'Tk. 15,999' / '৳14499.00' / 15999 -> 15999.0"""
    if v is None: return None
    if isinstance(v, (int, float)): return float(v)
    s = re.sub(r"[^\d.]", "", str(v).replace(",", ""))
    try: return float(s) if s else None
    except ValueError: return None

def parse(html_text):
    """Daraz ships the product as JSON inside the page. Try the structured blobs first, then fall back to
    the visible markup, so a template change degrades instead of breaking."""
    title = price = was = None; stock = True

    # 1) __moduleData__ / pageData blob
    for pat in (r"var __moduleData__\s*=\s*(\{.*?\});?\s*</script>", r"window\.pageData\s*=\s*(\{.*?\});?\s*</script>"):
        m = re.search(pat, html_text, re.S)
        if not m: continue
        try: data = json.loads(m.group(1))
        except Exception: continue
        blob = json.dumps(data)
        for k in ("salePrice", "price", "priceNumber"):
            mm = re.search(rf'"{k}"\s*:\s*"?([\d,.]+)"?', blob)
            if mm and money(mm.group(1)): price = money(mm.group(1)); break
        mm = re.search(r'"(?:originalPrice|listPrice|oriPriceNumber)"\s*:\s*"?([\d,.]+)"?', blob)
        if mm: was = money(mm.group(1))
        mm = re.search(r'"(?:name|title)"\s*:\s*"([^"]{6,160})"', blob)
        if mm: title = html.unescape(mm.group(1))
        if re.search(r'"(?:stock|quantity)"\s*:\s*"?0"?[,}]', blob) or '"outOfStock":true' in blob: stock = False
        if price: break

    # 2) JSON-LD
    if not price:
        for m in re.finditer(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html_text, re.S):
            try: d = json.loads(m.group(1))
            except Exception: continue
            items = d if isinstance(d, list) else [d]
            for it in items:
                if not isinstance(it, dict): continue
                offer = it.get("offers") or {}
                if isinstance(offer, list): offer = offer[0] if offer else {}
                if offer.get("price"):
                    price = money(offer["price"]); title = title or it.get("name")
                    if "OutOfStock" in str(offer.get("availability", "")): stock = False
                    break
            if price: break

    # 3) visible markup
    if not price:
        m = re.search(r'class="[^"]*pdp-price[^"]*"[^>]*>\s*([^<]+)<', html_text)
        if m: price = money(m.group(1))
    if not title:
        m = re.search(r"<title>(.*?)</title>", html_text, re.S)
        if m: title = html.unescape(m.group(1)).split("|")[0].split(" price in Bangladesh")[0].strip()
    if re.search(r"out of stock|sold out", html_text[:200000], re.I): stock = False
    return {"title": (title or "")[:90], "price": price, "was": was, "in_stock": stock}

def fetch(url, tries=3):
    last = ""
    for i in range(tries):
        try:
            r = requests.get(url, headers=UA, timeout=25)
            if r.status_code == 200 and len(r.text) > 2000:
                got = parse(r.text)
                if got["price"]: return got, ""
                last = "parsed but no price found"
            else: last = f"HTTP {r.status_code}"
        except Exception as e: last = f"{type(e).__name__}: {str(e)[:70]}"
        time.sleep(2 + 3 * i)
    return None, last

# ---------- storage ----------
def load(p, d):
    try: return json.load(open(p))
    except Exception: return d
def save(p, d): json.dump(d, open(p, "w"), indent=1)
def git(*a): return subprocess.run(["git", *a], capture_output=True, text=True)
def commit(msg):
    if not IN_ACTIONS: return
    for f in (STATE, HIST, LOG):
        if not os.path.exists(f): open(f, "a").close()
    git("add", "--", STATE, HIST, LOG)
    if git("commit", "-q", "-m", msg).returncode: return
    git("pull", "--rebase", "--autostash", "-q")
    r = git("push", "-q")
    if r.returncode: log("push failed", r.stderr[-200:])

def urls():
    try: lines = open(WATCH).read().splitlines()
    except FileNotFoundError: return []
    return [l.strip() for l in lines if l.strip() and not l.strip().startswith("#")]

def key(u):
    m = re.search(r"-i(\d+)", u)
    return m.group(1) if m else u[-40:]

# ---------- run ----------
def main():
    watch, state, changes, errors = urls(), load(STATE, {}), [], []
    if not watch: notify("daraz: watch.txt is empty — add product URLs, one per line"); return
    log(f"checking {len(watch)} products")
    for u in watch:
        k = key(u)
        got, err = fetch(u)
        if not got:
            errors.append((k, err)); continue
        prev = state.get(k)
        row = {"ts": now().isoformat(timespec="seconds"), "url": u, **got}
        with open(HIST, "a") as f: f.write(json.dumps(row) + "\n")
        if prev and prev.get("price") is not None:
            dp = got["price"] - prev["price"]
            if abs(dp) >= 1:
                pct = 100 * dp / prev["price"]
                changes.append(f"{'📉' if dp < 0 else '📈'} {got['title']}\n   {prev['price']:,.0f} → {got['price']:,.0f} Tk ({pct:+.1f}%)\n   {u}")
            if prev.get("in_stock") and not got["in_stock"]:
                changes.append(f"⛔ OUT OF STOCK — {got['title']}\n   {u}")
            elif got["in_stock"] and not prev.get("in_stock"):
                changes.append(f"✅ BACK IN STOCK — {got['title']} at {got['price']:,.0f} Tk\n   {u}")
        else:
            log(f"first read: {got['title']} = {got['price']}")
        state[k] = {**got, "url": u, "ts": row["ts"]}
        time.sleep(3)                      # be polite: one request every few seconds

    save(STATE, state)
    if changes: notify("Daraz changes:\n\n" + "\n\n".join(changes[:10]))
    if errors:
        notify("daraz: couldn't read " + ", ".join(f"{k} ({e})" for k, e in errors[:5]))
    with open(LOG, "a") as f:
        f.write(f"- {now().isoformat(timespec='seconds')} checked {len(watch)}, {len(changes)} changes, {len(errors)} errors\n")
    commit(f"daraz: {len(changes)} changes, {len(errors)} errors")
    log(f"done: {len(changes)} changes, {len(errors)} errors")

if __name__ == "__main__":
    main()
