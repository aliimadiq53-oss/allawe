# Accounting Platform

Multi-tenant accounting/commercial platform based on `Accounting_Platform_Master_Blueprint_AR.pdf`.
Backend: FastAPI + PostgreSQL (RLS, triggers). UI: static Arabic RTL SPA in `app/static`.

## Run
```
docker compose up -d appdb
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000   # UI: /ui/  API docs: /docs
.venv/bin/python -m pytest -q
```
Environment: `DATABASE_URL`, `PLATFORM_KEY` (defaults are for development only; change before deploying).

Create a tenant: `POST /api/v1/admin/tenants` with header `X-Platform-Key`; the response contains the tenant's API key.

## Not implemented yet
Multi-entity accounting, FX revaluation at period close, Windows local printing agent, LAN branch server
with incremental sync (sync is a full snapshot + push with conflict queue), in-transit transfers,
segregation of duties beyond API-key scopes.

Implemented: returns, FX gain/loss on settlement, stock counts, FIFO/average costing, restaurant,
print templates, webhooks (HMAC, retries, SSRF guard), rate limiting, offline sync push with conflicts.
