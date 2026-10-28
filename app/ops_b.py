import html
from datetime import date
from decimal import Decimal
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

from .main import current_auth, has_scope
from .ops import (PaymentIn, SaleIn, SLine, create_payment, create_sale, need, run, visible)

router = APIRouter(prefix="/api/v1")

# ---------- restaurant ----------


class TableIn(BaseModel):
    name: str


@router.post("/restaurant/tables", status_code=201)
def create_table(body: TableIn, auth=Depends(current_auth)):
    need(auth, "restaurant.manage")
    return run(auth["tenant"], lambda c: {"id": str(c.execute(
        text("INSERT INTO restaurant_tables(tenant_id, name) VALUES (:t, :n) RETURNING id"),
        {"t": auth["tenant"], "n": body.name}).scalar())})


@router.get("/restaurant/tables")
def list_tables(auth=Depends(current_auth)):
    def f(c):
        return [{"id": str(r.id), "name": r.name, "open_order": None if r.oid is None else str(r.oid)}
                for r in c.execute(text(
                    "SELECT t.id, t.name, (SELECT o.id FROM dining_orders o WHERE o.table_id = t.id "
                    "AND o.status = 'open' ORDER BY o.created_at LIMIT 1) AS oid FROM restaurant_tables t ORDER BY t.name"))]
    return run(auth["tenant"], f)


class OrderIn(BaseModel):
    table_id: UUID


@router.post("/restaurant/orders", status_code=201)
def open_order(body: OrderIn, auth=Depends(current_auth)):
    need(auth, "restaurant.order")

    def f(c):
        visible(c, "restaurant_tables", body.table_id, "table")
        return {"id": str(c.execute(
            text("INSERT INTO dining_orders(tenant_id, table_id) VALUES (:t, :b) RETURNING id"),
            {"t": auth["tenant"], "b": body.table_id}).scalar())}
    return run(auth["tenant"], f)


class OLine(BaseModel):
    item_id: UUID
    qty: Decimal = Field(gt=0)
    note: Optional[str] = None


class OLines(BaseModel):
    lines: list[OLine] = Field(min_length=1)


def _open_order(c, order_id):
    o = c.execute(text("SELECT status FROM dining_orders WHERE id = :i FOR UPDATE"), {"i": order_id}).first()
    if not o:
        raise HTTPException(404, "order not found")
    if o.status != "open":
        raise HTTPException(409, f"order is {o.status}")


@router.post("/restaurant/orders/{order_id}/lines", status_code=201)
def add_lines(order_id: UUID, body: OLines, auth=Depends(current_auth)):
    need(auth, "restaurant.order")
    tenant = auth["tenant"]

    def f(c):
        _open_order(c, order_id)
        tid = c.execute(text("INSERT INTO kitchen_tickets(tenant_id, order_id) VALUES (:t, :o) RETURNING id"),
                        {"t": tenant, "o": order_id}).scalar()
        for ln in body.lines:
            visible(c, "items", ln.item_id, "item")
            c.execute(text("INSERT INTO dining_order_lines(tenant_id, order_id, item_id, qty, note, ticket_id) "
                           "VALUES (:t, :o, :i, :q, :n, :k)"),
                      {"t": tenant, "o": order_id, "i": ln.item_id, "q": ln.qty, "n": ln.note, "k": tid})
        return {"ticket_id": str(tid)}
    return run(tenant, f)


@router.get("/restaurant/orders/{order_id}")
def get_order(order_id: UUID, auth=Depends(current_auth)):
    def f(c):
        o = c.execute(text("SELECT status, table_id FROM dining_orders WHERE id = :i"), {"i": order_id}).first()
        if not o:
            raise HTTPException(404, "order not found")
        lines = c.execute(text("SELECT l.id, i.sku, i.name, l.qty, l.note FROM dining_order_lines l "
                               "JOIN items i ON i.id = l.item_id WHERE l.order_id = :i ORDER BY l.id"),
                          {"i": order_id}).all()
        return {"status": o.status, "table_id": str(o.table_id),
                "lines": [{"id": str(l.id), "sku": l.sku, "name": l.name, "qty": str(l.qty), "note": l.note} for l in lines]}
    return run(auth["tenant"], f)


