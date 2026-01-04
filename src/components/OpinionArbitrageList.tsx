"use client"

import React, { useState, useMemo } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { ExternalLink, ArrowUpDown } from "lucide-react";
import { Button } from "@/components/ui/button";
import { VolumeChart } from "@/components/charts/VolumeChart";
import Link from 'next/link';

interface OpinionDetailsData {
  marketId: number;
  marketTitle: string;
  statusEnum: string;
  volume: string;
  volume24h: string;
  volume7d: string;
  yesTokenId: string;
  noTokenId: string;
}

interface OpinionDetails {
  result: {
    data: OpinionDetailsData;
  };
}

interface Market {
  opinion_outcome: string;
  polymarket_outcome: string;
  outcome_match_score: number;
  opinion_market_id: number;
  opinion_details: OpinionDetails;
  opinion_price?: string | null;
  polymarket_prices: string[];
  polymarket_market_id: string;
  polymarket_volume: string;
}

export interface Event {
  event_title: string;
  polymarket_event_title: string;
  match_score: number;
  cutoffAt?: number;
  opinion_stats: {
    volume24h: string;
    volume7d: string;
  };
  polymarket_stats: {
    volume24hr: number;
    volume1wk: number;
  };
  markets: Market[];
}

interface Props {
  initialEvents: Event[];
}

type SortOption = 'opinionVol' | 'polyVol' | 'cutoff';

