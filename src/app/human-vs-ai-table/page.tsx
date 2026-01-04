"use client"

import * as React from "react"
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { Badge } from "@/components/ui/badge"
import { CheckCircle2, XCircle, HelpCircle, Bot, User, Trophy } from "lucide-react"
import { cn } from "@/lib/utils"
import { supabase } from "@/lib/supabase"

export default function HumanVsAITablePage() {
  const [aiPredictions, setAiPredictions] = React.useState<any[]>([])
  const [statusFilter, setStatusFilter] = React.useState<string>("all")

  React.useEffect(() => {
    async function fetchPredictions() {
      const { data, error } = await supabase
        .from("ai_predictions")
        .select("*")
      
      if (data) {
        setAiPredictions(data)
      } else {
        console.error("Failed to fetch predictions:", error)
      }
    }
    fetchPredictions()
  }, [])
  
  // Helper to determine status color
  const getStatusBadge = (status: string, outcome: string) => {
    if (status === "CLOSED") {
      if (outcome === "Yes") {
        return <Badge className="bg-green-600 hover:bg-green-700">{outcome}</Badge>
      }
      if (outcome === "No") {
        return <Badge className="bg-red-600 hover:bg-red-700">{outcome}</Badge>
      }
      return <Badge variant="secondary">{outcome}</Badge>
    }
    return <Badge variant="outline" className="text-muted-foreground">Pending</Badge>
  }

  const filteredPredictions = React.useMemo(() => {
    return aiPredictions.filter((item: any) => {
      // Basic exclusions
      if (item.is_excluded) return false
      
      // NEW: Exclude items where NO AI made a prediction
      const hasGemini = item.ai_outcome && item.ai_outcome.trim() !== "" && item.ai_outcome !== "Unknown";
      const hasGrok = item.grok_outcome && item.grok_outcome.trim() !== "" && item.grok_outcome !== "Unknown";
      const hasDoubao = item.doubao_outcome && item.doubao_outcome.trim() !== "" && item.doubao_outcome !== "Unknown";
      if (!hasGemini && !hasGrok && !hasDoubao) return false;

      // Status filter
      if (statusFilter === "all") return true
      if (statusFilter === "pending") return item.market_status !== "CLOSED"
      if (statusFilter === "settled") return item.market_status === "CLOSED"
      if (statusFilter === "settled_exclusive") {
        if (item.market_status !== "CLOSED") return false
        // Exclude if human_price >= 0.97
        if (item.human_price && item.human_price >= 0.97) return false
        return true
      }
      
      return true
    })
  }, [aiPredictions, statusFilter])

  // Helper to check if prediction was correct (only if closed)
  const isPredictionCorrect = (prediction: string, realOutcome: string) => {
    if (!prediction || !realOutcome) return false
    const normalizedPred = prediction.toLowerCase().trim()
    const normalizedReal = realOutcome.toLowerCase().trim()
    
    // Special handling for "Yes" / "No"
    if (normalizedReal === "yes" || normalizedReal === "no") {
        return normalizedPred === normalizedReal
    }

    // For other cases, try inclusion
    return normalizedPred.includes(normalizedReal) || normalizedReal.includes(normalizedPred)
  }

  const winRates = React.useMemo(() => {
    // Only consider rows visible in the current filtered view
    // (though 'filteredPredictions' already respects statusFilter, 
    // we need to further filter for 'CLOSED' to calculate win rates meaningfully)
    const closedItems = filteredPredictions.filter((item: any) => 
      item.market_status === "CLOSED" && 
      item.real_outcome && 
      item.real_outcome !== "Unknown" && 
      item.real_outcome !== "Parse Error"
    )
    
    // For denominator: we can either use total closed events (common denominator)
    // or per-agent denominator (only count if they made a prediction).
    // Let's use per-agent denominator to be fair (don't penalize for missing predictions).
    
    const calcRate = (field: string) => {
      // Valid attempts: closed events where this agent actually made a non-empty prediction
      const validAttempts = closedItems.filter((item: any) => 
        item[field] && item[field].trim() !== "" && item[field] !== "Unknown"
      )
      
      if (validAttempts.length === 0) return { rate: "0.0", correct: 0, total: 0 }

      const correctCount = validAttempts.filter((item: any) => 
        isPredictionCorrect(item[field], item.real_outcome)
      ).length
      
      return {
          rate: ((correctCount / validAttempts.length) * 100).toFixed(1),
          correct: correctCount,
          total: validAttempts.length
      }
    }

    return {
      human: calcRate('human_outcome'),
      gemini: calcRate('ai_outcome'),
      grok: calcRate('grok_outcome'),
      doubao: calcRate('doubao_outcome'),
      total: closedItems.length
    }
  }, [filteredPredictions])

  const getPredictionStatus = (prediction: string, realOutcome: string, status: string) => {
    // If prediction is missing or empty, do not show any status icon
    if (!prediction || prediction.trim() === "") {
        return null
    }

    if (status !== "CLOSED" || realOutcome === "Unknown" || realOutcome === "Parse Error") {
      return null // No judgment yet
    }
    
    if (isPredictionCorrect(prediction, realOutcome)) {
        return <CheckCircle2 className="h-5 w-5 text-green-500" />
    }
    return <XCircle className="h-5 w-5 text-red-500" />
  }

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader>
          <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
            <CardTitle className="flex items-center gap-2">
              <Trophy className="h-6 w-6 text-yellow-500" />
              Human VS AI Performance Overview
            </CardTitle>
            <div className="flex items-center gap-2">
              <label htmlFor="table-status-filter" className="text-sm font-medium">
                Filter:
              </label>
              <select
                id="table-status-filter"
                value={statusFilter}
                onChange={(e) => setStatusFilter(e.target.value)}
                className="h-9 rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-sm transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
              >
                <option value="all">All</option>
                <option value="pending">Pending</option>
                <option value="settled">Settled</option>
                <option value="settled_exclusive">Settled (Exclusive)</option>
              </select>
            </div>
          </div>
        </CardHeader>
        <CardContent>
          <div className="rounded-md border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-[400px]">Event Title</TableHead>
                  <TableHead className="w-[120px]">Real Result</TableHead>
                  <TableHead className="text-center">
                    <div className="flex flex-col items-center gap-1">
                      <div className="flex items-center gap-1 text-primary">
                        <User className="h-4 w-4" />
                        Human
                      </div>
                      <span className="text-xs font-normal text-muted-foreground">Active Position</span>
                    </div>
                  </TableHead>
                  <TableHead className="text-center">
                    <div className="flex flex-col items-center gap-1">
                      <div className="flex items-center gap-1 text-blue-600">
                        <Bot className="h-4 w-4" />
                        Gemini 2.5
                      </div>
                      <span className="text-xs font-normal text-muted-foreground">AI Agent</span>
                    </div>
                  </TableHead>
                  <TableHead className="text-center">
                    <div className="flex flex-col items-center gap-1">
                      <div className="flex items-center gap-1 text-purple-600">
                        <Bot className="h-4 w-4" />
                        Grok
                      </div>
                      <span className="text-xs font-normal text-muted-foreground">AI Agent</span>
                    </div>
                  </TableHead>
                  <TableHead className="text-center">
                    <div className="flex flex-col items-center gap-1">
                      <div className="flex items-center gap-1 text-orange-600">
                        <Bot className="h-4 w-4" />
                        Doubao
                      </div>
                      <span className="text-xs font-normal text-muted-foreground">AI Agent</span>
                    </div>
                  </TableHead>
                </TableRow>
                {/* Win Rate Summary Row */}
                <TableRow className="bg-muted/50 hover:bg-muted/50">
                  <TableHead className="font-bold text-gray-700">Win Rate Summary (Total Closed: {winRates.total})</TableHead>
                  <TableHead></TableHead>
                  <TableHead className="text-center font-bold text-primary">
                    <div className="flex flex-col items-center">
                        <span className="text-lg">{winRates.human.rate}%</span>
                        <span className="text-xs font-normal text-muted-foreground">({winRates.human.correct}/{winRates.human.total})</span>
                    </div>
                  </TableHead>
                  <TableHead className="text-center font-bold text-blue-600">
                    <div className="flex flex-col items-center">
                        <span className="text-lg">{winRates.gemini.rate}%</span>
                        <span className="text-xs font-normal text-muted-foreground">({winRates.gemini.correct}/{winRates.gemini.total})</span>
                    </div>
                  </TableHead>
                  <TableHead className="text-center font-bold text-purple-600">
                    <div className="flex flex-col items-center">
                        <span className="text-lg">{winRates.grok.rate}%</span>
                        <span className="text-xs font-normal text-muted-foreground">({winRates.grok.correct}/{winRates.grok.total})</span>
                    </div>
                  </TableHead>
                  <TableHead className="text-center font-bold text-orange-600">
                    <div className="flex flex-col items-center">
                        <span className="text-lg">{winRates.doubao.rate}%</span>
                        <span className="text-xs font-normal text-muted-foreground">({winRates.doubao.correct}/{winRates.doubao.total})</span>
                    </div>
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {filteredPredictions.map((item: any) => (
                  <TableRow key={item.slug}>
                    <TableCell className="font-medium">
                      <div className="flex flex-col gap-1">
                        <span>{item.title || item.question}</span>
                        <span className="text-xs text-muted-foreground truncate max-w-[380px]">
                            {item.question !== item.title ? item.question : ""}
                        </span>
                      </div>
                    </TableCell>
                    <TableCell>
                      {getStatusBadge(item.market_status, item.real_outcome)}
                    </TableCell>
                    <TableCell className="text-center">
                        <div className="flex items-center justify-center gap-2">
                            <span className={cn(
                              "font-bold",
                              item.human_outcome?.trim().toLowerCase() === "yes" ? "text-green-600" :
                              item.human_outcome?.trim().toLowerCase() === "no" ? "text-red-600" : ""
                            )}>{item.human_outcome || "-"}</span>
                            {getPredictionStatus(item.human_outcome, item.real_outcome, item.market_status)}
                        </div>
                    </TableCell>
                    <TableCell className="text-center">
                        <div className="flex items-center justify-center gap-2">
                            <span className={cn(
                              "font-bold",
                              item.ai_outcome?.trim().toLowerCase() === "yes" ? "text-green-600" :
                              item.ai_outcome?.trim().toLowerCase() === "no" ? "text-red-600" : "text-blue-600"
                            )}>{item.ai_outcome || "-"}</span>
                            {getPredictionStatus(item.ai_outcome, item.real_outcome, item.market_status)}
                        </div>
                    </TableCell>
                    <TableCell className="text-center">
                        <div className="flex items-center justify-center gap-2">
                            <span className={cn(
                              "font-bold",
                              item.grok_outcome?.trim().toLowerCase() === "yes" ? "text-green-600" :
                              item.grok_outcome?.trim().toLowerCase() === "no" ? "text-red-600" : "text-purple-600"
                            )}>{item.grok_outcome || "-"}</span>
                            {getPredictionStatus(item.grok_outcome, item.real_outcome, item.market_status)}
                        </div>
                    </TableCell>
                    <TableCell className="text-center">
                        <div className="flex items-center justify-center gap-2">
                            <span className={cn(
                              "font-bold",
                              item.doubao_outcome?.trim().toLowerCase() === "yes" ? "text-green-600" :
                              item.doubao_outcome?.trim().toLowerCase() === "no" ? "text-red-600" : "text-orange-600"
                            )}>{item.doubao_outcome || "-"}</span>
                            {getPredictionStatus(item.doubao_outcome, item.real_outcome, item.market_status)}
                        </div>
                    </TableCell>
                  </TableRow>
                ))}
                {filteredPredictions.length === 0 && (
                  <TableRow>
                    <TableCell colSpan={6} className="h-24 text-center">
                      No data available.
                    </TableCell>
                  </TableRow>
                )}
              </TableBody>
            </Table>
          </div>
        </CardContent>
      </Card>
    </div>
  )
}
