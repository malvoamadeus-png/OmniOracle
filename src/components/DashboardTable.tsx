"use client"

import * as React from "react"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { ChevronDown, ChevronRight, ExternalLink } from "lucide-react"
import { Trade, TraderStats } from "@/types"
import { cn } from "@/lib/utils"

interface DashboardTableProps {
  data: TraderStats[]
}

export function DashboardTable({ data }: DashboardTableProps) {
  const [expandedRows, setExpandedRows] = React.useState<Set<string>>(new Set())

  const toggleRow = (wallet: string) => {
    const newExpanded = new Set(expandedRows)
    if (newExpanded.has(wallet)) {
      newExpanded.delete(wallet)
    } else {
      newExpanded.add(wallet)
    }
    setExpandedRows(newExpanded)
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>跟单收益榜</CardTitle>
      </CardHeader>
      <CardContent>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="w-[50px]"></TableHead>
              <TableHead>跟单对象</TableHead>
              <TableHead>跟单总次数</TableHead>
              <TableHead className="text-right">总交易额 (USDC)</TableHead>
              <TableHead className="text-right">总已实现盈亏 (USDC)</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {data.map((trader) => (
              <React.Fragment key={trader.proxy_wallet}>
                <TableRow
                  className="cursor-pointer"
                  onClick={() => toggleRow(trader.proxy_wallet)}
                >
                  <TableCell>
                    {expandedRows.has(trader.proxy_wallet) ? (
                      <ChevronDown className="h-4 w-4" />
                    ) : (
                      <ChevronRight className="h-4 w-4" />
                    )}
                  </TableCell>
                  <TableCell className="font-medium">
                    <div className="flex flex-col">
                      <span>{trader.label || "未知"}</span>
                      <span className="text-xs text-muted-foreground font-mono">
                        {trader.proxy_wallet.slice(0, 6)}...{trader.proxy_wallet.slice(-4)}
                      </span>
                    </div>
                  </TableCell>
                  <TableCell>{trader.total_trades}</TableCell>
                  <TableCell className="text-right">
                    ${trader.total_invested.toFixed(2)}
                  </TableCell>
                  <TableCell
                    className={cn(
                      "text-right font-bold",
                      trader.total_realized_pnl >= 0
                        ? "text-green-600"
                        : "text-red-600"
                    )}
                  >
                    {trader.total_realized_pnl >= 0 ? "+" : ""}
                    ${trader.total_realized_pnl.toFixed(2)}
                  </TableCell>
                </TableRow>
                {expandedRows.has(trader.proxy_wallet) && (
                  <TableRow>
                    <TableCell colSpan={5} className="bg-muted/50 p-4">
                      <div className="rounded-md border bg-background">
                        <Table>
                          <TableHeader>
                            <TableRow>
                              <TableHead>时间</TableHead>
                              <TableHead>事件标题</TableHead>
                              <TableHead className="text-right">投入</TableHead>
                              <TableHead className="text-right">盈亏 (Realized/Cash)</TableHead>
                              <TableHead className="text-center">状态</TableHead>
                              <TableHead className="w-[50px]"></TableHead>
                            </TableRow>
                          </TableHeader>
                          <TableBody>
                            {trader.trades.map((trade) => (
                              <TableRow key={trade.id}>
                                <TableCell className="text-xs text-muted-foreground whitespace-nowrap">
                                  {trade.timestamp
                                    ? new Date(trade.timestamp).toLocaleString()
                                    : "-"}
                                </TableCell>
                                <TableCell className="max-w-[300px] truncate" title={trade.title || ""}>
                                  {trade.title || "无标题"}
                                </TableCell>
                                <TableCell className="text-right font-mono text-xs">
                                  ${trade.invested_amount.toFixed(2)}
                                </TableCell>
                                <TableCell
                                  className={cn(
                                    "text-right font-mono text-xs font-medium",
                                    trade.realized_pnl >= 0
                                      ? "text-green-600"
                                      : "text-red-600"
                                  )}
                                >
                                  {trade.realized_pnl >= 0 ? "+" : ""}
                                  {trade.realized_pnl.toFixed(2)}
                                </TableCell>
                                <TableCell className="text-center">
                                  <Badge
                                    variant={
                                      trade.status === "OPEN"
                                        ? "default"
                                        : "secondary"
                                    }
                                    className={cn(
                                      "text-[10px]",
                                      trade.status === "OPEN" && "bg-blue-100 text-blue-700 hover:bg-blue-200 border-blue-200"
                                    )}
                                  >
                                    {trade.status}
                                  </Badge>
                                </TableCell>
                                <TableCell>
                                  <a
                                    href={`https://polymarket.com/event/${trade.condition_id}`} // Polymarket URL structure guess, or use link if available
                                    target="_blank"
                                    rel="noreferrer"
                                    className="text-muted-foreground hover:text-primary"
                                  >
                                    <ExternalLink className="h-3 w-3" />
                                  </a>
                                </TableCell>
                              </TableRow>
                            ))}
                          </TableBody>
                        </Table>
                      </div>
                    </TableCell>
                  </TableRow>
                )}
              </React.Fragment>
            ))}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  )
}
