import { Fragment, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { supabase, supabaseConfig } from "./supabaseClient";

type CompareSummaryRow = {
  date_key: string;
  account_name: string | null;
  leader_address: string;
  leader_total_pnl: number | null;
  our_total_pnl: number | null;
  delta_pnl: number | null;
  leader_excluded_pnl: number | null;
  our_excluded_pnl: number | null;
  visible_leader_pnl: number | null;
  visible_our_pnl: number | null;
  updated_at: string | null;
};

type CompareMarketLegRow = {
  date_key: string;
  account_name: string | null;
  leader_address: string;
  condition_id: string;
  token_id: string;
  market_slug: string | null;
  outcome: string | null;
  exclusion_reason: string | null;
  leader_buy_fill_count: number | null;
  leader_buy_usd: number | null;
  leader_buy_avg_price: number | null;
  leader_total_pnl: number | null;
  our_buy_fill_count: number | null;
  our_buy_usd: number | null;
  our_buy_avg_price: number | null;
  our_total_pnl: number | null;
};

const LEADER_PNL_VISIBLE_THRESHOLD = 50;
const PAGE_SIZE = 1000;
const REFRESH_INTERVAL_MS = 60_000;
const SUMMARY_SELECT =
  "date_key,account_name,leader_address,leader_total_pnl,our_total_pnl,delta_pnl,leader_excluded_pnl,our_excluded_pnl,visible_leader_pnl,visible_our_pnl,updated_at";
const MARKET_LEG_SELECT =
  "date_key,account_name,leader_address,condition_id,token_id,market_slug,outcome,exclusion_reason,leader_buy_fill_count,leader_buy_usd,leader_buy_avg_price,leader_total_pnl,our_buy_fill_count,our_buy_usd,our_buy_avg_price,our_total_pnl";

type PnlSortKey = "delta_abs" | "leader_desc" | "our_desc";

function n(v: number | null | undefined): number {
  return typeof v === "number" && Number.isFinite(v) ? v : 0;
}

function isVisibleLeaderPnl(v: number | null | undefined): boolean {
  return typeof v === "number" && Number.isFinite(v) && Math.abs(v) >= LEADER_PNL_VISIBLE_THRESHOLD;
}

function fmtUsd(v: number | null | undefined): string {
  if (typeof v !== "number" || Number.isNaN(v)) return "-";
  return v.toLocaleString(undefined, {
    minimumFractionDigits: 0,
    maximumFractionDigits: 2,
  });
}

function fmtLeaderUsd(v: number | null | undefined): string {
  if (typeof v !== "number" || Number.isNaN(v)) return "-";
  const rounded = Math.round(v);
  const safe = Object.is(rounded, -0) ? 0 : rounded;
  return safe.toLocaleString(undefined, {
    maximumFractionDigits: 0,
  });
}

function fmtPrice(v: number | null | undefined): string {
  if (typeof v !== "number" || Number.isNaN(v)) return "-";
  return v.toFixed(4);
}

function shortAddr(v: string): string {
  return v.length > 12 ? `${v.slice(0, 6)}...${v.slice(-4)}` : v;
}

function outcomeLabel(v: string | null | undefined): string {
  const text = String(v || "").trim().toLowerCase();
  if (text === "1" || text === "yes") return "Yes";
  if (text === "0" || text === "no") return "No";
  return v || "-";
}

function marketTitle(leg: CompareMarketLegRow): string {
  const title = String(leg.market_slug || "").trim();
  return title || leg.condition_id;
}

function pnlColor(v: number | null | undefined): string {
  return n(v) >= 0 ? "#1f7a1f" : "#b02a2a";
}

function formatUtc8Timestamp(value: string | null | undefined): string {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value.replace("T", " ").slice(0, 19);
  const shifted = new Date(date.getTime() + 8 * 60 * 60 * 1000);
  const pad = (part: number) => String(part).padStart(2, "0");
  return [
    `${shifted.getUTCFullYear()}-${pad(shifted.getUTCMonth() + 1)}-${pad(shifted.getUTCDate())}`,
    `${pad(shifted.getUTCHours())}:${pad(shifted.getUTCMinutes())}:${pad(shifted.getUTCSeconds())}`,
  ].join(" ");
}

async function fetchSummaryRows(): Promise<CompareSummaryRow[]> {
  if (!supabase) return [];
  const out: CompareSummaryRow[] = [];
  for (let page = 0; page < 100; page += 1) {
    const from = page * PAGE_SIZE;
    const to = from + PAGE_SIZE - 1;
    const res = await supabase
      .from("copytrade_compare_daily_summary")
      .select(SUMMARY_SELECT)
      .order("date_key", { ascending: false })
      .order("account_name", { ascending: true })
      .order("leader_address", { ascending: true })
      .range(from, to);
    if (res.error) throw new Error(res.error.message);
    const chunk = (res.data ?? []) as CompareSummaryRow[];
    out.push(...chunk);
    if (chunk.length < PAGE_SIZE) break;
  }
  return out;
}

async function fetchMarketRowsForPage(
  dateKey: string,
  accountName: string,
): Promise<CompareMarketLegRow[]> {
  if (!supabase || !dateKey || !accountName) return [];
  const out: CompareMarketLegRow[] = [];
  for (let page = 0; page < 100; page += 1) {
    const from = page * PAGE_SIZE;
    const to = from + PAGE_SIZE - 1;
    const res = await supabase
      .from("copytrade_compare_daily_market_leg")
      .select(MARKET_LEG_SELECT)
      .eq("date_key", dateKey)
      .eq("account_name", accountName)
      .order("leader_address", { ascending: true })
      .order("condition_id", { ascending: true })
      .order("token_id", { ascending: true })
      .range(from, to);
    if (res.error) throw new Error(res.error.message);
    const chunk = (res.data ?? []) as CompareMarketLegRow[];
    out.push(...chunk.filter((row) => isVisibleLeaderPnl(row.leader_total_pnl)));
    if (chunk.length < PAGE_SIZE) break;
  }
  return out;
}

export function CopytradeDailyCompareApp() {
  const [summaryLoading, setSummaryLoading] = useState(true);
  const [marketLoading, setMarketLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [marketError, setMarketError] = useState<string | null>(null);
  const [summaryRows, setSummaryRows] = useState<CompareSummaryRow[]>([]);
  const [marketRows, setMarketRows] = useState<CompareMarketLegRow[]>([]);
  const [selectedAccount, setSelectedAccount] = useState("");
  const [selectedDate, setSelectedDate] = useState("");
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [showExcluded, setShowExcluded] = useState(false);
  const [sortKey, setSortKey] = useState<PnlSortKey>("delta_abs");

  useEffect(() => {
    let cancelled = false;

    async function run(showLoading = true) {
      if (!supabase) {
        setError("Supabase 未配置");
        setSummaryLoading(false);
        return;
      }
      if (showLoading) setSummaryLoading(true);
      setError(null);
      try {
        const summary = await fetchSummaryRows();
        if (cancelled) return;
        setSummaryRows(summary.filter((row) => isVisibleLeaderPnl(row.leader_total_pnl)));
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : "加载失败");
      } finally {
        if (!cancelled && showLoading) setSummaryLoading(false);
      }
    }

    void run();
    const intervalId = window.setInterval(() => {
      void run(false);
    }, REFRESH_INTERVAL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(intervalId);
    };
  }, []);

  const availableAccounts = useMemo(() => {
    return Array.from(
      new Set(summaryRows.map((row) => String(row.account_name || "").trim()).filter(Boolean)),
    ).sort();
  }, [summaryRows]);

  useEffect(() => {
    if (!selectedAccount && availableAccounts.length > 0) {
      setSelectedAccount(availableAccounts[0]);
    }
  }, [availableAccounts, selectedAccount]);

  const availableDates = useMemo(() => {
    const dates = new Set<string>();
    for (const row of summaryRows) {
      const accountName = String(row.account_name || "").trim();
      if (accountName === selectedAccount && row.date_key) {
        dates.add(row.date_key);
      }
    }
    return Array.from(dates).sort((a, b) => b.localeCompare(a));
  }, [selectedAccount, summaryRows]);

  useEffect(() => {
    if (!selectedAccount || availableDates.length === 0) {
      if (selectedDate) setSelectedDate("");
      return;
    }
    if (!selectedDate || !availableDates.includes(selectedDate)) {
      setSelectedDate(availableDates[0]);
    }
  }, [availableDates, selectedAccount, selectedDate]);

  useEffect(() => {
    setExpanded({});
  }, [selectedAccount, selectedDate]);

  useEffect(() => {
    let cancelled = false;

    async function run(showLoading = true) {
      if (!supabase || !selectedAccount || !selectedDate) {
        setMarketRows([]);
        setMarketLoading(false);
        setMarketError(null);
        return;
      }
      if (showLoading) setMarketLoading(true);
      setMarketError(null);
      try {
        const rows = await fetchMarketRowsForPage(selectedDate, selectedAccount);
        if (!cancelled) setMarketRows(rows);
      } catch (err) {
        if (!cancelled) {
          setMarketRows([]);
          setMarketError(err instanceof Error ? err.message : "明细加载失败");
        }
      } finally {
        if (!cancelled && showLoading) setMarketLoading(false);
      }
    }

    void run();
    const intervalId = window.setInterval(() => {
      void run(false);
    }, REFRESH_INTERVAL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(intervalId);
    };
  }, [selectedAccount, selectedDate]);

  const currentSummaryRows = useMemo(() => {
    const rows = summaryRows.filter(
      (row) => row.account_name === selectedAccount && row.date_key === selectedDate,
    );
    return [...rows].sort((a, b) => {
      if (sortKey === "leader_desc") {
        return n(b.leader_total_pnl) - n(a.leader_total_pnl);
      }
      if (sortKey === "our_desc") {
        return n(b.our_total_pnl) - n(a.our_total_pnl);
      }
      return Math.abs(n(b.delta_pnl)) - Math.abs(n(a.delta_pnl));
    });
  }, [selectedAccount, selectedDate, sortKey, summaryRows]);

  const currentMarketRows = useMemo(() => {
    return marketRows.filter(
      (row) => row.account_name === selectedAccount && row.date_key === selectedDate,
    );
  }, [selectedAccount, selectedDate, marketRows]);

  const marketRowsByLeader = useMemo(() => {
    const out = new Map<string, CompareMarketLegRow[]>();
    for (const row of currentMarketRows) {
      const list = out.get(row.leader_address) ?? [];
      list.push(row);
      out.set(row.leader_address, list);
    }
    return out;
  }, [currentMarketRows]);

  const selectedDateIndex = availableDates.indexOf(selectedDate);
  const canGoNewer = selectedDateIndex > 0;
  const canGoOlder = selectedDateIndex >= 0 && selectedDateIndex < availableDates.length - 1;
  const goToDateIndex = (index: number) => {
    const nextDate = availableDates[index];
    if (nextDate) setSelectedDate(nextDate);
  };

  return (
    <div
      style={{
        fontFamily: "system-ui, -apple-system, Segoe UI, sans-serif",
        padding: 16,
        maxWidth: 1800,
        margin: "0 auto",
      }}
    >
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "baseline",
          gap: 12,
          marginBottom: 12,
        }}
      >
        <div>
          <h2 style={{ margin: 0 }}>跟单日盈亏对照</h2>
          <div style={{ fontSize: 12, color: "#666", marginTop: 4 }}>
            {selectedDate ? `日期 ${selectedDate} (UTC+8)` : "等待数据"}
          </div>
        </div>
        <div style={{ display: "flex", gap: 10, fontSize: 12 }}>
          <Link to="/" style={{ color: "#2d6cdf", textDecoration: "none" }}>
            首页
          </Link>
          <Link to="/leader-attribution" style={{ color: "#2d6cdf", textDecoration: "none" }}>
            Leader 归因页
          </Link>
        </div>
      </div>

      {!supabaseConfig.ok ? (
        <div style={{ color: "#b02a2a" }}>Supabase 未配置，页面不可用。</div>
      ) : null}
      {summaryLoading ? <div>加载中...</div> : null}
      {error ? <div style={{ color: "#b02a2a" }}>{error}</div> : null}
      {marketError ? <div style={{ color: "#b02a2a" }}>明细加载失败：{marketError}</div> : null}

      {!summaryLoading && !error ? (
        <>
          <div style={{ display: "flex", gap: 8, marginBottom: 12, flexWrap: "wrap" }}>
            {availableAccounts.map((accountName) => (
              <button
                key={accountName}
                onClick={() => setSelectedAccount(accountName)}
                style={{
                  padding: "6px 10px",
                  borderRadius: 999,
                  border:
                    accountName === selectedAccount
                      ? "1px solid #1f4e9e"
                      : "1px solid #ddd",
                  background: accountName === selectedAccount ? "#e8f0ff" : "#fff",
                  cursor: "pointer",
                  fontSize: 12,
                }}
              >
                {accountName}
              </button>
            ))}
            {availableDates.length ? (
              <div
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  gap: 6,
                  fontSize: 12,
                  marginLeft: "auto",
                }}
              >
                <button
                  type="button"
                  onClick={() => goToDateIndex(selectedDateIndex + 1)}
                  disabled={!canGoOlder}
                  style={dateButtonStyle}
                >
                  较早
                </button>
                <select
                  value={selectedDate}
                  onChange={(e) => setSelectedDate(e.target.value)}
                  style={dateSelectStyle}
                >
                  {availableDates.map((date) => (
                    <option key={date} value={date}>
                      {date}
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  onClick={() => goToDateIndex(selectedDateIndex - 1)}
                  disabled={!canGoNewer}
                  style={dateButtonStyle}
                >
                  较新
                </button>
                <span style={{ color: "#777" }}>
                  明细 {marketLoading ? "加载中" : `${currentMarketRows.length} 行`}
                </span>
              </div>
            ) : null}
            <label
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 6,
                fontSize: 12,
              }}
            >
              排序
              <select
                value={sortKey}
                onChange={(e) => setSortKey(e.target.value as PnlSortKey)}
                style={dateSelectStyle}
              >
                <option value="delta_abs">差值绝对值</option>
                <option value="leader_desc">Leader 盈亏降序</option>
                <option value="our_desc">我的盈亏降序</option>
              </select>
            </label>
            <label
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 6,
                fontSize: 12,
                marginLeft: availableDates.length ? 0 : "auto",
              }}
            >
              <input
                type="checkbox"
                checked={showExcluded}
                onChange={(e) => setShowExcluded(e.target.checked)}
              />
              显示已排除市场
            </label>
          </div>

          <div style={{ overflowX: "auto", border: "1px solid #eee", borderRadius: 10 }}>
            <table style={{ width: "100%", borderCollapse: "collapse", minWidth: 980 }}>
              <thead>
                <tr style={{ background: "#fafafa" }}>
                  {[
                    "Leader",
                    "Leader 今日总盈亏",
                    "我的今日总盈亏",
                    "差值",
                    "已排除盈亏",
                    "更新时间(UTC+8)",
                  ].map((label) => (
                    <th
                      key={label}
                      style={{
                        textAlign: "left",
                        padding: "10px 12px",
                        fontSize: 12,
                        borderBottom: "1px solid #eee",
                      }}
                    >
                      {label}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {currentSummaryRows.map((row) => {
                  const leaderKey = `${row.date_key}::${row.account_name}::${row.leader_address}`;
                  const isOpen = Boolean(expanded[leaderKey]);
                  const leaderLegs = marketRowsByLeader.get(row.leader_address) ?? [];
                  const visibleLegs = showExcluded
                    ? leaderLegs
                    : leaderLegs.filter((leg) => !leg.exclusion_reason);
                  const sortedLegs = [...visibleLegs].sort((a, b) => {
                    if (sortKey === "leader_desc") {
                      return n(b.leader_total_pnl) - n(a.leader_total_pnl);
                    }
                    if (sortKey === "our_desc") {
                      return n(b.our_total_pnl) - n(a.our_total_pnl);
                    }
                    const marketCmp = marketTitle(a).localeCompare(marketTitle(b));
                    if (marketCmp !== 0) return marketCmp;
                    return String(a.outcome || "").localeCompare(String(b.outcome || ""));
                  });

                  return (
                    <Fragment key={leaderKey}>
                      <tr
                        onClick={() =>
                          setExpanded((prev) => ({ ...prev, [leaderKey]: !prev[leaderKey] }))
                        }
                        style={{ cursor: "pointer", background: isOpen ? "#fcfdff" : "#fff" }}
                      >
                        <td style={cellStyleLeft}>
                          <div style={{ fontWeight: 600 }}>{shortAddr(row.leader_address)}</div>
                          <div style={{ fontSize: 11, color: "#888" }}>
                            {isOpen ? "收起明细" : "展开明细"}
                          </div>
                        </td>
                        <td style={cellStyle}>
                          <span style={{ color: pnlColor(row.leader_total_pnl) }}>
                            {fmtLeaderUsd(row.leader_total_pnl)}
                          </span>
                        </td>
                        <td style={cellStyle}>
                          <span style={{ color: pnlColor(row.our_total_pnl) }}>
                            {fmtUsd(row.our_total_pnl)}
                          </span>
                        </td>
                        <td style={cellStyle}>
                          <span style={{ color: pnlColor(row.delta_pnl) }}>
                            {fmtUsd(row.delta_pnl)}
                          </span>
                        </td>
                        <td style={cellStyle}>
                          L {fmtLeaderUsd(row.leader_excluded_pnl)} / 我 {fmtUsd(row.our_excluded_pnl)}
                        </td>
                        <td style={cellStyleLeft}>
                          {formatUtc8Timestamp(row.updated_at)}
                        </td>
                      </tr>

                      {isOpen ? (
                        <tr>
                          <td
                            colSpan={6}
                            style={{
                              padding: 12,
                              borderBottom: "1px solid #eee",
                              background: "#fcfdff",
                            }}
                          >
                            <div style={{ fontSize: 12, color: "#666", marginBottom: 10 }}>
                              主明细合计: Leader {fmtLeaderUsd(row.visible_leader_pnl)} / 我{" "}
                              {fmtUsd(row.visible_our_pnl)}，已排除: Leader{" "}
                              {fmtLeaderUsd(row.leader_excluded_pnl)} / 我{" "}
                              {fmtUsd(row.our_excluded_pnl)}
                            </div>
                            <div style={{ fontSize: 12, color: "#777", marginBottom: 10 }}>
                              盈亏列为当日增量；买次、买额、买均价为截至当日累计。
                            </div>

                            {marketLoading ? (
                              <div style={{ fontSize: 12, color: "#888" }}>明细加载中...</div>
                            ) : sortedLegs.length === 0 ? (
                              <div style={{ fontSize: 12, color: "#888" }}>
                                没有可显示的市场明细。
                              </div>
                            ) : (
                              <div style={{ overflowX: "auto" }}>
                                <table
                                  style={{
                                    width: "100%",
                                    borderCollapse: "collapse",
                                    minWidth: 1100,
                                  }}
                                >
                                  <thead>
                                    <tr style={{ background: "#fffdf5" }}>
                                      {[
                                        "市场题目",
                                        "方向",
                                        "leader今日盈亏",
                                        "我的今日盈亏",
                                        "leader累计买次",
                                        "leader累计买额",
                                        "leader累计买均价",
                                        "我的累计买次",
                                        "我的累计买额",
                                        "我的累计买均价",
                                      ].map((label) => (
                                        <th
                                          key={label}
                                          style={label === "市场题目" || label === "方向" ? compactHeadLeft : compactHeadRight}
                                        >
                                          {label}
                                        </th>
                                      ))}
                                    </tr>
                                  </thead>
                                  <tbody>
                                    {sortedLegs.map((leg) => (
                                      <tr key={`${leg.condition_id}::${leg.token_id}`}>
                                        <td style={compactCellLeft}>
                                          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                                            <span>{marketTitle(leg)}</span>
                                            {leg.exclusion_reason ? (
                                              <span style={excludedBadgeStyle}>已排除</span>
                                            ) : null}
                                          </div>
                                        </td>
                                        <td style={compactCellLeft}>{outcomeLabel(leg.outcome)}</td>
                                        <td style={{ ...compactCellRight, color: pnlColor(leg.leader_total_pnl) }}>
                                          {fmtLeaderUsd(leg.leader_total_pnl)}
                                        </td>
                                        <td style={{ ...compactCellRight, color: pnlColor(leg.our_total_pnl) }}>
                                          {fmtUsd(leg.our_total_pnl)}
                                        </td>
                                        <td style={compactCellRight}>{n(leg.leader_buy_fill_count)}</td>
                                        <td style={compactCellRight}>{fmtUsd(leg.leader_buy_usd)}</td>
                                        <td style={compactCellRight}>{fmtPrice(leg.leader_buy_avg_price)}</td>
                                        <td style={compactCellRight}>{n(leg.our_buy_fill_count)}</td>
                                        <td style={compactCellRight}>{fmtUsd(leg.our_buy_usd)}</td>
                                        <td style={compactCellRight}>{fmtPrice(leg.our_buy_avg_price)}</td>
                                      </tr>
                                    ))}
                                  </tbody>
                                </table>
                              </div>
                            )}
                          </td>
                        </tr>
                      ) : null}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>

          {!currentSummaryRows.length ? (
            <div style={{ marginTop: 12, color: "#888" }}>当前账号暂无 compare 数据。</div>
          ) : null}
        </>
      ) : null}
    </div>
  );
}

const cellStyle = {
  padding: "10px 12px",
  borderBottom: "1px solid #eee",
  fontSize: 12,
};

const cellStyleLeft = {
  ...cellStyle,
  textAlign: "left" as const,
};

const smallCell = {
  padding: "6px 8px",
  borderBottom: "1px solid #f1f1f1",
  fontSize: 11,
  textAlign: "left" as const,
  whiteSpace: "nowrap" as const,
};

const dateButtonStyle = {
  padding: "5px 8px",
  border: "1px solid #ddd",
  borderRadius: 6,
  background: "#fff",
  cursor: "pointer",
  fontSize: 12,
};

const dateSelectStyle = {
  padding: "5px 8px",
  border: "1px solid #ddd",
  borderRadius: 6,
  background: "#fff",
  fontSize: 12,
};

const compactHeadBase = {
  padding: "7px 8px",
  borderBottom: "1px solid #eee",
  fontSize: 11,
  fontWeight: 600,
  whiteSpace: "nowrap" as const,
};

const compactHeadLeft = {
  ...compactHeadBase,
  textAlign: "left" as const,
};

const compactHeadRight = {
  ...compactHeadBase,
  textAlign: "right" as const,
};

const compactCellBase = {
  padding: "7px 8px",
  borderBottom: "1px solid #f1f1f1",
  fontSize: 11,
  whiteSpace: "nowrap" as const,
};

const compactCellLeft = {
  ...compactCellBase,
  textAlign: "left" as const,
};

const compactCellRight = {
  ...compactCellBase,
  textAlign: "right" as const,
};

const excludedBadgeStyle = {
  display: "inline-block",
  padding: "1px 6px",
  borderRadius: 999,
  background: "#f1f3f5",
  color: "#666",
  fontSize: 10,
  lineHeight: 1.5,
};