@router.get("/restaurant/kitchen")
def kitchen(auth=Depends(current_auth)):
    def f(c):
        out = []
        for t in c.execute(text("SELECT k.id, k.status, k.created_at, t.name AS tname FROM kitchen_tickets k "
                                "JOIN dining_orders o ON o.id = k.order_id JOIN restaurant_tables t ON t.id = o.table_id "
                                "WHERE k.status <> 'served' AND o.status = 'open' ORDER BY k.created_at")):
            lines = c.execute(text("SELECT i.name, l.qty, l.note FROM dining_order_lines l JOIN items i ON i.id = l.item_id "
                                   "WHERE l.ticket_id = :k"), {"k": t.id}).all()
            out.append({"id": str(t.id), "table": t.tname, "status": t.status,
                        "lines": [{"name": l.name, "qty": str(l.qty), "note": l.note} for l in lines]})
        return out
    return run(auth["tenant"], f)


NEXT = {"new": "preparing", "preparing": "ready", "ready": "served"}


class StatusIn(BaseModel):
    status: str


@router.post("/restaurant/tickets/{ticket_id}/status")
def ticket_status(ticket_id: UUID, body: StatusIn, auth=Depends(current_auth)):
    need(auth, "restaurant.kitchen")

    def f(c):
        t = c.execute(text("SELECT status FROM kitchen_tickets WHERE id = :i FOR UPDATE"), {"i": ticket_id}).first()
        if not t:
            raise HTTPException(404, "ticket not found")
        if NEXT.get(t.status) != body.status:
            raise HTTPException(409, f"cannot move {t.status} -> {body.status}")
        c.execute(text("UPDATE kitchen_tickets SET status = :s WHERE id = :i"), {"s": body.status, "i": ticket_id})
        return {"status": body.status}
    return run(auth["tenant"], f)


class MergeIn(BaseModel):
    from_order_id: UUID


@router.post("/restaurant/orders/{order_id}/merge")
def merge_orders(order_id: UUID, body: MergeIn, auth=Depends(current_auth)):
    need(auth, "restaurant.order")

    def f(c):
        if order_id == body.from_order_id:
            raise HTTPException(422, "cannot merge an order with itself")
        _open_order(c, order_id)
        _open_order(c, body.from_order_id)
        c.execute(text("UPDATE dining_order_lines SET order_id = :t WHERE order_id = :f"),
                  {"t": order_id, "f": body.from_order_id})
        c.execute(text("UPDATE kitchen_tickets SET order_id = :t WHERE order_id = :f"),
                  {"t": order_id, "f": body.from_order_id})
        c.execute(text("UPDATE dining_orders SET status = 'merged' WHERE id = :f"), {"f": body.from_order_id})
        return {"merged": True}
    return run(auth["tenant"], f)


class SplitLine(BaseModel):
    line_id: UUID
    qty: Decimal = Field(gt=0)


class SplitIn(BaseModel):
    lines: list[SplitLine] = Field(min_length=1)


@router.post("/restaurant/orders/{order_id}/split", status_code=201)
def split_order(order_id: UUID, body: SplitIn, auth=Depends(current_auth)):
    need(auth, "restaurant.order")
    tenant = auth["tenant"]

    def f(c):
        _open_order(c, order_id)
        table_id = c.execute(text("SELECT table_id FROM dining_orders WHERE id = :i"), {"i": order_id}).scalar()
        new_id = c.execute(text("INSERT INTO dining_orders(tenant_id, table_id) VALUES (:t, :b) RETURNING id"),
                           {"t": tenant, "b": table_id}).scalar()
        for sl in body.lines:
            ln = c.execute(text("SELECT item_id, qty, note, ticket_id FROM dining_order_lines "
                                "WHERE id = :i AND order_id = :o FOR UPDATE"), {"i": sl.line_id, "o": order_id}).first()
            if not ln:
                raise HTTPException(422, "line not on this order")
            if sl.qty > ln.qty:
                raise HTTPException(409, "split quantity exceeds line quantity")
            if sl.qty == ln.qty:
                c.execute(text("UPDATE dining_order_lines SET order_id = :n WHERE id = :i"), {"n": new_id, "i": sl.line_id})
            else:
                c.execute(text("UPDATE dining_order_lines SET qty = qty - :q WHERE id = :i"), {"q": sl.qty, "i": sl.line_id})
                c.execute(text("INSERT INTO dining_order_lines(tenant_id, order_id, item_id, qty, note, ticket_id) "
                               "VALUES (:t, :o, :it, :q, :n, :k)"),
                          {"t": tenant, "o": new_id, "it": ln.item_id, "q": sl.qty, "n": ln.note, "k": ln.ticket_id})
        return {"id": str(new_id)}
    return run(tenant, f)


