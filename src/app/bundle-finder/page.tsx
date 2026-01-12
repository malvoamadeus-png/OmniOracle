"use client"

import React, { useState } from 'react';
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Loader2, Search, AlertTriangle, CheckCircle, ExternalLink } from "lucide-react";
import { runBundleAnalysis } from './actions';

interface Step {
  name: string;
  status: string;
  message: string;
  ts: number;
}

interface Suspect {
  address: string;
  score: number;
  count: number;
  totalAnalyzed: number;
}

interface Result {
  steps: Step[];
  suspects: Suspect[];
  hasBundle: boolean;
  fromCache?: boolean;
}

export default function BundleFinderPage() {
  const [address, setAddress] = useState("");
  const [chainId, setChainId] = useState("56");
  const [tokenCount, setTokenCount] = useState("50");
  const [historyLimit, setHistoryLimit] = useState("100");
  
  const [isLoading, setIsLoading] = useState(false);
  const [result, setResult] = useState<Result | null>(null);
  const [error, setError] = useState("");

  const handleAnalyze = async () => {
    if (!address) {
      setError("Please enter a wallet address");
      return;
    }
    
    setIsLoading(true);
    setError("");
    setResult(null);

    try {
      const data = await runBundleAnalysis(
        address, 
        chainId, 
        parseInt(tokenCount), 
        parseInt(historyLimit)
      );
      setResult(data);
    } catch (e: any) {
      setError(e.message || "An unknown error occurred");
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div className="container mx-auto p-6 max-w-5xl space-y-8">
      <div className="flex flex-col gap-2">
        <h1 className="text-3xl font-bold tracking-tight">小号查询 (Bundle Finder)</h1>
        <p className="text-gray-500">
          Analyze a wallet's trading history to detect potential insider trading ("mouse barns") or bundled wallets.
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Configuration</CardTitle>
          <CardDescription>Set the target parameters for analysis.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
          <div className="grid gap-4 md:grid-cols-2">
            <div className="space-y-2 md:col-span-2">
              <Label>Target Wallet Address</Label>
              <Input 
                placeholder="0x..." 
                value={address} 
                onChange={(e) => setAddress(e.target.value)} 
              />
            </div>
            
            <div className="space-y-2">
              <Label>Blockchain</Label>
              <Select value={chainId} onValueChange={setChainId}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="56">BSC (Binance Smart Chain)</SelectItem>
                  <SelectItem value="501">Solana</SelectItem>
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-2">
              <Label>Token Search Limit</Label>
              <Select value={tokenCount} onValueChange={setTokenCount}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="50">50 Tokens</SelectItem>
                  <SelectItem value="100">100 Tokens</SelectItem>
                  <SelectItem value="150">150 Tokens</SelectItem>
                  <SelectItem value="200">200 Tokens</SelectItem>
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-2">
              <Label>History Depth per Token</Label>
              <Select value={historyLimit} onValueChange={setHistoryLimit}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="100">100 Txns</SelectItem>
                  <SelectItem value="200">200 Txns</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>

          <Button 
            className="w-full md:w-auto bg-indigo-600 hover:bg-indigo-700 text-white shadow-lg transition-all transform hover:scale-105" 
            size="lg"
            onClick={handleAnalyze} 
            disabled={isLoading}
          >
            {isLoading ? (
              <>
                <Loader2 className="mr-2 h-5 w-5 animate-spin" />
                Connecting to Remote Node...
              </>
            ) : (
              <>
                <Search className="mr-2 h-5 w-5" />
                Start Deep Analysis
              </>
            )}
          </Button>

          {error && (
            <div className="p-4 bg-red-50 text-red-600 rounded-md text-sm border border-red-100 flex items-center gap-2">
              <AlertTriangle className="h-4 w-4" />
              {error}
            </div>
          )}
        </CardContent>
      </Card>

      {result && (
        <div className="space-y-6 animate-in fade-in slide-in-from-bottom-4 duration-500">
          
          {/* Status Overview */}
          <div className="grid gap-4 md:grid-cols-3">
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm font-medium text-gray-500">Analysis Status</CardTitle>
              </CardHeader>
              <CardContent>
                <div className="flex items-center gap-2">
                  {result.hasBundle ? (
                    <Badge variant="destructive" className="text-sm px-3 py-1">High Risk Detected</Badge>
                  ) : (
                    <Badge variant="outline" className="text-sm px-3 py-1 bg-green-50 text-green-700 border-green-200">
                        <CheckCircle className="w-3 h-3 mr-1" /> No Bundle Found
                    </Badge>
                  )}
                  {result.fromCache && (
                    <Badge variant="secondary" className="text-xs">Cached Result</Badge>
                  )}
                </div>
              </CardContent>
            </Card>
            
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm font-medium text-gray-500">Suspects Found</CardTitle>
              </CardHeader>
              <CardContent>
                <div className="text-2xl font-bold">{result.suspects.length}</div>
                <p className="text-xs text-gray-400">Addresses with overlapping buys</p>
              </CardContent>
            </Card>

            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm font-medium text-gray-500">Max Overlap</CardTitle>
              </CardHeader>
              <CardContent>
                <div className={`text-2xl font-bold ${result.hasBundle ? 'text-red-600' : 'text-green-600'}`}>
                  {result.suspects.length > 0 
                    ? `${(result.suspects[0].score * 100).toFixed(1)}%` 
                    : "0%"}
                </div>
                <p className="text-xs text-gray-400">Highest coincidence score</p>
              </CardContent>
            </Card>
          </div>

          {/* Execution Steps */}
          <Card>
            <CardHeader>
              <CardTitle>Execution Log</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="space-y-4">
                {result.steps.map((step, i) => (
                  <div key={i} className="flex items-start gap-3 text-sm">
                    <div className={`mt-0.5 h-2 w-2 rounded-full ${
                      step.status === 'ok' ? 'bg-green-500' : 
                      step.status === 'running' ? 'bg-blue-500 animate-pulse' : 'bg-gray-300'
                    }`} />
                    <div className="flex-1">
                      <div className="font-medium text-gray-900">{step.name}</div>
                      <div className="text-gray-500">{step.message}</div>
                    </div>
                    <div className="text-xs text-gray-400 font-mono">
                      {new Date(step.ts * 1000).toLocaleTimeString()}
                    </div>
                  </div>
                ))}
              </div>
            </CardContent>
          </Card>

          {/* Suspects Table */}
          {result.suspects.length > 0 && (
            <Card>
              <CardHeader>
                <CardTitle>Suspect Addresses</CardTitle>
                <CardDescription>
                  Wallets that bought the same tokens before the target wallet.
                  Score = Overlap Count / Total Analyzed Tokens.
                </CardDescription>
              </CardHeader>
              <CardContent>
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Address</TableHead>
                      <TableHead className="text-right">Bundle Score</TableHead>
                      <TableHead className="text-right">Overlap Count</TableHead>
                      <TableHead className="text-right">Total Analyzed</TableHead>
                      <TableHead className="w-[50px]"></TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {result.suspects.map((suspect, i) => (
                      <TableRow key={i}>
                        <TableCell className="font-mono text-xs">{suspect.address}</TableCell>
                        <TableCell className="text-right">
                          <span className={`font-bold ${suspect.score >= 0.2 ? 'text-red-600' : 'text-gray-700'}`}>
                            {(suspect.score * 100).toFixed(1)}%
                          </span>
                        </TableCell>
                        <TableCell className="text-right">{suspect.count}</TableCell>
                        <TableCell className="text-right">{suspect.totalAnalyzed}</TableCell>
                        <TableCell>
                          <a 
                            href={chainId === "56" ? `https://bscscan.com/address/${suspect.address}` : `https://solscan.io/account/${suspect.address}`}
                            target="_blank"
                            rel="noreferrer"
                            className="text-blue-500 hover:text-blue-700"
                          >
                            <ExternalLink className="h-4 w-4" />
                          </a>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </CardContent>
            </Card>
          )}
        </div>
      )}
    </div>
  );
}
