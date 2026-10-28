import hashlib
import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from sqlalchemy import text

import app.main as main
from app.db import admin_conn
from app.webhooks import deliver_pending
from tests.test_batch_a import buy, client, sell, setup  # noqa: F401


@pytest.fixture
def receiver():
    got, plan = [], []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            got.append((dict(self.headers), body))
            self.send_response(plan.pop(0) if plan else 200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/hook", got, plan
    srv.shutdown()


def test_webhooks_signed_with_retry(client, receiver):
    url, got, plan = receiver
    key, wh, item, cust, vend = setup(client)
    r = client.post("/api/v1/webhooks", headers=key, json={"url": url, "events": ["invoice.posted"]})
    assert r.status_code == 201
    secret = r.json()["secret"]
    buy(client, key, wh, item, vend, "5", "100")
    plan.append(500)  # first delivery fails
    assert sell(client, key, wh, item, cust, "1").status_code == 201
    deliver_pending()
    assert len(got) == 1
    d = client.get("/api/v1/webhooks/deliveries", headers=key).json()
    assert d[0]["status"] == "pending" and d[0]["attempts"] == 1
    with admin_conn() as c:
        c.execute(text("UPDATE webhook_deliveries SET next_attempt_at = now()"))
    deliver_pending()
    assert len(got) == 2
    headers, body = got[1]
    expected = "sha256=" + hmac.new(secret.encode(), headers["X-Timestamp"].encode() + b"." + body, hashlib.sha256).hexdigest()
    assert headers["X-Signature"] == expected and headers["X-Event"] == "invoice.posted"
    assert json.loads(body)["data"]["currency"] == "IQD"
    assert client.get("/api/v1/webhooks/deliveries", headers=key).json()[0]["status"] == "sent"


def test_webhook_rejects_private_urls_in_production(client, monkeypatch):
    key, *_ = setup(client)
    monkeypatch.setenv("ALLOW_PRIVATE_WEBHOOKS", "0")
    for bad in ("http://127.0.0.1:9/x", "http://169.254.169.254/latest", "file:///etc/passwd"):
        assert client.post("/api/v1/webhooks", headers=key, json={"url": bad, "events": ["invoice.posted"]}).status_code == 422


def test_rate_limit(client, monkeypatch):
    key, *_ = setup(client)
    monkeypatch.setattr(main, "RATE_LIMIT_PER_MIN", 3)
    main._hits.clear()
    codes = [client.get("/api/v1/warehouses", headers=key).status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and codes[3] == 429


def op(op_id, cust, wh, item, qty):
    return {"op_id": op_id, "type": "sales_invoice", "payload": {
        "customer_id": cust, "warehouse_id": wh, "invoice_date": "2026-01-10", "currency": "IQD",
        "lines": [{"item_id": item, "qty": qty, "unit_price": "150"}]}}


def test_offline_outbox_recovery_and_conflicts(client):
    key, wh, item, cust, vend = setup(client)
    buy(client, key, wh, item, vend, "3", "100")
    ops = [op("o1", cust, wh, item, "2"), op("o2", cust, wh, item, "5"), op("o3", cust, wh, item, "1")]
    r = client.post("/api/v1/sync/push", headers=key, json={"device_id": "pos-1", "device_name": "Till", "ops": ops}).json()
    assert [x["status"] for x in r["results"]] == ["accepted", "conflict", "accepted"]  # o2 lacks stock; others unaffected
    # the device retries the whole batch after a dropped connection: nothing is applied twice
    r2 = client.post("/api/v1/sync/push", headers=key, json={"device_id": "pos-1", "ops": ops}).json()
    assert all(x["duplicate"] for x in r2["results"])
    assert client.get("/api/v1/inventory/balances", headers=key).json()[0]["qty"] == "0.0000"
    assert len(client.get(f"/api/v1/customers/{cust}/ledger", headers=key).json()["rows"]) == 2
    cf = client.get("/api/v1/sync/conflicts", headers=key).json()
    assert len(cf) == 1 and "insufficient stock" in cf[0]["error"]
    assert client.post("/api/v1/sync/conflicts/pos-1/o2/resolve", headers=key, json={"action": "retry"}).status_code == 409
    buy(client, key, wh, item, vend, "10", "100")
    assert client.post("/api/v1/sync/conflicts/pos-1/o2/resolve", headers=key, json={"action": "retry"}).status_code == 200
    assert client.get("/api/v1/sync/conflicts", headers=key).json() == []
    assert client.get("/api/v1/sync/devices", headers=key).json()[0]["device_id"] == "pos-1"
    snap = client.get("/api/v1/sync/pull", params={"device_id": "pos-1"}, headers=key).json()
    assert snap["items"][0]["sku"] == "S1" and "cursor" in snap


def test_sync_respects_permissions(client):
    key, wh, item, cust, vend = setup(client)
    buy(client, key, wh, item, vend, "3", "100")
    nobody = {"X-API-Key": client.post("/api/v1/api-keys", headers=key, json={"scopes": ["warehouse.transfer"]}).json()["api_key"]}
    r = client.post("/api/v1/sync/push", headers=nobody, json={"device_id": "d", "ops": [op("x", cust, wh, item, "1")]}).json()
    assert r["results"][0]["status"] == "conflict" and "403" in r["results"][0]["error"]
