"use client";

import React, { useEffect, useMemo, useState } from "react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { supabase } from "@/lib/supabase";
import { Loader2 } from "lucide-react";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import {
  CartesianGrid,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

type Row = {
  address: string;
  short_name: string | null;
  create_date_ms: number | null;
  max_market_cap_wan: number | null;
  is_eligible: boolean | null;
  is_binance: boolean | null;
};

type Point = {
  x: number;
  y: number;
  address: string;
  shortName: string;
  isBinance: boolean;
};

function DotShape(props: any) {
  const cx = props?.cx;
  const cy = props?.cy;
  const payload: Point | undefined = props?.payload;
  if (typeof cx !== "number" || typeof cy !== "number" || !payload) {
    return <circle cx={0} cy={0} r={0} fill="transparent" />;
  }
  const fill = payload.isBinance ? "#facc15" : "#6366f1";
  return <circle cx={cx} cy={cy} r={4} fill={fill} stroke="#ffffff" strokeWidth={1} />;
}

function formatBeijing(ms: number) {
  const date = new Date(ms);
  const formatter = new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: "Asia/Shanghai",
  });
  return formatter.format(date).replaceAll("-", "/");
}

function formatWan(value: number) {
  if (!Number.isFinite(value)) return "-";
  return `${value.toFixed(1)} 万`;
}

function formatBeijingHour(ms: number) {
  const date = new Date(ms);
  const formatter = new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    hour12: false,
    timeZone: "Asia/Shanghai",
  });
  return formatter.format(date).replaceAll("-", "/");
}

function buildTicks(domain: [number, number], stepHours: number) {
  const [min, max] = domain;
  const stepMs = stepHours * 60 * 60 * 1000;
  const start = Math.ceil(min / stepMs) * stepMs;
  const ticks: number[] = [];
  for (let t = start; t <= max; t += stepMs) {
    ticks.push(t);
  }
  return ticks.length ? ticks : [min, max];
}

