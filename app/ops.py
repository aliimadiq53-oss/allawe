import json
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from .db import tenant_conn
from .webhooks import emit
from .main import (EntryIn, LineIn, _base_currency, _map_db_error, _post_entry,
                   _resolve_rate, current_auth, has_scope)

router = APIRouter(prefix="/api/v1")
Q = Decimal("0.0001")


def need(auth, perm):
    if not has_scope(auth, perm):
        raise HTTPException(403, f"missing permission: {perm}")


def q4(x: Decimal) -> Decimal:
    return x.quantize(Q, ROUND_HALF_EVEN)


def visible(c, table, id_, what):
    if not c.execute(text(f"SELECT 1 FROM {table} WHERE id = :i"), {"i": id_}).first():
        raise HTTPException(422, f"unknown {what}")


EXTRA_ACCOUNTS = {
    "4300": ("FX Gain", "income"),
    "5300": ("FX Loss", "expense"),
    "5400": ("Inventory Adjustments", "expense"),
}


def acct_id(c, code):
    found = c.execute(text("SELECT id FROM accounts WHERE code = :c"), {"c": code}).scalar()
    if found is None and code in EXTRA_ACCOUNTS:
        name, typ = EXTRA_ACCOUNTS[code]
        found = c.execute(
            text("INSERT INTO accounts(tenant_id, code, name, type) "
                 "VALUES (current_setting('app.tenant_id')::uuid, :c, :n, :t) RETURNING id"),
            {"c": code, "n": name, "t": typ}).scalar()
    return found


def post_sys(c, tenant, on, currency, rate, lines, memo):
    body = EntryIn(
        entry_date=on, currency=currency, fx_rate=rate, memo=memo,
        lines=[LineIn(account_id=acct_id(c, code), debit=d, credit=cr) for code, d, cr in lines],
    )
    return _post_entry(c, tenant, body)


def run(tenant, fn):
    try:
        with tenant_conn(tenant) as c:
            return fn(c)
    except DBAPIError as e:
        _map_db_error(e)


def idempotent(c, tenant, key, fn):
    if not key:
        return fn()
    claimed = c.execute(
        text("INSERT INTO idempotency_keys(tenant_id, key, response) VALUES (:t, :k, '{}') "
             "ON CONFLICT DO NOTHING RETURNING key"),
        {"t": tenant, "k": key},
    ).first()
    if not claimed:
        return c.execute(text("SELECT response FROM idempotency_keys WHERE key = :k"), {"k": key}).scalar()
    result = fn()
    c.execute(text("UPDATE idempotency_keys SET response = :r WHERE key = :k"),
              {"r": json.dumps(result), "k": key})
    return result


# ---------- stock ----------
def _costing(c, tenant) -> str:
    return c.execute(text("SELECT costing FROM tenants WHERE id = :t"), {"t": tenant}).scalar()


def stock_in(c, tenant, item, wh, qty, unit_cost_base, doc_type, doc_id):
    c.execute(
        text("INSERT INTO stock_balances(tenant_id, item_id, warehouse_id) VALUES (:t, :i, :w) "
             "ON CONFLICT DO NOTHING"),
        {"t": tenant, "i": item, "w": wh},
    )
    row = c.execute(
        text("SELECT qty, avg_cost FROM stock_balances WHERE item_id = :i AND warehouse_id = :w FOR UPDATE"),
        {"i": item, "w": wh},
    ).first()
    new_qty = row.qty + qty
    new_avg = ((row.qty * row.avg_cost + qty * unit_cost_base) / new_qty).quantize(Decimal("0.000001"))
    c.execute(
        text("UPDATE stock_balances SET qty = :q, avg_cost = :a WHERE item_id = :i AND warehouse_id = :w"),
        {"q": new_qty, "a": new_avg, "i": item, "w": wh},
    )
    if _costing(c, tenant) == "fifo":
        c.execute(
            text("INSERT INTO stock_layers(tenant_id, item_id, warehouse_id, qty_remaining, unit_cost) "
                 "VALUES (:t, :i, :w, :q, :u)"),
            {"t": tenant, "i": item, "w": wh, "q": qty, "u": unit_cost_base},
        )
    c.execute(
        text("INSERT INTO stock_moves(tenant_id, item_id, warehouse_id, qty, unit_cost, doc_type, doc_id) "
             "VALUES (:t, :i, :w, :q, :u, :dt, :d)"),
        {"t": tenant, "i": item, "w": wh, "q": qty, "u": unit_cost_base, "dt": doc_type, "d": doc_id},
    )
    emit(c, tenant, "stock.changed", {"item_id": item, "warehouse_id": wh, "qty_change": qty, "doc_type": doc_type})


