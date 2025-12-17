export interface Trade {
  id: string
  proxy_wallet: string
  label: string | null
  condition_id: string
  asset_id: string | null
  title: string | null
  invested_amount: number
  realized_pnl: number
  status: 'OPEN' | 'CLOSED'
  timestamp: string | null
  created_at: string
}

export interface TraderStats {
  label: string
  proxy_wallet: string
  total_trades: number
  total_invested: number
  total_realized_pnl: number
  trades: Trade[]
}
