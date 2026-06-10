create table if not exists public.master_results (
  id bigint primary key,
  timestamp text,
  sport text,
  min_trades integer,
  limit_count integer,
  total_holders integer,
  successful_metrics integer,
  failed_metrics integer,
  nba_markets_json jsonb,
  top_holders_json jsonb,
  metrics_summary_json jsonb,
  created_at timestamptz default now()
);

create table if not exists public.address_metrics (
  address text primary key,
  roi double precision,
  max_drawdown double precision,
  sharpe double precision,
  total_pnl double precision,
  realized_pnl double precision,
  unrealized_pnl double precision,
  profit_factor double precision,
  current_position_value_usd double precision,
  total_trades bigint,
  winning_trades bigint,
  losing_trades bigint,
  win_rate double precision,
  avg_trade_price double precision,
  realized_edge_score double precision,
  avg_open_top5_depth_usd double precision,
  avg_open_settlement_days double precision,
  ct_score_total_100 double precision,
  ct_score_roi double precision,
  ct_score_pf double precision,
  ct_score_mdd double precision,
  ct_score_sharpe double precision,
  ct_score_ui double precision,
  ct_score_r2 double precision,
  copytrade_value_score double precision,
  copytrade_value_level text,
  copytrade_value_exclusion_reason text,
  copytrade_value_score_version text,
  confidence text,
  source_tags text,
  details_json jsonb,
  snapshot_utc timestamptz,
  updated_at timestamptz,
  created_at timestamptz default now()
);

-- copytrade: 按 leader 地址归因汇总
create table if not exists public.copytrade_leader_summary (
  leader_address text not null,
  account_name text not null default 'default',
  total_realized_pnl double precision not null default 0,
  total_unrealized_pnl double precision not null default 0,
  total_pnl double precision not null default 0,
  winning_markets bigint not null default 0,
  losing_markets bigint not null default 0,
  total_markets bigint not null default 0,
  win_rate double precision,
  updated_at timestamptz,
  created_at timestamptz default now(),
  primary key (leader_address, account_name)
);

create table if not exists public.copytrade_leader_market_pnl (
  leader_address text not null,
  condition_id text not null,
  account_name text not null default 'default',
  market_slug text,
  total_realized_pnl double precision not null default 0,
  total_unrealized_pnl double precision not null default 0,
  total_pnl double precision not null default 0,
  market_result text not null default 'flat',
  updated_at timestamptz,
  created_at timestamptz default now(),
  primary key (leader_address, condition_id, account_name)
);

create index if not exists idx_address_metrics_address on public.address_metrics(address);
create index if not exists idx_address_metrics_total_pnl on public.address_metrics(total_pnl desc nulls last);
create index if not exists idx_address_metrics_updated_at on public.address_metrics(updated_at desc nulls last);
create index if not exists idx_copytrade_leader_summary_total_pnl on public.copytrade_leader_summary(total_pnl desc nulls last);
create index if not exists idx_copytrade_leader_market_leader on public.copytrade_leader_market_pnl(leader_address);
create index if not exists idx_copytrade_leader_market_total_pnl on public.copytrade_leader_market_pnl(total_pnl desc nulls last);

alter table public.address_metrics add column if not exists current_position_value_usd double precision;
alter table public.address_metrics add column if not exists total_trades bigint;
alter table public.address_metrics add column if not exists winning_trades bigint;
alter table public.address_metrics add column if not exists losing_trades bigint;
alter table public.address_metrics add column if not exists win_rate double precision;
alter table public.address_metrics add column if not exists avg_trade_price double precision;
alter table public.address_metrics drop column if exists brier_weighted;
alter table public.address_metrics add column if not exists realized_edge_score double precision;
alter table public.address_metrics drop column if exists skew_unweighted;
alter table public.address_metrics drop column if exists skew_weighted;
alter table public.address_metrics add column if not exists source_tags text;
alter table public.address_metrics add column if not exists ulcer_index double precision;
alter table public.address_metrics add column if not exists equity_r2 double precision;
alter table public.address_metrics add column if not exists avg_open_top5_depth_usd double precision;
alter table public.address_metrics add column if not exists avg_open_settlement_days double precision;
alter table public.address_metrics add column if not exists ct_score_total_100 double precision;
alter table public.address_metrics add column if not exists ct_score_roi double precision;
alter table public.address_metrics add column if not exists ct_score_pf double precision;
alter table public.address_metrics add column if not exists ct_score_mdd double precision;
alter table public.address_metrics add column if not exists ct_score_sharpe double precision;
alter table public.address_metrics add column if not exists ct_score_ui double precision;
alter table public.address_metrics add column if not exists ct_score_r2 double precision;
alter table public.address_metrics add column if not exists copytrade_value_score double precision;
alter table public.address_metrics add column if not exists copytrade_value_level text;
alter table public.address_metrics add column if not exists copytrade_value_exclusion_reason text;
alter table public.address_metrics add column if not exists copytrade_value_score_version text;