def stock_out(c, tenant, item, wh, qty, doc_type, doc_id) -> Decimal:
    row = c.execute(
        text("SELECT qty, avg_cost FROM stock_balances WHERE item_id = :i AND warehouse_id = :w FOR UPDATE"),
        {"i": item, "w": wh},
    ).first()
    if not row or row.qty < qty:
        raise HTTPException(409, "insufficient stock (negative stock is blocked)")
    unit_cost = row.avg_cost
    if _costing(c, tenant) == "fifo":
        need_qty, total = qty, Decimal(0)
        layers = c.execute(
            text("SELECT id, qty_remaining, unit_cost FROM stock_layers WHERE item_id = :i AND warehouse_id = :w "
                 "AND qty_remaining > 0 ORDER BY created_at, id FOR UPDATE"),
            {"i": item, "w": wh}).all()
        for ly in layers:
            take = min(need_qty, ly.qty_remaining)
            total += take * ly.unit_cost
            need_qty -= take
            c.execute(text("UPDATE stock_layers SET qty_remaining = qty_remaining - :t WHERE id = :i"),
                      {"t": take, "i": ly.id})
            if need_qty == 0:
                break
        if need_qty > 0:
            raise HTTPException(409, "insufficient stock layers")
        unit_cost = (total / qty).quantize(Decimal("0.000001"))
    c.execute(
        text("UPDATE stock_balances SET qty = qty - :q WHERE item_id = :i AND warehouse_id = :w"),
        {"q": qty, "i": item, "w": wh},
    )
    c.execute(
        text("INSERT INTO stock_moves(tenant_id, item_id, warehouse_id, qty, unit_cost, doc_type, doc_id) "
             "VALUES (:t, :i, :w, :q, :u, :dt, :d)"),
        {"t": tenant, "i": item, "w": wh, "q": -qty, "u": unit_cost, "dt": doc_type, "d": doc_id},
    )
    emit(c, tenant, "stock.changed", {"item_id": item, "warehouse_id": wh, "qty_change": -qty, "doc_type": doc_type})
    return unit_cost


# ---------- masters ----------
class NameIn(BaseModel):
    name: str


@router.post("/warehouses", status_code=201)
def create_warehouse(body: NameIn, auth=Depends(current_auth)):
    def f(c):
        return {"id": str(c.execute(
            text("INSERT INTO warehouses(tenant_id, name) VALUES (:t, :n) RETURNING id"),
            {"t": auth["tenant"], "n": body.name}).scalar())}
    return run(auth["tenant"], f)


@router.get("/warehouses")
def list_warehouses(auth=Depends(current_auth)):
    return run(auth["tenant"], lambda c: [
        {"id": str(r.id), "name": r.name} for r in c.execute(text("SELECT id, name FROM warehouses ORDER BY name"))])


@router.post("/vendors", status_code=201)
def create_vendor(body: NameIn, auth=Depends(current_auth)):
    def f(c):
        return {"id": str(c.execute(
            text("INSERT INTO vendors(tenant_id, name) VALUES (:t, :n) RETURNING id"),
            {"t": auth["tenant"], "n": body.name}).scalar())}
    return run(auth["tenant"], f)


@router.get("/vendors")
def list_vendors(auth=Depends(current_auth)):
    return run(auth["tenant"], lambda c: [
        {"id": str(r.id), "name": r.name} for r in c.execute(text("SELECT id, name FROM vendors ORDER BY name"))])


@router.get("/customers")
def list_customers(auth=Depends(current_auth)):
    def f(c):
        return [{"id": str(r.id), "name": r.name,
                 "credit_limit": None if r.credit_limit is None else str(r.credit_limit),
                 "credit_currency": r.credit_currency}
                for r in c.execute(text("SELECT id, name, credit_limit, credit_currency FROM customers ORDER BY name"))]
    return run(auth["tenant"], f)


@router.get("/items")
def list_items(auth=Depends(current_auth)):
    return run(auth["tenant"], lambda c: [
        {"id": str(r.id), "sku": r.sku, "name": r.name}
        for r in c.execute(text("SELECT id, sku, name FROM items ORDER BY sku"))])


# ---------- purchases ----------
class PLine(BaseModel):
    item_id: UUID
    qty: Decimal = Field(gt=0)
    unit_cost: Decimal = Field(ge=0)


class PurchaseIn(BaseModel):
    vendor_id: UUID
    warehouse_id: UUID
    invoice_date: date
    currency: str
    fx_rate: Optional[Decimal] = Field(default=None, gt=0)
    lines: list[PLine] = Field(min_length=1)