export function OpinionArbitrageList({ initialEvents }: Props) {
  const [sortError, setSortError] = useState<SortOption>('opinionVol'); // Default sort by Opinion Volume
  const [sortDirection, setSortDirection] = useState<'asc' | 'desc'>('desc');

  const toggleSort = (option: SortOption) => {
    if (sortError === option) {
      setSortDirection(prev => prev === 'asc' ? 'desc' : 'asc');
    } else {
      setSortError(option);
      setSortDirection('desc'); // Default to desc for new sort
    }
  };

  const sortedEvents = useMemo(() => {
    return [...initialEvents].sort((a, b) => {
      let valA = 0;
      let valB = 0;

      if (sortError === 'opinionVol') {
        valA = parseFloat(a.opinion_stats.volume24h || "0");
        valB = parseFloat(b.opinion_stats.volume24h || "0");
      } else if (sortError === 'polyVol') {
        valA = a.polymarket_stats.volume24hr || 0;
        valB = b.polymarket_stats.volume24hr || 0;
      } else if (sortError === 'cutoff') {
        valA = a.cutoffAt || 0;
        valB = b.cutoffAt || 0;
        // For dates, typically we want Ascending (nearest first) as default "desc" might be furthest?
        // Let's stick to standard logic: desc = bigger number (later date) -> first.
        // User wants "Settlement Date" usually nearest first.
        // So if user selects 'cutoff' and direction is 'desc', we show later dates first.
        // If user wants nearest, they click again for 'asc'.
      }

      if (sortDirection === 'asc') {
        return valA - valB;
      } else {
        return valB - valA;
      }
    });
  }, [initialEvents, sortError, sortDirection]);

  // Chart Data (Top 20 of sorted)
  const chartData = sortedEvents.slice(0, 20).map(event => ({
    name: event.event_title,
    opinionVol: parseFloat(event.opinion_stats.volume24h || "0"),
    polyVol: event.polymarket_stats.volume24hr || 0,
  }));

  const formatTime = (ts?: number) => {
    if (!ts) return "-";
    return new Date(ts * 1000).toLocaleString();
  };

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <h1 className="text-3xl font-bold">Opinion 对冲</h1>
        <div className="flex gap-4 items-center">
            {/* Sort Controls */}
            <div className="flex gap-2">
                <Button 
                    variant={sortError === 'cutoff' ? "default" : "outline"} 
                    size="sm"
                    onClick={() => toggleSort('cutoff')}
                >
                    Settlement <ArrowUpDown className="ml-2 h-3 w-3" />
                </Button>
                <Button 
                    variant={sortError === 'opinionVol' ? "default" : "outline"} 
                    size="sm"
                    onClick={() => toggleSort('opinionVol')}
                >
                    Opinion Vol <ArrowUpDown className="ml-2 h-3 w-3" />
                </Button>
                <Button 
                    variant={sortError === 'polyVol' ? "default" : "outline"} 
                    size="sm"
                    onClick={() => toggleSort('polyVol')}
                >
                    Poly Vol <ArrowUpDown className="ml-2 h-3 w-3" />
                </Button>
            </div>

            <Link 
            href="https://app.opinion.trade?code=tIIQu4" 
            target="_blank"
            className="inline-flex items-center gap-2 bg-blue-600 hover:bg-blue-700 text-white px-4 py-2 rounded-md transition-colors font-medium text-sm"
            >
            开始交易 <ExternalLink className="h-4 w-4" />
            </Link>
        </div>
      </div>

      {/* Volume Comparison Chart */}
      {chartData.length > 0 && (
        <VolumeChart data={chartData} />
      )}

      {sortedEvents.length === 0 ? (
        <Card>
          <CardContent className="p-6 text-center text-gray-500">
            No arbitrage opportunities found.
          </CardContent>
        </Card>
      ) : (
        <div className="grid gap-6">
          {sortedEvents.map((event, index) => {
            // Calculate max values for data bars
            const marketsWithStats = event.markets.map(market => {
              const polyYesPrice = market.polymarket_prices && market.polymarket_prices.length > 0
                ? parseFloat(market.polymarket_prices[0])
                : null;
              const opinionPrice = market.opinion_price ? parseFloat(market.opinion_price) : null;
              const spread = (opinionPrice !== null && polyYesPrice !== null)
                ? Math.abs(opinionPrice - polyYesPrice)
                : 0;
              const opVol = parseFloat(market.opinion_details?.result?.data?.volume || "0");
              const polyVol = parseFloat(market.polymarket_volume || "0");
              
              return { ...market, polyYesPrice, opinionPrice, spread, opVol, polyVol };
            });

            const maxSpread = Math.max(...marketsWithStats.map(m => m.spread), 0.0001); // Avoid div by 0
            const maxOpVol = Math.max(...marketsWithStats.map(m => m.opVol), 1);
            const maxPolyVol = Math.max(...marketsWithStats.map(m => m.polyVol), 1);

            return (
            <Card key={index} className="overflow-hidden">
              <CardHeader className="bg-gray-50 dark:bg-gray-900 border-b py-3">
                <div className="flex justify-between items-start">
                  <div>
                    <CardTitle className="text-lg text-blue-600 flex items-center gap-2">
                        {event.event_title}
                    </CardTitle>
                    <div className="text-sm text-gray-500 mt-1 flex items-center flex-wrap gap-2">
                      <span className="font-medium text-gray-700">Polymarket Match:</span> {event.polymarket_event_title}
                      <Badge className="ml-2" variant={event.match_score > 0.9 ? "default" : "secondary"}>
                        Match: {(event.match_score * 100).toFixed(1)}%
                      </Badge>
                      {event.cutoffAt && (
                          <Badge variant="outline" className="ml-2 border-orange-200 bg-orange-50 text-orange-700">
                              Settlement: {formatTime(event.cutoffAt)}
                          </Badge>
                      )}
                    </div>
                  </div>
                  <div className="text-right text-sm">
                    <div className="grid grid-cols-2 gap-x-4 gap-y-1">
                      <span className="text-gray-500">Opinion Vol (24h):</span>
                      <span className="font-mono font-medium">
                        ${parseFloat(event.opinion_stats.volume24h || "0").toLocaleString(undefined, { maximumFractionDigits: 0 })}
                      </span>
                      <span className="text-gray-500">Polymarket Vol (24h):</span>
                      <span className="font-mono font-medium">
                        ${(event.polymarket_stats.volume24hr || 0).toLocaleString(undefined, { maximumFractionDigits: 0 })}
                      </span>
                    </div>
                  </div>
                </div>
              </CardHeader>
              
              <CardContent className="p-0">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead className="w-[30%]">Outcome</TableHead>
                      <TableHead className="text-right">Opinion Price</TableHead>
                      <TableHead className="text-right">Polymarket Price</TableHead>
                      <TableHead className="text-right w-[15%]">Spread</TableHead>
                      <TableHead className="text-right w-[15%]">Opinion Vol</TableHead>
                      <TableHead className="text-right w-[15%]">Poly Vol</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {marketsWithStats.map((market, mIndex) => {
                      const spreadPercent = (market.spread / maxSpread) * 100;
                      const opVolPercent = (market.opVol / maxOpVol) * 100;
                      const polyVolPercent = (market.polyVol / maxPolyVol) * 100;
                      
                      const spreadValue = (market.opinionPrice !== null && market.polyYesPrice !== null)
                         ? market.spread
                         : null;

                      return (
                        <TableRow key={mIndex}>
                          <TableCell className="font-medium">
                            <div className="flex flex-col">
                              <span>{market.opinion_outcome}</span>
                              {market.opinion_outcome !== market.polymarket_outcome && (
                                <span className="text-xs text-gray-400">Poly: {market.polymarket_outcome}</span>
                              )}
                            </div>
                          </TableCell>
                          <TableCell className="text-right font-mono">
                            {market.opinionPrice !== null ? market.opinionPrice.toFixed(3) : <span className="text-gray-300">-</span>}
                          </TableCell>
                          <TableCell className="text-right font-mono text-blue-600">
                            {market.polyYesPrice !== null ? market.polyYesPrice.toFixed(3) : <span className="text-gray-300">-</span>}
                          </TableCell>
                          
                          {/* Spread Column with Data Bar */}
                          <TableCell className="relative p-0 h-full align-middle w-[15%]">
                            <div className="relative w-full h-10 flex items-center justify-end px-4">
                              {spreadValue !== null && (
                                <>
                                  <div 
                                    className="absolute right-2 top-2 bottom-2 bg-green-100 opacity-60 rounded-sm" 
                                    style={{ width: `calc(${spreadPercent}% - 16px)`, maxWidth: 'calc(100% - 16px)', zIndex: 0 }} 
                                  />
                                  <span className={`relative z-10 font-mono ${spreadValue > 0.1 ? "text-green-700 font-bold" : ""}`}>
                                    {(spreadValue * 100).toFixed(1)}%
                                  </span>
                                </>
                              )}
                              {spreadValue === null && <span className="text-gray-300 relative z-10">-</span>}
                            </div>
                          </TableCell>

                          {/* Opinion Volume Column with Data Bar */}
                          <TableCell className="relative p-0 h-full align-middle w-[15%]">
                            <div className="relative w-full h-10 flex items-center justify-end px-4">
                                <div 
                                  className="absolute right-2 top-2 bottom-2 bg-indigo-100 opacity-60 rounded-sm" 
                                  style={{ width: `calc(${opVolPercent}% - 16px)`, maxWidth: 'calc(100% - 16px)', zIndex: 0 }} 
                                />
                                <span className="relative z-10 text-gray-600 text-xs">
                                  ${market.opVol.toLocaleString(undefined, { maximumFractionDigits: 0 })}
                                </span>
                            </div>
                          </TableCell>

                          {/* Polymarket Volume Column with Data Bar */}
                          <TableCell className="relative p-0 h-full align-middle w-[15%]">
                             <div className="relative w-full h-10 flex items-center justify-end px-4">
                                <div 
                                  className="absolute right-2 top-2 bottom-2 bg-blue-100 opacity-60 rounded-sm" 
                                  style={{ width: `calc(${polyVolPercent}% - 16px)`, maxWidth: 'calc(100% - 16px)', zIndex: 0 }} 
                                />
                                <span className="relative z-10 text-gray-600 text-xs">
                                  ${market.polyVol.toLocaleString(undefined, { maximumFractionDigits: 0 })}
                                </span>
                            </div>
                          </TableCell>
                        </TableRow>
                      );
                    })}
                  </TableBody>
                </Table>
              </CardContent>
            </Card>
          );
          })}
        </div>
      )}
    </div>
  );
}
