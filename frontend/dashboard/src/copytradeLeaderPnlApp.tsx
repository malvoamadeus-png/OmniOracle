import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Bar, BarChart, CartesianGrid, Cell, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { supabase, supabaseConfig } from "./supabaseClient";

type LeaderSummary = {
  leader_address: string;
  account_name: string | null;
  total_realized_pnl: number | null;
  total_unrealized_pnl: number | null;
  total_pnl: number | null;
  winning_markets: number | null;
  losing_markets: number | null;
  total_markets: number | null;
  win_rate: number | null;
  updated_at: string | null;
};

type DailyLeaderPnl = {
  date_key: string;
  leader_address: string;
  account_name: string | null;
  realized_pnl: number | null;
  unrealized_pnl: number | null;
  total_pnl: number | null;
  market_count: number | null;
};

type DailyLeaderMarketLegPnl = {
  date_key: string;
  leader_address: string;
  account_name: string | null;
  condition_id: string;
  token_id: string;
  market_slug: string | null;
  outcome: string | null;
  buy_fill_count: number | null;
  buy_size: number | null;
  buy_cost_usd: number | null;
  sell_fill_count: number | null;
  sell_size: number | null;
  sell_proceeds_usd: number | null;
  settled_size: number | null;
  open_size_eod: number | null;
  close_state_eod: string | null;
  realized_pnl_delta: number | null;
  unrealized_pnl_delta: number | null;
  total_pnl_delta: number | null;
  realized_pnl_eod: number | null;
  unrealized_pnl_eod: number | null;
  total_pnl_eod: number | null;
};

type LegCumulativeStats = {
  buyFillCount: number;
  buySize: number;
  buyCostUsd: number;
  sellFillCount: number;
  sellSize: number;
  sellProceedsUsd: number;
  settledSize: number;
};

type LegCumulativeSourceRow = Pick<
  DailyLeaderMarketLegPnl,
  | "condition_id"
  | "token_id"
  | "buy_fill_count"
  | "buy_size"
  | "buy_cost_usd"
  | "sell_fill_count"
  | "sell_size"
  | "sell_proceeds_usd"
  | "settled_size"
>;

type DrilldownSelection = {
  accountName: string;
  leaderAddress: string;
  dateKey: string;
};

type PnlCurvePoint = { t: number; p: number };

type AddressCurveRow = {
  address: string;
  daily: Map<string, number>;
  latestCum: number;
};

type DailyPnlDisplayRow = {
  key: string;
  label: string;
  leaderAddress: string | null;
  daily: Map<string, number>;
  latestCum: number;
  isSelf?: boolean;
};

const PNL_CURVE_URL = "https://user-pnl-api.polymarket.com/user-pnl";

const ACCOUNT_PNL_ADDRESS_FALLBACK: Record<string, string> = {
  main: "0x5f39d698c8b1f2efadb1042a3c6085e82ae3d603",
  "pm-1": "0x17360267181cbc47300119871bbf04bef33374dd",
  "pm-2": "0x98cf229448e993e9a9c3b8ed34a5d5b221f6a088",
};
const DRILLDOWN_MARKET_MIN_ABS_PNL = 5;
const DRILLDOWN_SIGNAL_OR_FILTER = [
  "buy_fill_count.gt.0",
  "sell_fill_count.gt.0",
  "settled_size.gt.0",
  "settled_size.lt.0",
  "realized_pnl_delta.gt.0",
  "realized_pnl_delta.lt.0",
  "unrealized_pnl_delta.gt.0",
  "unrealized_pnl_delta.lt.0",
  "total_pnl_delta.gt.0",
  "total_pnl_delta.lt.0",
].join(",");
const DRILLDOWN_TOKEN_CHUNK_SIZE = 100;

const pnlColor = (v: number) => (v >= 0 ? "#1f7a1f" : "#b02a2a");
const shortAddr = (a: string) => (a.length > 12 ? `${a.slice(0, 6)}...${a.slice(-4)}` : a);
const cellR = { padding: "6px 8px", textAlign: "right" as const, borderBottom: "1px solid #f5f5f5", fontSize: 11 };

function fmtNum(v: number | null | undefined, _digits = 0): string {
  if (typeof v !== "number" || Number.isNaN(v)) return "-";
  const rounded = Math.round(v);
  const safe = Object.is(rounded, -0) ? 0 : rounded;
  return safe.toLocaleString(undefined, { maximumFractionDigits: 0 });
}

function fmtDec(v: number | null | undefined, digits = 2): string {
  if (typeof v !== "number" || Number.isNaN(v)) return "-";
  const safe = Object.is(v, -0) ? 0 : v;
  return safe.toLocaleString(undefined, {
    minimumFractionDigits: 0,
    maximumFractionDigits: digits,
  });
}

function isNonZero(v: number | null | undefined): boolean {
  return typeof v === "number" && Number.isFinite(v) && Math.abs(v) > 1e-9;
}

function closeStateLabel(v: string | null | undefined): string {
  if (v === "redeemable") return "待赎回";
  if (v === "settled") return "到期/赎回（含 merge 无法区分）";
  if (v === "sold") return "卖出";
  if (v === "mixed") return "部分卖出 / 部分到期或持有";
  if (v === "flat") return "已平/无当日动作";
  return "持有中";
}

function hasDailySignal(row: DailyLeaderMarketLegPnl): boolean {
  return (
    (row.buy_fill_count ?? 0) > 0 ||
    (row.sell_fill_count ?? 0) > 0 ||
    Math.abs(row.settled_size ?? 0) > 1e-9 ||
    Math.abs(row.realized_pnl_delta ?? 0) > 1e-9 ||
    Math.abs(row.unrealized_pnl_delta ?? 0) > 1e-9 ||
    Math.abs(row.total_pnl_delta ?? 0) > 1e-9
  );
}

function legKey(conditionId: string, tokenId: string): string {
  return `${conditionId}::${tokenId}`;
}

function n(v: number | null | undefined): number {
  return typeof v === "number" && Number.isFinite(v) ? v : 0;
}

function chunkArray<T>(items: T[], size: number): T[][] {
  if (size <= 0) return [items];
  const out: T[][] = [];
  for (let idx = 0; idx < items.length; idx += size) {
    out.push(items.slice(idx, idx + size));
  }
  return out;
}

function buildLegCumulativeStats(rows: LegCumulativeSourceRow[]): Record<string, LegCumulativeStats> {
  const out: Record<string, LegCumulativeStats> = {};
  for (const row of rows) {
    const key = legKey(row.condition_id, row.token_id);
    if (!out[key]) {
      out[key] = {
        buyFillCount: 0,
        buySize: 0,
        buyCostUsd: 0,
        sellFillCount: 0,
        sellSize: 0,
        sellProceedsUsd: 0,
        settledSize: 0,
      };
    }
    out[key].buyFillCount += n(row.buy_fill_count);
    out[key].buySize += n(row.buy_size);
    out[key].buyCostUsd += n(row.buy_cost_usd);
    out[key].sellFillCount += n(row.sell_fill_count);
    out[key].sellSize += n(row.sell_size);
    out[key].sellProceedsUsd += n(row.sell_proceeds_usd);
    out[key].settledSize += n(row.settled_size);
  }
  return out;
}