@router.post("/purchases/invoices", status_code=201)
def purchase(body: PurchaseIn, auth=Depends(current_auth), idempotency_key: Optional[str] = Header(default=None)):
    need(auth, "purchase.post")
    tenant = auth["tenant"]

    def work(c):
        def go():
            visible(c, "vendors", body.vendor_id, "vendor")
            visible(c, "warehouses", body.warehouse_id, "warehouse")
            for ln in body.lines:
                visible(c, "items", ln.item_id, "item")
            rate = _resolve_rate(c, tenant, body.currency, body.invoice_date, body.fx_rate)
            total = q4(sum((l.qty * l.unit_cost for l in body.lines), Decimal(0)))
            doc = c.execute(
                text("INSERT INTO purchase_invoices(tenant_id, vendor_id, warehouse_id, invoice_date, currency, "
                     "fx_rate, total, total_base) VALUES (:t, :v, :w, :d, :c, :r, :tot, :tb) RETURNING id"),
                {"t": tenant, "v": body.vendor_id, "w": body.warehouse_id, "d": body.invoice_date,
                 "c": body.currency, "r": rate, "tot": total, "tb": q4(total * rate)}).scalar()
            for ln in body.lines:
                stock_in(c, tenant, ln.item_id, body.warehouse_id, ln.qty, ln.unit_cost * rate, "purchase", doc)
            eid = post_sys(c, tenant, body.invoice_date, body.currency, rate,
                           [("1200", total, 0), ("2100", 0, total)], f"purchase {doc}")
            c.execute(text("UPDATE purchase_invoices SET entry_id = :e WHERE id = :i"), {"e": eid, "i": doc})
            return {"id": str(doc), "total": str(total)}
        return idempotent(c, tenant, idempotency_key, go)
    return run(tenant, work)


# ---------- sales ----------
class SLine(BaseModel):
    item_id: UUID
    qty: Decimal = Field(gt=0)
    unit_price: Optional[Decimal] = Field(default=None, ge=0)


class SaleIn(BaseModel):
    customer_id: UUID
    warehouse_id: UUID
    invoice_date: date
    currency: str
    fx_rate: Optional[Decimal] = Field(default=None, gt=0)
    price_kind: str = "retail"
    override_credit: bool = False
    external_id: Optional[str] = None
    lines: list[SLine] = Field(min_length=1)


def customer_balance_base(c, customer_id) -> Decimal:
    inv = c.execute(text("SELECT COALESCE(SUM(total_base),0) FROM sales_invoices "
                         "WHERE customer_id = :i AND status = 'posted'"), {"i": customer_id}).scalar()
    pay = c.execute(text("SELECT COALESCE(SUM(amount_base),0) FROM payments "
                         "WHERE party_type = 'customer' AND party_id = :i"), {"i": customer_id}).scalar()
    ret = c.execute(text("SELECT COALESCE(SUM(total_base),0) FROM sales_returns WHERE customer_id = :i"),
                    {"i": customer_id}).scalar()
    return inv - pay - ret


