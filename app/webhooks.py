import hashlib
import hmac
import ipaddress
import json
import os
import socket
import threading
import time
from urllib.parse import urlparse

import httpx
from sqlalchemy import text

from .db import admin_conn

MAX_ATTEMPTS = 6


def validate_url(url: str):
    """Reject non-http(s) targets and, unless explicitly allowed, private/loopback addresses (SSRF)."""
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("webhook url must be http(s)")
    if os.environ.get("ALLOW_PRIVATE_WEBHOOKS") == "1":
        return
    for info in socket.getaddrinfo(u.hostname, u.port or 443):
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ValueError("webhook url resolves to a non-public address")


def emit(c, tenant, event: str, data: dict):
    """Outbox: queue deliveries in the same transaction as the business change."""
    rows = c.execute(text("SELECT id FROM webhook_endpoints WHERE active AND :e = ANY(events)"), {"e": event}).all()
    if not rows:
        return
    payload = json.dumps({"event": event, "tenant_id": str(tenant), "data": data}, default=str)
    for r in rows:
        c.execute(text("INSERT INTO webhook_deliveries(tenant_id, endpoint_id, event, payload) "
                       "VALUES (:t, :e, :ev, CAST(:p AS jsonb))"),
                  {"t": tenant, "e": r.id, "ev": event, "p": payload})


def sign(secret: str, ts: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()


def deliver_pending() -> int:
    with admin_conn() as c:
        rows = c.execute(text(
            "SELECT d.id, d.payload, d.attempts, d.event, e.url, e.secret FROM webhook_deliveries d "
            "JOIN webhook_endpoints e ON e.id = d.endpoint_id "
            "WHERE d.status = 'pending' AND d.next_attempt_at <= now() ORDER BY d.created_at LIMIT 50")).all()
    for r in rows:
        body = json.dumps(r.payload, separators=(",", ":"), sort_keys=True).encode()
        ts = str(int(time.time()))
        err = None
        try:
            validate_url(r.url)
            resp = httpx.post(r.url, content=body, timeout=5, follow_redirects=False, headers={
                "Content-Type": "application/json", "X-Event": r.event, "X-Delivery-Id": str(r.id),
                "X-Timestamp": ts, "X-Signature": sign(r.secret, ts, body)})
            if not 200 <= resp.status_code < 300:
                err = f"http {resp.status_code}"
        except Exception as e:  # network errors are retried like bad statuses
            err = str(e)[:200]
        with admin_conn() as c:
            if err is None:
                c.execute(text("UPDATE webhook_deliveries SET status='sent', attempts=attempts+1, last_error=NULL WHERE id=:i"),
                          {"i": r.id})
            else:
                attempts = r.attempts + 1
                status = "failed" if attempts >= MAX_ATTEMPTS else "pending"
                c.execute(text("UPDATE webhook_deliveries SET status=:s, attempts=:a, last_error=:e, "
                               "next_attempt_at = now() + make_interval(secs => :d) WHERE id=:i"),
                          {"s": status, "a": attempts, "e": err, "d": 5 * 2 ** attempts, "i": r.id})
    return len(rows)


def start_worker():
    def loop():
        while True:
            try:
                deliver_pending()
            except Exception:
                pass
            time.sleep(2)
    threading.Thread(target=loop, daemon=True, name="webhook-worker").start()
