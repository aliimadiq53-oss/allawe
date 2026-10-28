import hashlib
import json
import os
import secrets
import threading
import time
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Optional
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from .db import admin_conn, init_schema, tenant_conn

app = FastAPI(title="Accounting Platform API", version="1")
Q = Decimal("0.0001")


@app.on_event("startup")
def _startup():
    init_schema()
    if os.environ.get("WEBHOOK_WORKER", "1") != "0":
        from .webhooks import start_worker
        start_worker()


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


RATE_LIMIT_PER_MIN = int(os.environ.get("RATE_LIMIT_PER_MIN", "600"))
_hits: dict = {}
_hits_lock = threading.Lock()


def _rate_limit(key_hash: str):
    now = time.monotonic()
    with _hits_lock:
        window = [t for t in _hits.get(key_hash, []) if now - t < 60]
        if len(window) >= RATE_LIMIT_PER_MIN:
            _hits[key_hash] = window
            raise HTTPException(429, "rate limit exceeded", headers={"Retry-After": "60"})
        window.append(now)
        _hits[key_hash] = window


def current_auth(x_api_key: str = Header(...)) -> dict:
    h = _hash(x_api_key)
    with admin_conn() as c:
        row = c.execute(
            text("SELECT tenant_id, scopes FROM api_keys WHERE key_hash = :h"),
            {"h": h},
        ).first()
    if not row:
        raise HTTPException(401, "invalid api key")
    _rate_limit(h)
    return {"tenant": row[0], "scopes": list(row[1])}


def current_tenant(auth: dict = Depends(current_auth)) -> UUID:
    return auth["tenant"]


def has_scope(auth: dict, perm: str) -> bool:
    return "*" in auth["scopes"] or perm in auth["scopes"]


def require(perm: str):
    def dep(auth: dict = Depends(current_auth)) -> UUID:
        if not has_scope(auth, perm):
            raise HTTPException(403, f"missing permission: {perm}")
        return auth["tenant"]

    return dep


DEFAULT_CHART = [
    ("1010", "Cash", "asset"),
    ("1100", "Accounts Receivable", "asset"),
    ("1200", "Inventory", "asset"),
    ("2100", "Accounts Payable", "liability"),
    ("4100", "Sales", "income"),
    ("5100", "Cost of Goods Sold", "expense"),
    ("5200", "Cash Shortage", "expense"),
    ("4200", "Cash Overage", "income"),
]


def _map_db_error(e: DBAPIError):
    msg = str(e.orig)
    if "period is locked" in msg:
        raise HTTPException(409, "accounting period is locked")
    if "unbalanced" in msg:
        raise HTTPException(422, "unbalanced journal entry")
    if "immutable" in msg:
        raise HTTPException(409, "posted entries are immutable")
    if isinstance(e, IntegrityError):
        raise HTTPException(409, "constraint violation (duplicate or invalid reference)")
    raise e


class TenantIn(BaseModel):
    name: str
    base_currency: str = "IQD"
    costing: str = Field(default="avg", pattern="^(avg|fifo)$")


@app.post("/api/v1/admin/tenants", status_code=201)
def create_tenant(body: TenantIn, x_platform_key: str = Header(...)):
    expected = os.environ.get("PLATFORM_KEY", "dev-platform-key")
    if not secrets.compare_digest(x_platform_key, expected):
        raise HTTPException(403, "forbidden")
    api_key = secrets.token_urlsafe(32)
    with admin_conn() as c:
        tid = c.execute(
            text("INSERT INTO tenants(name, base_currency, costing) VALUES (:n, :b, :co) RETURNING id"),
            {"n": body.name, "b": body.base_currency, "co": body.costing},
        ).scalar()
        c.execute(
            text("INSERT INTO api_keys(key_hash, tenant_id, scopes) VALUES (:h, :t, ARRAY['*'])"),
            {"h": _hash(api_key), "t": tid},
        )
        for code, name, typ in DEFAULT_CHART:
            c.execute(
                text("INSERT INTO accounts(tenant_id, code, name, type) VALUES (:t, :c, :n, :ty)"),
                {"t": tid, "c": code, "n": name, "ty": typ},
            )
    return {"tenant_id": str(tid), "api_key": api_key}


class KeyIn(BaseModel):
    scopes: list[str]