def create_sale(c, tenant, auth, body: SaleIn, shift_id=None):
    if body.external_id:
        ex = c.execute(text("SELECT id, total FROM sales_invoices WHERE external_id = :e"),
                       {"e": body.external_id}).first()
        if ex:
            return {"id": str(ex.id), "total": str(ex.total), "replayed": True}
    cust = c.execute(
        text("SELECT credit_limit, credit_currency FROM customers WHERE id = :i FOR UPDATE"),
        {"i": body.customer_id}).first()
    if not cust:
        raise HTTPException(422, "unknown customer")
    visible(c, "warehouses", body.warehouse_id, "warehouse")
    rate = _resolve_rate(c, tenant, body.currency, body.invoice_date, body.fx_rate)

    priced = []
    for ln in body.lines:
        visible(c, "items", ln.item_id, "item")
        price = ln.unit_price
        if price is None:
            row = c.execute(
                text("SELECT price, currency FROM item_prices WHERE item_id = :i AND list_kind = :k "
                     "AND valid_from <= :d ORDER BY valid_from DESC LIMIT 1"),
                {"i": ln.item_id, "k": body.price_kind, "d": body.invoice_date}).first()
            if not row or row.currency != body.currency:
                raise HTTPException(422, "no price in invoice currency for item")
            price = row.price
        priced.append((ln, price))
    total = q4(sum((l.qty * p for l, p in priced), Decimal(0)))
    total_base = q4(total * rate)

    if cust.credit_limit is not None and shift_id is None:
        base_cur = _base_currency(c, tenant)
        lim_cur = cust.credit_currency or base_cur
        lim_rate = _resolve_rate(c, tenant, lim_cur, body.invoice_date, None) if lim_cur != base_cur else Decimal(1)
        limit_base = q4(cust.credit_limit * lim_rate)
        if customer_balance_base(c, body.customer_id) + total_base > limit_base:
            if body.override_credit and has_scope(auth, "credit_limit.override"):
                c.execute(text("INSERT INTO audit_events(tenant_id, action, detail) VALUES (:t, 'credit_limit.override', :d)"),
                          {"t": tenant, "d": json.dumps({"customer_id": str(body.customer_id), "total_base": str(total_base)})})
            else:
                raise HTTPException(409, "credit limit exceeded")

    inv = c.execute(
        text("INSERT INTO sales_invoices(tenant_id, customer_id, warehouse_id, invoice_date, currency, fx_rate, "
             "total, total_base, external_id, shift_id) VALUES (:t, :cu, :w, :d, :c, :r, :tot, :tb, :e, :s) RETURNING id"),
        {"t": tenant, "cu": body.customer_id, "w": body.warehouse_id, "d": body.invoice_date, "c": body.currency,
         "r": rate, "tot": total, "tb": total_base, "e": body.external_id, "s": shift_id}).scalar()
    cogs = Decimal(0)
    for ln, price in priced:
        cost = stock_out(c, tenant, ln.item_id, body.warehouse_id, ln.qty, "sale", inv)
        cogs += ln.qty * cost
        c.execute(
            text("INSERT INTO sales_invoice_lines(tenant_id, invoice_id, item_id, qty, unit_price, unit_cost) "
                 "VALUES (:t, :i, :it, :q, :p, :u)"),
            {"t": tenant, "i": inv, "it": ln.item_id, "q": ln.qty, "p": price, "u": cost})
    eid = post_sys(c, tenant, body.invoice_date, body.currency, rate,
                   [("1100", total, 0), ("4100", 0, total)], f"sale {inv}")
    cogs = q4(cogs)
    cogs_eid = None
    if cogs > 0:
        cogs_eid = post_sys(c, tenant, body.invoice_date, _base_currency(c, tenant), Decimal(1),
                            [("5100", cogs, 0), ("1200", 0, cogs)], f"cogs {inv}")
    c.execute(text("UPDATE sales_invoices SET entry_id = :e, cogs_entry_id = :ce WHERE id = :i"),
              {"e": eid, "ce": cogs_eid, "i": inv})
    emit(c, tenant, "invoice.posted", {"invoice_id": inv, "customer_id": body.customer_id, "total": total,
                                       "currency": body.currency, "external_id": body.external_id})
    return {"id": str(inv), "total": str(total), "total_base": str(total_base)}


@router.post("/sales/invoices", status_code=201)
def sales_invoice(body: SaleIn, auth=Depends(current_auth), idempotency_key: Optional[str] = Header(default=None)):
    need(auth, "invoice.post")
    tenant = auth["tenant"]
    return run(tenant, lambda c: idempotent(c, tenant, idempotency_key, lambda: create_sale(c, tenant, auth, body)))


@router.get("/sales/invoices")
def list_invoices(auth=Depends(current_auth)):
    def f(c):
        return [{"id": str(r.id), "customer": r.name, "date": str(r.invoice_date), "currency": r.currency,
                 "total": str(r.total), "status": r.status}
                for r in c.execute(text(
                    "SELECT s.id, c.name, s.invoice_date, s.currency, s.total, s.status FROM sales_invoices s "
                    "JOIN customers c ON c.id = s.customer_id ORDER BY s.created_at DESC LIMIT 200"))]
    return run(auth["tenant"], f)


# ---------- payments ----------
class PaymentIn(BaseModel):
    party_type: str
    party_id: UUID
    pay_date: date
    currency: str
    amount: Decimal = Field(gt=0)
    fx_rate: Optional[Decimal] = Field(default=None, gt=0)
    invoice_id: Optional[UUID] = None


