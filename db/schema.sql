-- CampusFind AI — database schema (PROJECT_GUIDE.md §9.3, adapted for the no-paid-API build)
-- Run the whole file in Supabase Dashboard → SQL Editor → New query → Run.
-- Safe to re-run: every statement uses IF NOT EXISTS / ON CONFLICT / DROP ... IF EXISTS.
--
-- Changes from §9.3:
--   * items.embedding is vector(768)   (Ollama nomic-embed-text instead of OpenAI's 1536)
--   * sequences for report codes L-0001 / F-0001
--   * agent_sessions table             (state for the rule-based fallback engine)
--   * agent_logs.mode column           ('llm' or 'rules' — which engine chose the tool)
--   * the 14 campus locations are inserted at the bottom

create extension if not exists vector;

create table if not exists public.users (
  id uuid primary key references auth.users(id) on delete cascade,
  name text not null,
  email text unique not null,
  role text not null default 'student' check (role in ('student','staff','admin')),
  department text,
  created_at timestamptz default now()
);

-- Auto-create a profile row when someone signs up through Supabase Auth
create or replace function public.handle_new_user()
returns trigger language plpgsql security definer set search_path = '' as $$
begin
  insert into public.users (id, email, name)
  values (new.id, new.email,
          coalesce(new.raw_user_meta_data->>'name', split_part(new.email, '@', 1)))
  on conflict (id) do nothing;
  return new;
end; $$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute procedure public.handle_new_user();

-- Backfill profiles for any Auth users created BEFORE this script ran
insert into public.users (id, email, name)
select u.id, u.email, coalesce(u.raw_user_meta_data->>'name', split_part(u.email, '@', 1))
from auth.users u
on conflict (id) do nothing;

create table if not exists public.locations (
  id serial primary key,
  name text unique not null,
  zone text not null check (zone in ('academic','common','sports','hostel'))
);

create sequence if not exists public.lost_code_seq;
create sequence if not exists public.found_code_seq;

create table if not exists public.items (
  id uuid primary key default gen_random_uuid(),
  code text unique not null,                        -- 'L-0042' / 'F-0017'
  report_type text not null check (report_type in ('lost','found')),
  category text not null,
  item_name text not null,
  brand text,
  color text,
  description text not null check (char_length(description) <= 1000),
  location_id int references public.locations(id),
  location_note text,
  event_date date not null,
  event_time time,
  status text not null default 'open'
    check (status in ('open','matched','verified','returned','cancelled','closed')),
  embedding vector(768),
  created_by uuid not null references public.users(id),
  created_at timestamptz default now()
);
create index if not exists items_type_status_idx
  on public.items (report_type, status, category, event_date);

create table if not exists public.lost_reports (
  item_id uuid primary key references public.items(id) on delete cascade,
  last_seen_location text,
  last_seen_time time
);

create table if not exists public.found_reports (
  item_id uuid primary key references public.items(id) on delete cascade,
  found_location text,
  found_time time,
  current_holding text check (current_holding in ('finder','admin_office','security')),
  verification_hint text                              -- PRIVATE: admins only
);

create table if not exists public.matches (
  id uuid primary key default gen_random_uuid(),
  lost_item_id uuid not null references public.items(id),
  found_item_id uuid not null references public.items(id),
  similarity_score numeric(4,3) not null,
  score_breakdown jsonb not null,
  status text not null default 'candidate'
    check (status in ('candidate','owner_confirmed','verified','rejected','closed')),
  owner_answer text,
  reject_reason text,
  created_at timestamptz default now(),
  unique (lost_item_id, found_item_id)
);

create table if not exists public.notifications (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.users(id),
  type text not null,
  message text not null,
  item_id uuid references public.items(id),
  status text not null default 'unread' check (status in ('unread','read')),
  created_at timestamptz default now()
);

create table if not exists public.admin_actions (
  id uuid primary key default gen_random_uuid(),
  admin_id uuid not null references public.users(id),
  action text not null,               -- verify_match | reject_match | mark_returned
  item_id uuid references public.items(id),
  match_id uuid references public.matches(id),
  note text,
  created_at timestamptz default now()
);

create table if not exists public.chat_messages (
  id bigserial primary key,
  session_id uuid not null,
  user_id uuid not null references public.users(id),
  role text not null check (role in ('user','assistant')),
  content jsonb not null,
  created_at timestamptz default now()
);
create index if not exists chat_messages_session_idx
  on public.chat_messages (session_id, created_at);

-- One row per chat session: which engine runs it and the rule engine's state machine
create table if not exists public.agent_sessions (
  session_id uuid primary key,
  user_id uuid not null references public.users(id),
  mode text not null check (mode in ('llm','rules')),
  state jsonb not null default '{}'::jsonb,
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

create table if not exists public.agent_logs (
  id bigserial primary key,
  session_id uuid,
  mode text,                          -- 'llm' or 'rules'
  tool_name text,
  tool_input jsonb,
  tool_output jsonb,
  latency_ms int,
  error text,
  created_at timestamptz default now()
);
alter table public.agent_logs add column if not exists mode text;

-- The browser never queries tables directly: enable RLS with no policies,
-- so the publishable key can read nothing. The backend connects with DATABASE_URL.
alter table public.users          enable row level security;
alter table public.locations      enable row level security;
alter table public.items          enable row level security;
alter table public.lost_reports   enable row level security;
alter table public.found_reports  enable row level security;
alter table public.matches        enable row level security;
alter table public.notifications  enable row level security;
alter table public.admin_actions  enable row level security;
alter table public.chat_messages  enable row level security;
alter table public.agent_sessions enable row level security;
alter table public.agent_logs     enable row level security;

-- Campus locations (PROJECT_GUIDE.md §22). Replace with your real campus places; keep the zones.
insert into public.locations (name, zone) values
  ('CSE Block','academic'), ('Mech Block','academic'), ('Civil Block','academic'),
  ('E&TC Block','academic'), ('Workshop','academic'), ('Central Library','academic'),
  ('Auditorium','common'), ('Main Canteen','common'), ('Admin Building','common'),
  ('Bus Stop','common'), ('Parking Lot','common'), ('Sports Ground','sports'),
  ('Boys Hostel A','hostel'), ('Girls Hostel B','hostel')
on conflict (name) do nothing;

-- Make someone an admin (run separately, with the real email):
--   update public.users set role = 'admin' where email = 'admin@example.com';
