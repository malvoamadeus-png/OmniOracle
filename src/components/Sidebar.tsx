import Link from "next/link"
import { BarChart3, Database, BrainCircuit, Wallet, History, ArrowLeftRight } from "lucide-react"

export function Sidebar() {
  return (
    <div className="flex h-screen w-64 flex-col border-r bg-gray-50/40">
      <div className="flex h-14 items-center border-b px-6">
        <Link className="flex items-center gap-2 font-semibold" href="/">
          <span className="">OmniOracle</span>
        </Link>
      </div>
      <div className="flex-1 overflow-auto py-2">
        <nav className="grid items-start px-4 text-sm font-medium">
          <Link
            className="flex items-center gap-3 rounded-lg px-3 py-2 text-gray-500 transition-all hover:text-gray-900 hover:bg-gray-100"
            href="/"
          >
            <BarChart3 className="h-4 w-4" />
            跟单收益榜
          </Link>
          <Link
            className="flex items-center gap-3 rounded-lg px-3 py-2 text-gray-500 transition-all hover:text-gray-900 hover:bg-gray-100"
            href="/smart-wallets"
          >
            <Wallet className="h-4 w-4" />
            聪明钱地址库
          </Link>
          <Link
            className="flex items-center gap-3 rounded-lg px-3 py-2 text-gray-500 transition-all hover:text-gray-900 hover:bg-gray-100"
            href="/opinion-arbitrage"
          >
            <ArrowLeftRight className="h-4 w-4" />
            Opinion套利
          </Link>
          <Link
            className="flex items-center gap-3 rounded-lg px-3 py-2 text-gray-500 transition-all hover:text-gray-900 hover:bg-gray-100"
            href="/human-vs-ai"
          >
            <BrainCircuit className="h-4 w-4" />
            Human Vs AI 详情
          </Link>
          <Link
            className="flex items-center gap-3 rounded-lg px-3 py-2 text-gray-500 transition-all hover:text-gray-900 hover:bg-gray-100"
            href="/human-vs-ai-settled"
          >
            <History className="h-4 w-4" />
            Human Vs AI 详情（已结算）
          </Link>
          <Link
            className="flex items-center gap-3 rounded-lg px-3 py-2 text-gray-500 transition-all hover:text-gray-900 hover:bg-gray-100"
            href="/human-vs-ai-table"
          >
            <Database className="h-4 w-4" />
            Human VS AI 一览表
          </Link>
        </nav>
      </div>
    </div>
  )
}