def create_payment(c, tenant, body: PaymentIn, shift_id=None):
    if body.party_type not in ("customer", "vendor"):
        raise HTTPException(422, "party_type must be customer or vendor")
    visible(c, "customers" if body.party_type == "customer" else "vendors", body.party_id, "party")
    rate = _resolve_rate(c, tenant, body.currency, body.pay_date, body.fx_rate)
    cash_base = ar_base = q4(body.amount * rate)
    if body.invoice_id:
        if body.party_type != "customer":
            raise HTTPException(422, "invoice_id applies to customer payments only")
        inv = c.execute(text("SELECT customer_id, currency, fx_rate FROM sales_invoices WHERE id = :i"),
                        {"i": body.invoice_id}).first()
        if not inv or inv.customer_id != body.party_id:
            raise HTTPException(422, "unknown invoice for this customer")
        if inv.currency != body.currency:
            raise HTTPException(422, "payment currency must match invoice currency")
        ar_base = q4(body.amount * inv.fx_rate)
    base = _base_currency(c, tenant)
    if cash_base != ar_base:
        diff = cash_base - ar_base
        lines = [("1010", cash_base, 0), ("1100", 0, ar_base)]
        lines.append(("4300", 0, diff) if diff > 0 else ("5300", -diff, 0))
        eid = post_sys(c, tenant, body.pay_date, base, Decimal(1), lines, "payment with fx difference")
    else:
        lines = ([("1010", body.amount, 0), ("1100", 0, body.amount)] if body.party_type == "customer"
                 else [("2100", body.amount, 0), ("1010", 0, body.amount)])
        eid = post_sys(c, tenant, body.pay_date, body.currency, rate, lines, f"payment {body.party_type}")
    pid = c.execute(
        text("INSERT INTO payments(tenant_id, party_type, party_id, pay_date, currency, fx_rate, amount, "
             "amount_base, cash_base, invoice_id, shift_id, entry_id) "
             "VALUES (:t, :pt, :pi, :d, :c, :r, :a, :ab, :cb, :inv, :s, :e) RETURNING id"),
        {"t": tenant, "pt": body.party_type, "pi": body.party_id, "d": body.pay_date, "c": body.currency,
         "r": rate, "a": body.amount, "ab": ar_base, "cb": cash_base, "inv": body.invoice_id,
         "s": shift_id, "e": eid}).scalar()
    emit(c, tenant, "payment.confirmed", {"payment_id": pid, "party_type": body.party_type, "party_id": body.party_id,
                                          "amount": body.amount, "currency": body.currency})
    return {"id": str(pid), "fx_difference": str(cash_base - ar_base)}


@router.post("/payments", status_code=201)
def payment(body: PaymentIn, auth=Depends(current_auth), idempotency_key: Optional[str] = Header(default=None)):
    need(auth, "payment.create")
    tenant = auth["tenant"]
    return run(tenant, lambda c: idempotent(c, tenant, idempotency_key, lambda: create_payment(c, tenant, body)))


# ---------- customer ledger / aging ----------
def _ledger_rows(c, customer_id):
    inv = c.execute(text("SELECT id, invoice_date AS d, currency, total, total_base FROM sales_invoices "
                         "WHERE customer_id = :i AND status = 'posted'"), {"i": customer_id}).all()
    pay = c.execute(text("SELECT id, pay_date AS d, currency, amount, amount_base FROM payments "
                         "WHERE party_type = 'customer' AND party_id = :i"), {"i": customer_id}).all()
    rows = [("invoice", r.id, r.d, r.currency, r.total, r.total_base) for r in inv]
    rows += [("payment", r.id, r.d, r.currency, r.amount, -r.amount_base) for r in pay]
    ret = c.execute(text("SELECT r.id, r.return_date AS d, i.currency, r.total, r.total_base FROM sales_returns r "
                         "JOIN sales_invoices i ON i.id = r.invoice_id WHERE r.customer_id = :i"),
                    {"i": customer_id}).all()
    rows += [("return", r.id, r.d, r.currency, r.total, -r.total_base) for r in ret]
    rows.sort(key=lambda x: (x[2], x[0]))
    return rows


@router.get("/customers/{customer_id}/ledger")
def customer_ledger(customer_id: UUID, auth=Depends(current_auth)):
    def f(c):
        visible(c, "customers", customer_id, "customer")
        bal = Decimal(0)
        out = []
        for kind, id_, d, cur, amt, signed_base in _ledger_rows(c, customer_id):
            bal += signed_base
            out.append({"type": kind, "id": str(id_), "date": str(d), "currency": cur,
                        "amount": str(amt), "base_effect": str(signed_base), "balance_base": str(bal)})
        return {"rows": out, "balance_base": str(bal)}
    return run(auth["tenant"], f)


@router.get("/customers/{customer_id}/aging")
def customer_aging(customer_id: UUID, as_of: date, auth=Depends(current_auth)):
    def f(c):
        visible(c, "customers", customer_id, "customer")
        invoices = [(d, base) for k, _, d, _, _, base in _ledger_rows(c, customer_id) if k == "invoice"]
        paid = -sum((b for k, _, _, _, _, b in _ledger_rows(c, customer_id) if k != "invoice"), Decimal(0))
        buckets = {"0-30": Decimal(0), "31-60": Decimal(0), "61-90": Decimal(0), "90+": Decimal(0)}
        for d, base in sorted(invoices):
            applied = min(paid, base)
            paid -= applied
            open_amt = base - applied
            if open_amt <= 0:
                continue
            age = (as_of - d).days
            key = "0-30" if age <= 30 else "31-60" if age <= 60 else "61-90" if age <= 90 else "90+"
            buckets[key] += open_amt
        return {k: str(v) for k, v in buckets.items()}
    return run(auth["tenant"], f)


