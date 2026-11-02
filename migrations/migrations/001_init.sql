-- Movie Recap Studio
-- Database Schema v1
-- PostgreSQL / Supabase

create extension if not exists pgcrypto;

-- =========================================
-- 1. USERS
-- =========================================

create table users (
  id uuid primary key default gen_random_uuid(),
  email text not null,
  password_hash text,
  google_sub text unique,
  display_name text,

  role text not null default 'user'
    check (role in ('user', 'admin')),

  plan text not null default 'free'
    check (plan in ('free', 'paid')),

  plan_expires_at timestamptz,

  daily_project_limit integer
    check (daily_project_limit is null or daily_project_limit >= 1),

  daily_unlimited boolean not null default false,

  paid_total_mmk bigint not null default 0
    check (paid_total_mmk >= 0),

  status text not null default 'active'
    check (status in ('active', 'suspended')),

  created_at timestamptz not null default now(),

  check (password_hash is not null or google_sub is not null),

  -- Paid user must have either a numeric limit or Unlimited.
  check (
    plan <> 'paid'
    or (
      daily_unlimited = true
      and daily_project_limit is null
    )
    or (
      daily_unlimited = false
      and daily_project_limit is not null
    )
  )
);

-- Email is case-insensitive unique.
create unique index users_email_lower_uq
on users (lower(email));


-- =========================================
-- 2. USER API KEYS
-- =========================================

create table user_api_keys (
  id bigserial primary key,

  user_id uuid not null
    references users(id) on delete cascade,

  provider text not null
    check (provider in ('gemini', 'elevenlabs')),

  key_enc text not null,
  last4 text not null,

  updated_at timestamptz not null default now(),

  unique (user_id, provider)
);


-- =========================================
-- 3. ADMIN API KEYS
-- =========================================

create table admin_api_keys (
  id bigserial primary key,

  provider text not null
    check (provider in ('gemini', 'elevenlabs')),

  key_enc text not null,
  last4 text not null,

  is_active boolean not null default true,

  created_by uuid references users(id),

  created_at timestamptz not null default now()
);

create unique index admin_api_keys_one_active
on admin_api_keys(provider)
where is_active = true;


-- =========================================
-- 4. ADMIN PRICE LIST
-- =========================================

create table price_list (
  id bigserial primary key,

  label text not null,

  amount_mmk bigint not null
    check (amount_mmk > 0),

  suggested_days integer
    check (suggested_days is null or suggested_days > 0),

  suggested_daily_limit integer
    check (
      suggested_daily_limit is null
      or suggested_daily_limit >= 1
    ),

  suggested_unlimited boolean not null default false,

  is_active boolean not null default true,

  sort_order integer not null default 0,

  deleted_at timestamptz,

  created_at timestamptz not null default now(),

  check (
    suggested_unlimited = false
    or suggested_daily_limit is null
  )
);


-- =========================================
-- 5. PAYMENT METHODS
-- =========================================

create table payment_methods (
  id bigserial primary key,

  type text not null
    check (type in ('wavepay', 'kbzpay', 'bank')),

  account_name text not null,
  account_number text not null,

  bank_name text,
  note text,

  qr_code_path text,

  is_active boolean not null default true,

  sort_order integer not null default 0,

  deleted_at timestamptz,

  created_at timestamptz not null default now()
);


-- =========================================
-- 6. PAYMENTS
-- =========================================

create table payments (
  id bigserial primary key,

  user_id uuid not null
    references users(id),

  price_item_id bigint
    references price_list(id) on delete set null,

  price_label_snapshot text,

  method_id bigint not null
    references payment_methods(id) on delete restrict,

  method_snapshot text not null,

  amount_mmk bigint not null
    check (amount_mmk > 0),

  txn_id text not null
    check (length(trim(txn_id)) > 0),

  screenshot_path text,

  status text not null default 'pending'
    check (status in ('pending', 'approved', 'rejected')),

  reject_reason text,

  grant_mode text
    check (grant_mode in ('extend', 'reset')),

  granted_days integer
    check (granted_days is null or granted_days > 0),

  granted_daily_limit integer
    check (
      granted_daily_limit is null
      or granted_daily_limit >= 1
    ),

  granted_unlimited boolean,

  admin_note text,

  reviewed_by uuid references users(id),

  reviewed_at timestamptz,

  created_at timestamptz not null default now(),

  check (
    granted_unlimited is null
    or (
      granted_unlimited = true
      and granted_daily_limit is null
    )
    or (
      granted_unlimited = false
      and granted_daily_limit is not null
    )
  ),

  check (
    status <> 'approved'
    or (
      grant_mode is not null
      and granted_days is not null
      and granted_unlimited is not null
      and reviewed_by is not null
      and reviewed_at is not null
    )
  )
);

