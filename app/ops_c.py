import json
import secrets
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from .db import tenant_conn
from .main import _map_db_error, current_auth
from .ops import PaymentIn, SaleIn, create_payment, create_sale, need, run
from .webhooks import validate_url

router = APIRouter(prefix="/api/v1")

EVENTS = {"invoice.posted", "stock.changed", "payment.confirmed"}

# ---------- webhooks ----------


class WebhookIn(BaseModel):
    url: str
    events: list[str] = Field(min_length=1)


@router.post("/webhooks", status_code=201)
def create_webhook(body: WebhookIn, auth=Depends(current_auth)):
    need(auth, "webhooks.manage")
    if not set(body.events) <= EVENTS:
        raise HTTPException(422, f"unknown event; allowed: {sorted(EVENTS)}")
    try:
        validate_url(body.url)
    except (ValueError, OSError) as e:
        raise HTTPException(422, str(e))
    secret = secrets.token_urlsafe(32)
    wid = run(auth["tenant"], lambda c: c.execute(
        text("INSERT INTO webhook_endpoints(tenant_id, url, secret, events) VALUES (:t, :u, :s, :e) RETURNING id"),
        {"t": auth["tenant"], "u": body.url, "s": secret, "e": body.events}).scalar())
    return {"id": str(wid), "secret": secret}


@router.get("/webhooks/deliveries")
def deliveries(auth=Depends(current_auth)):
    need(auth, "webhooks.manage")
    return run(auth["tenant"], lambda c: [
        {"id": str(r.id), "event": r.event, "status": r.status, "attempts": r.attempts, "last_error": r.last_error}
        for r in c.execute(text("SELECT id, event, status, attempts, last_error FROM webhook_deliveries "
                                "ORDER BY created_at DESC LIMIT 200"))])


# ---------- offline sync ----------


class Op(BaseModel):
    op_id: str = Field(min_length=1, max_length=100)
    type: str
    payload: dict[str, Any]


class PushIn(BaseModel):
    device_id: str = Field(min_length=1, max_length=100)
    device_name: Optional[str] = None
    ops: list[Op] = Field(min_length=1, max_length=200)


def _apply(c, tenant, auth, op_type, payload):
    if op_type == "sales_invoice":
        need(auth, "invoice.post")
        return create_sale(c, tenant, auth, SaleIn(**payload))
    if op_type == "payment":
        need(auth, "payment.create")
        return create_payment(c, tenant, PaymentIn(**payload))
    raise HTTPException(422, f"unsupported op type {op_type}")


def _try_apply(c, tenant, auth, op_type, payload):
    """Apply one op inside a savepoint so a conflict never rolls back the other ops."""
    try:
        with c.begin_nested():
            return "accepted", _apply(c, tenant, auth, op_type, payload), None
    except HTTPException as e:
        return "conflict", None, f"{e.status_code}: {e.detail}"
    except ValidationError as e:
        return "conflict", None, f"422: invalid payload ({len(e.errors())} errors)"
    except DBAPIError as e:
        try:
            _map_db_error(e)
        except HTTPException as he:
            return "conflict", None, f"{he.status_code}: {he.detail}"


def _touch_device(c, tenant, device_id, name=None):
    c.execute(text("INSERT INTO devices(tenant_id, device_id, name) VALUES (:t, :d, :n) "
                   "ON CONFLICT (tenant_id, device_id) DO UPDATE SET last_seen = now(), "
                   "name = COALESCE(:n, devices.name)"), {"t": tenant, "d": device_id, "n": name})