# ---------- inventory ----------
@router.get("/inventory/balances")
def balances(auth=Depends(current_auth)):
    def f(c):
        return [{"item": r.name, "sku": r.sku, "warehouse": r.wname, "qty": str(r.qty), "avg_cost": str(r.avg_cost)}
                for r in c.execute(text(
                    "SELECT i.sku, i.name, w.name AS wname, b.qty, b.avg_cost FROM stock_balances b "
                    "JOIN items i ON i.id = b.item_id JOIN warehouses w ON w.id = b.warehouse_id ORDER BY i.sku"))]
    return run(auth["tenant"], f)


class TransferIn(BaseModel):
    item_id: UUID
    from_warehouse: UUID
    to_warehouse: UUID
    qty: Decimal = Field(gt=0)


@router.post("/inventory/transfers", status_code=201)
def transfer(body: TransferIn, auth=Depends(current_auth)):
    need(auth, "warehouse.transfer")
    tenant = auth["tenant"]

    def f(c):
        if body.from_warehouse == body.to_warehouse:
            raise HTTPException(422, "same warehouse")
        for t, i, w in (("items", body.item_id, "item"), ("warehouses", body.from_warehouse, "warehouse"),
                        ("warehouses", body.to_warehouse, "warehouse")):
            visible(c, t, i, w)
        doc = c.execute(text("SELECT gen_random_uuid()")).scalar()
        cost = stock_out(c, tenant, body.item_id, body.from_warehouse, body.qty, "transfer", doc)
        stock_in(c, tenant, body.item_id, body.to_warehouse, body.qty, cost, "transfer", doc)
        return {"id": str(doc)}
    return run(tenant, f)


# ---------- POS ----------
class ShiftIn(BaseModel):
    warehouse_id: UUID
    opening_cash: Decimal = Field(default=Decimal(0), ge=0)


@router.post("/pos/shifts", status_code=201)
def open_shift(body: ShiftIn, auth=Depends(current_auth)):
    need(auth, "pos.sell")

    def f(c):
        visible(c, "warehouses", body.warehouse_id, "warehouse")
        return {"id": str(c.execute(
            text("INSERT INTO pos_shifts(tenant_id, warehouse_id, opening_cash) VALUES (:t, :w, :o) RETURNING id"),
            {"t": auth["tenant"], "w": body.warehouse_id, "o": body.opening_cash}).scalar())}
    return run(auth["tenant"], f)


class PosSaleIn(BaseModel):
    customer_id: UUID
    invoice_date: date
    currency: str
    fx_rate: Optional[Decimal] = Field(default=None, gt=0)
    external_id: Optional[str] = None
    lines: list[SLine] = Field(min_length=1)


@router.post("/pos/shifts/{shift_id}/sales", status_code=201)
def pos_sale(shift_id: UUID, body: PosSaleIn, auth=Depends(current_auth)):
    need(auth, "pos.sell")
    tenant = auth["tenant"]

    def f(c):
        sh = c.execute(text("SELECT warehouse_id, status FROM pos_shifts WHERE id = :i FOR UPDATE"), {"i": shift_id}).first()
        if not sh:
            raise HTTPException(404, "shift not found")
        if sh.status != "open":
            raise HTTPException(409, "shift is closed")
        sale = create_sale(c, tenant, auth, SaleIn(
            customer_id=body.customer_id, warehouse_id=sh.warehouse_id, invoice_date=body.invoice_date,
            currency=body.currency, fx_rate=body.fx_rate, external_id=body.external_id, lines=body.lines), shift_id)
        if not sale.get("replayed"):
            create_payment(c, tenant, PaymentIn(party_type="customer", party_id=body.customer_id,
                           pay_date=body.invoice_date, currency=body.currency, amount=Decimal(sale["total"]),
                           fx_rate=body.fx_rate), shift_id)
        return sale
    return run(tenant, f)


class CloseIn(BaseModel):
    counted_cash: Decimal = Field(ge=0)


