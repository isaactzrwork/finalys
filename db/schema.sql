-- ============================================================
-- Acacia — Supabase (Postgres) schema
-- Current final state — run top to bottom on a fresh project.
-- ============================================================

-- Fixed category list, with an optional per-category monthly budget
create table categories (
    id serial primary key,
    name text not null unique,
    monthly_budget numeric(12,2),
    created_at timestamptz not null default now()
);

-- Cards / payment methods, with miles-program expiry-rule metadata
create table payment_methods (
    id serial primary key,
    label text not null unique,
    card_last4 text,
    miles_program text,
    expiry_rule_type text check (expiry_rule_type in ('no_expiry', 'rolling_quarterly', 'rolling_monthly', 'account_cycle')),
    expiry_rule_params jsonb,
    expiry_rule_source_url text,
    expiry_rule_verified_at date,
    created_at timestamptz not null default now()
);

-- One row per logged transaction (photo, voice, or text — not statement PDFs)
create table transactions (
    id bigserial primary key,
    merchant text not null,
    amount numeric(12,2) not null,
    currency text not null default 'SGD',
    transaction_date date not null,
    category_id integer not null references categories(id),
    payment_method_id integer not null references payment_methods(id),
    source_type text not null check (source_type in ('photo', 'voice', 'text')),
    raw_input text,
    created_at timestamptz not null default now()
);

-- One row per statement cycle's miles/points earned, per card
create table miles_ledger (
    id bigserial primary key,
    payment_method_id integer not null references payment_methods(id),
    statement_period_start date not null,
    statement_period_end date not null,
    miles_earned numeric(12,2) not null,
    balance numeric(12,2),
    bank_flagged_expiring_amount numeric(12,2),
    bank_flagged_expiry_date date,
    extracted_from text,
    created_at timestamptz not null default now()
);

-- One row per calendar month's budget decision
create table budget_periods (
    id serial primary key,
    effective_month date not null unique,
    mode text not null check (mode in ('per_category', 'overall', 'none')),
    overall_amount numeric(12,2),
    status text not null check (status in ('confirmed', 'pending_confirmation')),
    decided_at timestamptz,
    created_at timestamptz not null default now()
);

-- Per-category budget amounts — only populated when mode = 'per_category'
create table budget_category_amounts (
    id serial primary key,
    budget_period_id integer not null references budget_periods(id),
    category_id integer not null references categories(id),
    amount numeric(12,2) not null,
    unique (budget_period_id, category_id)
);

-- Recurring-charge detection memory (confirmed subscriptions + declined false-positives)
create table recurring_charges (
    id serial primary key,
    merchant text not null,
    reference_amount numeric(12,2) not null,
    status text not null check (status in ('confirmed', 'declined')),
    frequency text check (frequency in ('monthly', 'quarterly', 'semi_annual', 'yearly', 'custom')),
    custom_frequency_note text,
    category_id integer references categories(id),
    last_seen_date date,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

-- ============================================================
-- Seed data
-- ============================================================

insert into categories (name) values
    ('Food & Dining'), ('Groceries'), ('Transport'), ('Shopping'),
    ('Bills & Subscriptions'), ('Entertainment'), ('Health & Wellness'),
    ('Travel'), ('ting<3'), ('Others');

insert into payment_methods (label) values
    ('Cash/Paynow/Paylah etc.'),
    ('DBS World Woman''s Master Card'),
    ('DBS Altitude Card'),
    ('Citi Rewards Master Card'),
    ('Citi Premier Miles Card'),
    ('HSBC Travel 1 Card');

-- Miles program + expiry rules, verified against each bank's T&Cs on 2026-08-01.
-- card_last4 is only pre-filled for cards whose statements have actually been tested —
-- others get filled in (or auto-matched) as you upload each card's first statement.
-- NOTE: 'XXXX' below is a placeholder — replace with your own card's real last-4 digits
-- (never commit real card numbers to a public repo).
update payment_methods set miles_program='DBS Points', expiry_rule_type='no_expiry',
    expiry_rule_params='{}'::jsonb, card_last4='XXXX',
    expiry_rule_source_url='https://www.dbs.com.sg/personal/support/card-rewards-dbs-points-expiry.html',
    expiry_rule_verified_at='2026-08-01'
where label = 'DBS Altitude Card';

update payment_methods set miles_program='DBS Points', expiry_rule_type='rolling_quarterly',
    expiry_rule_params='{"years_after_quarter_end": 1}'::jsonb, card_last4='XXXX',
    expiry_rule_source_url='https://www.dbs.com.sg/personal/support/card-rewards-dbs-points-expiry.html',
    expiry_rule_verified_at='2026-08-01'
where label = 'DBS World Woman''s Master Card';

update payment_methods set miles_program='Citi ThankYou Points', expiry_rule_type='account_cycle',
    expiry_rule_params='{"cycle_months": 60, "grace_months": 3, "anchor_date": "2025-08-01"}'::jsonb,
    expiry_rule_source_url='https://www.citibank.com.sg/pdf/citi-thankyou-rewards-tncs.pdf',
    expiry_rule_verified_at='2026-08-01'
where label = 'Citi Rewards Master Card';

update payment_methods set miles_program='Citi Miles', expiry_rule_type='no_expiry',
    expiry_rule_params='{}'::jsonb,
    expiry_rule_source_url='https://www.you.co/sg/blog/citi-premiermiles-card/',
    expiry_rule_verified_at='2026-08-01'
where label = 'Citi Premier Miles Card';

update payment_methods set miles_program='HSBC Reward Points', expiry_rule_type='rolling_monthly',
    expiry_rule_params='{"start_offset_months": 1, "duration_months": 37}'::jsonb,
    expiry_rule_source_url='https://www.hsbc.com.sg/content/dam/hsbc/sg/documents/credit-cards/travelone/travelone-rewards-programme-2023-faq.pdf',
    expiry_rule_verified_at='2026-08-01'
where label = 'HSBC Travel 1 Card';
