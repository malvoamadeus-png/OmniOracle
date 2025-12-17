import { supabase } from "@/lib/supabase"
import { DashboardTable } from "@/components/DashboardTable"
import { Trade, TraderStats } from "@/types"

export const revalidate = 0 // Disable caching for real-time data

async function getTrades() {
  const { data, error } = await supabase
    .from("trades")
    .select("*")
    .order("timestamp", { ascending: false })

  if (error) {
    console.error("Error fetching trades:", error)
    return []
  }

  return data as Trade[]
}

export default async function Home() {
  const trades = await getTrades()

  // Group by proxy_wallet (label)
  const statsMap = new Map<string, TraderStats>()

  trades.forEach((trade) => {
    const key = trade.proxy_wallet
    if (!statsMap.has(key)) {
      statsMap.set(key, {
        label: trade.label || "未知",
        proxy_wallet: key,
        total_trades: 0,
        total_invested: 0,
        total_realized_pnl: 0,
        trades: [],
      })
    }

    const stat = statsMap.get(key)!
    stat.total_trades += 1
    stat.total_invested += trade.invested_amount
    stat.total_realized_pnl += trade.realized_pnl
    stat.trades.push(trade)
  })

  // Convert map to array and sort by PnL descending
  const stats = Array.from(statsMap.values()).sort(
    (a, b) => b.total_realized_pnl - a.total_realized_pnl
  )

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold tracking-tight">Dashboard</h1>
      </div>
      
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
        <div className="rounded-xl border bg-card text-card-foreground shadow p-6">
            <div className="text-sm font-medium text-muted-foreground">总已实现盈亏</div>
            <div className="text-2xl font-bold mt-2">
                ${stats.reduce((acc, curr) => acc + curr.total_realized_pnl, 0).toFixed(2)}
            </div>
        </div>
        <div className="rounded-xl border bg-card text-card-foreground shadow p-6">
            <div className="text-sm font-medium text-muted-foreground">总投入金额</div>
            <div className="text-2xl font-bold mt-2">
                ${stats.reduce((acc, curr) => acc + curr.total_invested, 0).toFixed(2)}
            </div>
        </div>
        <div className="rounded-xl border bg-card text-card-foreground shadow p-6">
            <div className="text-sm font-medium text-muted-foreground">跟单总数</div>
            <div className="text-2xl font-bold mt-2">
                {trades.length}
            </div>
        </div>
      </div>

      <DashboardTable data={stats} />
    </div>
  )
}
