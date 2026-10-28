import os
from contextlib import contextmanager

from sqlalchemy import create_engine, text

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg://ledger:ledger@localhost:5433/ledger"
)
engine = create_engine(DATABASE_URL, pool_pre_ping=True)


def init_schema():
    with engine.begin() as conn:
        conn.exec_driver_sql("SELECT pg_advisory_xact_lock(424242)")
        for name in ("schema.sql", "schema2.sql", "schema3.sql"):
            path = os.path.join(os.path.dirname(__file__), name)
            with open(path, encoding="utf-8") as f:
                conn.exec_driver_sql(f.read().replace("%", "%%"))


@contextmanager
def admin_conn():
    with engine.begin() as conn:
        yield conn


@contextmanager
def tenant_conn(tenant_id):
    """Transaction restricted by RLS to a single tenant."""
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL ROLE app_user"))
        conn.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)}
        )
        yield conn
