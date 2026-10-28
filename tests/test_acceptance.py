import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.main import app

H = {"X-Platform-Key": "dev-platform-key"}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def make_tenant(client, base="IQD"):
    r = client.post("/api/v1/admin/tenants", json={"name": f"t-{uuid.uuid4()}", "base_currency": base}, headers=H)
    assert r.status_code == 201
    key = {"X-API-Key": r.json()["api_key"]}
    ids = {}
    for code, typ in [("1000", "asset"), ("4000", "income"), ("5000", "expense")]:
        ids[code] = client.post("/api/v1/accounts", json={"code": code, "name": code, "type": typ}, headers=key).json()["id"]
    return key, ids


def entry(ids, amount="100", cur="IQD", date="2026-01-10", rate=None, a="1000", b="4000"):
    body = {"entry_date": date, "currency": cur, "lines": [
        {"account_id": ids[a], "debit": amount},
        {"account_id": ids[b], "credit": amount}]}
    if rate:
        body["fx_rate"] = rate
    return body


def test_ac01_tenant_isolation(client):
    a, a_ids = make_tenant(client)
    b, b_ids = make_tenant(client)
    cid = client.post("/api/v1/customers", json={"name": "x"}, headers=a).json()["id"]
    assert client.get(f"/api/v1/customers/{cid}", headers=b).status_code == 404
    assert client.get(f"/api/v1/customers/{cid}", headers=a).status_code == 200
    # tenant B cannot post to tenant A's accounts
    assert client.post("/api/v1/journal-entries", json=entry(a_ids), headers=b).status_code == 422
    assert client.get("/api/v1/accounts", headers=b).json() != client.get("/api/v1/accounts", headers=a).json()
    assert client.get("/api/v1/accounts", headers={"X-API-Key": "bad"}).status_code == 401


def test_ac02_unbalanced_rejected(client):
    k, ids = make_tenant(client)
    body = entry(ids)
    body["lines"][1]["credit"] = "99"
    assert client.post("/api/v1/journal-entries", json=body, headers=k).status_code == 422
    assert Decimal(client.get("/api/v1/reports/trial-balance", headers=k).json()["total_debit"]) == 0


def test_ac02_db_rejects_unbalanced_even_bypassing_api(client):
    from sqlalchemy import text
    from app.db import admin_conn
    k, ids = make_tenant(client)
    tid = None
    with pytest.raises(Exception, match="unbalanced"):
        with admin_conn() as c:
            tid = c.execute(text("SELECT tenant_id FROM accounts WHERE id = :i"), {"i": ids["1000"]}).scalar()
            eid = c.execute(text("INSERT INTO journal_entries(tenant_id, entry_date, currency, fx_rate) VALUES (:t, '2026-03-01', 'IQD', 1) RETURNING id"), {"t": tid}).scalar()
            c.execute(text("INSERT INTO journal_lines(tenant_id, entry_id, account_id, debit, base_debit) VALUES (:t, :e, :a, 5, 5)"), {"t": tid, "e": eid, "a": ids["1000"]})
            c.execute(text("INSERT INTO journal_lines(tenant_id, entry_id, account_id, credit, base_credit) VALUES (:t, :e, :a, 4, 4)"), {"t": tid, "e": eid, "a": ids["4000"]})


def test_ac03_idempotent_replay(client):
    k, ids = make_tenant(client)
    h = {**k, "Idempotency-Key": "inv-1"}
    with ThreadPoolExecutor(5) as ex:
        rs = list(ex.map(lambda _: client.post("/api/v1/journal-entries", json=entry(ids), headers=h), range(5)))
    assert len({r.json()["id"] for r in rs}) == 1
    tb = client.get("/api/v1/reports/trial-balance", headers=k).json()
    assert tb["total_debit"] == "100.0000"


def test_ac05_mixed_currency_precision(client):
    k, ids = make_tenant(client)
    client.post("/api/v1/exchange-rates", json={"currency": "USD", "rate_date": "2026-01-01", "rate": "1310.5"}, headers=k)
    assert client.post("/api/v1/journal-entries", json=entry(ids, "0.3333", "USD"), headers=k).status_code == 201
    assert client.post("/api/v1/journal-entries", json=entry(ids, "5000", "IQD"), headers=k).status_code == 201
    tb = client.get("/api/v1/reports/trial-balance", headers=k).json()
    assert tb["balanced"]
    row = next(r for r in tb["rows"] if r["code"] == "1000")
    expected = (Decimal("0.3333") * Decimal("1310.5")).quantize(Decimal("0.0001"), "ROUND_HALF_EVEN") + 5000
    assert Decimal(row["debit"]) == expected


def test_ac06_price_change_keeps_history(client):
    k, _ = make_tenant(client)
    iid = client.post("/api/v1/items", json={"sku": "A", "name": "A"}, headers=k).json()["id"]
    client.post(f"/api/v1/items/{iid}/prices", json={"list_kind": "retail", "price": "10", "currency": "USD", "valid_from": "2026-01-01"}, headers=k)
    client.post(f"/api/v1/items/{iid}/prices", json={"list_kind": "retail", "price": "12", "currency": "USD", "valid_from": "2026-02-01"}, headers=k)
    p = lambda d: client.get(f"/api/v1/items/{iid}/price", params={"kind": "retail", "on": d}, headers=k).json()["price"]
    assert p("2026-01-15") == "10.0000"
    assert p("2026-02-15") == "12.0000"


def test_ac11_locked_period_rejected(client):
    k, ids = make_tenant(client)
    pid = client.post("/api/v1/periods", json={"start_date": "2026-01-01", "end_date": "2026-01-31"}, headers=k).json()["id"]
    assert client.post("/api/v1/journal-entries", json=entry(ids), headers=k).status_code == 201
    assert client.post(f"/api/v1/periods/{pid}/lock", headers=k).status_code == 200
    assert client.post("/api/v1/journal-entries", json=entry(ids), headers=k).status_code == 409
    assert client.post("/api/v1/journal-entries", json=entry(ids, date="2026-02-05"), headers=k).status_code == 201


def test_ac12_trial_balance_and_reversal(client):
    k, ids = make_tenant(client)
    eid = client.post("/api/v1/journal-entries", json=entry(ids, "250"), headers=k).json()["id"]
    r = client.post(f"/api/v1/journal-entries/{eid}/reverse", params={"entry_date": "2026-01-20"}, headers=k)
    assert r.status_code == 201
    tb = client.get("/api/v1/reports/trial-balance", headers=k).json()
    assert tb["balanced"] and tb["total_debit"] == "500.0000"
    assert all(Decimal(x["debit"]) == Decimal(x["credit"]) for x in tb["rows"] if x["code"] in ("1000", "4000"))


def test_posted_entries_immutable():
    from sqlalchemy import text
    from app.db import admin_conn
    with pytest.raises(Exception, match="immutable"):
        with admin_conn() as c:
            c.execute(text("UPDATE journal_lines SET debit = debit"))