export default function DailyMarketCapCeilingPage() {
  const [loading, setLoading] = useState(true);
  const [rows, setRows] = useState<Row[]>([]);
  const [timeWindowHours, setTimeWindowHours] = useState<number>(24);
  const [yMaxWanInput, setYMaxWanInput] = useState<string>("");

  useEffect(() => {
    const fetchData = async () => {
      setLoading(true);
      try {
        const cutoff = Date.now() - timeWindowHours * 60 * 60 * 1000;
        const { data, error } = await supabase
          .from("daily_market_cap_ceiling")
          .select("address, short_name, create_date_ms, max_market_cap_wan, is_eligible, is_binance")
          .gte("create_date_ms", cutoff)
          .eq("is_eligible", true)
          .order("create_date_ms", { ascending: true });
        if (error) throw error;
        setRows((data as Row[]) || []);
      } catch (e) {
        console.error(e);
        setRows([]);
      } finally {
        setLoading(false);
      }
    };

    fetchData();
  }, [timeWindowHours]);

  const points = useMemo<Point[]>(() => {
    return rows
      .map((r) => {
        const x = Number(r.create_date_ms);
        const y = Number(r.max_market_cap_wan);
        if (!r.address || !Number.isFinite(x) || !Number.isFinite(y)) return null;
        return {
          x,
          y,
          address: r.address,
          shortName: r.short_name ?? r.address.slice(0, 8),
          isBinance: Boolean(r.is_binance),
        };
      })
      .filter((p): p is Point => Boolean(p));
  }, [rows]);

  const parsedYMaxWan: number | undefined = useMemo(() => {
    const v = Number(yMaxWanInput);
    if (!isFinite(v) || v <= 0) return undefined;
    return v;
  }, [yMaxWanInput]);

  const filteredPoints = useMemo<Point[]>(() => {
    if (!parsedYMaxWan) return points;
    return points.filter((p) => p.y <= parsedYMaxWan);
  }, [points, parsedYMaxWan]);

  const launchCount = points.length;

  const avgNormalWan = useMemo<number | null>(() => {
    const arr = points.filter((p) => !p.isBinance).map((p) => p.y);
    if (arr.length === 0) return null;
    return arr.reduce((a, b) => a + b, 0) / arr.length;
  }, [points]);

  const avgBinanceWan = useMemo<number | null>(() => {
    const arr = points.filter((p) => p.isBinance).map((p) => p.y);
    if (arr.length === 0) return null;
    return arr.reduce((a, b) => a + b, 0) / arr.length;
  }, [points]);

  const domainX = useMemo<[number, number]>(() => {
    const now = Date.now();
    const min = now - timeWindowHours * 60 * 60 * 1000;
    return [min, now];
  }, [timeWindowHours]);

  const xTickStepHours = useMemo(() => {
    if (timeWindowHours <= 6) return 1;
    if (timeWindowHours <= 12) return 2;
    if (timeWindowHours <= 24) return 3;
    if (timeWindowHours <= 48) return 6;
    return 6;
  }, [timeWindowHours]);

  const xTicks = useMemo(() => buildTicks(domainX, xTickStepHours), [domainX, xTickStepHours]);

  const autoYMaxWan = useMemo<number>(() => {
    const arr = (parsedYMaxWan ? filteredPoints : points).map((p) => p.y);
    const max = arr.length ? Math.max(...arr) : 0;
    return max || 0;
  }, [points, filteredPoints, parsedYMaxWan]);

  return (
    <div className="container mx-auto p-6 max-w-6xl space-y-6">
      <div className="space-y-2">
        <h1 className="text-3xl font-bold tracking-tight">每日市值上限</h1>
        <p className="text-gray-500">过去 {timeWindowHours} 小时新币的最高市值散点分布（单位：万美元）</p>
        <div className="flex flex-col md:flex-row gap-3 md:items-center">
          <div className="flex items-center gap-2">
            <span className="text-sm text-gray-600">X轴范围</span>
            <Select value={String(timeWindowHours)} onValueChange={(v) => setTimeWindowHours(Number(v))}>
              <SelectTrigger className="h-8 w-32">
                <SelectValue placeholder="时间范围" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="1">最近 1 小时</SelectItem>
                <SelectItem value="3">最近 3 小时</SelectItem>
                <SelectItem value="6">最近 6 小时</SelectItem>
                <SelectItem value="12">最近 12 小时</SelectItem>
                <SelectItem value="24">最近 24 小时</SelectItem>
                <SelectItem value="48">最近 48 小时</SelectItem>
                <SelectItem value="72">最近 72 小时</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="flex items-center gap-2">
            <span className="text-sm text-gray-600">Y轴上限（万美金）</span>
            <Input
              value={yMaxWanInput}
              onChange={(e) => setYMaxWanInput(e.target.value)}
              className="h-8 w-40"
              placeholder={`默认 ${autoYMaxWan.toFixed(0)} 万`}
              inputMode="decimal"
            />
          </div>
        </div>
      </div>

      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
        <Card>
          <CardContent className="pt-6">
            <div className="text-sm font-medium text-gray-500">当前时间范围</div>
            <div className="text-2xl font-bold mt-2">{timeWindowHours}H</div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="pt-6">
            <div className="text-sm font-medium text-gray-500">过去 {timeWindowHours} 小时发射数量</div>
            <div className="text-2xl font-bold mt-2">{launchCount}</div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="pt-6">
            <div className="text-sm font-medium text-gray-500">平均最高市值（常规）</div>
            <div className="text-2xl font-bold mt-2">{avgNormalWan === null ? "无数据" : formatWan(avgNormalWan)}</div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="pt-6">
            <div className="text-sm font-medium text-gray-500">平均最高市值（币安系）</div>
            <div className="text-2xl font-bold mt-2">{avgBinanceWan === null ? "无数据" : formatWan(avgBinanceWan)}</div>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>{timeWindowHours}H 最高市值散点图</CardTitle>
          <CardDescription>鼠标悬停查看 shortName、最高市值和创建时间</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="h-[520px] w-full">
            {loading ? (
              <div className="h-full w-full flex items-center justify-center bg-slate-50 rounded-lg">
                <Loader2 className="h-10 w-10 animate-spin text-gray-300" />
              </div>
            ) : points.length === 0 ? (
              <div className="h-full w-full flex items-center justify-center bg-slate-50 rounded-lg text-sm text-gray-500">
                暂无数据
              </div>
            ) : (
              <ResponsiveContainer width="100%" height="100%">
                <ScatterChart margin={{ top: 20, right: 20, bottom: 20, left: 10 }}>
                  <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="#e2e8f0" />
                  <XAxis
                    dataKey="x"
                    type="number"
                    domain={domainX}
                    ticks={xTicks}
                    tickFormatter={(v) => formatBeijingHour(v)}
                    stroke="#94a3b8"
                    fontSize={12}
                    tickLine={false}
                    axisLine={false}
                  />
                  <YAxis
                    dataKey="y"
                    type="number"
                    tickFormatter={(v) => `${v.toFixed(0)}万`}
                    stroke="#94a3b8"
                    fontSize={12}
                    tickLine={false}
                    axisLine={false}
                    domain={[0, parsedYMaxWan ?? "auto"]}
                  />
                  <Tooltip
                    cursor={{ strokeDasharray: "3 3" }}
                    content={({ active, payload }) => {
                      if (!active || !payload?.length) return null;
                      const p = payload[0].payload as Point;
                      return (
                        <div className="rounded-lg bg-white border border-gray-200 shadow-lg p-3 text-xs space-y-1">
                          <div className="font-semibold text-gray-900">
                            {p.shortName}{p.isBinance ? "（币安系）" : ""}
                          </div>
                          <div className="text-gray-600">最高市值：{formatWan(p.y)}</div>
                          <div className="text-gray-600">创建时间：{formatBeijing(p.x)}</div>
                          <div className="text-gray-400 font-mono break-all">{p.address}</div>
                        </div>
                      );
                    }}
                  />
                  <Scatter data={filteredPoints} shape={DotShape} />
                </ScatterChart>
              </ResponsiveContainer>
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