alter table public.master_results enable row level security;
alter table public.address_metrics enable row level security;
alter table public.copytrade_leader_summary enable row level security;
alter table public.copytrade_leader_market_pnl enable row level security;

do $$
begin
  if not exists (
    select 1 from pg_policies where schemaname='public' and tablename='master_results' and policyname='public_read_master_results'
  ) then
    create policy public_read_master_results on public.master_results for select using (true);
  end if;
  if not exists (
    select 1 from pg_policies where schemaname='public' and tablename='address_metrics' and policyname='public_read_address_metrics'
  ) then
    create policy public_read_address_metrics on public.address_metrics for select using (true);
  end if;
  if not exists (
    select 1 from pg_policies where schemaname='public' and tablename='copytrade_leader_summary' and policyname='public_read_copytrade_leader_summary'
  ) then
    create policy public_read_copytrade_leader_summary on public.copytrade_leader_summary for select using (true);
  end if;
  if not exists (
    select 1 from pg_policies where schemaname='public' and tablename='copytrade_leader_market_pnl' and policyname='public_read_copytrade_leader_market_pnl'
  ) then
    create policy public_read_copytrade_leader_market_pnl on public.copytrade_leader_market_pnl for select using (true);
  end if;
end $$;

grant select on public.master_results to anon, authenticated;
grant select on public.address_metrics to anon, authenticated;
grant select on public.copytrade_leader_summary to anon, authenticated;
grant select on public.copytrade_leader_market_pnl to anon, authenticated;

-- copytrade: 每日净值快照
create table if not exists public.copytrade_daily_equity (
  date_key text primary key,
  total_equity double precision not null default 0,
  total_realized_pnl double precision not null default 0,
  total_unrealized_pnl double precision not null default 0,
  total_cost_basis double precision not null default 0,
  open_position_count bigint not null default 0,
  updated_at timestamptz,
  created_at timestamptz default now()
);

-- copytrade: leader 每日盈亏
create table if not exists public.copytrade_daily_leader_pnl (
  date_key text not null,
  leader_address text not null,
  account_name text not null default 'default',
  realized_pnl double precision not null default 0,
  unrealized_pnl double precision not null default 0,
  total_pnl double precision not null default 0,
  market_count bigint not null default 0,
  updated_at timestamptz,
  created_at timestamptz default now(),
  primary key (date_key, leader_address, account_name)
);

create table if not exists public.copytrade_daily_leader_market_leg_pnl (
  date_key text not null,
  leader_address text not null,
  account_name text not null default 'default',
  condition_id text not null,
  token_id text not null,
  market_slug text,
  outcome text,
  buy_fill_count bigint not null default 0,
  buy_size double precision not null default 0,
  buy_cost_usd double precision not null default 0,
  sell_fill_count bigint not null default 0,
  sell_size double precision not null default 0,
  sell_proceeds_usd double precision not null default 0,
  settled_size double precision not null default 0,
  open_size_eod double precision not null default 0,
  close_state_eod text not null default 'open',
  realized_pnl_delta double precision not null default 0,
  unrealized_pnl_delta double precision not null default 0,
  total_pnl_delta double precision not null default 0,
  realized_pnl_eod double precision not null default 0,
  unrealized_pnl_eod double precision not null default 0,
  total_pnl_eod double precision not null default 0,
  updated_at timestamptz,
  created_at timestamptz default now(),
  primary key (date_key, leader_address, account_name, condition_id, token_id)
);