async function fetchPagedRows<T>(
  buildPage: (from: number, to: number) => PromiseLike<{ data: T[] | null; error: { message?: string } | null }>
): Promise<T[]> {
  const rows: T[] = [];
  const pageSize = 1000;
  for (let page = 0; page < 500; page += 1) {
    const from = page * pageSize;
    const to = from + pageSize - 1;
    const res = await buildPage(from, to);
    if (res.error) throw new Error(res.error.message || "Failed to fetch paged rows");
    const chunk = res.data ?? [];
    rows.push(...chunk);
    if (chunk.length < pageSize) break;
  }
  return rows;
}

function buildAccountEnvSuffixes(accountName: string): string[] {
  const base = accountName.trim().toUpperCase();
  if (!base) return [];
  return Array.from(
    new Set(
      [
        base,
        base.replace(/[^A-Z0-9]/g, "_"),
        base.replace(/[^A-Z0-9]/g, "-"),
        base.replace(/-/g, "_"),
        base.replace(/_/g, "-"),
        base.replace(/[^A-Z0-9]/g, ""),
      ]
        .map((s) => s.trim())
        .filter(Boolean)
    )
  );
}

function normalizeAddress(v: string | null | undefined): string | null {
  if (!v) return null;
  const s = String(v).trim().toLowerCase();
  return /^0x[0-9a-f]{40}$/.test(s) ? s : null;
}

function parseDateKeyUTC(key: string): Date | null {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(key)) return null;
  const d = new Date(`${key}T00:00:00Z`);
  if (Number.isNaN(d.getTime())) return null;
  return d;
}

function fmtDateKeyUTC(d: Date): string {
  return d.toISOString().slice(0, 10);
}

function buildRecentDateWindow(data: DailyLeaderPnl[], days = 14): string[] {
  const keys = Array.from(
    new Set(
      data
        .map((r) => String(r.date_key || "").trim())
        .filter((k) => /^\d{4}-\d{2}-\d{2}$/.test(k))
    )
  ).sort();
  if (!keys.length) return [];
  const maxKeyDate = parseDateKeyUTC(keys[keys.length - 1]);
  const today = new Date();
  const todayUtc = new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate()));
  const end = maxKeyDate && maxKeyDate.getTime() < todayUtc.getTime() ? maxKeyDate : todayUtc;
  if (!end) return [];
  const out: string[] = [];
  for (let i = days - 1; i >= 0; i -= 1) {
    const d = new Date(end.getTime());
    d.setUTCDate(d.getUTCDate() - i);
    out.push(fmtDateKeyUTC(d));
  }
  return out;
}

function resolveAccountAddress(accountName: string): string | null {
  const env = import.meta.env as Record<string, string | undefined>;
  const normalizedName = accountName.trim();
  if (!normalizedName) return null;

  const suffixes = buildAccountEnvSuffixes(normalizedName);
  for (const suffix of suffixes) {
    const fromDedicatedVar = normalizeAddress(env[`VITE_COPYTRADE_ACCOUNT_ADDRESS_${suffix}`]);
    if (fromDedicatedVar) return fromDedicatedVar;
  }
  for (const suffix of suffixes) {
    const fromFunderVar = normalizeAddress(env[`VITE_FUNDER_ADDRESS_${suffix}`]);
    if (fromFunderVar) return fromFunderVar;
  }

  const jsonMapRaw = env.VITE_COPYTRADE_ACCOUNT_ADDRESS_MAP;
  if (jsonMapRaw) {
    try {
      const m = JSON.parse(jsonMapRaw) as Record<string, unknown>;
      const fromMap = normalizeAddress(
        String(
          m[normalizedName] ??
          m[normalizedName.toLowerCase()] ??
          m[normalizedName.toUpperCase()] ??
          ""
        )
      );
      if (fromMap) return fromMap;
    } catch {
      // ignore malformed json
    }
  }
  return normalizeAddress(ACCOUNT_PNL_ADDRESS_FALLBACK[normalizedName.toLowerCase()]);
}

async function fetchAllRows<T>(table: string, selectCols: string, orderBy: string, ascending = false): Promise<T[]> {
  if (!supabase) return [];
  const pageSize = 1000;
  const out: T[] = [];
  for (let page = 0; page < 100; page += 1) {
    const from = page * pageSize;
    const to = from + pageSize - 1;
    const res = await supabase.from(table).select(selectCols).order(orderBy, { ascending }).range(from, to);
    if (res.error) throw new Error(res.error.message);
    const chunk = (res.data ?? []) as T[];
    out.push(...chunk);
    if (chunk.length < pageSize) break;
  }
  return out;
}

async function fetchPnlCurve(address: string): Promise<PnlCurvePoint[]> {
  const res = await fetch(`${PNL_CURVE_URL}?user_address=${address}&interval=all&fidelity=12h`);
  if (!res.ok) return [];
  const data: unknown = await res.json();
  if (!Array.isArray(data)) return [];
  return data
    .filter((r: any) => typeof r?.t === "number" && typeof r?.p === "number")
    .map((r: any) => ({ t: r.t as number, p: r.p as number }));
}

function toDailyDeltaMap(points: PnlCurvePoint[]): { daily: Map<string, number>; latestCum: number } {
  const byDay = new Map<string, number>();
  for (const pt of points) {
    const d = new Date(pt.t * 1000);
    const key = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
    byDay.set(key, pt.p);
  }
  const allDays = Array.from(byDay.keys()).sort();
  const daily = new Map<string, number>();
  let prev = 0;
  for (const day of allDays) {
    const cum = byDay.get(day) ?? 0;
    daily.set(day, cum - prev);
    prev = cum;
  }
  return { daily, latestCum: allDays.length ? byDay.get(allDays[allDays.length - 1]) ?? 0 : 0 };
}

