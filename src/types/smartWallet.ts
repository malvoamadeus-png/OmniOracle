export interface SmartWallet {
  address: string
  label: string | null
  total_trades: number
  total_profit: number
  avg_profit_per_trade: number
  avg_profit_rate: number
  win_rate: number
  avg_total_profit: number
  top5_profit_ratio: number
  top10_profit_ratio: number
  top5_loss_ratio: number
  top10_loss_ratio: number
  updated_at: string
}