@app.post("/api/v1/api-keys", status_code=201)
def create_key(body: KeyIn, tenant=Depends(require("api.keys.manage"))):
    api_key = secrets.token_urlsafe(32)
    with admin_conn() as c:
        c.execute(
            text("INSERT INTO api_keys(key_hash, tenant_id, scopes) VALUES (:h, :t, :s)"),
            {"h": _hash(api_key), "t": tenant, "s": body.scopes},
        )
    return {"api_key": api_key, "scopes": body.scopes}


class AccountIn(BaseModel):
    code: str
    name: str
    type: str


@app.post("/api/v1/accounts", status_code=201)
def create_account(body: AccountIn, tenant=Depends(current_tenant)):
    try:
        with tenant_conn(tenant) as c:
            aid = c.execute(
                text(
                    "INSERT INTO accounts(tenant_id, code, name, type) "
                    "VALUES (:t, :c, :n, :ty) RETURNING id"
                ),
                {"t": tenant, "c": body.code, "n": body.name, "ty": body.type},
            ).scalar()
    except DBAPIError as e:
        _map_db_error(e)
    return {"id": str(aid)}


@app.get("/api/v1/accounts")
def list_accounts(tenant=Depends(current_tenant)):
    with tenant_conn(tenant) as c:
        rows = c.execute(text("SELECT id, code, name, type FROM accounts ORDER BY code"))
        return [
            {"id": str(r.id), "code": r.code, "name": r.name, "type": r.type} for r in rows
        ]


class PeriodIn(BaseModel):
    start_date: date
    end_date: date


@app.post("/api/v1/periods", status_code=201)
def create_period(body: PeriodIn, tenant=Depends(current_tenant)):
    with tenant_conn(tenant) as c:
        pid = c.execute(
            text(
                "INSERT INTO accounting_periods(tenant_id, start_date, end_date) "
                "VALUES (:t, :s, :e) RETURNING id"
            ),
            {"t": tenant, "s": body.start_date, "e": body.end_date},
        ).scalar()
    return {"id": str(pid)}


@app.post("/api/v1/periods/{period_id}/lock")
def lock_period(period_id: UUID, tenant=Depends(current_tenant)):
    with tenant_conn(tenant) as c:
        n = c.execute(
            text("UPDATE accounting_periods SET locked = true WHERE id = :i"),
            {"i": period_id},
        ).rowcount
        if not n:
            raise HTTPException(404, "period not found")
        c.execute(
            text("INSERT INTO audit_events(tenant_id, action, detail) VALUES (:t, 'period.lock', :d)"),
            {"t": tenant, "d": json.dumps({"period_id": str(period_id)})},
        )
    return {"locked": True}


class RateIn(BaseModel):
    currency: str
    rate_date: date
    rate: Decimal = Field(gt=0)


@app.post("/api/v1/exchange-rates", status_code=201)
def set_rate(body: RateIn, tenant=Depends(current_tenant)):
    with tenant_conn(tenant) as c:
        c.execute(
            text(
                "INSERT INTO exchange_rates(tenant_id, currency, rate_date, rate) "
                "VALUES (:t, :c, :d, :r) "
                "ON CONFLICT (tenant_id, currency, rate_date) DO UPDATE SET rate = :r"
            ),
            {"t": tenant, "c": body.currency, "d": body.rate_date, "r": body.rate},
        )
    return {"ok": True}


class LineIn(BaseModel):
    account_id: UUID
    debit: Decimal = Field(default=Decimal(0), ge=0)
    credit: Decimal = Field(default=Decimal(0), ge=0)


class EntryIn(BaseModel):
    entry_date: date
    currency: str
    memo: Optional[str] = None
    fx_rate: Optional[Decimal] = Field(default=None, gt=0)
    lines: list[LineIn] = Field(min_length=2)


def _base_currency(c, tenant) -> str:
    return c.execute(text("SELECT base_currency FROM tenants WHERE id = :t"), {"t": tenant}).scalar()


def _resolve_rate(c, tenant, currency, on_date, given):
    if currency == _base_currency(c, tenant):
        return Decimal(1)
    if given is not None:
        return given
    row = c.execute(
        text(
            "SELECT rate FROM exchange_rates WHERE currency = :c AND rate_date <= :d "
            "ORDER BY rate_date DESC LIMIT 1"
        ),
        {"c": currency, "d": on_date},
    ).first()
    if not row:
        raise HTTPException(422, "no exchange rate available")
    return row[0]


