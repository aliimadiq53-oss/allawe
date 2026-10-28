ALTER TABLE tenants ADD COLUMN IF NOT EXISTS costing text NOT NULL DEFAULT 'avg' CHECK (costing IN ('avg','fifo'));
ALTER TABLE payments ADD COLUMN IF NOT EXISTS cash_base numeric(20,4);
ALTER TABLE payments ADD COLUMN IF NOT EXISTS invoice_id uuid REFERENCES sales_invoices(id);

CREATE TABLE IF NOT EXISTS stock_layers (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  item_id uuid NOT NULL REFERENCES items(id),
  warehouse_id uuid NOT NULL REFERENCES warehouses(id),
  qty_remaining numeric(20,4) NOT NULL CHECK (qty_remaining >= 0),
  unit_cost numeric(20,6) NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS sales_returns (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  invoice_id uuid NOT NULL REFERENCES sales_invoices(id),
  customer_id uuid NOT NULL REFERENCES customers(id),
  return_date date NOT NULL,
  total numeric(20,4) NOT NULL,
  total_base numeric(20,4) NOT NULL,
  entry_id uuid REFERENCES journal_entries(id),
  cogs_entry_id uuid REFERENCES journal_entries(id),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sales_return_lines (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  return_id uuid NOT NULL REFERENCES sales_returns(id),
  item_id uuid NOT NULL REFERENCES items(id),
  qty numeric(20,4) NOT NULL CHECK (qty > 0),
  unit_price numeric(20,4) NOT NULL,
  unit_cost numeric(20,6) NOT NULL
);

CREATE TABLE IF NOT EXISTS stock_counts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  warehouse_id uuid NOT NULL REFERENCES warehouses(id),
  status text NOT NULL DEFAULT 'pending_approval',
  created_at timestamptz NOT NULL DEFAULT now(),
  approved_at timestamptz
);

CREATE TABLE IF NOT EXISTS stock_count_lines (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  count_id uuid NOT NULL REFERENCES stock_counts(id),
  item_id uuid NOT NULL REFERENCES items(id),
  counted_qty numeric(20,4) NOT NULL CHECK (counted_qty >= 0)
);

CREATE TABLE IF NOT EXISTS restaurant_tables (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  name text NOT NULL,
  UNIQUE (tenant_id, name)
);

CREATE TABLE IF NOT EXISTS dining_orders (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  table_id uuid NOT NULL REFERENCES restaurant_tables(id),
  status text NOT NULL DEFAULT 'open' CHECK (status IN ('open','paid','merged','cancelled')),
  invoice_id uuid REFERENCES sales_invoices(id),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS kitchen_tickets (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  order_id uuid NOT NULL REFERENCES dining_orders(id),
  status text NOT NULL DEFAULT 'new' CHECK (status IN ('new','preparing','ready','served')),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS dining_order_lines (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  order_id uuid NOT NULL REFERENCES dining_orders(id),
  item_id uuid NOT NULL REFERENCES items(id),
  qty numeric(20,4) NOT NULL CHECK (qty > 0),
  note text,
  ticket_id uuid REFERENCES kitchen_tickets(id)
);

CREATE TABLE IF NOT EXISTS print_templates (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  name text NOT NULL,
  paper text NOT NULL CHECK (paper IN ('a4','a5','thermal58','thermal80','custom')),
  width_mm numeric(8,2),
  height_mm numeric(8,2),
  header text NOT NULL DEFAULT '',
  footer text NOT NULL DEFAULT '',
  show_cost boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS print_history (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  template_id uuid NOT NULL REFERENCES print_templates(id),
  doc_type text NOT NULL,
  doc_id uuid NOT NULL,
  is_reprint boolean NOT NULL,
  printed_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS webhook_endpoints (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  url text NOT NULL,
  secret text NOT NULL,
  events text[] NOT NULL,
  active boolean NOT NULL DEFAULT true
);

CREATE TABLE IF NOT EXISTS webhook_deliveries (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  endpoint_id uuid NOT NULL REFERENCES webhook_endpoints(id),
  event text NOT NULL,
  payload jsonb NOT NULL,
  status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','sent','failed')),
  attempts int NOT NULL DEFAULT 0,
  next_attempt_at timestamptz NOT NULL DEFAULT now(),
  last_error text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS devices (
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  device_id text NOT NULL,
  name text,
  last_seen timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, device_id)
);

CREATE TABLE IF NOT EXISTS sync_inbox (
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  device_id text NOT NULL,
  op_id text NOT NULL,
  op_type text NOT NULL,
  payload jsonb NOT NULL,
  status text NOT NULL CHECK (status IN ('accepted','conflict','resolved')),
  result jsonb,
  error text,
  received_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, device_id, op_id)
);

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['stock_layers','sales_returns','sales_return_lines','stock_counts','stock_count_lines',
    'restaurant_tables','dining_orders','kitchen_tickets','dining_order_lines','print_templates','print_history',
    'webhook_endpoints','webhook_deliveries','devices','sync_inbox'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS tenant_iso ON %I', t);
    EXECUTE format('CREATE POLICY tenant_iso ON %I USING (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid) WITH CHECK (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid)', t);
    EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON %I TO app_user', t);
  END LOOP;
END $$;