@router.post("/pos/shifts/{shift_id}/close")
def close_shift(shift_id: UUID, body: CloseIn, auth=Depends(current_auth)):
    need(auth, "pos.sell")
    tenant = auth["tenant"]

    def f(c):
        sh = c.execute(text("SELECT opening_cash, status FROM pos_shifts WHERE id = :i FOR UPDATE"), {"i": shift_id}).first()
        if not sh:
            raise HTTPException(404, "shift not found")
        if sh.status != "open":
            raise HTTPException(409, "shift already closed")
        sales = c.execute(text("SELECT COALESCE(SUM(COALESCE(cash_base, amount_base)),0) FROM payments WHERE shift_id = :i"), {"i": shift_id}).scalar()
        expected = sh.opening_cash + sales
        diff = body.counted_cash - expected
        if diff != 0:
            base = _base_currency(c, tenant)
            lines = ([("5200", -diff, 0), ("1010", 0, -diff)] if diff < 0 else [("1010", diff, 0), ("4200", 0, diff)])
            post_sys(c, tenant, date.today(), base, Decimal(1), lines, f"shift {shift_id} cash difference")
        c.execute(text("UPDATE pos_shifts SET status='closed', expected_cash=:e, counted_cash=:c, difference=:d, "
                       "closed_at=now() WHERE id=:i"),
                  {"e": expected, "c": body.counted_cash, "d": diff, "i": shift_id})
        return {"expected_cash": str(expected), "counted_cash": str(body.counted_cash), "difference": str(diff)}
    return run(tenant, f)


# ---------- reports ----------
@router.get("/reports/profit")
def profit(auth=Depends(current_auth)):
    need(auth, "reports.profit")

    def f(c):
        r = c.execute(text(
            "SELECT COALESCE(SUM(CASE WHEN a.type='income' THEN l.base_credit - l.base_debit END),0) AS inc, "
            "COALESCE(SUM(CASE WHEN a.type='expense' THEN l.base_debit - l.base_credit END),0) AS exp "
            "FROM journal_lines l JOIN accounts a ON a.id = l.account_id")).first()
        return {"income": str(r.inc), "expense": str(r.exp), "profit": str(r.inc - r.exp)}
    return run(auth["tenant"], f)


# ---------- sales returns ----------
class RLine(BaseModel):
    item_id: UUID
    qty: Decimal = Field(gt=0)


class ReturnIn(BaseModel):
    return_date: date
    lines: list[RLine] = Field(min_length=1)


@router.post("/sales/invoices/{invoice_id}/returns", status_code=201)
def sales_return(invoice_id: UUID, body: ReturnIn, auth=Depends(current_auth),
                 idempotency_key: Optional[str] = Header(default=None)):
    need(auth, "invoice.return")
    tenant = auth["tenant"]

    def work(c):
        def go():
            inv = c.execute(text("SELECT customer_id, warehouse_id, currency, fx_rate FROM sales_invoices "
                                 "WHERE id = :i FOR UPDATE"), {"i": invoice_id}).first()
            if not inv:
                raise HTTPException(404, "invoice not found")
            rid = c.execute(text("SELECT gen_random_uuid()")).scalar()
            total, cogs, rows = Decimal(0), Decimal(0), []
            for ln in body.lines:
                sold = c.execute(
                    text("SELECT COALESCE(SUM(qty),0) AS q, COALESCE(SUM(qty*unit_price),0) AS v, "
                         "COALESCE(SUM(qty*unit_cost),0) AS cst FROM sales_invoice_lines "
                         "WHERE invoice_id = :i AND item_id = :it"), {"i": invoice_id, "it": ln.item_id}).first()
                back = c.execute(
                    text("SELECT COALESCE(SUM(l.qty),0) FROM sales_return_lines l JOIN sales_returns r "
                         "ON r.id = l.return_id WHERE r.invoice_id = :i AND l.item_id = :it"),
                    {"i": invoice_id, "it": ln.item_id}).scalar()
                if sold.q == 0:
                    raise HTTPException(422, "item was not sold on this invoice")
                if ln.qty > sold.q - back:
                    raise HTTPException(409, "return quantity exceeds quantity sold")
                price = sold.v / sold.q
                cost = (sold.cst / sold.q).quantize(Decimal("0.000001"))
                total += ln.qty * price
                cogs += ln.qty * cost
                rows.append((ln, price, cost))
            total = q4(total)
            total_base = q4(total * inv.fx_rate)
            c.execute(
                text("INSERT INTO sales_returns(id, tenant_id, invoice_id, customer_id, return_date, total, total_base) "
                     "VALUES (:r, :t, :i, :c, :d, :tot, :tb)"),
                {"r": rid, "t": tenant, "i": invoice_id, "c": inv.customer_id, "d": body.return_date,
                 "tot": total, "tb": total_base})
            for ln, price, cost in rows:
                c.execute(
                    text("INSERT INTO sales_return_lines(tenant_id, return_id, item_id, qty, unit_price, unit_cost) "
                         "VALUES (:t, :r, :it, :q, :p, :u)"),
                    {"t": tenant, "r": rid, "it": ln.item_id, "q": ln.qty, "p": price, "u": cost})
                stock_in(c, tenant, ln.item_id, inv.warehouse_id, ln.qty, cost, "sales_return", rid)
            eid = post_sys(c, tenant, body.return_date, inv.currency, inv.fx_rate,
                           [("4100", total, 0), ("1100", 0, total)], f"return {rid}")
            cogs = q4(cogs)
            ceid = None
            if cogs > 0:
                ceid = post_sys(c, tenant, body.return_date, _base_currency(c, tenant), Decimal(1),
                                [("1200", cogs, 0), ("5100", 0, cogs)], f"return cogs {rid}")
            c.execute(text("UPDATE sales_returns SET entry_id = :e, cogs_entry_id = :ce WHERE id = :i"),
                      {"e": eid, "ce": ceid, "i": rid})
            return {"id": str(rid), "total": str(total), "total_base": str(total_base)}
        return idempotent(c, tenant, idempotency_key, go)
    return run(tenant, work)


