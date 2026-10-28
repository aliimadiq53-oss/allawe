from decimal import Decimal

from tests.test_batch_a import buy, client, sell, setup, tb  # noqa: F401


def shift(client, key, wh):
    return client.post("/api/v1/pos/shifts", json={"warehouse_id": wh, "opening_cash": "0"}, headers=key).json()["id"]


def resto(client):
    key, wh, item, cust, vend = setup(client)
    buy(client, key, wh, item, vend, "20", "50")
    client.post(f"/api/v1/items/{item}/prices", headers=key, json={
        "list_kind": "retail", "price": "100", "currency": "IQD", "valid_from": "2026-01-01"})
    table = client.post("/api/v1/restaurant/tables", json={"name": "T1"}, headers=key).json()["id"]
    return key, wh, item, cust, table


def test_order_posts_only_at_payment_with_kitchen_flow(client):
    key, wh, item, cust, table = resto(client)
    before = tb(client, key)
    oid = client.post("/api/v1/restaurant/orders", json={"table_id": table}, headers=key).json()["id"]
    t = client.post(f"/api/v1/restaurant/orders/{oid}/lines", headers=key,
                    json={"lines": [{"item_id": item, "qty": "2", "note": "no onion"}]}).json()["ticket_id"]
    k = client.get("/api/v1/restaurant/kitchen", headers=key).json()
    assert k[0]["table"] == "T1" and k[0]["lines"][0]["note"] == "no onion"
    assert tb(client, key) == before  # nothing posted yet
    assert client.post(f"/api/v1/restaurant/tickets/{t}/status", json={"status": "ready"}, headers=key).status_code == 409
    for s in ("preparing", "ready", "served"):
        assert client.post(f"/api/v1/restaurant/tickets/{t}/status", json={"status": s}, headers=key).status_code == 200
    sid = shift(client, key, wh)
    r = client.post(f"/api/v1/restaurant/orders/{oid}/pay", headers=key, json={
        "shift_id": sid, "customer_id": cust, "currency": "IQD", "invoice_date": "2026-01-10"})
    assert r.status_code == 201 and Decimal(r.json()["total"]) == 200
    assert tb(client, key)["4100"] == -200
    assert client.post(f"/api/v1/restaurant/orders/{oid}/pay", headers=key, json={
        "shift_id": sid, "customer_id": cust, "currency": "IQD", "invoice_date": "2026-01-10"}).status_code == 409


def test_split_and_merge(client):
    key, wh, item, cust, table = resto(client)
    a = client.post("/api/v1/restaurant/orders", json={"table_id": table}, headers=key).json()["id"]
    client.post(f"/api/v1/restaurant/orders/{a}/lines", headers=key, json={"lines": [{"item_id": item, "qty": "4"}]})
    line = client.get(f"/api/v1/restaurant/orders/{a}", headers=key).json()["lines"][0]["id"]
    b = client.post(f"/api/v1/restaurant/orders/{a}/split", headers=key, json={"lines": [{"line_id": line, "qty": "1"}]}).json()["id"]
    qa = lambda o: sum(Decimal(l["qty"]) for l in client.get(f"/api/v1/restaurant/orders/{o}", headers=key).json()["lines"])
    assert qa(a) == 3 and qa(b) == 1
    assert client.post(f"/api/v1/restaurant/orders/{a}/split", headers=key, json={"lines": [{"line_id": line, "qty": "9"}]}).status_code == 409
    assert client.post(f"/api/v1/restaurant/orders/{a}/merge", headers=key, json={"from_order_id": b}).status_code == 200
    assert qa(a) == 4
    assert client.get(f"/api/v1/restaurant/orders/{b}", headers=key).json()["status"] == "merged"


def test_print_sizes_reprint_log_and_cost_visibility(client):
    key, wh, item, cust, vend = setup(client)
    buy(client, key, wh, item, vend, "5", "100")
    inv = sell(client, key, wh, item, cust, "2", price="1234567.5").json()["id"]
    sizes = {}
    for paper, extra in [("a4", {}), ("a5", {}), ("thermal58", {}), ("thermal80", {}),
                         ("custom", {"width_mm": "70", "height_mm": "150"})]:
        tid = client.post("/api/v1/print/templates", headers=key, json={
            "name": paper, "paper": paper, "header": "<b>متجر</b>", "footer": "شكراً", "show_cost": True, **extra}).json()["id"]
        r = client.post(f"/api/v1/sales/invoices/{inv}/print", headers=key, json={"template_id": tid})
        assert r.status_code == 200
        sizes[paper] = r.text
        assert "2,469,135.00 IQD" in r.text and "id=\"grand-total\"" in r.text
        assert "&lt;b&gt;" in r.text  # header is escaped, not injected
        assert "table-layout:fixed" in r.text
    assert "size: 210mm 297mm" in sizes["a4"] and "size: 148mm 210mm" in sizes["a5"]
    assert "size: 58mm auto" in sizes["thermal58"] and "size: 80mm auto" in sizes["thermal80"]
    assert "<th>التكلفة</th>" in sizes["a4"]  # owner key has view_cost
    cashier = {"X-API-Key": client.post("/api/v1/api-keys", headers=key, json={"scopes": ["invoice.print"]}).json()["api_key"]}
    tid = client.get("/api/v1/print/templates", headers=key).json()[0]["id"]
    assert "<th>التكلفة</th>" not in client.post(f"/api/v1/sales/invoices/{inv}/print", headers=cashier, json={"template_id": tid}).text
    hist = client.get("/api/v1/print/history", headers=key).json()
    assert sum(1 for h in hist if not h["is_reprint"]) == 1 and sum(1 for h in hist if h["is_reprint"]) == len(hist) - 1
