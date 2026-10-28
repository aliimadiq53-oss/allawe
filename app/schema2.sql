CREATE TABLE IF NOT EXISTS vendors (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  name text NOT NULL
);

CREATE TABLE IF NOT EXISTS warehouses (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  name text NOT NULL
);

-- Weighted-average cost in tenant base currency; CHECK blocks negative stock.
CREATE TABLE IF NOT EXISTS stock_balances (
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  item_id uuid NOT NULL REFERENCES items(id),
  warehouse_id uuid NOT NULL REFERENCES warehouses(id),
  qty numeric(20,4) NOT NULL DEFAULT 0 CHECK (qty >= 0),
  avg_cost numeric(20,6) NOT NULL DEFAULT 0,
  PRIMARY KEY (item_id, warehouse_id)
);

CREATE TABLE IF NOT EXISTS stock_moves (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  item_id uuid NOT NULL REFERENCES items(id),
  warehouse_id uuid NOT NULL REFERENCES warehouses(id),
  qty numeric(20,4) NOT NULL,
  unit_cost numeric(20,6) NOT NULL,
  doc_type text NOT NULL,
  doc_id uuid NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sales_invoices (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  customer_id uuid NOT NULL REFERENCES customers(id),
  warehouse_id uuid NOT NULL REFERENCES warehouses(id),
  invoice_date date NOT NULL,
  currency char(3) NOT NULL,
  fx_rate numeric(20,8) NOT NULL,
  total numeric(20,4) NOT NULL,
  total_base numeric(20,4) NOT NULL,
  status text NOT NULL DEFAULT 'posted',
  external_id text,
  shift_id uuid,
  entry_id uuid REFERENCES journal_entries(id),
  cogs_entry_id uuid REFERENCES journal_entries(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, external_id)
);

CREATE TABLE IF NOT EXISTS sales_invoice_lines (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  invoice_id uuid NOT NULL REFERENCES sales_invoices(id),
  item_id uuid NOT NULL REFERENCES items(id),
  qty numeric(20,4) NOT NULL CHECK (qty > 0),
  unit_price numeric(20,4) NOT NULL,
  unit_cost numeric(20,6) NOT NULL
);

CREATE TABLE IF NOT EXISTS purchase_invoices (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  vendor_id uuid NOT NULL REFERENCES vendors(id),
  warehouse_id uuid NOT NULL REFERENCES warehouses(id),
  invoice_date date NOT NULL,
  currency char(3) NOT NULL,
  fx_rate numeric(20,8) NOT NULL,
  total numeric(20,4) NOT NULL,
  total_base numeric(20,4) NOT NULL,
  entry_id uuid REFERENCES journal_entries(id),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS payments (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  party_type text NOT NULL CHECK (party_type IN ('customer','vendor')),
  party_id uuid NOT NULL,
  pay_date date NOT NULL,
  currency char(3) NOT NULL,
  fx_rate numeric(20,8) NOT NULL,
  amount numeric(20,4) NOT NULL CHECK (amount > 0),
  amount_base numeric(20,4) NOT NULL,
  shift_id uuid,
  entry_id uuid REFERENCES journal_entries(id),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pos_shifts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  warehouse_id uuid NOT NULL REFERENCES warehouses(id),
  opening_cash numeric(20,4) NOT NULL DEFAULT 0,
  expected_cash numeric(20,4),
  counted_cash numeric(20,4),
  difference numeric(20,4),
  status text NOT NULL DEFAULT 'open',
  opened_at timestamptz NOT NULL DEFAULT now(),
  closed_at timestamptz
);

DROP TRIGGER IF EXISTS moves_immutable ON stock_moves;
CREATE TRIGGER moves_immutable BEFORE UPDATE OR DELETE ON stock_moves
  FOR EACH ROW EXECUTE FUNCTION trg_block_mutation();

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['vendors','warehouses','stock_balances','stock_moves','sales_invoices',
    'sales_invoice_lines','purchase_invoices','payments','pos_shifts'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS tenant_iso ON %I', t);
    EXECUTE format('CREATE POLICY tenant_iso ON %I USING (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid) WITH CHECK (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid)', t);
    EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON %I TO app_user', t);
  END LOOP;
END $$;