# ---------- stock counts (approval required) ----------
class CountLine(BaseModel):
    item_id: UUID
    counted_qty: Decimal = Field(ge=0)


class CountIn(BaseModel):
    warehouse_id: UUID
    lines: list[CountLine] = Field(min_length=1)


@router.post("/inventory/counts", status_code=201)
def create_count(body: CountIn, auth=Depends(current_auth)):
    need(auth, "inventory.count")
    tenant = auth["tenant"]

    def f(c):
        visible(c, "warehouses", body.warehouse_id, "warehouse")
        cid = c.execute(text("INSERT INTO stock_counts(tenant_id, warehouse_id) VALUES (:t, :w) RETURNING id"),
                        {"t": tenant, "w": body.warehouse_id}).scalar()
        for ln in body.lines:
            visible(c, "items", ln.item_id, "item")
            c.execute(text("INSERT INTO stock_count_lines(tenant_id, count_id, item_id, counted_qty) "
                           "VALUES (:t, :c, :i, :q)"), {"t": tenant, "c": cid, "i": ln.item_id, "q": ln.counted_qty})
        return {"id": str(cid), "status": "pending_approval"}
    return run(tenant, f)


@router.post("/inventory/counts/{count_id}/approve")
def approve_count(count_id: UUID, auth=Depends(current_auth)):
    need(auth, "inventory.adjust")
    tenant = auth["tenant"]

    def f(c):
        cnt = c.execute(text("SELECT warehouse_id, status FROM stock_counts WHERE id = :i FOR UPDATE"),
                        {"i": count_id}).first()
        if not cnt:
            raise HTTPException(404, "count not found")
        if cnt.status != "pending_approval":
            raise HTTPException(409, "count already processed")
        net = Decimal(0)
        for ln in c.execute(text("SELECT item_id, counted_qty FROM stock_count_lines WHERE count_id = :i"),
                            {"i": count_id}).all():
            cur = c.execute(text("SELECT qty, avg_cost FROM stock_balances WHERE item_id = :i AND warehouse_id = :w "
                                 "FOR UPDATE"), {"i": ln.item_id, "w": cnt.warehouse_id}).first()
            have, avg = (cur.qty, cur.avg_cost) if cur else (Decimal(0), Decimal(0))
            diff = ln.counted_qty - have
            if diff > 0:
                stock_in(c, tenant, ln.item_id, cnt.warehouse_id, diff, avg, "count", count_id)
                net += diff * avg
            elif diff < 0:
                cost = stock_out(c, tenant, ln.item_id, cnt.warehouse_id, -diff, "count", count_id)
                net -= -diff * cost
        net = q4(net)
        if net != 0:
            lines = [("1200", net, 0), ("5400", 0, net)] if net > 0 else [("5400", -net, 0), ("1200", 0, -net)]
            post_sys(c, tenant, date.today(), _base_currency(c, tenant), Decimal(1), lines, f"count {count_id}")
        c.execute(text("UPDATE stock_counts SET status = 'approved', approved_at = now() WHERE id = :i"),
                  {"i": count_id})
        c.execute(text("INSERT INTO audit_events(tenant_id, action, detail) VALUES (:t, 'inventory.adjust', :d)"),
                  {"t": tenant, "d": json.dumps({"count_id": str(count_id), "value_base": str(net)})})
        return {"status": "approved", "adjustment_value": str(net)}
    return run(tenant, f)
