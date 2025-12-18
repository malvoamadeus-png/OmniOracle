"use client"

import * as React from "react"
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { ChevronDown, ChevronRight, Bot, User, Trophy, CheckCircle2, XCircle } from "lucide-react"
import { cn } from "@/lib/utils"
import { supabase } from "@/lib/supabase"

interface AIPrediction {
  slug: string
  title: string
  question: string
  ai_outcome: string
  ai_reasoning: string
  human_outcome?: string
  grok_outcome?: string
  grok_reasoning?: string
  market_status?: string
  real_outcome?: string
  is_excluded?: boolean
}

export default function HumanVsAISettledPage() {
  const [expandedItems, setExpandedItems] = React.useState<Set<string>>(new Set())
  const [expandedAIReasoning, setExpandedAIReasoning] = React.useState<Set<string>>(new Set())
  const [predictions, setPredictions] = React.useState<AIPrediction[]>([])
  const [loading, setLoading] = React.useState(true)

  React.useEffect(() => {
    async function fetchPredictions() {
      try {
        const { data, error } = await supabase
          .from("ai_predictions")
          .select("*")
          .eq("market_status", "CLOSED")
          .order("title")
        
        if (error) throw error
        
        if (data) {
          setPredictions(data)
        }
      } catch (err) {
        console.error("Failed to fetch settled predictions:", err)
      } finally {
        setLoading(false)
      }
    }
    fetchPredictions()
  }, [])

  const toggleItem = (slug: string) => {
    const newExpanded = new Set(expandedItems)
    if (newExpanded.has(slug)) {
      newExpanded.delete(slug)
    } else {
      newExpanded.add(slug)
    }
    setExpandedItems(newExpanded)
  }

  const toggleAIReasoning = (slug: string, e: React.MouseEvent) => {
    e.stopPropagation()
    const newExpanded = new Set(expandedAIReasoning)
    if (newExpanded.has(slug)) {
      newExpanded.delete(slug)
    } else {
      newExpanded.add(slug)
    }
    setExpandedAIReasoning(newExpanded)
  }

  // Helper to determine if a prediction was correct
  const isCorrect = (prediction?: string, real?: string) => {
    if (!prediction || !real || real === "Unknown" || real === "Parse Error") return null
    return prediction.toLowerCase().includes(real.toLowerCase())
  }

  if (loading) {
    return <div className="p-8 text-center text-muted-foreground">Loading settled events...</div>
  }

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Trophy className="h-6 w-6 text-yellow-500" />
            Human Vs AI 详情（已结算）
            <span className="ml-2 text-sm font-normal text-muted-foreground">
              ({predictions.length} records)
            </span>
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="space-y-2">
            {predictions.length === 0 ? (
              <div className="text-center py-8 text-muted-foreground">
                No settled events found.
              </div>
            ) : (
              predictions.map((prediction) => {
                const isExpanded = expandedItems.has(prediction.slug)
                const isReasoningExpanded = expandedAIReasoning.has(prediction.slug)
                
                const humanCorrect = isCorrect(prediction.human_outcome, prediction.real_outcome)
                const aiCorrect = isCorrect(prediction.ai_outcome, prediction.real_outcome)
                const grokCorrect = isCorrect(prediction.grok_outcome, prediction.real_outcome)

                return (
                  <div
                    key={prediction.slug}
                    className="rounded-lg border bg-card text-card-foreground shadow-sm transition-all"
                  >
                    {/* Level 1: Menu Title */}
                    <div
                      className="flex cursor-pointer items-center justify-between p-4 hover:bg-muted/50"
                      onClick={() => toggleItem(prediction.slug)}
                    >
                      <div className="flex items-center gap-3 overflow-hidden">
                        {isExpanded ? (
                          <ChevronDown className="h-4 w-4 text-muted-foreground flex-shrink-0" />
                        ) : (
                          <ChevronRight className="h-4 w-4 text-muted-foreground flex-shrink-0" />
                        )}
                        <span className="font-medium truncate">{prediction.title || prediction.question}</span>
                      </div>
                      <div className="ml-auto flex items-center gap-2 flex-shrink-0">
                         <span className="text-sm text-muted-foreground mr-2">
                           Real Result: <span className={cn(
                             "font-bold",
                             prediction.real_outcome === "Yes" ? "text-green-600" :
                             prediction.real_outcome === "No" ? "text-red-600" : ""
                           )}>{prediction.real_outcome}</span>
                         </span>
                        <Badge variant="secondary">
                          Settled
                        </Badge>
                      </div>
                    </div>

                    {/* Level 2: Expanded Details */}
                    {isExpanded && (
                      <div className="border-t bg-muted/20 p-4">
                        
                        {/* Real Result Highlight */}
                        <div className="mb-4 p-3 bg-background border rounded-md flex items-center justify-between">
                            <div className="flex items-center gap-2">
                                <Trophy className="h-5 w-5 text-yellow-500" />
                                <span className="font-semibold">Real Outcome:</span>
                            </div>
                            <span className={cn(
                                "text-lg font-bold",
                                prediction.real_outcome === "Yes" ? "text-green-600" :
                                prediction.real_outcome === "No" ? "text-red-600" : ""
                            )}>
                                {prediction.real_outcome}
                            </span>
                        </div>

                        <div className="grid gap-4 md:grid-cols-3">
                          {/* Human Prediction */}
                          <Card className={cn("bg-background/50", humanCorrect === true ? "border-green-500/50 bg-green-50/50" : humanCorrect === false ? "border-red-500/50 bg-red-50/50" : "")}>
                            <CardHeader className="pb-2">
                              <CardTitle className="flex items-center gap-2 text-sm font-medium text-muted-foreground">
                                <User className="h-4 w-4" />
                                Human Prediction
                                {humanCorrect === true && <CheckCircle2 className="h-4 w-4 text-green-600 ml-auto" />}
                                {humanCorrect === false && <XCircle className="h-4 w-4 text-red-600 ml-auto" />}
                              </CardTitle>
                            </CardHeader>
                            <CardContent>
                              <div className={cn(
                                "text-2xl font-bold",
                                prediction.human_outcome === "Yes" ? "text-green-600" : 
                                prediction.human_outcome === "No" ? "text-red-600" : "text-primary"
                              )}>
                                {prediction.human_outcome || "N/A"}
                              </div>
                              <p className="text-xs text-muted-foreground mt-1">
                                Based on active position
                              </p>
                            </CardContent>
                          </Card>

                          {/* AI Prediction (Gemini) */}
                          <Card className={cn("bg-background/50", aiCorrect === true ? "border-green-500/50 bg-green-50/50" : aiCorrect === false ? "border-red-500/50 bg-red-50/50" : "")}>
                            <CardHeader className="pb-2">
                              <CardTitle className="flex items-center gap-2 text-sm font-medium text-muted-foreground">
                                <Bot className="h-4 w-4" />
                                Gemini 2.5 Pro
                                {aiCorrect === true && <CheckCircle2 className="h-4 w-4 text-green-600 ml-auto" />}
                                {aiCorrect === false && <XCircle className="h-4 w-4 text-red-600 ml-auto" />}
                              </CardTitle>
                            </CardHeader>
                            <CardContent>
                              <div className="flex items-center justify-between">
                                <div className={cn(
                                  "text-2xl font-bold",
                                  prediction.ai_outcome === "Yes" ? "text-green-600" :
                                  prediction.ai_outcome === "No" ? "text-red-600" : "text-blue-600"
                                )}>
                                  {prediction.ai_outcome || "Analyzing..."}
                                </div>
                                <button
                                  onClick={(e) => toggleAIReasoning(prediction.slug, e)}
                                  className="flex items-center gap-1 text-xs text-blue-500 hover:underline"
                                >
                                  {isReasoningExpanded ? "Hide Reasoning" : "View Reasoning"}
                                  {isReasoningExpanded ? (
                                    <ChevronDown className="h-3 w-3" />
                                  ) : (
                                    <ChevronRight className="h-3 w-3" />
                                  )}
                                </button>
                              </div>
                            </CardContent>
                          </Card>

                          {/* AI Prediction (Grok) */}
                          <Card className={cn("bg-background/50", grokCorrect === true ? "border-green-500/50 bg-green-50/50" : grokCorrect === false ? "border-red-500/50 bg-red-50/50" : "")}>
                            <CardHeader className="pb-2">
                              <CardTitle className="flex items-center gap-2 text-sm font-medium text-muted-foreground">
                                <Bot className="h-4 w-4" />
                                Grok
                                {grokCorrect === true && <CheckCircle2 className="h-4 w-4 text-green-600 ml-auto" />}
                                {grokCorrect === false && <XCircle className="h-4 w-4 text-red-600 ml-auto" />}
                              </CardTitle>
                            </CardHeader>
                            <CardContent>
                              <div className="flex items-center justify-between">
                                <div className={cn(
                                  "text-2xl font-bold",
                                  prediction.grok_outcome === "Yes" ? "text-green-600" :
                                  prediction.grok_outcome === "No" ? "text-red-600" : "text-purple-600"
                                )}>
                                  {prediction.grok_outcome || "Analyzing..."}
                                </div>
                                {prediction.grok_reasoning && (
                                  <button
                                    onClick={(e) => toggleAIReasoning(prediction.slug, e)}
                                    className="flex items-center gap-1 text-xs text-blue-500 hover:underline"
                                  >
                                    {isReasoningExpanded ? "Hide Reasoning" : "View Reasoning"}
                                    {isReasoningExpanded ? (
                                      <ChevronDown className="h-3 w-3" />
                                    ) : (
                                      <ChevronRight className="h-3 w-3" />
                                    )}
                                  </button>
                                )}
                              </div>
                            </CardContent>
                          </Card>
                        </div>

                        {/* Level 3: AI Reasoning */}
                        {isReasoningExpanded && (
                          <div className="mt-4 grid gap-4 md:grid-cols-2 animate-in slide-in-from-top-2 fade-in duration-200">
                            <div className="rounded-md bg-muted p-4">
                              <h4 className="mb-2 font-semibold flex items-center gap-2 text-blue-600">
                                <Bot className="h-4 w-4" />
                                Gemini Reasoning
                              </h4>
                              <p className="text-sm leading-relaxed whitespace-pre-wrap text-muted-foreground">
                                {prediction.ai_reasoning || "No reasoning available."}
                              </p>
                            </div>
                            
                            <div className="rounded-md bg-muted p-4">
                              <h4 className="mb-2 font-semibold flex items-center gap-2 text-purple-600">
                                <Bot className="h-4 w-4" />
                                Grok Reasoning
                              </h4>
                              <p className="text-sm leading-relaxed whitespace-pre-wrap text-muted-foreground">
                                {prediction.grok_reasoning || "No reasoning available."}
                              </p>
                            </div>
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                )
              })
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  )
}
