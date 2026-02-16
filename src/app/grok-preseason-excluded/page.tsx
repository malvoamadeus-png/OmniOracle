import Link from "next/link";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { supabaseRest } from "@/lib/supabaseRest";

type ExclusionRow = {
  market_slug: string;
  excluded_reason: string | null;
  model_used: string | null;
  raw_response: string | null;
  created_at: string | null;
};

type MarketRow = {
  market_slug: string;
  question: string | null;
  market_url: string | null;
  outcome_prices_snapshot: unknown;
  updated_at: string | null;
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

function previewJson(raw: string | null) {
  if (!raw) return "";
  if (raw.length <= 600) return raw;
  return raw.slice(0, 600) + "…";
}

export default async function GrokPreseasonExcludedPage() {
  const exclusions = await supabaseRest<ExclusionRow>({
    table: "grok_preseason_exclusions",
    select: "market_slug,excluded_reason,model_used,raw_response,created_at",
    params: { order: "created_at.desc", limit: "300" },
  });

  const slugs = exclusions.map((x) => x.market_slug).filter(Boolean);
  const markets =
    slugs.length === 0
      ? []
      : await supabaseRest<MarketRow>({
          table: "grok_preseason_markets",
          select: "market_slug,question,market_url,outcome_prices_snapshot,updated_at",
          params: { market_slug: `in.(${slugs.slice(0, 200).join(",")})`, limit: "300" },
        });

  const marketMap = new Map(markets.map((m) => [m.market_slug, m]));

  return (
    <div className="container mx-auto p-6 max-w-6xl space-y-6">
      <div className="space-y-1">
        <h1 className="text-3xl font-bold tracking-tight">Grok 预测（已排除）</h1>
        <p className="text-gray-500">展示被规则/模型排除的题目及排除原因（便于人工排查误杀）</p>
      </div>

      {exclusions.length === 0 ? (
        <div className="text-sm text-gray-500">暂无数据</div>
      ) : (
        <div className="space-y-4">
          {exclusions.map((ex) => {
            const m = marketMap.get(ex.market_slug);
            const question = m?.question ?? ex.market_slug;
            const marketUrl = m?.market_url ?? `https://polymarket.com/market/${ex.market_slug}`;
            return (
              <Card key={ex.market_slug}>
                <CardHeader>
                  <CardTitle className="text-base">
                    <div className="flex flex-col gap-1">
                      <div className="flex items-center justify-between gap-3">
                        <span className="font-semibold">{question}</span>
                        <span className="text-xs text-gray-500">{ex.excluded_reason ?? "-"}</span>
                      </div>
                      <div className="flex items-center gap-3 text-xs text-gray-500">
                        <span>排除时间：{formatTime(ex.created_at)}</span>
                        <span>来源：{ex.model_used ?? "-"}</span>
                        <Link className="underline" href={marketUrl} target="_blank">
                          市场链接
                        </Link>
                      </div>
                    </div>
                  </CardTitle>
                </CardHeader>
                <CardContent className="space-y-3">
                  <div className="rounded-lg border p-3 text-sm text-gray-700">
                    <div className="font-semibold mb-2">盘口快照</div>
                    <div className="whitespace-pre-wrap break-words">
                      {m?.outcome_prices_snapshot ? JSON.stringify(m.outcome_prices_snapshot) : "-"}
                    </div>
                  </div>
                  <div className="rounded-lg border p-3 text-sm text-gray-700">
                    <div className="font-semibold mb-2">排除证据（raw_response）</div>
                    <div className="whitespace-pre-wrap break-words">{previewJson(ex.raw_response)}</div>
                  </div>
                </CardContent>
              </Card>
            );
          })}
        </div>
      )}
    </div>
  );
}