-- Prevent duplicate transaction IDs for the same payment method.
-- Rejected payments do not block a future submission.
create unique index payments_txn_uq
on payments (
  method_id,
  lower(trim(txn_id))
)
where status <> 'rejected';

create index payments_status_idx
on payments(status, created_at);


-- =========================================
-- 7. PROJECTS
-- =========================================

create table projects (
  id bigserial primary key,

  user_id uuid not null
    references users(id) on delete cascade,

  title text,
  source_type text,
  source_ref text,

  language text,
  style text,

  script_text text,

  key_source text not null
    check (key_source in ('user', 'admin')),

  mm_date date not null,

  created_at timestamptz not null default now()
);

create index projects_user_idx
on projects(user_id, created_at desc);


-- =========================================
-- 8. DAILY USAGE
-- =========================================

create table usage_daily (
  user_id uuid not null
    references users(id) on delete cascade,

  mm_date date not null,

  projects_count integer not null default 0
    check (projects_count >= 0),

  primary key (user_id, mm_date)
);


-- =========================================
-- 9. FEATURES
-- =========================================

create table features (
  key text primary key,
  label text not null
);

create table feature_flags (
  feature_key text not null
    references features(key) on delete cascade,

  tier text not null
    check (tier in ('free', 'paid')),

  enabled boolean not null default true,

  primary key (feature_key, tier)
);

-- Initial feature list.
insert into features(key, label) values
  ('script', 'AI Movie Recap / Script Generator'),
  ('tts', 'Text to Speech / MP3'),
  ('auto_subtitle', 'AI Auto Subtitle'),
  ('auto_dub', 'AI Auto Dub'),
  ('mp4_export', 'MP4 Video Export'),
  ('blur_editor', 'Blur Editor'),
  ('voice_clone', 'Voice Cloning'),
  ('video_upload', 'Video Upload'),
  ('myanmar_voice', 'Myanmar Voice'),
  ('gemini_api', 'Gemini API'),
  ('elevenlabs_api', 'ElevenLabs API'),
  ('edge_tts', 'Edge TTS');

-- Enable all features initially for both tiers.
insert into feature_flags(feature_key, tier, enabled)
select
  f.key,
  t.tier,
  true
from features f
cross join (
  values ('free'), ('paid')
) as t(tier);


-- =========================================
-- 10. VOICE CLONES
-- =========================================

create table voice_clones (
  id bigserial primary key,

  user_id uuid not null
    references users(id) on delete cascade,

  name text not null,

  elevenlabs_voice_id text,

  consent_confirmed_at timestamptz not null,

  consent_version text not null,

  status text not null default 'active'
    check (status in ('active', 'revoked')),

  created_at timestamptz not null default now()
);


-- =========================================
-- 11. ENTITLEMENT LOG
-- =========================================

create table entitlement_log (
  id bigserial primary key,

  user_id uuid not null
    references users(id),

  payment_id bigint
    references payments(id),

  actor_id uuid
    references users(id),

  source text not null
    check (
      source in (
        'payment',
        'manual',
        'revoke',
        'adjust'
      )
    ),

  old_plan text,
  new_plan text,

  old_expires timestamptz,
  new_expires timestamptz,

  old_limit integer,
  new_limit integer,

  old_unlimited boolean,
  new_unlimited boolean,

  reason text not null,

  created_at timestamptz not null default now()
);


-- =========================================
-- 12. AUDIT LOG
-- =========================================

create table audit_log (
  id bigserial primary key,

  actor_id uuid references users(id),

  action text not null,

  target text,

  meta jsonb,

  created_at timestamptz not null default now()
);


-- =========================================
-- 13. APP SETTINGS
-- =========================================

create table app_settings (
  key text primary key,
  value text not null
);

insert into app_settings(key, value)
values ('free_daily_limit', '2');


-- =========================================
-- END OF SCHEMA
-- =========================================
