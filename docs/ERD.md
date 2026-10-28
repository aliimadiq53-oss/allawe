# مخطط ERD - المرحلة 1 (مسودة للمراجعة)

مبني على الصفحة 23 من الملف. كل جدول تشغيلي يحمل `tenant_id`، ويُفرض العزل في الخادم وعلى مستوى قاعدة البيانات (RLS).
المبالغ كلها `NUMERIC(20,4)` وأسعار الصرف `NUMERIC(20,8)`.

```mermaid
erDiagram
  tenants ||--o{ legal_entities : has
  tenants ||--o{ memberships : has
  tenants ||--o{ subscriptions : has
  tenants ||--o{ support_sessions : "temporary access"
  legal_entities ||--o{ branches : has
  branches ||--o{ warehouses : has
  users ||--o{ memberships : joins
  memberships }o--o{ roles : assigned
  roles }o--o{ permissions : grants

  legal_entities ||--o{ accounts : chart
  legal_entities ||--o{ accounting_periods : has
  legal_entities ||--o{ journals : has
  journals ||--o{ journal_entries : contains
  accounting_periods ||--o{ journal_entries : "locks"
  journal_entries ||--|{ journal_lines : "balanced"
  accounts ||--o{ journal_lines : posted_to
  journal_entries ||--o| journal_entries : "reversal_of"

  customers ||--o{ credit_limits : has
  customers ||--o{ settlements : pays
  vendors ||--o{ settlements : paid
  settlements ||--|{ payment_allocations : allocates
  journal_lines ||--o{ payment_allocations : "open item"

  items ||--o{ item_prices : priced
  price_lists ||--o{ item_prices : contains
  items }o--|| units : measured_in
  items ||--o{ stock_moves : moves
  warehouses ||--o{ stock_moves : in_out

  currencies ||--o{ exchange_rates : has
  journal_entries }o--|| currencies : "doc currency"

  users ||--o{ audit_events : actor
  tenants ||--o{ api_clients : has
  tenants ||--o{ idempotency_keys : has

  tenants { uuid id PK
    text name
    text status }
  users { uuid id PK
    text email
    text password_hash }
  memberships { uuid id PK
    uuid tenant_id FK
    uuid user_id FK }
  roles { uuid id PK
    uuid tenant_id FK
    text name }
  permissions { text code PK }
  accounts { uuid id PK
    uuid tenant_id FK
    text code
    text type }
  accounting_periods { uuid id PK
    date start_date
    date end_date
    bool locked }
  journals { uuid id PK
    text kind }
  journal_entries { uuid id PK
    uuid tenant_id FK
    text status "draft|posted"
    date entry_date
    char3 currency
    numeric fx_rate
    uuid reversal_of FK }
  journal_lines { uuid id PK
    uuid entry_id FK
    uuid account_id FK
    numeric debit
    numeric credit
    numeric base_debit
    numeric base_credit }
  currencies { char3 code PK }
  exchange_rates { char3 code
    date rate_date
    numeric rate }
  customers { uuid id PK
    uuid tenant_id FK }
  vendors { uuid id PK
    uuid tenant_id FK }
  credit_limits { uuid id PK
    numeric amount
    char3 currency }
  settlements { uuid id PK
    numeric amount
    numeric fx_gain_loss }
  payment_allocations { uuid id PK
    numeric amount }
  items { uuid id PK
    text sku
    text tracking }
  price_lists { uuid id PK
    text kind "purchase|retail|wholesale|merchant" }
  item_prices { uuid id PK
    numeric price
    date valid_from }
  stock_moves { uuid id PK
    uuid source_doc_id
    numeric qty
    numeric unit_cost }
  audit_events { uuid id PK
    text action
    text reason
    timestamptz at }
  idempotency_keys { text key PK
    uuid tenant_id FK
    text response_hash }
```

## قواعد تُفرض في قاعدة البيانات
- **AC-02:** قيد مرحَّل = مجموع `base_debit` يساوي مجموع `base_credit`، ويُرحَّل داخل معاملة واحدة.
- **عدم التعديل:** منع `UPDATE/DELETE` على القيود المرحَّلة بـ trigger؛ التصحيح بقيد عكسي (`reversal_of`).
- **AC-11:** رفض الترحيل في فترة `locked`.
- **AC-03:** `UNIQUE(tenant_id, key)` على `idempotency_keys`.
- **AC-06:** `item_prices` بتاريخ سريان؛ سطور الفواتير تحفظ السعر الذي استُخدم وقت الإصدار.
- **AC-01:** سياسات RLS على `tenant_id` لكل جدول.
- **المخزون:** حركة مخزون واحدة لكل عملية مرحَّلة، ومنع الرصيد السالب افتراضياً.

## نقاط تحتاج قرارك
1. هل تحتاج عدة كيانات قانونية لكل مستأجر؟ (المسودة تدعمها.)
2. تكلفة المخزون: متوسط مرجَّح أم FIFO؟
3. السياسة الضريبية المحلية وشجرة الحسابات الافتراضية.
4. أسلوب حل تعارضات المزامنة (المرحلة 3).
