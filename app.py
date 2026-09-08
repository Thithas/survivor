"""
SURVIVOR relay — Vercel Python entrypoint (WSGI, stdlib only), pinned to Dublin via vercel.json.
Forwards the brain's already-signed Polymarket CLOB calls from a non-geoblocked IP. Holds no key.
URL shape the brain uses:  https://<project>.vercel.app/r/<secret>   (+ the CLOB path)
"""
import os, json
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from http.client import responses as REASON

UPSTREAM = "https://clob.polymarket.com"
SECRET = os.environ.get("RELAY_SECRET", "s7v-relay-2026")
DROP_REQ = {"host", "connection", "content-length", "transfer-encoding", "accept-encoding", "forwarded", "x-real-ip"}
DROP_RES = {"content-encoding", "content-length", "transfer-encoding", "connection"}

def _headers(environ):
    """Rebuild request headers. Polymarket's auth headers use underscores (POLY_API_KEY); WSGI folds '-' and '_'
    into '_', so anything starting with POLY_ is restored verbatim and everything else gets dashes."""
    out = {}
    for k, v in environ.items():
        if not k.startswith("HTTP_"): continue
        raw = k[5:]
        name = raw if raw.startswith("POLY_") else raw.replace("_", "-")
        low = name.lower()
        if low in DROP_REQ or low.startswith("x-vercel") or low.startswith("x-forwarded"): continue
        out[name] = v
    if environ.get("CONTENT_TYPE"): out["Content-Type"] = environ["CONTENT_TYPE"]
    return out

def _reply(start_response, status, body, headers=None):
    hdrs = list(headers or []) + [("Content-Length", str(len(body)))]
    start_response(f"{status} {REASON.get(status, 'OK')}", hdrs)
    return [body]

def app(environ, start_response):
    path, qs = environ.get("PATH_INFO", ""), environ.get("QUERY_STRING", "")
    prefix = f"/r/{SECRET}"
    if path != prefix and not path.startswith(prefix + "/"):
        return _reply(start_response, 404, b"not found", [("Content-Type", "text/plain")])
    sub = path[len(prefix):] or "/"
    if sub == "/health":
        return _reply(start_response, 200, json.dumps({"ok": True, "region": os.environ.get("VERCEL_REGION", "?")}).encode(), [("Content-Type", "application/json")])
    method = environ.get("REQUEST_METHOD", "GET")
    n = int(environ.get("CONTENT_LENGTH") or 0)
    body = environ["wsgi.input"].read(n) if n else None            # raw bytes: the L2 HMAC covers them byte-for-byte
    req = Request(UPSTREAM + sub + (f"?{qs}" if qs else ""), data=body, method=method, headers=_headers(environ))
    try:
        with urlopen(req, timeout=20) as r: status, rh, data = r.status, r.getheaders(), r.read()
    except HTTPError as e: status, rh, data = e.code, list(e.headers.items()), e.read()
    except Exception as e:
        return _reply(start_response, 502, str(e).encode(), [("Content-Type", "text/plain")])
    return _reply(start_response, status, data, [(k, v) for k, v in rh if k.lower() not in DROP_RES])