function DailyPnlTable(
  {
    rows,
    leaderRankByAddr,
  }: {
    rows: DailyPnlDisplayRow[];
    leaderRankByAddr: Record<string, number>;
  }
) {
  if (!rows.length) return <div style={{ color: "#888", fontSize: 12, padding: 8 }}>暂无净值数据</div>;
  const allDays = Array.from(new Set(rows.flatMap((r) => Array.from(r.daily.keys())))).sort();
  const recentDays = allDays.slice(-14);
  if (!recentDays.length) return <div style={{ color: "#888", fontSize: 12, padding: 8 }}>暂无净值数据</div>;

  return (
    <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
      <thead>
        <tr style={{ background: "#fafafa" }}>
          <th style={{ textAlign: "right", padding: "6px 8px", borderBottom: "1px solid #eee", whiteSpace: "nowrap" }}>序号</th>
          <th style={{ textAlign: "left", padding: "6px 8px", borderBottom: "1px solid #eee" }}>地址</th>
          {recentDays.map((d) => (
            <th key={d} style={{ textAlign: "right", padding: "6px 8px", borderBottom: "1px solid #eee", whiteSpace: "nowrap" }}>{d.slice(5)}</th>
          ))}
          <th style={{ textAlign: "right", padding: "6px 8px", borderBottom: "1px solid #eee", fontWeight: 700 }}>14日合计</th>
          <th style={{ textAlign: "right", padding: "6px 8px", borderBottom: "1px solid #eee", fontWeight: 700 }}>累计PnL</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => {
          const totalDelta = recentDays.reduce((s, d) => s + (r.daily.get(d) ?? 0), 0);
          const addr = (r.leaderAddress || "").toLowerCase();
          const rank = r.isSelf ? "-" : (leaderRankByAddr[addr] ?? "-");
          return (
            <tr key={r.key} style={r.isSelf ? { background: "#fafafa" } : undefined}>
              <td style={{ ...cellR, fontWeight: r.isSelf ? 700 : 400 }}>{rank}</td>
              <td style={{ padding: "6px 8px", borderBottom: "1px solid #f5f5f5", fontWeight: r.isSelf ? 700 : 500 }} title={r.leaderAddress ?? undefined}>
                {r.leaderAddress ? (
                  <a href={`https://polymarket.com/profile/${r.leaderAddress}`} target="_blank" rel="noreferrer" style={{ color: "#1a4fff", textDecoration: "none", fontSize: 11 }}>
                    {r.label}
                  </a>
                ) : r.label}
              </td>
              {recentDays.map((d) => {
                const v = r.daily.get(d) ?? 0;
                return (
                  <td key={d} style={{ ...cellR, color: pnlColor(v), fontWeight: r.isSelf ? 700 : 400 }}>
                    {v >= 0 ? "+" : ""}{fmtNum(v, 2)}
                  </td>
                );
              })}
              <td style={{ ...cellR, fontWeight: 700, color: pnlColor(totalDelta) }}>{totalDelta >= 0 ? "+" : ""}{fmtNum(totalDelta, 2)}</td>
              <td style={{ ...cellR, fontWeight: 700, color: pnlColor(r.latestCum) }}>{fmtNum(r.latestCum, 2)}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function DailyLeaderPnlTable(
  {
    data,
    dates,
    totalByLeader,
    leaderOrder,
    leaderRankByAddr,
    selectedLeaderAddress,
    selectedDrilldown,
    onSelectLeader,
    onSelectDrilldown,
  }: {
    data: DailyLeaderPnl[];
    dates: string[];
    totalByLeader: Record<string, number>;
    leaderOrder: string[];
    leaderRankByAddr: Record<string, number>;
    selectedLeaderAddress: string | null;
    selectedDrilldown: DrilldownSelection | null;
    onSelectLeader: (addr: string) => void;
    onSelectDrilldown: (selection: DrilldownSelection) => void;
  }
) {
  if (!data.length) return <div style={{ color: "#888", fontSize: 12, padding: 8 }}>暂无 Leader 每日盈亏数据</div>;
  if (!dates.length) return <div style={{ color: "#888", fontSize: 12, padding: 8 }}>暂无 Leader 每日盈亏数据</div>;

  const dateSet = new Set(dates);
  const dailyMap = new Map<string, Map<string, number>>();
  const accountByLeader = new Map<string, string>();
  for (const r of data) {
    if (!dateSet.has(r.date_key)) continue;
    const addr = (r.leader_address || "").toLowerCase();
    if (!addr) continue;
    if (!accountByLeader.has(addr)) accountByLeader.set(addr, r.account_name || "default");
    if (!dailyMap.has(addr)) dailyMap.set(addr, new Map());
    dailyMap.get(addr)!.set(r.date_key, r.total_pnl ?? 0);
  }

  let leaders = leaderOrder.filter((addr) =>
    dailyMap.has(addr) || Object.prototype.hasOwnProperty.call(totalByLeader, addr)
  );
  if (!leaders.length) leaders = Array.from(dailyMap.keys());

  const columnTotals = dates.map((d) => leaders.reduce((s, addr) => s + (dailyMap.get(addr)?.get(d) ?? 0), 0));
  const summary14DayTotal = columnTotals.reduce((s, v) => s + v, 0);
  const summaryTotal = leaders.reduce((s, addr) => {
    const row = dailyMap.get(addr) ?? new Map<string, number>();
    const rangeTotal = dates.reduce((acc, d) => acc + (row.get(d) ?? 0), 0);
    const hasTotal = Object.prototype.hasOwnProperty.call(totalByLeader, addr);
    const total = hasTotal ? totalByLeader[addr] : rangeTotal;
    return s + total;
  }, 0);
  const summaryPreviousTotal = summaryTotal - summary14DayTotal;

  return (
    <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
      <thead>
        <tr style={{ background: "#fafafa" }}>
          <th style={{ textAlign: "center", padding: "6px 8px", borderBottom: "1px solid #eee", whiteSpace: "nowrap" }}>选择</th>
          <th style={{ textAlign: "right", padding: "6px 8px", borderBottom: "1px solid #eee", whiteSpace: "nowrap" }}>序号</th>
          <th style={{ textAlign: "left", padding: "6px 8px", borderBottom: "1px solid #eee", position: "sticky", left: 0, background: "#fafafa", zIndex: 1 }}>Leader</th>
          {dates.map((d) => (
            <th key={d} style={{ textAlign: "right", padding: "6px 8px", borderBottom: "1px solid #eee", whiteSpace: "nowrap" }}>{d.slice(5)}</th>
          ))}
          <th style={{ textAlign: "right", padding: "6px 8px", borderBottom: "1px solid #eee", fontWeight: 700 }}>14天盈亏</th>
          <th style={{ textAlign: "right", padding: "6px 8px", borderBottom: "1px solid #eee", fontWeight: 700 }}>此前盈亏</th>
          <th style={{ textAlign: "right", padding: "6px 8px", borderBottom: "1px solid #eee", fontWeight: 700 }}>总盈亏</th>
        </tr>
      </thead>
      <tbody>
        {leaders.map((addr) => {
          const row = dailyMap.get(addr) ?? new Map<string, number>();
          const rangeTotal = dates.reduce((s, d) => s + (row.get(d) ?? 0), 0);
          const hasTotal = Object.prototype.hasOwnProperty.call(totalByLeader, addr);
          const total = hasTotal ? totalByLeader[addr] : rangeTotal;
          const previousTotal = total - rangeTotal;
          const selected = selectedLeaderAddress === addr;
          return (
            <tr key={addr} style={selected ? { background: "#f5f8ff" } : undefined}>
              <td style={{ ...cellR, textAlign: "center" }}>
                <input
                  type="radio"
                  name="leader-daily-select"
                  checked={selected}
                  onChange={() => onSelectLeader(addr)}
                />
              </td>
              <td style={cellR}>{leaderRankByAddr[addr] ?? "-"}</td>
              <td style={{ padding: "6px 8px", borderBottom: "1px solid #f5f5f5", position: "sticky", left: 0, background: "#fff", zIndex: 1 }} title={addr}>
                <a href={`https://polymarket.com/profile/${addr}`} target="_blank" rel="noreferrer" style={{ color: "#1a4fff", textDecoration: "none", fontSize: 11 }}>{shortAddr(addr)}</a>
              </td>
              {dates.map((d) => {
                const v = row.get(d) ?? 0;
                const accountName = accountByLeader.get(addr) || "default";
                const activeCell =
                  selectedDrilldown?.leaderAddress === addr &&
                  selectedDrilldown?.accountName === accountName &&
                  selectedDrilldown?.dateKey === d;
                if (!isNonZero(v)) {
                  return <td key={d} style={{ ...cellR, color: pnlColor(v) }}>-</td>;
                }
                return (
                  <td
                    key={d}
                    style={{
                      ...cellR,
                      color: pnlColor(v),
                      background: activeCell ? "#eef3ff" : undefined,
                    }}
                  >
                    <button
                      type="button"
                      onClick={() =>
                        onSelectDrilldown({
                          accountName,
                          leaderAddress: addr,
                          dateKey: d,
                        })
                      }
                      style={{
                        border: "none",
                        background: "transparent",
                        padding: 0,
                        margin: 0,
                        color: pnlColor(v),
                        cursor: "pointer",
                        font: "inherit",
                        textDecoration: "underline",
                      }}
                    >
                      {fmtNum(v, 2)}
                    </button>
                  </td>
                );
              })}
              <td style={{ ...cellR, fontWeight: 700, color: pnlColor(rangeTotal) }}>{fmtNum(rangeTotal, 2)}</td>
              <td style={{ ...cellR, fontWeight: 700, color: pnlColor(previousTotal) }}>{fmtNum(previousTotal, 2)}</td>
              <td style={{ ...cellR, fontWeight: 700, color: pnlColor(total) }}>{fmtNum(total, 2)}</td>
            </tr>
          );
        })}
        <tr style={{ background: "#fafafa" }}>
          <td style={{ ...cellR, borderTop: "1px solid #eee", fontWeight: 700, textAlign: "center" }}>-</td>
          <td style={{ ...cellR, borderTop: "1px solid #eee", fontWeight: 700 }}>-</td>
          <td style={{ padding: "6px 8px", borderTop: "1px solid #eee", fontWeight: 700, position: "sticky", left: 0, zIndex: 1 }}>区间合计</td>
          {columnTotals.map((v, idx) => (
            <td key={idx} style={{ ...cellR, borderTop: "1px solid #eee", fontWeight: 700, color: pnlColor(v) }}>{v === 0 ? "-" : fmtNum(v, 2)}</td>
          ))}
          <td style={{ ...cellR, borderTop: "1px solid #eee", fontWeight: 700, color: pnlColor(summary14DayTotal) }}>{fmtNum(summary14DayTotal, 2)}</td>
          <td style={{ ...cellR, borderTop: "1px solid #eee", fontWeight: 700, color: pnlColor(summaryPreviousTotal) }}>{fmtNum(summaryPreviousTotal, 2)}</td>
          <td style={{ ...cellR, borderTop: "1px solid #eee", fontWeight: 700, color: pnlColor(summaryTotal) }}>{fmtNum(summaryTotal, 2)}</td>
        </tr>
      </tbody>
    </table>
  );
}

function LeaderDailyBarSection({
  leaderAddress,
  dates,
  valuesByDate,
}: {
  leaderAddress: string | null;
  dates: string[];
  valuesByDate: Map<string, number>;
}) {
  if (!leaderAddress) {
    return <div style={{ color: "#888", fontSize: 12, padding: 8 }}>请先在上方表格选择一个 Leader</div>;
  }
  if (!dates.length) {
    return <div style={{ color: "#888", fontSize: 12, padding: 8 }}>暂无可展示的近14天数据</div>;
  }
  const chartData = dates.map((d) => ({ date: d.slice(5), pnl: valuesByDate.get(d) ?? 0 }));
  const total = chartData.reduce((s, r) => s + r.pnl, 0);

  return (
    <div>
      <div style={{ fontSize: 12, color: "#555", marginBottom: 8 }}>
        选中 Leader: <a href={`https://polymarket.com/profile/${leaderAddress}`} target="_blank" rel="noreferrer" style={{ color: "#1a4fff", textDecoration: "none" }}>{shortAddr(leaderAddress)}</a>
        <span style={{ marginLeft: 12, fontWeight: 700, color: pnlColor(total) }}>14天合计: {fmtNum(total, 2)}</span>
      </div>
      <div style={{ width: "100%", height: 280 }}>
        <ResponsiveContainer>
          <BarChart data={chartData} margin={{ top: 8, right: 16, left: 6, bottom: 8 }}>
            <CartesianGrid strokeDasharray="3 3" />
            <XAxis dataKey="date" tick={{ fontSize: 11 }} />
            <YAxis tick={{ fontSize: 11 }} />
            <Tooltip
              formatter={(value: any) => fmtNum(typeof value === "number" ? value : Number(value ?? 0), 2)}
              labelFormatter={(label: any) => `日期: ${String(label ?? "")}`}
            />
            <ReferenceLine y={0} stroke="#999" />
            <Bar dataKey="pnl">
              {chartData.map((entry, idx) => (
                <Cell key={`${entry.date}-${idx}`} fill={entry.pnl >= 0 ? "#1f7a1f" : "#b02a2a"} />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}

function DailyLeaderDrilldownPanel({
  selection,
  rows,
  cumulativeByLeg,
  loading,
  error,
}: {
  selection: DrilldownSelection | null;
  rows: DailyLeaderMarketLegPnl[];
  cumulativeByLeg: Record<string, LegCumulativeStats>;
  loading: boolean;
  error: string | null;
}) {
  if (!selection) {
    return <div style={{ color: "#888", fontSize: 12, padding: 8 }}>点击上方某个 Leader 的单日利润后，这里会展示市场级归因明细。</div>;
  }
  if (loading) {
    return <div style={{ color: "#888", fontSize: 12, padding: 8 }}>正在加载该日市场级归因明细...</div>;
  }
  if (error) {
    return <div style={{ color: "#7a1b1b", fontSize: 12, padding: 8 }}>{error}</div>;
  }
  if (!rows.length) {
    return <div style={{ color: "#888", fontSize: 12, padding: 8 }}>该日无市场级归因明细</div>;
  }

  const visibleMarketGroups = Array.from(
    rows.reduce((map, row) => {
      const key = `${row.condition_id}::${row.market_slug || row.condition_id}`;
      const existing = map.get(key) || {
        key,
        conditionId: row.condition_id,
        marketSlug: row.market_slug || row.condition_id,
        rows: [] as DailyLeaderMarketLegPnl[],
      };
      existing.rows.push(row);
      map.set(key, existing);
      return map;
    }, new Map<string, { key: string; conditionId: string; marketSlug: string; rows: DailyLeaderMarketLegPnl[] }>())
      .values()
  )
    .map((group) => ({
      ...group,
      rows: [...group.rows].sort((a, b) => String(a.outcome || "").localeCompare(String(b.outcome || ""))),
      marketTotal: group.rows.reduce((sum, row) => sum + (row.total_pnl_delta ?? 0), 0),
      hasSignal: group.rows.some((row) => hasDailySignal(row)),
    }))
    .filter((group) => group.hasSignal)
    .filter((group) => Math.abs(group.marketTotal) >= DRILLDOWN_MARKET_MIN_ABS_PNL)
    .sort((a, b) => Math.abs(b.marketTotal) - Math.abs(a.marketTotal));

  if (!visibleMarketGroups.length) {
    return <div style={{ color: "#888", fontSize: 12, padding: 8 }}>该日无可见市场级归因明细（已隐藏 |市场当日合计| &lt; 5 USD 的市场）</div>;
  }

  const visibleTotal = visibleMarketGroups.reduce((sum, group) => sum + group.marketTotal, 0);

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", gap: 12, marginBottom: 10 }}>
        <div style={{ fontSize: 12, color: "#555" }}>
          账户 <b>{selection.accountName}</b> / Leader{" "}
          <a href={`https://polymarket.com/profile/${selection.leaderAddress}`} target="_blank" rel="noreferrer" style={{ color: "#1a4fff", textDecoration: "none" }}>
            {shortAddr(selection.leaderAddress)}
          </a>{" "}
          / 日期 <b>{selection.dateKey}</b>
        </div>
        <div style={{ fontSize: 12, fontWeight: 700, color: pnlColor(visibleTotal) }}>
          可见合计 {visibleTotal >= 0 ? "+" : ""}{fmtDec(visibleTotal, 2)}
        </div>
      </div>
      <div style={{ fontSize: 11, color: "#777", marginBottom: 10 }}>
        按归因入账日展示。市场名称里的日期可能早于 {selection.dateKey}，这表示仓位可能在更早日期买入，但利润是在这一天卖出、结算或产生未实现变化。
      </div>
      <div style={{ fontSize: 11, color: "#777", marginBottom: 10 }}>
        买入/卖出/结算列展示截至当日的累计值，PnL 列展示当日增量。
      </div>
      <div style={{ fontSize: 11, color: "#777", marginBottom: 10 }}>
        已隐藏 |市场当日合计| &lt; {DRILLDOWN_MARKET_MIN_ABS_PNL} USD 的市场卡片。
      </div>
      <div style={{ display: "grid", gap: 10 }}>
        {visibleMarketGroups.map((group) => (
          <div key={group.key} style={{ border: "1px solid #eee", borderRadius: 8, overflow: "hidden" }}>
            <div style={{ padding: "10px 12px", background: "#fafafa", borderBottom: "1px solid #eee" }}>
              <div style={{ fontSize: 13, fontWeight: 600 }}>{group.marketSlug}</div>
              <div style={{ fontSize: 11, color: "#888", marginTop: 2 }}>{group.conditionId}</div>
            </div>
            <div style={{ overflowX: "auto" }}>
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
                <thead>
                  <tr style={{ background: "#fcfcfc" }}>
                    <th style={{ textAlign: "left", padding: "8px 10px", borderBottom: "1px solid #f0f0f0" }}>方向</th>
                    <th style={{ textAlign: "right", padding: "8px 10px", borderBottom: "1px solid #f0f0f0" }}>累计买入次数</th>
                    <th style={{ textAlign: "right", padding: "8px 10px", borderBottom: "1px solid #f0f0f0" }}>累计买入份额</th>
                    <th style={{ textAlign: "right", padding: "8px 10px", borderBottom: "1px solid #f0f0f0" }}>累计买入金额</th>
                    <th style={{ textAlign: "right", padding: "8px 10px", borderBottom: "1px solid #f0f0f0" }}>累计卖出次数</th>
                    <th style={{ textAlign: "right", padding: "8px 10px", borderBottom: "1px solid #f0f0f0" }}>累计卖出份额</th>
                    <th style={{ textAlign: "right", padding: "8px 10px", borderBottom: "1px solid #f0f0f0" }}>累计卖出金额</th>
                    <th style={{ textAlign: "right", padding: "8px 10px", borderBottom: "1px solid #f0f0f0" }}>累计结算份额</th>
                    <th style={{ textAlign: "left", padding: "8px 10px", borderBottom: "1px solid #f0f0f0" }}>状态</th>
                    <th style={{ textAlign: "right", padding: "8px 10px", borderBottom: "1px solid #f0f0f0" }}>已实现</th>
                    <th style={{ textAlign: "right", padding: "8px 10px", borderBottom: "1px solid #f0f0f0" }}>未实现</th>
                    <th style={{ textAlign: "right", padding: "8px 10px", borderBottom: "1px solid #f0f0f0" }}>当日利润</th>
                    <th style={{ textAlign: "right", padding: "8px 10px", borderBottom: "1px solid #f0f0f0" }}>截至当日累计PnL</th>
                  </tr>
                </thead>
                <tbody>
                  {group.rows.map((row) => {
                    const cumulative = cumulativeByLeg[legKey(row.condition_id, row.token_id)];
                    const rowBackground =
                      row.close_state_eod === "settled"
                        ? "#fffdf7"
                        : row.close_state_eod === "redeemable"
                          ? "#f8fbff"
                          : undefined;
                    return (
                      <tr key={row.token_id} style={{ background: rowBackground }}>
                        <td style={{ padding: "8px 10px", borderBottom: "1px solid #f5f5f5", fontWeight: 600 }}>{row.outcome || "-"}</td>
                        <td style={{ ...cellR, padding: "8px 10px" }}>{fmtNum(cumulative?.buyFillCount ?? row.buy_fill_count, 0)}</td>
                        <td style={{ ...cellR, padding: "8px 10px" }}>{fmtDec(cumulative?.buySize ?? row.buy_size, 2)}</td>
                        <td style={{ ...cellR, padding: "8px 10px" }}>{fmtDec(cumulative?.buyCostUsd ?? row.buy_cost_usd, 2)}</td>
                        <td style={{ ...cellR, padding: "8px 10px" }}>{fmtNum(cumulative?.sellFillCount ?? row.sell_fill_count, 0)}</td>
                        <td style={{ ...cellR, padding: "8px 10px" }}>{fmtDec(cumulative?.sellSize ?? row.sell_size, 2)}</td>
                        <td style={{ ...cellR, padding: "8px 10px" }}>{fmtDec(cumulative?.sellProceedsUsd ?? row.sell_proceeds_usd, 2)}</td>
                        <td style={{ ...cellR, padding: "8px 10px" }}>{fmtDec(cumulative?.settledSize ?? row.settled_size, 2)}</td>
                        <td style={{ padding: "8px 10px", borderBottom: "1px solid #f5f5f5", color: "#666", whiteSpace: "nowrap" }}>{closeStateLabel(row.close_state_eod)}</td>
                        <td style={{ ...cellR, padding: "8px 10px", color: pnlColor(row.realized_pnl_delta ?? 0) }}>{fmtDec(row.realized_pnl_delta, 2)}</td>
                        <td style={{ ...cellR, padding: "8px 10px", color: pnlColor(row.unrealized_pnl_delta ?? 0) }}>{fmtDec(row.unrealized_pnl_delta, 2)}</td>
                        <td style={{ ...cellR, padding: "8px 10px", fontWeight: 700, color: pnlColor(row.total_pnl_delta ?? 0) }}>{fmtDec(row.total_pnl_delta, 2)}</td>
                        <td style={{ ...cellR, padding: "8px 10px", color: pnlColor(row.total_pnl_eod ?? 0) }}>{fmtDec(row.total_pnl_eod, 2)}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <div style={{ padding: "8px 12px", borderTop: "1px solid #eee", display: "flex", justifyContent: "space-between", alignItems: "center", fontSize: 12 }}>
              <span style={{ color: "#666" }}>市场当日合计</span>
              <span style={{ fontWeight: 700, color: pnlColor(group.marketTotal) }}>{group.marketTotal >= 0 ? "+" : ""}{fmtDec(group.marketTotal, 2)}</span>
            </div>
          </div>
        ))}
      </div>

      <div style={{ marginTop: 12, paddingTop: 10, borderTop: "1px solid #eee", display: "flex", justifyContent: "space-between", alignItems: "center", fontSize: 13 }}>
        <span style={{ color: "#666" }}>可见合计</span>
        <span style={{ fontWeight: 700, color: pnlColor(visibleTotal) }}>
          {visibleTotal >= 0 ? "+" : ""}{fmtDec(visibleTotal, 2)}
        </span>
      </div>
    </div>
  );
}

export function CopytradeLeaderPnlApp() {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [summaryRows, setSummaryRows] = useState<LeaderSummary[]>([]);
  const [dailyLeaderPnl, setDailyLeaderPnl] = useState<DailyLeaderPnl[]>([]);
  const [curveByAddress, setCurveByAddress] = useState<Record<string, PnlCurvePoint[]>>({});
  const [accountAddressByName, setAccountAddressByName] = useState<Record<string, string>>({});
  const [activeAccount, setActiveAccount] = useState<string>("");
  const [showAccountDailyDetails, setShowAccountDailyDetails] = useState(false);
  const [selectedLeaderAddress, setSelectedLeaderAddress] = useState<string | null>(null);
  const [showLeaderDailyTable, setShowLeaderDailyTable] = useState(true);
  const [showLeaderDailyChart, setShowLeaderDailyChart] = useState(false);
  const [selectedDrilldown, setSelectedDrilldown] = useState<DrilldownSelection | null>(null);
  const [drilldownRows, setDrilldownRows] = useState<DailyLeaderMarketLegPnl[]>([]);
  const [drilldownCumulativeByLeg, setDrilldownCumulativeByLeg] = useState<Record<string, LegCumulativeStats>>({});
  const [drilldownLoading, setDrilldownLoading] = useState(false);
  const [drilldownError, setDrilldownError] = useState<string | null>(null);
  const [refreshVersion, setRefreshVersion] = useState(0);
  const drilldownPanelRef = useRef<HTMLDivElement | null>(null);

  const refresh = async () => {
    if (!supabase) return;
    setLoading(true);
    setError(null);
    try {
      const [summary, leaderDaily] = await Promise.all([
        fetchAllRows<LeaderSummary>(
          "copytrade_leader_summary",
          "leader_address,account_name,total_realized_pnl,total_unrealized_pnl,total_pnl,winning_markets,losing_markets,total_markets,win_rate,updated_at",
          "total_pnl",
          false
        ),
        fetchAllRows<DailyLeaderPnl>(
          "copytrade_daily_leader_pnl",
          "date_key,leader_address,account_name,realized_pnl,unrealized_pnl,total_pnl,market_count",
          "date_key",
          false
        ),
      ]);
      setSummaryRows(summary);
      setDailyLeaderPnl(leaderDaily);

      const accountNames = new Set<string>();
      for (const r of summary) accountNames.add(r.account_name || "default");
      for (const r of leaderDaily) accountNames.add(r.account_name || "default");
      const accountAddrMap: Record<string, string> = {};
      for (const name of accountNames) {
        const addr = resolveAccountAddress(name);
        if (addr) accountAddrMap[name] = addr;
      }
      setAccountAddressByName(accountAddrMap);

      const leaderAddrs = Array.from(
        new Set(
          [
            ...summary.map((r) => (r.leader_address || "").toLowerCase().trim()),
            ...leaderDaily.map((r) => (r.leader_address || "").toLowerCase().trim()),
          ].filter(Boolean)
        )
      );
      const curveAddrs = Array.from(new Set([...leaderAddrs, ...Object.values(accountAddrMap)]));

      const curveEntries = await Promise.all(
        curveAddrs.map(async (addr) => {
          try {
            return [addr, await fetchPnlCurve(addr)] as const;
          } catch {
            return [addr, [] as PnlCurvePoint[]] as const;
          }
        })
      );
      const curves: Record<string, PnlCurvePoint[]> = {};
      for (const [addr, points] of curveEntries) curves[addr] = points;
      setCurveByAddress(curves);
      setRefreshVersion((v) => v + 1);
    } catch (e: any) {
      setError(String(e?.message ?? e));
      setSummaryRows([]);
      setCurveByAddress({});
      setDailyLeaderPnl([]);
      setAccountAddressByName({});
      setDrilldownRows([]);
      setDrilldownCumulativeByLeg({});
      setDrilldownError(null);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void refresh();
  }, []);

  const accountNames = useMemo(() => {
    const names = new Set<string>();
    for (const r of summaryRows) names.add(r.account_name || "default");
    for (const r of dailyLeaderPnl) names.add(r.account_name || "default");
    return Array.from(names).sort();
  }, [summaryRows, dailyLeaderPnl]);

  const currentAccount = activeAccount || accountNames[0] || "";

  const acctSummaryAll = useMemo(
    () => summaryRows.filter((r) => (r.account_name || "default") === currentAccount),
    [summaryRows, currentAccount]
  );

  const acctDailyLeader = useMemo(
    () => dailyLeaderPnl.filter((r) => (r.account_name || "default") === currentAccount),
    [dailyLeaderPnl, currentAccount]
  );
  const leaderDailyWindowDates = useMemo(
    () => buildRecentDateWindow(acctDailyLeader, 14),
    [acctDailyLeader]
  );

  const acctLeaderAddresses = useMemo(() => {
    const addrs = new Set<string>();
    for (const r of acctSummaryAll) {
      const a = (r.leader_address || "").toLowerCase().trim();
      if (a) addrs.add(a);
    }
    for (const r of acctDailyLeader) {
      const a = (r.leader_address || "").toLowerCase().trim();
      if (a) addrs.add(a);
    }
    return Array.from(addrs);
  }, [acctSummaryAll, acctDailyLeader]);

  const leaderTotalByAddr = useMemo<Record<string, number>>(() => {
    const m: Record<string, number> = {};
    for (const r of acctSummaryAll) {
      const a = (r.leader_address || "").toLowerCase();
      if (!a) continue;
      if (typeof r.total_pnl !== "number" || Number.isNaN(r.total_pnl)) continue;
      m[a] = r.total_pnl;
    }
    return m;
  }, [acctSummaryAll]);

  const leaderOrder = useMemo(() => {
    const arr = [...acctLeaderAddresses];
    arr.sort((a, b) => {
      const av = leaderTotalByAddr[a];
      const bv = leaderTotalByAddr[b];
      const aOk = typeof av === "number";
      const bOk = typeof bv === "number";
      if (aOk && bOk && av !== bv) return bv - av;
      if (aOk && !bOk) return -1;
      if (!aOk && bOk) return 1;
      return a.localeCompare(b);
    });
    return arr;
  }, [acctLeaderAddresses, leaderTotalByAddr]);

  const leaderRankByAddr = useMemo<Record<string, number>>(() => {
    const out: Record<string, number> = {};
    leaderOrder.forEach((a, idx) => {
      out[a] = idx + 1;
    });
    return out;
  }, [leaderOrder]);

  const acctLeaderCurveRows = useMemo<AddressCurveRow[]>(
    () =>
      leaderOrder.map((addr) => {
        const points = curveByAddress[addr] ?? [];
        const delta = toDailyDeltaMap(points);
        return { address: addr, daily: delta.daily, latestCum: delta.latestCum };
      }),
    [leaderOrder, curveByAddress]
  );

  const currentAccountAddress = accountAddressByName[currentAccount] ?? null;

  const accountDailyRows = useMemo<DailyPnlDisplayRow[]>(() => {
    const out: DailyPnlDisplayRow[] = [];
    if (currentAccountAddress) {
      const selfDelta = toDailyDeltaMap(curveByAddress[currentAccountAddress] ?? []);
      out.push({
        key: "__self__",
        label: `我 (${shortAddr(currentAccountAddress)})`,
        leaderAddress: null,
        daily: selfDelta.daily,
        latestCum: selfDelta.latestCum,
        isSelf: true,
      });
    }
    for (const r of acctLeaderCurveRows) {
      out.push({
        key: r.address,
        label: shortAddr(r.address),
        leaderAddress: r.address,
        daily: r.daily,
        latestCum: r.latestCum,
      });
    }
    return out;
  }, [currentAccountAddress, curveByAddress, acctLeaderCurveRows]);

  const accountDailyRowsVisible = useMemo<DailyPnlDisplayRow[]>(() => {
    if (showAccountDailyDetails) return accountDailyRows;
    return accountDailyRows.filter((r) => r.isSelf);
  }, [accountDailyRows, showAccountDailyDetails]);

  const selectedLeaderValuesByDate = useMemo(() => {
    const out = new Map<string, number>();
    if (!selectedLeaderAddress) return out;
    for (const r of acctDailyLeader) {
      const addr = (r.leader_address || "").toLowerCase().trim();
      if (addr !== selectedLeaderAddress) continue;
      const dateKey = String(r.date_key || "").trim();
      const prev = out.get(dateKey) ?? 0;
      out.set(dateKey, prev + (r.total_pnl ?? 0));
    }
    return out;
  }, [acctDailyLeader, selectedLeaderAddress]);

  useEffect(() => {
    setSelectedLeaderAddress(null);
    setShowLeaderDailyTable(true);
    setShowLeaderDailyChart(false);
    setSelectedDrilldown(null);
    setDrilldownRows([]);
    setDrilldownCumulativeByLeg({});
    setDrilldownError(null);
  }, [currentAccount]);

  useEffect(() => {
    if (!selectedLeaderAddress) return;
    if (!leaderOrder.includes(selectedLeaderAddress)) {
      setSelectedLeaderAddress(null);
    }
  }, [leaderOrder, selectedLeaderAddress]);

  useEffect(() => {
    if (!selectedDrilldown) return;
    const stillExists = acctDailyLeader.some((row) => {
      const addr = (row.leader_address || "").toLowerCase().trim();
      return (
        addr === selectedDrilldown.leaderAddress &&
        (row.account_name || "default") === selectedDrilldown.accountName &&
        String(row.date_key || "").trim() === selectedDrilldown.dateKey
      );
    });
    if (!stillExists) {
      setSelectedDrilldown(null);
      setDrilldownRows([]);
      setDrilldownCumulativeByLeg({});
      setDrilldownError(null);
    }
  }, [acctDailyLeader, selectedDrilldown]);

  useEffect(() => {
    const client = supabase;
    if (!client || !selectedDrilldown) {
      setDrilldownRows([]);
      setDrilldownCumulativeByLeg({});
      setDrilldownError(null);
      setDrilldownLoading(false);
      return;
    }

    let cancelled = false;
    const load = async () => {
      setDrilldownLoading(true);
      setDrilldownError(null);
      try {
        const selectedDayRows = await fetchPagedRows<DailyLeaderMarketLegPnl>((from, to) =>
          client
            .from("copytrade_daily_leader_market_leg_pnl")
            .select(
              "date_key,leader_address,account_name,condition_id,token_id,market_slug,outcome,buy_fill_count,buy_size,buy_cost_usd,sell_fill_count,sell_size,sell_proceeds_usd,settled_size,open_size_eod,close_state_eod,realized_pnl_delta,unrealized_pnl_delta,total_pnl_delta,realized_pnl_eod,unrealized_pnl_eod,total_pnl_eod"
            )
            .eq("account_name", selectedDrilldown.accountName)
            .eq("leader_address", selectedDrilldown.leaderAddress)
            .eq("date_key", selectedDrilldown.dateKey)
            .or(DRILLDOWN_SIGNAL_OR_FILTER)
            .order("market_slug", { ascending: true })
            .order("outcome", { ascending: true })
            .order("condition_id", { ascending: true })
            .order("token_id", { ascending: true })
            .range(from, to)
        );

        const tokenIds = Array.from(
          new Set(
            selectedDayRows
              .map((row) => String(row.token_id || "").trim())
              .filter(Boolean)
          )
        );

        let cumulativeRows: LegCumulativeSourceRow[] = [];
        if (tokenIds.length) {
          const tokenChunks = chunkArray(tokenIds, DRILLDOWN_TOKEN_CHUNK_SIZE);
          const chunkResults = await Promise.all(
            tokenChunks.map((tokenChunk) =>
              fetchPagedRows<LegCumulativeSourceRow>((from, to) =>
                client
                  .from("copytrade_daily_leader_market_leg_pnl")
                  .select(
                    "date_key,condition_id,token_id,buy_fill_count,buy_size,buy_cost_usd,sell_fill_count,sell_size,sell_proceeds_usd,settled_size"
                  )
                  .eq("account_name", selectedDrilldown.accountName)
                  .eq("leader_address", selectedDrilldown.leaderAddress)
                  .lte("date_key", selectedDrilldown.dateKey)
                  .in("token_id", tokenChunk)
                  .order("date_key", { ascending: true })
                  .order("condition_id", { ascending: true })
                  .order("token_id", { ascending: true })
                  .range(from, to)
              )
            )
          );
          cumulativeRows = chunkResults.flat();
        }

        if (!cancelled) {
          setDrilldownRows(selectedDayRows);
          setDrilldownCumulativeByLeg(buildLegCumulativeStats(cumulativeRows));
        }
      } catch (e: any) {
        if (!cancelled) {
          setDrilldownRows([]);
          setDrilldownCumulativeByLeg({});
          setDrilldownError(String(e?.message ?? e));
        }
      } finally {
        if (!cancelled) setDrilldownLoading(false);
      }
    };

    void load();
    return () => {
      cancelled = true;
    };
  }, [selectedDrilldown, refreshVersion]);

  useEffect(() => {
    if (!selectedDrilldown || drilldownLoading) return;
    drilldownPanelRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [selectedDrilldown, drilldownLoading, drilldownRows, drilldownError]);

  const acctTotalPnl = useMemo(
    () => acctSummaryAll.reduce((s, r) => s + (r.total_pnl ?? 0), 0),
    [acctSummaryAll]
  );

  const acctLeaderCount = useMemo(() => acctLeaderAddresses.length, [acctLeaderAddresses]);

  const tabStyle = (name: string) => ({
    padding: "8px 16px",
    fontSize: 13,
    fontWeight: name === currentAccount ? 700 : 400,
    color: name === currentAccount ? "#1a4fff" : "#555",
    background: name === currentAccount ? "#eef3ff" : "transparent",
    border: name === currentAccount ? "1px solid #c5d5f7" : "1px solid transparent",
    borderBottom: name === currentAccount ? "2px solid #1a4fff" : "2px solid transparent",
    borderRadius: "6px 6px 0 0",
    cursor: "pointer" as const,
  });

  return (
    <div style={{ fontFamily: "system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial", padding: 16, maxWidth: 1600, margin: "0 auto" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", gap: 12 }}>
        <h2 style={{ margin: 0 }}>Copytrade Leader 归因看板</h2>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <Link to="/" style={{ fontSize: 12, color: "#2d6cdf", textDecoration: "none" }}>返回 Dashboard</Link>
          <Link to="/daily-compare" style={{ fontSize: 12, color: "#2d6cdf", textDecoration: "none" }}>跟单日盈亏对照</Link>
          <div style={{ fontSize: 12, color: "#666" }}>{loading ? "加载中..." : `共 ${summaryRows.length} 条`}</div>
          <button onClick={() => refresh()} style={{ fontSize: 12, padding: "4px 8px", borderRadius: 6, border: "1px solid #ddd", background: "#fff" }}>刷新</button>
        </div>
      </div>

      {!supabaseConfig.ok && (
        <div style={{ marginTop: 12, padding: 12, border: "1px solid #eee", borderRadius: 8, background: "#fafafa", color: "#333" }}>
          未配置 Supabase：请在 `dashboard/.env` 设置 `VITE_SUPABASE_URL` 和 `VITE_SUPABASE_ANON_KEY`。
        </div>
      )}
      {error && (
        <div style={{ marginTop: 12, padding: 12, border: "1px solid #f3b4b4", background: "#fff2f2", color: "#7a1b1b" }}>{error}</div>
      )}

      <div style={{ marginTop: 16, display: "flex", gap: 4, borderBottom: "1px solid #eee" }}>
        {accountNames.map((name) => (
          <div key={name} style={tabStyle(name)} onClick={() => setActiveAccount(name)}>{name}</div>
        ))}
      </div>

      <div style={{ marginTop: 12, display: "flex", gap: 16, fontSize: 13 }}>
        <span>账户: <b>{currentAccount}</b></span>
        <span>Leader 数: <b>{acctLeaderCount}</b></span>
        <span>总盈亏: <b style={{ color: pnlColor(acctTotalPnl) }}>{fmtNum(acctTotalPnl, 2)}</b></span>
      </div>

      <div style={{ marginTop: 12, border: "1px solid #eee", borderRadius: 8, background: "#fff", padding: 12 }}>
        <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 8 }}>账户每日收益（官方 PnL API）</div>
        {!currentAccountAddress && (
          <div style={{ color: "#b05a00", fontSize: 12, marginBottom: 8 }}>
            未找到 {currentAccount} 的账户地址映射，当前只展示 Leader 地址曲线。
          </div>
        )}
        <div style={{ marginBottom: 8 }}>
          <button
            onClick={() => setShowAccountDailyDetails((v) => !v)}
            style={{ fontSize: 12, padding: "4px 8px", borderRadius: 6, border: "1px solid #ddd", background: "#fff", cursor: "pointer" }}
          >
            {showAccountDailyDetails ? "折叠地址明细" : "展开地址明细"}
          </button>
        </div>
        <DailyPnlTable rows={accountDailyRowsVisible} leaderRankByAddr={leaderRankByAddr} />
      </div>

      <div style={{ marginTop: 12, border: "1px solid #eee", borderRadius: 8, background: "#fff", padding: 12, overflow: "auto" }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
          <div style={{ fontSize: 13, fontWeight: 600 }}>Leader 每日盈亏（近 14 天）— {currentAccount}</div>
          <button
            onClick={() => setShowLeaderDailyTable((v) => !v)}
            style={{ fontSize: 12, padding: "4px 8px", borderRadius: 6, border: "1px solid #ddd", background: "#fff", cursor: "pointer" }}
          >
            {showLeaderDailyTable ? "折叠表格" : "展开表格"}
          </button>
        </div>
        {showLeaderDailyTable ? (
          <DailyLeaderPnlTable
            data={acctDailyLeader}
            dates={leaderDailyWindowDates}
            totalByLeader={leaderTotalByAddr}
            leaderOrder={leaderOrder}
            leaderRankByAddr={leaderRankByAddr}
            selectedLeaderAddress={selectedLeaderAddress}
            selectedDrilldown={selectedDrilldown}
            onSelectLeader={(addr) => setSelectedLeaderAddress(addr)}
            onSelectDrilldown={(selection) => {
              setSelectedLeaderAddress(selection.leaderAddress);
              setSelectedDrilldown(selection);
            }}
          />
        ) : null}
      </div>

      <div style={{ marginTop: 12, border: "1px solid #eee", borderRadius: 8, background: "#fff", padding: 12 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
          <div style={{ fontSize: 13, fontWeight: 600 }}>所选 Leader 每日盈亏柱状图（近 14 天）</div>
          <button
            onClick={() => setShowLeaderDailyChart((v) => !v)}
            style={{ fontSize: 12, padding: "4px 8px", borderRadius: 6, border: "1px solid #ddd", background: "#fff", cursor: "pointer" }}
          >
            {showLeaderDailyChart ? "折叠图表" : "展开图表"}
          </button>
        </div>
        {showLeaderDailyChart ? (
          <LeaderDailyBarSection
            leaderAddress={selectedLeaderAddress}
            dates={leaderDailyWindowDates}
            valuesByDate={selectedLeaderValuesByDate}
          />
        ) : null}
      </div>

      <div ref={drilldownPanelRef} style={{ marginTop: 12, border: "1px solid #eee", borderRadius: 8, background: "#fff", padding: 12 }}>
        <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 8 }}>Leader 单日归因明细</div>
        <DailyLeaderDrilldownPanel
          selection={selectedDrilldown}
          rows={drilldownRows}
          cumulativeByLeg={drilldownCumulativeByLeg}
          loading={drilldownLoading}
          error={drilldownError}
        />
      </div>

    </div>
  );
}

