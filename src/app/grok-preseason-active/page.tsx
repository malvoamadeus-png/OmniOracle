import Link from "next/link";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { supabaseRest } from "@/lib/supabaseRest";

type MarketRow = {
  market_slug: string;
  question: string | null;
  market_url: string | null;
  status: string | null;
  updated_at: string | null;
};

type PredictionRow = {
  market_slug: string;
  predicted_outcome: string | null;
  final_conclusion: string | null;
  evidence: string | null;
  reasoning: string | null;
  raw_model_output?: string | null;
  predicted_at: string | null;
};

type OrderRow = {
  market_slug: string | null;
  order_id: string | null;
  price: number | null;
  size_usd: number | null;
  status: string | null;
  error_message?: string | null;
  created_at: string | null;
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

export default async function GrokPreseasonActivePage() {
  const [markets, predictions, orders] = await Promise.all([
    supabaseRest<MarketRow>({
      table: "grok_preseason_markets",
      select: "market_slug,question,market_url,status,updated_at",
      params: { status: "neq.SETTLED", order: "updated_at.desc", limit: "200" },
    }),
    supabaseRest<PredictionRow>({
      table: "grok_preseason_predictions",
      select: "market_slug,predicted_outcome,final_conclusion,evidence,reasoning,raw_model_output,predicted_at",
      params: { order: "predicted_at.desc", limit: "200" },
    }),
    supabaseRest<OrderRow>({
      table: "grok_preseason_orders",
      select: "market_slug,order_id,price,size_usd,status,error_message,created_at",
      params: { order: "created_at.desc", limit: "500" },
    }),
  ]);

  const predMap = new Map<string, PredictionRow>();
  predictions.forEach((p) => {
    if (!p.market_slug) return;
    if (!predMap.has(p.market_slug)) predMap.set(p.market_slug, p);
  });

  const orderMap = new Map<string, OrderRow>();
  orders.forEach((o) => {
    if (!o.market_slug) return;
    if (!orderMap.has(o.market_slug)) orderMap.set(o.market_slug, o);
  });

  const rows: { m: MarketRow; p: PredictionRow; o: OrderRow | undefined }[] = [];
  markets.forEach((m) => {
    const p = predMap.get(m.market_slug);
    if (!p) return;
    const o = orderMap.get(m.market_slug);
    rows.push({ m, p, o });
  });

  return (
    <div className="container mx-auto p-6 max-w-6xl space-y-6">
      <div className="space-y-1">
        <h1 className="text-3xl font-bold tracking-tight">Grok 预测（进行中）</h1>
        <p className="text-gray-500">展示已通过过滤并产出 Grok 结论的题目（未结算）</p>
      </div>

      {rows.length === 0 ? (
        <div className="text-sm text-gray-500">暂无数据</div>
      ) : (
        <div className="space-y-4">
          {rows.map(({ m, p, o }) => (
            <Card key={m.market_slug}>
              <CardHeader>
                <CardTitle className="text-base">
                  <div className="flex flex-col gap-1">
                    <div className="flex items-center justify-between gap-3">
                      <span className="font-semibold">{m.question ?? m.market_slug}</span>
                      <span className="text-xs text-gray-500">{m.status ?? "-"}</span>
                    </div>
                    <div className="flex items-center gap-3 text-xs text-gray-500">
                      <span>预测时间：{formatTime(p.predicted_at)}</span>
                      <span>更新时间：{formatTime(m.updated_at)}</span>
                      {m.market_url ? (
                        <Link className="underline" href={m.market_url} target="_blank">
                          市场链接
                        </Link>
                      ) : null}
                    </div>
                  </div>
                </CardTitle>
              </CardHeader>
              <CardContent className="space-y-4">
                <div className="rounded-lg border p-3 space-y-2">
                  <div className="text-sm font-semibold">最终结论：{p.predicted_outcome ?? "UNKNOWN"}</div>
                  <div className="text-sm text-gray-700 whitespace-pre-wrap">
                    {p.final_conclusion || ""}
                  </div>
                </div>
                {p.predicted_outcome === "UNKNOWN" || (!p.evidence && !p.reasoning) ? (
                  <details className="rounded-lg border p-3 text-sm text-gray-700">
                    <summary className="font-semibold cursor-pointer select-none">
                      原始输出（用于排查格式问题）
                    </summary>
                    <div className="mt-2 whitespace-pre-wrap break-words">
                      {(p.raw_model_output || "").slice(0, 2000)}
                    </div>
                  </details>
                ) : null}
                <div className="grid gap-3 md:grid-cols-2">
                  <div className="rounded-lg border p-3">
                    <div className="text-sm font-semibold mb-2">判断证据</div>
                    <div className="text-sm text-gray-700 whitespace-pre-wrap">{p.evidence || ""}</div>
                  </div>
                  <div className="rounded-lg border p-3">
                    <div className="text-sm font-semibold mb-2">推演逻辑</div>
                    <div className="text-sm text-gray-700 whitespace-pre-wrap">{p.reasoning || ""}</div>
                  </div>
                </div>
                <div className="rounded-lg border p-3 text-sm text-gray-700">
                  <div className="font-semibold mb-2">下单信息</div>
                  {o ? (
                    <div className="grid gap-1">
                      <div>状态：{o.status ?? "-"}</div>
                      <div>订单号：{o.order_id ?? "-"}</div>
                      <div>
                        价格：{o.price ?? "-"}，金额：{o.size_usd ?? "-"} USD，时间：{formatTime(o.created_at)}
                      </div>
                      {o.status && o.status !== "SUBMITTED" && o.error_message ? (
                        <div className="text-xs text-gray-500 whitespace-pre-wrap break-words">
                          原因：{o.error_message}
                        </div>
                      ) : null}
                    </div>
                  ) : (
                    <div className="text-gray-500">暂无下单记录</div>
                  )}
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