def _post_entry(c, tenant, body: EntryIn, reversal_of=None):
    for ln in body.lines:
        if (ln.debit == 0) == (ln.credit == 0):
            raise HTTPException(422, "each line needs exactly one of debit or credit")
    total_d = sum((ln.debit for ln in body.lines), Decimal(0))
    total_c = sum((ln.credit for ln in body.lines), Decimal(0))
    if total_d != total_c:
        raise HTTPException(422, "unbalanced journal entry")
    rate = _resolve_rate(c, tenant, body.currency, body.entry_date, body.fx_rate)
    ids = list({ln.account_id for ln in body.lines})
    visible = c.execute(
        text("SELECT count(*) FROM accounts WHERE id = ANY(:ids)"), {"ids": ids}
    ).scalar()
    if visible != len(ids):
        raise HTTPException(422, "unknown account")
    base = []
    for ln in body.lines:
        base.append(
            [
                (ln.debit * rate).quantize(Q, ROUND_HALF_EVEN),
                (ln.credit * rate).quantize(Q, ROUND_HALF_EVEN),
            ]
        )
    # Rounding residue goes on the largest line so base totals stay balanced.
    diff = sum(b[0] for b in base) - sum(b[1] for b in base)
    if diff != 0:
        idx = max(range(len(base)), key=lambda i: base[i][0] + base[i][1])
        if base[idx][0] > 0:
            base[idx][0] -= diff
        else:
            base[idx][1] += diff
    eid = c.execute(
        text(
            "INSERT INTO journal_entries(tenant_id, entry_date, memo, currency, fx_rate, reversal_of) "
            "VALUES (:t, :d, :m, :cur, :r, :rev) RETURNING id"
        ),
        {"t": tenant, "d": body.entry_date, "m": body.memo, "cur": body.currency,
         "r": rate, "rev": reversal_of},
    ).scalar()
    for ln, (bd, bc) in zip(body.lines, base):
        c.execute(
            text(
                "INSERT INTO journal_lines(tenant_id, entry_id, account_id, debit, credit, base_debit, base_credit) "
                "VALUES (:t, :e, :a, :d, :c, :bd, :bc)"
            ),
            {"t": tenant, "e": eid, "a": ln.account_id, "d": ln.debit, "c": ln.credit,
             "bd": bd, "bc": bc},
        )
    return str(eid)


@app.post("/api/v1/journal-entries", status_code=201)
def post_entry(
    body: EntryIn,
    tenant=Depends(current_tenant),
    idempotency_key: Optional[str] = Header(default=None),
):
    try:
        with tenant_conn(tenant) as c:
            if idempotency_key:
                claimed = c.execute(
                    text(
                        "INSERT INTO idempotency_keys(tenant_id, key, response) "
                        "VALUES (:t, :k, '{}') ON CONFLICT DO NOTHING RETURNING key"
                    ),
                    {"t": tenant, "k": idempotency_key},
                ).first()
                if not claimed:
                    stored = c.execute(
                        text("SELECT response FROM idempotency_keys WHERE key = :k"),
                        {"k": idempotency_key},
                    ).scalar()
                    return stored
            result = {"id": _post_entry(c, tenant, body)}
            if idempotency_key:
                c.execute(
                    text("UPDATE idempotency_keys SET response = :r WHERE key = :k"),
                    {"r": json.dumps(result), "k": idempotency_key},
                )
            return result
    except DBAPIError as e:
        _map_db_error(e)


@app.post("/api/v1/journal-entries/{entry_id}/reverse", status_code=201)
def reverse_entry(entry_id: UUID, entry_date: date, tenant=Depends(current_tenant)):
    try:
        with tenant_conn(tenant) as c:
            e = c.execute(
                text("SELECT currency, fx_rate, memo FROM journal_entries WHERE id = :i"),
                {"i": entry_id},
            ).first()
            if not e:
                raise HTTPException(404, "entry not found")
            lines = c.execute(
                text("SELECT account_id, debit, credit FROM journal_lines WHERE entry_id = :i"),
                {"i": entry_id},
            ).all()
            body = EntryIn(
                entry_date=entry_date,
                currency=e.currency,
                fx_rate=e.fx_rate,
                memo=f"reversal of {entry_id}",
                lines=[
                    LineIn(account_id=l.account_id, debit=l.credit, credit=l.debit)
                    for l in lines
                ],
            )
            return {"id": _post_entry(c, tenant, body, reversal_of=entry_id)}
    except DBAPIError as ex:
        _map_db_error(ex)


