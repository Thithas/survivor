// SURVIVOR relay — Vercel function pinned to Dublin (see vercel.json).
// Forwards the brain's already-signed Polymarket CLOB calls from a non-geoblocked IP. Holds no key.
export const config = { maxDuration: 30 };

const UPSTREAM = "https://clob.polymarket.com";
const SECRET = process.env.RELAY_SECRET || "s7v-relay-2026";
const DROP_REQ = new Set(["host", "connection", "content-length", "transfer-encoding", "accept-encoding", "forwarded",
  "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "x-real-ip"]);
const DROP_RES = new Set(["content-encoding", "content-length", "transfer-encoding", "connection"]);

export default async function handler(req) {
  const url = new URL(req.url);
  const prefix = `/api/${SECRET}`;
  if (url.pathname !== prefix && !url.pathname.startsWith(prefix + "/")) return new Response("not found", { status: 404 });
  const path = url.pathname.slice(prefix.length) || "/";
  if (path === "/health") return Response.json({ ok: true, region: process.env.VERCEL_REGION || "?" });

  const headers = new Headers();
  for (const [k, v] of req.headers) { const lk = k.toLowerCase(); if (!DROP_REQ.has(lk) && !lk.startsWith("x-vercel")) headers.set(k, v); }
  const body = req.method === "GET" || req.method === "HEAD" ? undefined : await req.text();   // raw text: the L2 HMAC covers it byte-for-byte
  const r = await fetch(UPSTREAM + path + url.search, { method: req.method, headers, body, redirect: "manual" });
  const out = new Headers();
  for (const [k, v] of r.headers) if (!DROP_RES.has(k.toLowerCase())) out.set(k, v);
  return new Response(await r.arrayBuffer(), { status: r.status, headers: out });
}
