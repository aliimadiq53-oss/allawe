from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.main import app

H = {"X-Platform-Key": "dev-platform-key"}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def setup(client, costing="avg"):
    import uuid
    r = client.post("/api/v1/admin/tenants", json={"name": f"t-{uuid.uuid4()}", "costing": costing}, headers=H).json()
    key = {"X-API-Key": r["api_key"]}
    mk = lambda p, b: client.post(f"/api/v1/{p}", json=b, headers=key).json()["id"]
    wh = mk("warehouses", {"name": "main"})
    item = mk("items", {"sku": "S1", "name": "W"})
    cust = mk("customers", {"name": "C"})
    vend = mk("vendors", {"name": "V"})
    return key, wh, item, cust, vend


def buy(client, key, wh, item, vend, qty, cost):
    r = client.post("/api/v1/purchases/invoices", headers=key, json={
        "vendor_id": vend, "warehouse_id": wh, "invoice_date": "2026-01-05", "currency": "IQD",
        "lines": [{"item_id": item, "qty": qty, "unit_cost": cost}]})
    assert r.status_code == 201, r.text


def sell(client, key, wh, item, cust, qty, price="150", cur="IQD", date="2026-01-10", **kw):
    body = {"customer_id": cust, "warehouse_id": wh, "invoice_date": date, "currency": cur,
            "lines": [{"item_id": item, "qty": qty, "unit_price": price}], **kw}
    return client.post("/api/v1/sales/invoices", headers=key, json=body)


def tb(client, key):
    t = client.get("/api/v1/reports/trial-balance", headers=key).json()
    assert t["balanced"]
    return {r["code"]: Decimal(r["debit"]) - Decimal(r["credit"]) for r in t["rows"]}


def test_sales_return_refers_to_original_invoice(client):
    key, wh, item, cust, vend = setup(client)
    buy(client, key, wh, item, vend, "10", "100")
    inv = sell(client, key, wh, item, cust, "4").json()["id"]
    r = client.post(f"/api/v1/sales/invoices/{inv}/returns", headers=key,
                    json={"return_date": "2026-01-12", "lines": [{"item_id": item, "qty": "3"}]})
    assert r.status_code == 201 and Decimal(r.json()["total"]) == 450
    assert client.get("/api/v1/inventory/balances", headers=key).json()[0]["qty"] == "9.0000"
    assert client.post(f"/api/v1/sales/invoices/{inv}/returns", headers=key,
                       json={"return_date": "2026-01-12", "lines": [{"item_id": item, "qty": "2"}]}).status_code == 409
    led = client.get(f"/api/v1/customers/{cust}/ledger", headers=key).json()
    assert Decimal(led["balance_base"]) == 150
    t = tb(client, key)
    assert t["1100"] == 150 and t["5100"] == 100 and t["1200"] == 900


def test_fx_gain_on_settlement(client):
    key, wh, item, cust, vend = setup(client)
    buy(client, key, wh, item, vend, "5", "100")
    inv = sell(client, key, wh, item, cust, "1", price="100", cur="USD", fx_rate="1300").json()["id"]
    p = client.post("/api/v1/payments", headers=key, json={
        "party_type": "customer", "party_id": cust, "pay_date": "2026-02-01", "currency": "USD",
        "amount": "100", "fx_rate": "1320", "invoice_id": inv})
    assert p.status_code == 201 and Decimal(p.json()["fx_difference"]) == 2000
    t = tb(client, key)
    assert t["4300"] == -2000 and t["1100"] == 0 and t["1010"] == 132000
    assert Decimal(client.get(f"/api/v1/customers/{cust}/ledger", headers=key).json()["balance_base"]) == 0
    loss = client.post("/api/v1/payments", headers=key, json={
        "party_type": "customer", "party_id": cust, "pay_date": "2026-02-02", "currency": "USD",
        "amount": "10", "fx_rate": "1290", "invoice_id": inv})
    assert Decimal(loss.json()["fx_difference"]) == -100
    assert tb(client, key)["5300"] == 100


def test_fifo_costing(client):
    key, wh, item, cust, vend = setup(client, "fifo")
    buy(client, key, wh, item, vend, "5", "100")
    buy(client, key, wh, item, vend, "5", "200")
    assert sell(client, key, wh, item, cust, "7").status_code == 201
    assert tb(client, key)["5100"] == 900
    assert sell(client, key, wh, item, cust, "4").status_code == 409
    assert sell(client, key, wh, item, cust, "3").status_code == 201
    assert tb(client, key)["5100"] == 900 + 600


def test_stock_count_needs_approval(client):
    key, wh, item, cust, vend = setup(client)
    buy(client, key, wh, item, vend, "10", "100")
    cid = client.post("/api/v1/inventory/counts", headers=key,
                      json={"warehouse_id": wh, "lines": [{"item_id": item, "counted_qty": "8"}]}).json()["id"]
    assert client.get("/api/v1/inventory/balances", headers=key).json()[0]["qty"] == "10.0000"
    clerk = {"X-API-Key": client.post("/api/v1/api-keys", json={"scopes": ["inventory.count"]}, headers=key).json()["api_key"]}
    assert client.post(f"/api/v1/inventory/counts/{cid}/approve", headers=clerk).status_code == 403
    r = client.post(f"/api/v1/inventory/counts/{cid}/approve", headers=key)
    assert r.status_code == 200 and Decimal(r.json()["adjustment_value"]) == -200
    assert client.get("/api/v1/inventory/balances", headers=key).json()[0]["qty"] == "8.0000"
    assert tb(client, key)["5400"] == 200
    assert client.post(f"/api/v1/inventory/counts/{cid}/approve", headers=key).status_code == 409