create index if not exists idx_copytrade_daily_equity_date on public.copytrade_daily_equity(date_key);
create index if not exists idx_copytrade_daily_leader_pnl_date on public.copytrade_daily_leader_pnl(date_key);
create index if not exists idx_copytrade_daily_leader_pnl_leader on public.copytrade_daily_leader_pnl(leader_address);
create index if not exists idx_copytrade_daily_leader_market_leg_pnl_date on public.copytrade_daily_leader_market_leg_pnl(date_key);
create index if not exists idx_copytrade_daily_leader_market_leg_pnl_leader on public.copytrade_daily_leader_market_leg_pnl(account_name, leader_address, date_key);
create index if not exists idx_copytrade_daily_leader_market_leg_pnl_market on public.copytrade_daily_leader_market_leg_pnl(account_name, date_key, condition_id);

alter table public.copytrade_daily_equity enable row level security;
alter table public.copytrade_daily_leader_pnl enable row level security;
alter table public.copytrade_daily_leader_market_leg_pnl enable row level security;

do $$
begin
  if not exists (
    select 1 from pg_policies where schemaname='public' and tablename='copytrade_daily_equity' and policyname='public_read_copytrade_daily_equity'
  ) then
    create policy public_read_copytrade_daily_equity on public.copytrade_daily_equity for select using (true);
  end if;
  if not exists (
    select 1 from pg_policies where schemaname='public' and tablename='copytrade_daily_leader_pnl' and policyname='public_read_copytrade_daily_leader_pnl'
  ) then
    create policy public_read_copytrade_daily_leader_pnl on public.copytrade_daily_leader_pnl for select using (true);
  end if;
  if not exists (
    select 1 from pg_policies where schemaname='public' and tablename='copytrade_daily_leader_market_leg_pnl' and policyname='public_read_copytrade_daily_leader_market_leg_pnl'
  ) then
    create policy public_read_copytrade_daily_leader_market_leg_pnl on public.copytrade_daily_leader_market_leg_pnl for select using (true);
  end if;
end $$;

grant select on public.copytrade_daily_equity to anon, authenticated;
grant select on public.copytrade_daily_leader_pnl to anon, authenticated;
grant select on public.copytrade_daily_leader_market_leg_pnl to anon, authenticated;

create table if not exists public.copytrade_compare_daily_summary (
  date_key text not null,
  account_name text not null default 'default',
  leader_address text not null,
  leader_total_pnl double precision not null default 0,
  our_total_pnl double precision not null default 0,
  delta_pnl double precision not null default 0,
  leader_excluded_pnl double precision not null default 0,
  our_excluded_pnl double precision not null default 0,
  visible_leader_pnl double precision not null default 0,
  visible_our_pnl double precision not null default 0,
  updated_at timestamptz,
  created_at timestamptz default now(),
  primary key (date_key, account_name, leader_address)
);

create table if not exists public.copytrade_compare_daily_market_leg (
  date_key text not null,
  account_name text not null default 'default',
  leader_address text not null,
  condition_id text not null,
  token_id text not null,
  market_slug text,
  outcome text,
  exclusion_reason text,
  leader_buy_fill_count bigint not null default 0,
  leader_buy_usd double precision not null default 0,
  leader_buy_avg_price double precision,
  leader_sell_fill_count bigint not null default 0,
  leader_sell_usd double precision not null default 0,
  leader_sell_avg_price double precision,
  leader_realized_pnl double precision not null default 0,
  leader_unrealized_change double precision not null default 0,
  leader_total_pnl double precision not null default 0,
  our_buy_fill_count bigint not null default 0,
  our_buy_usd double precision not null default 0,
  our_buy_avg_price double precision,
  our_sell_fill_count bigint not null default 0,
  our_sell_usd double precision not null default 0,
  our_sell_avg_price double precision,
  our_realized_pnl double precision not null default 0,
  our_unrealized_change double precision not null default 0,
  our_total_pnl double precision not null default 0,
  primary_gap_reason text not null default 'none',
  updated_at timestamptz,
  created_at timestamptz default now(),
  primary key (date_key, account_name, leader_address, condition_id, token_id)
);

