"use client"

import * as React from "react"
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { ChevronDown, ChevronRight, ExternalLink, Bot, User } from "lucide-react"
import { cn } from "@/lib/utils"

// Import data directly (assuming it's available at build time/runtime)
// In a real app, this might come from an API
import activePositions from "@/data/active_positions.json"
// We need to handle the case where ai_predictions.json might not exist yet
let aiPredictions: any[] = []
try {
  aiPredictions = require("@/data/ai_predictions.json")
} catch (e) {
  aiPredictions = []
}

interface AIPrediction {
  slug: string
  question: string
  ai_outcome: string
  ai_reasoning: string
  citations: any[]
}

export default function HumanVsAIPage() {
  const [expandedItems, setExpandedItems] = React.useState<Set<string>>(new Set())
  const [expandedAIReasoning, setExpandedAIReasoning] = React.useState<Set<string>>(new Set())

  // Create a map for fast lookup
  const aiMap = React.useMemo(() => {
    const map = new Map<string, AIPrediction>()
    aiPredictions.forEach((p: any) => {
        // Filter out excluded items
        if (!p.is_excluded) {
            map.set(p.slug, p)
        }
    })
    return map
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

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Bot className="h-6 w-6" />
            Human Vs AI Predictions
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="space-y-2">
            {uniqueActivePositions.filter((pos: any) => aiMap.has(pos.slug)).map((position: any) => {
              const aiData = aiMap.get(position.slug)
              const isExpanded = expandedItems.has(position.slug)
              const isReasoningExpanded = expandedAIReasoning.has(position.slug)

              return (
                <div
                  key={position.slug}
                  className="rounded-lg border bg-card text-card-foreground shadow-sm transition-all"
                >
                  {/* Level 1: Menu Title */}
                  <div
                    className="flex cursor-pointer items-center justify-between p-4 hover:bg-muted/50"
                    onClick={() => toggleItem(position.slug)}
                  >
                    <div className="flex items-center gap-3">
                      {isExpanded ? (
                        <ChevronDown className="h-4 w-4 text-muted-foreground" />
                      ) : (
                        <ChevronRight className="h-4 w-4 text-muted-foreground" />
                      )}
                      <span className="font-medium">{position.title}</span>
                    </div>
                    <Badge variant="outline" className="ml-auto">
                      {position.outcome || "Pending"}
                    </Badge>
                  </div>

                  {/* Level 2: Expanded Details */}
                  {isExpanded && (
                    <div className="border-t bg-muted/20 p-4">
                      <div className="grid gap-4 md:grid-cols-3">
                        {/* Human Prediction */}
                        <Card className="bg-background/50">
                          <CardHeader className="pb-2">
                            <CardTitle className="flex items-center gap-2 text-sm font-medium text-muted-foreground">
                              <User className="h-4 w-4" />
                              Human Prediction
                            </CardTitle>
                          </CardHeader>
                          <CardContent>
                            <div className={cn(
                              "text-2xl font-bold",
                              position.outcome === "Yes" ? "text-green-600" : 
                              position.outcome === "No" ? "text-red-600" : "text-primary"
                            )}>
                              {position.outcome || "N/A"}
                            </div>
                            <p className="text-xs text-muted-foreground mt-1">
                              Based on active position direction
                            </p>
                          </CardContent>
                        </Card>

                        {/* AI Prediction (Gemini) */}
                        <Card className="bg-background/50">
                          <CardHeader className="pb-2">
                            <CardTitle className="flex items-center gap-2 text-sm font-medium text-muted-foreground">
                              <Bot className="h-4 w-4" />
                              Gemini 2.5 Pro
                            </CardTitle>
                          </CardHeader>
                          <CardContent>
                            <div className="flex items-center justify-between">
                              <div className={cn(
                                "text-2xl font-bold",
                                aiData?.ai_outcome === "Yes" ? "text-green-600" :
                                aiData?.ai_outcome === "No" ? "text-red-600" : "text-blue-600"
                              )}>
                                {aiData ? aiData.ai_outcome : "Analyzing..."}
                              </div>
                              {aiData && (
                                <button
                                  onClick={(e) => toggleAIReasoning(position.slug, e)}
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
                            {!aiData && (
                              <p className="text-xs text-muted-foreground mt-1">
                                AI analysis pending or data missing
                              </p>
                            )}
                          </CardContent>
                        </Card>

                        {/* AI Prediction (Grok) */}
                        <Card className="bg-background/50">
                          <CardHeader className="pb-2">
                            <CardTitle className="flex items-center gap-2 text-sm font-medium text-muted-foreground">
                              <Bot className="h-4 w-4" />
                              Grok
                            </CardTitle>
                          </CardHeader>
                          <CardContent>
                            <div className="flex items-center justify-between">
                              <div className={cn(
                                "text-2xl font-bold",
                                aiData?.grok_outcome === "Yes" ? "text-green-600" :
                                aiData?.grok_outcome === "No" ? "text-red-600" : "text-purple-600"
                              )}>
                                {aiData?.grok_outcome || "Analyzing..."}
                              </div>
                              {aiData?.grok_reasoning && (
                                <button
                                  onClick={(e) => toggleAIReasoning(position.slug, e)}
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
                            {!aiData?.grok_outcome && (
                              <p className="text-xs text-muted-foreground mt-1">
                                AI analysis pending or data missing
                              </p>
                            )}
                          </CardContent>
                        </Card>
                      </div>

                      {/* Level 3: AI Reasoning */}
                      {isReasoningExpanded && aiData && (
                        <div className="mt-4 grid gap-4 md:grid-cols-2 animate-in slide-in-from-top-2 fade-in duration-200">
                          <div className="rounded-md bg-muted p-4">
                            <h4 className="mb-2 font-semibold flex items-center gap-2 text-blue-600">
                              <Bot className="h-4 w-4" />
                              Gemini Reasoning
                            </h4>
                            <p className="text-sm leading-relaxed whitespace-pre-wrap text-muted-foreground">
                              {aiData.ai_reasoning}
                            </p>
                          </div>
                          
                          <div className="rounded-md bg-muted p-4">
                            <h4 className="mb-2 font-semibold flex items-center gap-2 text-purple-600">
                              <Bot className="h-4 w-4" />
                              Grok Reasoning
                            </h4>
                            <p className="text-sm leading-relaxed whitespace-pre-wrap text-muted-foreground">
                              {aiData.grok_reasoning || "No reasoning available."}
                            </p>
                          </div>
                        </div>
                      )}
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        </CardContent>
      </Card>
    </div>
  )
}
