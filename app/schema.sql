CREATE EXTENSION IF NOT EXISTS pgcrypto;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
    CREATE ROLE app_user NOLOGIN;
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS tenants (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL,
  base_currency char(3) NOT NULL DEFAULT 'IQD',
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS api_keys (
  key_hash text PRIMARY KEY,
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  scopes text[] NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS accounts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  code text NOT NULL,
  name text NOT NULL,
  type text NOT NULL CHECK (type IN ('asset','liability','equity','income','expense')),
  UNIQUE (tenant_id, code)
);

CREATE TABLE IF NOT EXISTS accounting_periods (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  start_date date NOT NULL,
  end_date date NOT NULL,
  locked boolean NOT NULL DEFAULT false
);

CREATE TABLE IF NOT EXISTS exchange_rates (
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  currency char(3) NOT NULL,
  rate_date date NOT NULL,
  rate numeric(20,8) NOT NULL CHECK (rate > 0),
  PRIMARY KEY (tenant_id, currency, rate_date)
);

CREATE TABLE IF NOT EXISTS journal_entries (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  entry_date date NOT NULL,
  memo text,
  currency char(3) NOT NULL,
  fx_rate numeric(20,8) NOT NULL CHECK (fx_rate > 0),
  status text NOT NULL DEFAULT 'posted' CHECK (status = 'posted'),
  reversal_of uuid REFERENCES journal_entries(id),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS journal_lines (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  entry_id uuid NOT NULL REFERENCES journal_entries(id),
  account_id uuid NOT NULL REFERENCES accounts(id),
  debit numeric(20,4) NOT NULL DEFAULT 0 CHECK (debit >= 0),
  credit numeric(20,4) NOT NULL DEFAULT 0 CHECK (credit >= 0),
  base_debit numeric(20,4) NOT NULL DEFAULT 0,
  base_credit numeric(20,4) NOT NULL DEFAULT 0,
  CHECK ((debit = 0) <> (credit = 0))
);

CREATE TABLE IF NOT EXISTS customers (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  name text NOT NULL,
  credit_limit numeric(20,4),
  credit_currency char(3)
);

CREATE TABLE IF NOT EXISTS items (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  sku text NOT NULL,
  name text NOT NULL,
  UNIQUE (tenant_id, sku)
);

CREATE TABLE IF NOT EXISTS item_prices (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  item_id uuid NOT NULL REFERENCES items(id),
  list_kind text NOT NULL CHECK (list_kind IN ('purchase','retail','wholesale','merchant')),
  price numeric(20,4) NOT NULL CHECK (price >= 0),
  currency char(3) NOT NULL,
  valid_from date NOT NULL
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  key text NOT NULL,
  response jsonb NOT NULL,
  PRIMARY KEY (tenant_id, key)
);

CREATE TABLE IF NOT EXISTS audit_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  action text NOT NULL,
  detail jsonb,
  at timestamptz NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION trg_block_mutation() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'posted ledger rows are immutable; use a reversing entry';
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS entries_immutable ON journal_entries;
CREATE TRIGGER entries_immutable BEFORE UPDATE OR DELETE ON journal_entries
  FOR EACH ROW EXECUTE FUNCTION trg_block_mutation();
DROP TRIGGER IF EXISTS lines_immutable ON journal_lines;
CREATE TRIGGER lines_immutable BEFORE UPDATE OR DELETE ON journal_lines
  FOR EACH ROW EXECUTE FUNCTION trg_block_mutation();

CREATE OR REPLACE FUNCTION trg_period_lock() RETURNS trigger AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM accounting_periods p
             WHERE p.tenant_id = NEW.tenant_id AND p.locked
               AND NEW.entry_date BETWEEN p.start_date AND p.end_date) THEN
    RAISE EXCEPTION 'accounting period is locked';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS entries_period_lock ON journal_entries;
CREATE TRIGGER entries_period_lock BEFORE INSERT ON journal_entries
  FOR EACH ROW EXECUTE FUNCTION trg_period_lock();

CREATE OR REPLACE FUNCTION trg_check_balance() RETURNS trigger AS $$
DECLARE d numeric; c numeric;
BEGIN
  SELECT COALESCE(SUM(base_debit),0), COALESCE(SUM(base_credit),0) INTO d, c
  FROM journal_lines WHERE entry_id = NEW.entry_id;
  IF d <> c THEN RAISE EXCEPTION 'unbalanced journal entry: % vs %', d, c; END IF;
  RETURN NULL;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS lines_balanced ON journal_lines;
CREATE CONSTRAINT TRIGGER lines_balanced AFTER INSERT ON journal_lines
  DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW EXECUTE FUNCTION trg_check_balance();

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['accounts','accounting_periods','exchange_rates','journal_entries',
    'journal_lines','customers','items','item_prices','idempotency_keys','audit_events'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS tenant_iso ON %I', t);
    EXECUTE format('CREATE POLICY tenant_iso ON %I USING (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid) WITH CHECK (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid)', t);
    EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON %I TO app_user', t);
  END LOOP;
END $$;
GRANT SELECT ON tenants TO app_user;
