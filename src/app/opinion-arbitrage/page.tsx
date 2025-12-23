import React from 'react';
import fs from 'fs';
import path from 'path';
import Link from 'next/link';
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { ExternalLink } from "lucide-react";
import { VolumeChart } from "@/components/charts/VolumeChart";

import { createClient } from '@supabase/supabase-js';

// --- Supabase Config ---
const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL || "";
const supabaseAnonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY || "";
const supabase = createClient(supabaseUrl, supabaseAnonKey);

// --- Types ---
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

interface Event {
  event_title: string;
  polymarket_event_title: string;
  match_score: number;
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

async function getArbitrageData(): Promise<Event[]> {
  try {
    // 1. Try to fetch from Supabase first
    const { data, error } = await supabase
      .from('opinion_arbitrage')
      .select('raw_data')
      .order('id', { ascending: true }); // Assuming we want them in insertion order or volume order if stored that way

    if (!error && data && data.length > 0) {
      // Extract raw_data from each row
      return data.map(row => row.raw_data as Event);
    }
    
    // 2. Fallback to local file if Supabase fails or is empty
    console.warn("Supabase fetch failed or empty, falling back to local file:", error?.message);
    const filePath = path.resolve(process.cwd(), '../backend/OpinionArbitrage/arbitrage_opportunities.json');
    
    if (!fs.existsSync(filePath)) {
      console.error(`File not found: ${filePath}`);
      return [];
    }

    const fileContent = fs.readFileSync(filePath, 'utf-8');
    return JSON.parse(fileContent) as Event[];
  } catch (error) {
    console.error("Error reading arbitrage data:", error);
    return [];
  }
}

// Custom Tooltip for the chart - REMOVED (moved to Client Component)
 
 export default async function OpinionArbitragePage() {
  const events = await getArbitrageData();

  // Prepare data for the chart
  const chartData = events
    .map(event => ({
      name: event.event_title,
      opinionVol: parseFloat(event.opinion_stats.volume24h || "0"),
      polyVol: event.polymarket_stats.volume24hr || 0,
    }))
    .sort((a, b) => (b.opinionVol + b.polyVol) - (a.opinionVol + a.polyVol)) // Sort by total volume
    .slice(0, 20); // Top 20

  return (
    <div className="container mx-auto p-6 space-y-6">
      <div className="flex justify-between items-center">
        <h1 className="text-3xl font-bold">Opinion Arbitrage Opportunities</h1>
        <Link 
          href="https://app.opinion.trade?code=tIIQu4" 
          target="_blank"
          className="inline-flex items-center gap-2 bg-blue-600 hover:bg-blue-700 text-white px-4 py-2 rounded-md transition-colors font-medium text-sm"
        >
          开始交易 <ExternalLink className="h-4 w-4" />
        </Link>
      </div>

      {/* Volume Comparison Chart */}
      {chartData.length > 0 && (
        <VolumeChart data={chartData} />
      )}

      {events.length === 0 ? (
        <Card>
          <CardContent className="p-6 text-center text-gray-500">
            No arbitrage opportunities found or data file missing.
            <p className="text-sm mt-2">Checked path: backend/OpinionArbitrage/arbitrage_opportunities.json</p>
          </CardContent>
        </Card>
      ) : (
        <div className="grid gap-6">
          {events.map((event, index) => {
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
              <CardHeader className="bg-gray-50 dark:bg-gray-900 border-b">
                <div className="flex justify-between items-start">
                  <div>
                    <CardTitle className="text-xl text-blue-600">{event.event_title}</CardTitle>
                    <div className="text-sm text-gray-500 mt-1 flex items-center">
                      Matches Polymarket: <span className="font-medium ml-1">{event.polymarket_event_title}</span> 
                      <Badge className="ml-2" variant={event.match_score > 0.9 ? "default" : "secondary"}>
                        Match: {(event.match_score * 100).toFixed(1)}%
                      </Badge>
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