class PayOrderIn(BaseModel):
    shift_id: UUID
    customer_id: UUID
    currency: str
    invoice_date: date
    fx_rate: Optional[Decimal] = Field(default=None, gt=0)


@router.post("/restaurant/orders/{order_id}/pay", status_code=201)
def pay_order(order_id: UUID, body: PayOrderIn, auth=Depends(current_auth)):
    need(auth, "pos.sell")
    tenant = auth["tenant"]

    def f(c):
        _open_order(c, order_id)
        sh = c.execute(text("SELECT warehouse_id, status FROM pos_shifts WHERE id = :i FOR UPDATE"),
                       {"i": body.shift_id}).first()
        if not sh or sh.status != "open":
            raise HTTPException(409, "an open shift is required to take payment")
        rows = c.execute(text("SELECT item_id, SUM(qty) AS q FROM dining_order_lines WHERE order_id = :o GROUP BY item_id"),
                         {"o": order_id}).all()
        if not rows:
            raise HTTPException(422, "order has no lines")
        sale = create_sale(c, tenant, auth, SaleIn(
            customer_id=body.customer_id, warehouse_id=sh.warehouse_id, invoice_date=body.invoice_date,
            currency=body.currency, fx_rate=body.fx_rate, lines=[SLine(item_id=r.item_id, qty=r.q) for r in rows]),
            body.shift_id)
        create_payment(c, tenant, PaymentIn(party_type="customer", party_id=body.customer_id, pay_date=body.invoice_date,
                                            currency=body.currency, amount=Decimal(sale["total"]), fx_rate=body.fx_rate),
                       body.shift_id)
        c.execute(text("UPDATE dining_orders SET status = 'paid', invoice_id = :v WHERE id = :o"),
                  {"v": sale["id"], "o": order_id})
        return sale
    return run(tenant, f)


# ---------- print templates ----------
PAPER = {"a4": (210, 297), "a5": (148, 210), "thermal58": (58, None), "thermal80": (80, None)}


class TemplateIn(BaseModel):
    name: str
    paper: str = Field(pattern="^(a4|a5|thermal58|thermal80|custom)$")
    width_mm: Optional[Decimal] = Field(default=None, gt=20, le=500)
    height_mm: Optional[Decimal] = Field(default=None, gt=20, le=2000)
    header: str = ""
    footer: str = ""
    show_cost: bool = False


@router.post("/print/templates", status_code=201)
def create_template(body: TemplateIn, auth=Depends(current_auth)):
    need(auth, "print.templates.manage")
    if body.paper == "custom" and not body.width_mm:
        raise HTTPException(422, "custom paper needs width_mm")
    return run(auth["tenant"], lambda c: {"id": str(c.execute(
        text("INSERT INTO print_templates(tenant_id, name, paper, width_mm, height_mm, header, footer, show_cost) "
             "VALUES (:t, :n, :p, :w, :h, :hd, :ft, :sc) RETURNING id"),
        {"t": auth["tenant"], "n": body.name, "p": body.paper, "w": body.width_mm, "h": body.height_mm,
         "hd": body.header, "ft": body.footer, "sc": body.show_cost}).scalar())})


@router.get("/print/templates")
def list_templates(auth=Depends(current_auth)):
    return run(auth["tenant"], lambda c: [
        {"id": str(r.id), "name": r.name, "paper": r.paper}
        for r in c.execute(text("SELECT id, name, paper FROM print_templates ORDER BY name"))])


class PrintIn(BaseModel):
    template_id: UUID


