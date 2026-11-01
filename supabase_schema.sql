-- Run this once in Supabase: SQL Editor -> New query -> paste -> Run

create table if not exists public.cards (
  id            bigint generated always as identity primary key,
  created_at    timestamptz not null default now(),

  -- Telegram's update_id is unique, so a retried update can never mail twice
  update_id     bigint not null unique,
  chat_id       bigint,
  added_by      text,

  -- details read from the card
  name          text,
  designation   text,
  company       text,
  email         text,
  phone         text,
  website       text,
  address       text,

  -- processing | mail_sent | no_email_found | duplicate_skipped | error
  status        text not null default 'processing',
  error         text,
  mail_sent_at  timestamptz,
  sheet_synced  boolean not null default false
);

create index if not exists cards_created_at_idx on public.cards (created_at desc);
create index if not exists cards_email_idx on public.cards (lower(email));

-- Lock the table: no public access. Only the service_role key (used by the
-- Vercel function, never in a browser) can read or write.
alter table public.cards enable row level security;
