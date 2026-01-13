import Link from "next/link"
import { BarChart3, Database, BrainCircuit, Wallet, History, ArrowLeftRight, Twitter, Clock, MessageSquare, Coffee } from "lucide-react"

export function Sidebar() {
  return (
    <div className="flex h-screen w-64 flex-col border-r bg-gray-50/40 dark:bg-gray-900/40 dark:border-gray-800">
      <div className="flex h-14 items-center border-b px-6 dark:border-gray-800">
        <Link className="flex items-center gap-2 font-semibold dark:text-gray-200" href="/">
          <span className="">OmniOracle</span>
        </Link>
      </div>
      <div className="flex-1 overflow-auto py-4">
        <nav className="grid items-start px-2 text-sm font-medium">
          
          {/* Polymarket Section */}
          <div className="mb-4">
            <h3 className="mb-2 px-4 text-xs font-semibold uppercase text-gray-500 dark:text-gray-400">
              Polymarket
            </h3>
            <div className="space-y-1">
              <Link
                className="flex items-center gap-3 rounded-lg px-3 py-2 text-gray-500 transition-all hover:text-gray-900 hover:bg-gray-100 dark:text-gray-400 dark:hover:text-gray-50 dark:hover:bg-gray-800"
                href="/"
              >
                <BarChart3 className="h-4 w-4" />
                跟单收益榜
              </Link>
              <Link
                className="flex items-center gap-3 rounded-lg px-3 py-2 text-gray-500 transition-all hover:text-gray-900 hover:bg-gray-100 dark:text-gray-400 dark:hover:text-gray-50 dark:hover:bg-gray-800"
                href="/human-vs-ai"
              >
                <BrainCircuit className="h-4 w-4" />
                Human Vs AI 详情
              </Link>
              <Link
                className="flex items-center gap-3 rounded-lg px-3 py-2 text-gray-500 transition-all hover:text-gray-900 hover:bg-gray-100 dark:text-gray-400 dark:hover:text-gray-50 dark:hover:bg-gray-800"
                href="/human-vs-ai-settled"
              >
                <History className="h-4 w-4" />
                Human Vs AI 详情（已结算）
              </Link>
              <Link
                className="flex items-center gap-3 rounded-lg px-3 py-2 text-gray-500 transition-all hover:text-gray-900 hover:bg-gray-100 dark:text-gray-400 dark:hover:text-gray-50 dark:hover:bg-gray-800"
                href="/human-vs-ai-table"
              >
                <Database className="h-4 w-4" />
                Human VS AI 一览表
              </Link>
            </div>
          </div>

          {/* Opinion Section */}
          <div className="mb-4">
            <h3 className="mb-2 px-4 text-xs font-semibold uppercase text-gray-500 dark:text-gray-400">
              Opinion
            </h3>
            <div className="space-y-1">
              <Link
                className="flex items-center gap-3 rounded-lg px-3 py-2 text-gray-500 transition-all hover:text-gray-900 hover:bg-gray-100 dark:text-gray-400 dark:hover:text-gray-50 dark:hover:bg-gray-800"
                href="/opinion-arbitrage"
              >
                <ArrowLeftRight className="h-4 w-4" />
                Opinion 对冲
              </Link>
              <Link
                className="flex items-center gap-3 rounded-lg px-3 py-2 text-gray-500 transition-all hover:text-gray-900 hover:bg-gray-100 dark:text-gray-400 dark:hover:text-gray-50 dark:hover:bg-gray-800"
                href="/opinion-closing"
              >
                <Clock className="h-4 w-4" />
                尾盘数据
              </Link>
            </div>
          </div>

          {/* Tools Section */}
          <div>
            <h3 className="mb-2 px-4 text-xs font-semibold uppercase text-gray-500 dark:text-gray-400">
              其他工具
            </h3>
            <div className="space-y-1">
              <Link
                className="flex items-center gap-3 rounded-lg px-3 py-2 text-gray-500 transition-all hover:text-gray-900 hover:bg-gray-100 dark:text-gray-400 dark:hover:text-gray-50 dark:hover:bg-gray-800"
                href="/bundle-finder"
              >
                <Wallet className="h-4 w-4" />
                小号查询
              </Link>
              <Link
                className="flex items-center gap-3 rounded-lg px-3 py-2 text-gray-500 transition-all hover:text-gray-900 hover:bg-gray-100 dark:text-gray-400 dark:hover:text-gray-50 dark:hover:bg-gray-800"
                href="/speech-probability"
              >
                <MessageSquare className="h-4 w-4" />
                二圣发言概率
              </Link>
              <Link
                className="flex items-center gap-3 rounded-lg px-3 py-2 text-gray-500 transition-all hover:text-gray-900 hover:bg-gray-100 dark:text-gray-400 dark:hover:text-gray-50 dark:hover:bg-gray-800"
                href="/binance-speech-probability"
              >
                <MessageSquare className="h-4 w-4" />
                币安系发言概率
              </Link>
            </div>
          </div>

        </nav>
      </div>
      
      {/* Footer with X/Twitter Link */}
      <div className="border-t p-4 space-y-4 dark:border-gray-800">
        <Link
          href="https://x.com/Assassin_Malvo"
          target="_blank"
          className="flex items-center gap-2 text-sm text-gray-500 hover:text-gray-900 transition-colors dark:text-gray-400 dark:hover:text-gray-50"
        >
          <Twitter className="h-4 w-4" />
          <span className="font-medium">By 南枳</span>
        </Link>

        <div className="space-y-1">
          <div className="flex items-center gap-2 text-xs font-semibold text-gray-500 uppercase dark:text-gray-400">
            <Coffee className="h-3 w-3" />
            <span>Buy Me a Coffee</span>
          </div>
          <div className="rounded bg-gray-100 p-2 text-[10px] text-gray-500 font-mono break-all border border-gray-200 select-all dark:bg-gray-900 dark:text-gray-300 dark:border-gray-800">
            0x30b4301e844f7432b8694b6bb92894c0b91746d1
          </div>
        </div>
      </div>
    </div>
  )
}
