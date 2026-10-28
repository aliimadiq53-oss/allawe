from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.test_acceptance import H, make_tenant


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def setup(client, credit=None):
    key, _ = make_tenant(client)
    mk = lambda path, body: client.post(f"/api/v1/{path}", json=body, headers=key).json()["id"]
    wh = mk("warehouses", {"name": "main"})
    item = mk("items", {"sku": "S1", "name": "Widget"})
    cust = mk("customers", {"name": "C", **({"credit_limit": credit, "credit_currency": "IQD"} if credit else {})})
    vend = mk("vendors", {"name": "V"})
    client.post(f"/api/v1/items/{item}/prices", json={"list_kind": "retail", "price": "150", "currency": "IQD", "valid_from": "2026-01-01"}, headers=key)
    return key, wh, item, cust, vend


def buy(client, key, wh, item, vend, qty, cost):
    return client.post("/api/v1/purchases/invoices", headers=key, json={
        "vendor_id": vend, "warehouse_id": wh, "invoice_date": "2026-01-05", "currency": "IQD",
        "lines": [{"item_id": item, "qty": qty, "unit_cost": cost}]})


def sell(client, key, wh, item, cust, qty, **kw):
    return client.post("/api/v1/sales/invoices", headers={**key, **kw.pop("headers", {})}, json={
        "customer_id": cust, "warehouse_id": wh, "invoice_date": "2026-01-10", "currency": "IQD",
        "lines": [{"item_id": item, "qty": qty}], **kw})


def bal(client, key):
    return {b["sku"]: Decimal(b["qty"]) for b in client.get("/api/v1/inventory/balances", headers=key).json()}


def test_weighted_average_and_cogs(client):
    key, wh, item, cust, vend = setup(client)
    assert buy(client, key, wh, item, vend, "10", "100").status_code == 201
    assert buy(client, key, wh, item, vend, "10", "120").status_code == 201
    b = client.get("/api/v1/inventory/balances", headers=key).json()[0]
    assert Decimal(b["avg_cost"]) == Decimal("110.000000") and Decimal(b["qty"]) == 20
    assert sell(client, key, wh, item, cust, "4").status_code == 201
    tb = client.get("/api/v1/reports/trial-balance", headers=key).json()
    assert tb["balanced"]
    row = {r["code"]: r for r in tb["rows"]}
    assert Decimal(row["5100"]["debit"]) == 440 and Decimal(row["4100"]["credit"]) == 600
    assert Decimal(row["1200"]["debit"]) - Decimal(row["1200"]["credit"]) == 2200 - 440


def test_negative_stock_blocked_and_atomic(client):
    key, wh, item, cust, vend = setup(client)
    buy(client, key, wh, item, vend, "2", "100")
    assert sell(client, key, wh, item, cust, "3").status_code == 409
    assert bal(client, key)["S1"] == 2
    assert client.get(f"/api/v1/customers/{cust}/ledger", headers=key).json()["rows"] == []


def test_ac03_sale_idempotent(client):
    key, wh, item, cust, vend = setup(client)
    buy(client, key, wh, item, vend, "10", "100")
    ids = {sell(client, key, wh, item, cust, "1", headers={"Idempotency-Key": "k1"}).json()["id"] for _ in range(5)}
    ids |= {sell(client, key, wh, item, cust, "1", external_id="ext-1").json()["id"] for _ in range(3)}
    assert len(ids) == 2
    assert bal(client, key)["S1"] == 8


def test_ac04_credit_limit_and_override(client):
    key, wh, item, cust, vend = setup(client, credit="200")
    buy(client, key, wh, item, vend, "10", "100")
    assert sell(client, key, wh, item, cust, "1").status_code == 201
    assert sell(client, key, wh, item, cust, "1").status_code == 409
    # override without permission is still refused
    cashier = {"X-API-Key": client.post("/api/v1/api-keys", json={"scopes": ["invoice.post"]}, headers=key).json()["api_key"]}
    assert sell(client, cashier, wh, item, cust, "1", override_credit=True).status_code == 409
    assert sell(client, key, wh, item, cust, "1", override_credit=True).status_code == 201


def test_ac09_permissions(client):
    key, *_ = setup(client)
    limited = {"X-API-Key": client.post("/api/v1/api-keys", json={"scopes": ["invoice.post"]}, headers=key).json()["api_key"]}
    assert client.get("/api/v1/reports/profit", headers=limited).status_code == 403
    assert client.post("/api/v1/api-keys", json={"scopes": ["*"]}, headers=limited).status_code == 403
    assert client.get("/api/v1/reports/profit", headers=key).status_code == 200


def test_ledger_payment_and_aging(client):
    key, wh, item, cust, vend = setup(client)
    buy(client, key, wh, item, vend, "10", "100")
    sell(client, key, wh, item, cust, "2")  # 300 on 2026-01-10
    pay = client.post("/api/v1/payments", headers=key, json={
        "party_type": "customer", "party_id": cust, "pay_date": "2026-01-20", "currency": "IQD", "amount": "100"})
    assert pay.status_code == 201
    led = client.get(f"/api/v1/customers/{cust}/ledger", headers=key).json()
    assert Decimal(led["balance_base"]) == 200
    aging = client.get(f"/api/v1/customers/{cust}/aging", params={"as_of": "2026-03-15"}, headers=key).json()
    assert Decimal(aging["61-90"]) == 200 and Decimal(aging["0-30"]) == 0


def test_ac08_cashier_shortage(client):
    key, wh, item, cust, vend = setup(client)
    buy(client, key, wh, item, vend, "10", "100")
    sid = client.post("/api/v1/pos/shifts", json={"warehouse_id": wh, "opening_cash": "1000"}, headers=key).json()["id"]
    r = client.post(f"/api/v1/pos/shifts/{sid}/sales", headers=key, json={
        "customer_id": cust, "invoice_date": "2026-01-10", "currency": "IQD", "lines": [{"item_id": item, "qty": "2"}]})
    assert r.status_code == 201
    c = client.post(f"/api/v1/pos/shifts/{sid}/close", json={"counted_cash": "1250"}, headers=key).json()
    assert Decimal(c["expected_cash"]) == 1300 and Decimal(c["difference"]) == -50
    row = {x["code"]: x for x in client.get("/api/v1/reports/trial-balance", headers=key).json()["rows"]}
    assert Decimal(row["5200"]["debit"]) == 50
    assert client.post(f"/api/v1/pos/shifts/{sid}/close", json={"counted_cash": "1"}, headers=key).status_code == 409


def test_cross_tenant_references_rejected(client):
    a = setup(client)
    b = setup(client)
    key_b, wh_b, item_b, cust_b, _ = b
    _, wh_a, item_a, cust_a, _ = a
    assert sell(client, key_b, wh_a, item_b, cust_b, "1").status_code == 422
    assert sell(client, key_b, wh_b, item_a, cust_b, "1").status_code == 422
    assert sell(client, key_b, wh_b, item_b, cust_a, "1").status_code == 422


def test_transfer_between_warehouses(client):
    key, wh, item, cust, vend = setup(client)
    wh2 = client.post("/api/v1/warehouses", json={"name": "b"}, headers=key).json()["id"]
    buy(client, key, wh, item, vend, "5", "100")
    r = client.post("/api/v1/inventory/transfers", headers=key, json={"item_id": item, "from_warehouse": wh, "to_warehouse": wh2, "qty": "3"})
    assert r.status_code == 201
    qty = sorted(Decimal(b["qty"]) for b in client.get("/api/v1/inventory/balances", headers=key).json())
    assert qty == [2, 3]
