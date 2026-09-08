#!/usr/bin/env python3
"""
SURVIVOR phone relay — the bot's hands.
Forwards the brain's Polymarket CLOB calls through this phone's (non-geoblocked) IP.
The private key never comes here: requests arrive already signed.

Termux:  pkg install python cloudflared   ·   pip install requests   ·   python relay.py
Env (put in phone/.env, see setup.sh): GH_TOKEN, REPO (default Thithas/survivor)
"""
import os, re, sys, json, time, base64, secrets, subprocess, threading, requests
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8787"))
UPSTREAM = "https://clob.polymarket.com"
SECRET = os.environ.get("RELAY_SECRET") or secrets.token_urlsafe(18)      # path prefix only the brain knows
GH_TOKEN, REPO = os.environ.get("GH_TOKEN"), os.environ.get("REPO", "Thithas/survivor")
DROP_REQ = {"host", "content-length", "transfer-encoding", "connection", "accept-encoding", "cf-connecting-ip", "cf-ray", "cf-visitor", "cf-ipcountry", "x-forwarded-for", "x-forwarded-proto", "cdn-loop", "cf-warp-tag-id"}
DROP_RES = {"content-length", "transfer-encoding", "content-encoding", "connection"}
stats = {"ok": 0, "err": 0, "last": ""}

def tg(msg):
    """Status to Telegram so nobody has to open a terminal."""
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    print(msg, flush=True)
    if not tok or not chat: return
    try: requests.post(f"https://api.telegram.org/bot{tok}/sendMessage", json={"chat_id": chat, "text": "relay: " + msg}, timeout=10)
    except Exception: pass

class Relay(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def handle_any(self):
        if self.path == "/" + SECRET + "/health":
            body = json.dumps(stats).encode(); self.send_response(200); self.send_header("content-length", str(len(body))); self.end_headers(); self.wfile.write(body); return
        if not self.path.startswith("/" + SECRET + "/"):
            self.send_response(404); self.send_header("content-length", "0"); self.end_headers(); return
        path = self.path[len(SECRET) + 1:]
        n = int(self.headers.get("content-length") or 0); body = self.rfile.read(n) if n else None
        hdr = {k: v for k, v in self.headers.items() if k.lower() not in DROP_REQ}
        try:
            r = requests.request(self.command, UPSTREAM + path, headers=hdr, data=body, timeout=20, allow_redirects=False)
            self.send_response(r.status_code)
            for k, v in r.headers.items():
                if k.lower() not in DROP_RES: self.send_header(k, v)
            self.send_header("content-length", str(len(r.content))); self.end_headers(); self.wfile.write(r.content)
            stats["ok"] += 1; stats["last"] = f"{self.command} {path.split('?')[0]} {r.status_code}"
        except Exception as e:
            msg = str(e).encode(); self.send_response(502); self.send_header("content-length", str(len(msg))); self.end_headers(); self.wfile.write(msg)
            stats["err"] += 1; stats["last"] = f"ERR {e}"
    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = handle_any
    def log_message(self, *a): pass

def publish(url):
    """Write the tunnel URL (with secret) to the repo file RELAY so the brain can find us."""
    if not GH_TOKEN: tg("no GH_TOKEN, cannot publish " + url); return
    api = f"https://api.github.com/repos/{REPO}/contents/RELAY"; h = {"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json"}
    cur = requests.get(api, headers=h, timeout=15); body = {"message": "phone relay online", "content": base64.b64encode(url.encode()).decode()}
    if cur.status_code == 200: body["sha"] = cur.json()["sha"]
    r = requests.put(api, headers=h, json=body, timeout=15); tg(f"published RELAY {r.status_code} → {url.split('/')[2]}")

def tunnel_forever():
    while True:
        p = subprocess.Popen(["cloudflared", "tunnel", "--url", f"http://localhost:{PORT}", "--no-autoupdate"],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        url = None
        for line in p.stdout:
            m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
            if m and not url:
                url = m.group(0) + "/" + SECRET; tg("tunnel up " + url.split('/')[2])
                try: publish(url)
                except Exception as e: tg(f"publish failed: {str(e)[:120]}")
        tg("tunnel exited, restarting in 5s"); time.sleep(5)

if __name__ == "__main__":
    threading.Thread(target=tunnel_forever, daemon=True).start()
    try: geo = requests.get("https://polymarket.com/api/geoblock", timeout=10).json(); geo = f"{geo.get('country')} blocked={geo.get('blocked')}"
    except Exception as e: geo = f"geo check failed {str(e)[:60]}"
    tg(f"starting on {os.environ.get('CODESPACE_NAME', 'this machine')} · Polymarket sees {geo}")
    try: ThreadingHTTPServer(("127.0.0.1", PORT), Relay).serve_forever()
    except KeyboardInterrupt: pass
