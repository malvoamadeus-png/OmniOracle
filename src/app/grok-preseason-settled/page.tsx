import React from "react";
import Link from "next/link";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { supabaseRest } from "@/lib/supabaseRest";

type MarketRow = {
  market_slug: string;
  question: string | null;
  market_url: string | null;
};

type PredictionRow = {
  market_slug: string;
  predicted_outcome: string | null;
  predicted_at: string | null;
};

type SettlementRow = {
  market_slug: string;
  resolved_outcome: string | null;
  settled_at: string | null;
  is_correct: boolean | null;
};

function formatTime(s: string | null) {
  if (!s) return "-";
  const d = new Date(s);
  const f = new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: "Asia/Shanghai",
  });
  return f.format(d).replaceAll("-", "/");
}

export default async function GrokPreseasonSettledPage() {
  const settlements = await supabaseRest<SettlementRow>({
    table: "grok_preseason_settlement",
    select: "market_slug,resolved_outcome,settled_at,is_correct",
    params: { order: "settled_at.desc", limit: "300" },
  });

  const slugs = settlements.map((x) => x.market_slug).filter(Boolean);
  const [markets, predictions] =
    slugs.length === 0
      ? [[], []]
      : await Promise.all([
          supabaseRest<MarketRow>({
            table: "grok_preseason_markets",
            select: "market_slug,question,market_url",
            params: { market_slug: `in.(${slugs.slice(0, 200).join(",")})`, limit: "300" },
          }),
          supabaseRest<PredictionRow>({
            table: "grok_preseason_predictions",
            select: "market_slug,predicted_outcome,predicted_at",
            params: { market_slug: `in.(${slugs.slice(0, 200).join(",")})`, limit: "300" },
          }),
        ]);

  const marketMap = new Map(markets.map((m) => [m.market_slug, m]));
  const predMap = new Map(predictions.map((p) => [p.market_slug, p]));

  return (
    <div className="container mx-auto p-6 max-w-6xl space-y-6">
      <div className="space-y-1">
        <h1 className="text-3xl font-bold tracking-tight">Grok 预测（已结束）</h1>
        <p className="text-gray-500">展示已结算题目的预测与结果对比</p>
      </div>

      {settlements.length === 0 ? (
        <div className="text-sm text-gray-500">暂无数据</div>
      ) : (
        <div className="space-y-4">
          {settlements.map((s) => {
            const m = marketMap.get(s.market_slug);
            const p = predMap.get(s.market_slug);
            const correctText =
              s.is_correct === null ? "未知" : s.is_correct ? "正确" : "错误";
            return (
              <Card key={s.market_slug}>
                <CardHeader>
                  <CardTitle className="text-base">
                    <div className="flex flex-col gap-1">
                      <div className="flex items-center justify-between gap-3">
                        <span className="font-semibold">{m?.question ?? s.market_slug}</span>
                        <span className="text-xs text-gray-500">{correctText}</span>
                      </div>
                      <div className="flex items-center gap-3 text-xs text-gray-500">
                        <span>结算时间：{formatTime(s.settled_at)}</span>
                        {m?.market_url ? (
                          <Link className="underline" href={m.market_url} target="_blank">
                            市场链接
                          </Link>
                        ) : null}
                      </div>
                    </div>
                  </CardTitle>
                </CardHeader>
                <CardContent className="grid gap-2 text-sm text-gray-700">
                  <div>预测：{p?.predicted_outcome ?? "UNKNOWN"}（{formatTime(p?.predicted_at ?? null)}）</div>
                  <div>结果：{s.resolved_outcome ?? "UNKNOWN"}</div>
                </CardContent>
              </Card>
            );
          })}
        </div>
      )}
    </div>
  );
}
