import { supabase } from '@/lib/supabase'
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { SmartWallet } from '@/types/smartWallet'

export const dynamic = 'force-dynamic'

async function getSmartWallets() {
  const { data, error } = await supabase
    .from('smart_wallets')
    .select('*')
    .order('total_profit', { ascending: false })

  if (error) {
    console.error('Error fetching smart wallets:', error)
    return []
  }

  return data as SmartWallet[]
}

export default async function SmartWalletsPage() {
  const wallets = await getSmartWallets()

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center justify-between">
        <h1 className="text-3xl font-bold tracking-tight text-gray-900">聪明钱地址库</h1>
        <Badge variant="outline" className="px-4 py-1 text-sm">
          共 {wallets.length} 个地址
        </Badge>
      </div>

      <Card className="border-none shadow-sm bg-white/50 backdrop-blur-sm">
        <CardHeader>
          <CardTitle>地址表现总览</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="overflow-x-auto">
            <table className="w-full text-sm text-left">
              <thead className="text-xs text-gray-500 uppercase bg-gray-50/50">
                <tr>
                  <th className="px-6 py-3 font-medium">标签 / 地址</th>
                  <th className="px-6 py-3 font-medium text-right">总盈利 (USDC)</th>
                  <th className="px-6 py-3 font-medium text-right">交易次数</th>
                  <th className="px-6 py-3 font-medium text-right">胜率</th>
                  <th className="px-6 py-3 font-medium text-right">单笔均盈</th>
                  <th className="px-6 py-3 font-medium text-right">盈利率均值</th>
                  <th className="px-6 py-3 font-medium text-right">Top5盈利占比</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {wallets.map((wallet) => (
                  <tr key={wallet.address} className="hover:bg-gray-50/50 transition-colors">
                    <td className="px-6 py-4 font-medium">
                      <div className="flex flex-col">
                        <span className="text-gray-900 font-semibold">{wallet.label || 'Unknown'}</span>
                        <span className="text-xs text-gray-400 font-mono mt-0.5 select-all">{wallet.address}</span>
                      </div>
                    </td>
                    <td className={`px-6 py-4 text-right font-bold ${wallet.total_profit >= 0 ? 'text-green-600' : 'text-red-600'}`}>
                      ${wallet.total_profit.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                    </td>
                    <td className="px-6 py-4 text-right text-gray-600">
                      {wallet.total_trades}
                    </td>
                    <td className="px-6 py-4 text-right">
                      <Badge variant={wallet.win_rate > 0.5 ? "default" : "secondary"} className={wallet.win_rate > 0.5 ? "bg-green-100 text-green-700 hover:bg-green-200" : ""}>
                        {(wallet.win_rate * 100).toFixed(1)}%
                      </Badge>
                    </td>
                    <td className="px-6 py-4 text-right text-gray-600">
                      ${wallet.avg_profit_per_trade.toFixed(2)}
                    </td>
                    <td className="px-6 py-4 text-right text-gray-600">
                      {(wallet.avg_profit_rate * 100).toFixed(1)}%
                    </td>
                    <td className="px-6 py-4 text-right text-gray-600">
                      {(wallet.top5_profit_ratio * 100).toFixed(1)}%
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </CardContent>
      </Card>
    </div>
  )
}