create index if not exists idx_copytrade_compare_daily_summary_lookup
  on public.copytrade_compare_daily_summary(account_name, date_key);
create index if not exists idx_copytrade_compare_daily_market_leg_lookup
  on public.copytrade_compare_daily_market_leg(account_name, date_key, leader_address);
create index if not exists idx_copytrade_compare_daily_market_leg_market
  on public.copytrade_compare_daily_market_leg(account_name, date_key, condition_id);

alter table public.copytrade_compare_daily_summary enable row level security;
alter table public.copytrade_compare_daily_market_leg enable row level security;

do $$
begin
  if not exists (
    select 1 from pg_policies where schemaname='public' and tablename='copytrade_compare_daily_summary' and policyname='public_read_copytrade_compare_daily_summary'
  ) then
    create policy public_read_copytrade_compare_daily_summary on public.copytrade_compare_daily_summary for select using (true);
  end if;
  if not exists (
    select 1 from pg_policies where schemaname='public' and tablename='copytrade_compare_daily_market_leg' and policyname='public_read_copytrade_compare_daily_market_leg'
  ) then
    create policy public_read_copytrade_compare_daily_market_leg on public.copytrade_compare_daily_market_leg for select using (true);
  end if;
end $$;

grant select on public.copytrade_compare_daily_summary to anon, authenticated;
grant select on public.copytrade_compare_daily_market_leg to anon, authenticated;

-- address_tags: 地址标签，当前为公开可编辑
create table if not exists public.address_tags (
  address text primary key,
  tag text not null check (tag in ('顶尖', '高手', '特殊策略', '待观察', '排除')),
  updated_by text,
  updated_at timestamptz default now()
);

alter table public.address_tags drop constraint if exists address_tags_tag_check;
alter table public.address_tags
  add constraint address_tags_tag_check
  check (tag in ('顶尖', '高手', '特殊策略', '待观察', '排除'));

alter table public.address_tags enable row level security;

drop policy if exists admin_write_address_tags on public.address_tags;
drop policy if exists public_write_address_tags on public.address_tags;

do $$
begin
  if not exists (
    select 1 from pg_policies where schemaname='public' and tablename='address_tags' and policyname='public_read_address_tags'
  ) then
    create policy public_read_address_tags on public.address_tags for select using (true);
  end if;
  if not exists (
    select 1 from pg_policies where schemaname='public' and tablename='address_tags' and policyname='public_write_address_tags'
  ) then
    create policy public_write_address_tags on public.address_tags
      for all using (true)
      with check (true);
  end if;
end $$;

grant select on public.address_tags to anon, authenticated;
grant insert, update, delete on public.address_tags to anon, authenticated;

-- ============================================================================
-- Migration: 添加 account_name 到 copytrade 归因表（已有数据库需手动执行）
-- ============================================================================
-- 1. copytrade_leader_summary
alter table public.copytrade_leader_summary add column if not exists account_name text not null default 'default';
alter table public.copytrade_leader_summary drop constraint if exists copytrade_leader_summary_pkey;
alter table public.copytrade_leader_summary add primary key (leader_address, account_name);

-- 2. copytrade_leader_market_pnl
alter table public.copytrade_leader_market_pnl add column if not exists account_name text not null default 'default';
alter table public.copytrade_leader_market_pnl drop constraint if exists copytrade_leader_market_pnl_pkey;
alter table public.copytrade_leader_market_pnl add primary key (leader_address, condition_id, account_name);

-- 3. copytrade_daily_leader_pnl
alter table public.copytrade_daily_leader_pnl add column if not exists account_name text not null default 'default';
alter table public.copytrade_daily_leader_pnl drop constraint if exists copytrade_daily_leader_pnl_pkey;
alter table public.copytrade_daily_leader_pnl add primary key (date_key, leader_address, account_name);
