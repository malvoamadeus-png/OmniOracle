"use client"

import React, { useState, useMemo } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ExternalLink, ArrowUpDown, ChevronDown, ChevronUp, Maximize2, Minimize2 } from "lucide-react";
import Link from 'next/link';

interface Order {
  price: string;
  amount: string;
  size?: string; // Add size as optional or alias to amount if backend sends "size"
}

interface Orderbook {
  bids: Order[];
  asks: Order[];
}

interface ChildMarket {
  marketTitle: string;
  volume: string;
  yesTokenId: string;
  noTokenId: string;
  yesOrderbook: Orderbook;
  noOrderbook: Orderbook;
}

export interface ClosingMarket {
  marketId: string;
  marketTitle: string;
  volume: string;
  volume24h: string;
  cutoffAt: number;
  childMarkets: ChildMarket[];
}

interface Props {
  initialMarkets: ClosingMarket[];
}

type SortOption = 'cutoff' | 'volume';

export function OpinionClosingList({ initialMarkets }: Props) {
  const [sortOption, setSortOption] = useState<SortOption>('cutoff');
  const [sortDirection, setSortDirection] = useState<'asc' | 'desc'>('asc'); 
  const [expandedMarkets, setExpandedMarkets] = useState<Set<string>>(new Set());

  const toggleSort = (option: SortOption) => {
    if (sortOption === option) {
      setSortDirection(prev => prev === 'asc' ? 'desc' : 'asc');
    } else {
      setSortOption(option);
      setSortDirection(option === 'cutoff' ? 'asc' : 'desc');
    }
  };

  const sortedMarkets = useMemo(() => {
    return [...initialMarkets].sort((a, b) => {
      let valA = 0;
      let valB = 0;

      if (sortOption === 'cutoff') {
        valA = a.cutoffAt;
        valB = b.cutoffAt;
      } else if (sortOption === 'volume') {
        valA = parseFloat(a.volume || "0");
        valB = parseFloat(b.volume || "0");
      }

      if (sortDirection === 'asc') {
        return valA - valB;
      } else {
        return valB - valA;
      }
    });
  }, [initialMarkets, sortOption, sortDirection]);

  const toggleExpand = (marketId: string) => {
    const newSet = new Set(expandedMarkets);
    if (newSet.has(marketId)) {
      newSet.delete(marketId);
    } else {
      newSet.add(marketId);
    }
    setExpandedMarkets(newSet);
  };

  const expandAll = () => {
    setExpandedMarkets(new Set(initialMarkets.map(m => m.marketId)));
  };

  const collapseAll = () => {
    setExpandedMarkets(new Set());
  };

  const formatTime = (ts: number) => {
    return new Date(ts * 1000).toLocaleString();
  };

  const renderOrderbookTable = (title: string, orderbook: Orderbook) => {
    // Safe access with fallback
    const bids = orderbook?.bids || [];
    const asks = orderbook?.asks || [];

    return (
        <div className="text-xs">
            <div className="font-semibold mb-1 text-gray-500">{title}</div>
            <div className="grid grid-cols-2 gap-2">
                {/* Bids Column */}
                <div>
                    <div className="text-green-600 font-medium mb-1 flex justify-between">
                        <span>Bids (Buy)</span>
                    </div>
                    {bids.length === 0 ? <div className="text-gray-300">-</div> : (
                        <div className="space-y-0.5">
                            {bids.map((o, i) => {
                                // Handle both 'amount' and 'size' keys
                                const amount = o.amount || o.size || "0";
                                return (
                                    <div key={i} className="flex justify-between">
                                        <span className="font-mono">{parseFloat(o.price).toFixed(3)}</span>
                                        <span className="text-gray-400 font-mono">{parseFloat(amount).toLocaleString(undefined, {maximumFractionDigits: 0})}</span>
                                    </div>
                                );
                            })}
                        </div>
                    )}
                </div>
                {/* Asks Column */}
                <div>
                    <div className="text-red-600 font-medium mb-1 flex justify-between">
                        <span>Asks (Sell)</span>
                    </div>
                    {asks.length === 0 ? <div className="text-gray-300">-</div> : (
                        <div className="space-y-0.5">
                            {asks.map((o, i) => {
                                const amount = o.amount || o.size || "0";
                                return (
                                    <div key={i} className="flex justify-between">
                                        <span className="font-mono">{parseFloat(o.price).toFixed(3)}</span>
                                        <span className="text-gray-400 font-mono">{parseFloat(amount).toLocaleString(undefined, {maximumFractionDigits: 0})}</span>
                                    </div>
                                );
                            })}
                        </div>
                    )}
                </div>
            </div>
        </div>
    );
  };

  return (
    <div className="space-y-6 relative">
      {/* Sticky Header */}
      <div className="sticky top-0 z-10 bg-white/95 backdrop-blur shadow-sm border-b py-4 -mx-6 px-6 mb-6">
        <div className="flex justify-between items-center container mx-auto">
            <h1 className="text-2xl font-bold flex items-center gap-2">
                尾盘数据 
                <span className="text-sm font-normal text-gray-500">
                    ({sortedMarkets.length} Markets)
                </span>
            </h1>
            
            <div className="flex items-center gap-4">
                <div className="flex gap-2">
                    <Button variant="outline" size="sm" onClick={expandAll}>
                        <Maximize2 className="h-4 w-4 mr-2" /> 全部展开
                    </Button>
                    <Button variant="outline" size="sm" onClick={collapseAll}>
                        <Minimize2 className="h-4 w-4 mr-2" /> 全部折叠
                    </Button>
                </div>

                <div className="h-6 w-px bg-gray-300 mx-2" />

                <div className="flex gap-2">
                    <Button 
                        variant={sortOption === 'cutoff' ? "default" : "outline"} 
                        size="sm"
                        onClick={() => toggleSort('cutoff')}
                    >
                        Settlement <ArrowUpDown className="ml-2 h-3 w-3" />
                    </Button>
                    <Button 
                        variant={sortOption === 'volume' ? "default" : "outline"} 
                        size="sm"
                        onClick={() => toggleSort('volume')}
                    >
                        Volume <ArrowUpDown className="ml-2 h-3 w-3" />
                    </Button>
                </div>

                <Link 
                    href="https://app.opinion.trade?code=tIIQu4" 
                    target="_blank"
                    className="inline-flex items-center gap-2 bg-blue-600 hover:bg-blue-700 text-white px-4 py-2 rounded-md transition-colors font-medium text-sm ml-2"
                >
                    开始交易 <ExternalLink className="h-4 w-4" />
                </Link>
            </div>
        </div>
      </div>

      <div className="grid gap-4 container mx-auto">
        {sortedMarkets.map((market) => {
            const isExpanded = expandedMarkets.has(market.marketId);
            return (
                <Card key={market.marketId} className="overflow-hidden border-l-4 border-l-blue-500">
                    <CardHeader className="py-3 px-4 bg-gray-50 cursor-pointer hover:bg-gray-100 transition-colors" onClick={() => toggleExpand(market.marketId)}>
                        <div className="flex justify-between items-center">
                            <div className="flex items-center gap-3">
                                {isExpanded ? <ChevronUp className="h-5 w-5 text-gray-500" /> : <ChevronDown className="h-5 w-5 text-gray-500" />}
                                <div>
                                    <CardTitle className="text-base font-semibold">{market.marketTitle}</CardTitle>
                                    <div className="text-xs text-gray-500 mt-1">
                                        Settlement: <span className="font-medium text-orange-600">{formatTime(market.cutoffAt)}</span>
                                    </div>
                                </div>
                            </div>
                            <div className="text-right">
                                <div className="text-sm font-mono font-medium">${parseFloat(market.volume || "0").toLocaleString()}</div>
                                <div className="text-xs text-gray-400">Total Volume</div>
                            </div>
                        </div>
                    </CardHeader>
                    
                    {isExpanded && (
                        <CardContent className="p-4 bg-white">
                            <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
                                {market.childMarkets.map((child, idx) => (
                                    <div key={idx} className="border rounded-lg p-3 hover:shadow-sm transition-shadow">
                                        <div className="flex justify-between items-start mb-2 border-b pb-2">
                                            <div className="font-medium text-sm line-clamp-2" title={child.marketTitle}>
                                                {child.marketTitle}
                                            </div>
                                            <Badge variant="secondary" className="ml-2 whitespace-nowrap">
                                                Vol: ${parseFloat(child.volume || "0").toLocaleString()}
                                            </Badge>
                                        </div>
                                        
                                        <div className="grid grid-cols-2 gap-3 mt-2">
                                            {renderOrderbookTable("YES Token", child.yesOrderbook)}
                                            {renderOrderbookTable("NO Token", child.noOrderbook)}
                                        </div>
                                    </div>
                                ))}
                            </div>
                        </CardContent>
                    )}
                </Card>
            );
        })}
      </div>
    </div>
  );
}
