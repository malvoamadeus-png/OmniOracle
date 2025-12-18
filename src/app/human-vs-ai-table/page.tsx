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
      if (item.is_excluded) return false
      
      if (statusFilter === "all") return true
      if (statusFilter === "pending") return item.market_status !== "CLOSED"
      if (statusFilter === "settled") return item.market_status === "CLOSED"
      
      return true
    })
  }, [aiPredictions, statusFilter])

  // Helper to check if prediction was correct (only if closed)
  const getPredictionStatus = (prediction: string, realOutcome: string, status: string) => {
    if (status !== "CLOSED" || realOutcome === "Unknown" || realOutcome === "Parse Error") {
      return null // No judgment yet
    }
    
    // Simple string matching
    const normalizedPred = prediction?.toLowerCase()
    const normalizedReal = realOutcome?.toLowerCase()
    
    if (!normalizedPred) return <span className="text-gray-300">-</span>
    
    if (normalizedPred.includes(normalizedReal)) {
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
                  </TableRow>
                ))}
                {filteredPredictions.length === 0 && (
                  <TableRow>
                    <TableCell colSpan={5} className="h-24 text-center">
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