@router.post("/sync/push")
def sync_push(body: PushIn, auth=Depends(current_auth)):
    tenant = auth["tenant"]

    def f(c):
        _touch_device(c, tenant, body.device_id, body.device_name)
        results = []
        for op in body.ops:
            prev = c.execute(text("SELECT status, result, error FROM sync_inbox WHERE device_id = :d AND op_id = :o"),
                             {"d": body.device_id, "o": op.op_id}).first()
            if prev:  # idempotent replay: never apply the same op twice
                results.append({"op_id": op.op_id, "status": prev.status, "result": prev.result,
                                "error": prev.error, "duplicate": True})
                continue
            status, result, error = _try_apply(c, tenant, auth, op.type, op.payload)
            c.execute(text("INSERT INTO sync_inbox(tenant_id, device_id, op_id, op_type, payload, status, result, error) "
                           "VALUES (:t, :d, :o, :ty, CAST(:p AS jsonb), :s, CAST(:r AS jsonb), :e)"),
                      {"t": tenant, "d": body.device_id, "o": op.op_id, "ty": op.type,
                       "p": json.dumps(op.payload, default=str), "s": status,
                       "r": json.dumps(result) if result is not None else None, "e": error})
            results.append({"op_id": op.op_id, "status": status, "result": result, "error": error})
        return {"server_time": datetime.now(timezone.utc).isoformat(), "results": results}
    return run(tenant, f)


@router.get("/sync/pull")
def sync_pull(device_id: str, auth=Depends(current_auth)):
    """Full master-data snapshot; the cursor is the trusted server time of this snapshot."""
    tenant = auth["tenant"]

    def f(c):
        _touch_device(c, tenant, device_id)
        q = lambda sql: [dict(r._mapping) for r in c.execute(text(sql))]
        data = {
            "customers": q("SELECT id::text, name, credit_limit::text FROM customers"),
            "items": q("SELECT id::text, sku, name FROM items"),
            "warehouses": q("SELECT id::text, name FROM warehouses"),
            "balances": q("SELECT item_id::text, warehouse_id::text, qty::text FROM stock_balances"),
        }
        return {"cursor": datetime.now(timezone.utc).isoformat(), **data}
    return run(tenant, f)


@router.get("/sync/conflicts")
def conflicts(auth=Depends(current_auth)):
    return run(auth["tenant"], lambda c: [
        {"device_id": r.device_id, "op_id": r.op_id, "type": r.op_type, "error": r.error, "payload": r.payload}
        for r in c.execute(text("SELECT device_id, op_id, op_type, error, payload FROM sync_inbox "
                                "WHERE status = 'conflict' ORDER BY received_at"))])


@router.get("/sync/devices")
def devices(auth=Depends(current_auth)):
    return run(auth["tenant"], lambda c: [
        {"device_id": r.device_id, "name": r.name, "last_seen": r.last_seen.isoformat()}
        for r in c.execute(text("SELECT device_id, name, last_seen FROM devices ORDER BY last_seen DESC"))])


class ResolveIn(BaseModel):
    action: str = Field(pattern="^(retry|discard)$")


@router.post("/sync/conflicts/{device_id}/{op_id}/resolve")
def resolve(device_id: str, op_id: str, body: ResolveIn, auth=Depends(current_auth)):
    need(auth, "sync.resolve")
    tenant = auth["tenant"]

    def f(c):
        row = c.execute(text("SELECT op_type, payload FROM sync_inbox WHERE device_id = :d AND op_id = :o "
                             "AND status = 'conflict' FOR UPDATE"), {"d": device_id, "o": op_id}).first()
        if not row:
            raise HTTPException(404, "no open conflict")
        if body.action == "discard":
            c.execute(text("UPDATE sync_inbox SET status = 'resolved', error = 'discarded by operator' "
                           "WHERE device_id = :d AND op_id = :o"), {"d": device_id, "o": op_id})
            return {"status": "resolved"}
        status, result, error = _try_apply(c, tenant, auth, row.op_type, row.payload)
        if status == "conflict":
            raise HTTPException(409, error)
        c.execute(text("UPDATE sync_inbox SET status = 'resolved', result = CAST(:r AS jsonb), error = NULL "
                       "WHERE device_id = :d AND op_id = :o"), {"r": json.dumps(result), "d": device_id, "o": op_id})
        return {"status": "resolved", "result": result}
    return run(tenant, f)