@app.get("/api/v1/reports/trial-balance")
def trial_balance(tenant=Depends(current_tenant)):
    with tenant_conn(tenant) as c:
        rows = c.execute(
            text(
                "SELECT a.code, a.name, COALESCE(SUM(l.base_debit),0) AS d, "
                "COALESCE(SUM(l.base_credit),0) AS cr "
                "FROM accounts a LEFT JOIN journal_lines l ON l.account_id = a.id "
                "GROUP BY a.code, a.name ORDER BY a.code"
            )
        ).all()
        base = _base_currency(c, tenant)
    td = sum((r.d for r in rows), Decimal(0))
    tc = sum((r.cr for r in rows), Decimal(0))
    return {
        "currency": base,
        "rows": [{"code": r.code, "name": r.name, "debit": str(r.d), "credit": str(r.cr)} for r in rows],
        "total_debit": str(td),
        "total_credit": str(tc),
        "balanced": td == tc,
    }


class CustomerIn(BaseModel):
    name: str
    credit_limit: Optional[Decimal] = Field(default=None, ge=0)
    credit_currency: Optional[str] = None


@app.post("/api/v1/customers", status_code=201)
def create_customer(body: CustomerIn, tenant=Depends(current_tenant)):
    with tenant_conn(tenant) as c:
        cid = c.execute(
            text(
                "INSERT INTO customers(tenant_id, name, credit_limit, credit_currency) "
                "VALUES (:t, :n, :l, :c) RETURNING id"
            ),
            {"t": tenant, "n": body.name, "l": body.credit_limit, "c": body.credit_currency},
        ).scalar()
    return {"id": str(cid)}


@app.get("/api/v1/customers/{customer_id}")
def get_customer(customer_id: UUID, tenant=Depends(current_tenant)):
    with tenant_conn(tenant) as c:
        r = c.execute(
            text("SELECT id, name, credit_limit, credit_currency FROM customers WHERE id = :i"),
            {"i": customer_id},
        ).first()
    if not r:
        raise HTTPException(404, "not found")
    return {"id": str(r.id), "name": r.name,
            "credit_limit": None if r.credit_limit is None else str(r.credit_limit),
            "credit_currency": r.credit_currency}


class ItemIn(BaseModel):
    sku: str
    name: str


@app.post("/api/v1/items", status_code=201)
def create_item(body: ItemIn, tenant=Depends(current_tenant)):
    try:
        with tenant_conn(tenant) as c:
            iid = c.execute(
                text("INSERT INTO items(tenant_id, sku, name) VALUES (:t, :s, :n) RETURNING id"),
                {"t": tenant, "s": body.sku, "n": body.name},
            ).scalar()
    except DBAPIError as e:
        _map_db_error(e)
    return {"id": str(iid)}


class PriceIn(BaseModel):
    list_kind: str
    price: Decimal = Field(ge=0)
    currency: str
    valid_from: date


@app.post("/api/v1/items/{item_id}/prices", status_code=201)
def add_price(item_id: UUID, body: PriceIn, tenant=Depends(current_tenant)):
    try:
        with tenant_conn(tenant) as c:
            if not c.execute(text("SELECT 1 FROM items WHERE id = :i"), {"i": item_id}).first():
                raise HTTPException(404, "item not found")
            c.execute(
                text(
                    "INSERT INTO item_prices(tenant_id, item_id, list_kind, price, currency, valid_from) "
                    "VALUES (:t, :i, :k, :p, :c, :v)"
                ),
                {"t": tenant, "i": item_id, "k": body.list_kind, "p": body.price,
                 "c": body.currency, "v": body.valid_from},
            )
    except DBAPIError as e:
        _map_db_error(e)
    return {"ok": True}


@app.get("/api/v1/items/{item_id}/price")
def price_on(item_id: UUID, kind: str, on: date, tenant=Depends(current_tenant)):
    with tenant_conn(tenant) as c:
        r = c.execute(
            text(
                "SELECT price, currency FROM item_prices WHERE item_id = :i AND list_kind = :k "
                "AND valid_from <= :d ORDER BY valid_from DESC LIMIT 1"
            ),
            {"i": item_id, "k": kind, "d": on},
        ).first()
    if not r:
        raise HTTPException(404, "no price")
    return {"price": str(r.price), "currency": r.currency}


from fastapi.responses import RedirectResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from .ops import router as ops_router  # noqa: E402
from .ops_b import router as ops_b_router  # noqa: E402
from .ops_c import router as ops_c_router  # noqa: E402

app.include_router(ops_router)
app.include_router(ops_b_router)
app.include_router(ops_c_router)
app.mount("/ui", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static"), html=True), name="ui")


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/ui/")