def render_invoice(c, tpl, inv, lines, show_cost) -> str:
    e = html.escape
    width, height = PAPER.get(tpl.paper, (float(tpl.width_mm or 80), float(tpl.height_mm) if tpl.height_mm else None))
    thermal = height is None
    size = f"{width}mm auto" if thermal else f"{width}mm {height}mm"
    font = "9pt" if thermal else "11pt"
    cur = e(inv.currency)
    rows = "".join(
        f"<tr><td class='nm'>{e(l.name)}</td><td class='n'>{l.qty:f}</td><td class='n'>{l.unit_price:,.2f}</td>"
        f"<td class='n'>{(l.qty * l.unit_price):,.2f}</td>"
        + (f"<td class='n'>{l.unit_cost:,.2f}</td>" if show_cost else "") + "</tr>" for l in lines)
    cost_h = "<th>التكلفة</th>" if show_cost else ""
    return f"""<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8"><style>
@page {{ size: {size}; margin: 4mm; }}
html,body {{ margin:0; padding:0; font-family: Tahoma, sans-serif; font-size:{font}; }}
.sheet {{ width: calc({width}mm - 8mm); }}
table {{ width:100%; border-collapse:collapse; table-layout:fixed; }}
th,td {{ padding:2px 3px; border-bottom:1px dotted #888; word-wrap:break-word; overflow-wrap:anywhere; }}
td.n,th.n {{ text-align:left; direction:ltr; }}
td.nm {{ width:40%; }}
.total {{ font-weight:bold; font-size:1.15em; page-break-inside:avoid; break-inside:avoid; }}
.hdr,.ftr {{ text-align:center; white-space:pre-wrap; overflow-wrap:anywhere; }}
</style></head><body><div class="sheet">
<div class="hdr">{e(tpl.header)}</div>
<div>فاتورة #{e(str(inv.id)[:8])} — {e(str(inv.invoice_date))}</div>
<div>العميل: {e(inv.customer)}</div>
<table><tr><th class="nm">الصنف</th><th class="n">الكمية</th><th class="n">السعر</th><th class="n">المبلغ</th>{cost_h}</tr>{rows}
<tr class="total"><td colspan="3">الإجمالي</td><td class="n" id="grand-total">{inv.total:,.2f} {cur}</td></tr></table>
<div class="ftr">{e(tpl.footer)}</div></div></body></html>"""


@router.post("/sales/invoices/{invoice_id}/print", response_class=HTMLResponse)
def print_invoice(invoice_id: UUID, body: PrintIn, auth=Depends(current_auth)):
    need(auth, "invoice.print")
    tenant = auth["tenant"]

    def f(c):
        tpl = c.execute(text("SELECT * FROM print_templates WHERE id = :i"), {"i": body.template_id}).first()
        if not tpl:
            raise HTTPException(404, "template not found")
        inv = c.execute(text("SELECT s.id, s.invoice_date, s.currency, s.total, c.name AS customer FROM sales_invoices s "
                             "JOIN customers c ON c.id = s.customer_id WHERE s.id = :i"), {"i": invoice_id}).first()
        if not inv:
            raise HTTPException(404, "invoice not found")
        lines = c.execute(text("SELECT i.name, l.qty, l.unit_price, l.unit_cost FROM sales_invoice_lines l "
                               "JOIN items i ON i.id = l.item_id WHERE l.invoice_id = :i ORDER BY l.id"),
                          {"i": invoice_id}).all()
        again = c.execute(text("SELECT 1 FROM print_history WHERE doc_type = 'sales_invoice' AND doc_id = :d LIMIT 1"),
                          {"d": invoice_id}).first() is not None
        c.execute(text("INSERT INTO print_history(tenant_id, template_id, doc_type, doc_id, is_reprint) "
                       "VALUES (:t, :tp, 'sales_invoice', :d, :r)"),
                  {"t": tenant, "tp": body.template_id, "d": invoice_id, "r": again})
        return render_invoice(c, tpl, inv, lines, tpl.show_cost and has_scope(auth, "invoice.view_cost"))
    return HTMLResponse(run(tenant, f))


@router.get("/print/history")
def print_history(auth=Depends(current_auth)):
    return run(auth["tenant"], lambda c: [
        {"doc_id": str(r.doc_id), "doc_type": r.doc_type, "is_reprint": r.is_reprint, "printed_at": r.printed_at.isoformat()}
        for r in c.execute(text("SELECT doc_id, doc_type, is_reprint, printed_at FROM print_history "
                                "ORDER BY printed_at DESC LIMIT 200"))])
