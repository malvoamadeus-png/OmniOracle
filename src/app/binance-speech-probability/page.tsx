"use client";

import React, { useState, useEffect } from "react";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { supabase } from "@/lib/supabase";
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Legend } from "recharts";
import { Loader2, MessageSquare, Clock } from "lucide-react";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";

interface ProbabilityData {
  hour: number;
  [key: string]: number; // Allow dynamic access by handle
}

const ACCOUNTS = [
  { handle: "@cz_binance", name: "CZ", color: "#eab308" },
  { handle: "@heyibinance", name: "He Yi", color: "#1f2937" },
  { handle: "@nina_rong", name: "Nina", color: "#ec4899" },
  { handle: "@Binance_intern", name: "Binance Intern", color: "#f59e0b" },
  { handle: "@binancezh", name: "Binance ZH", color: "#3b82f6" },
  { handle: "@binance", name: "Binance", color: "#fcd34d" },
];

export default function BinanceSpeechProbabilityPage() {
  const [loading, setLoading] = useState(true);
  const [category, setCategory] = useState("all");
  const [currentTimeStr, setCurrentTimeStr] = useState("");
  const [chartData, setChartData] = useState<ProbabilityData[]>([]);
  
  // Selected accounts state
  const [selectedAccounts, setSelectedAccounts] = useState<string[]>(
    ACCOUNTS.map(a => a.handle) // Default select all
  );

  // 更新时间显示 (北京时间)
  useEffect(() => {
    const updateTime = () => {
      const now = new Date();
      // 格式化为 2026/1/12 23:06
      const formatter = new Intl.DateTimeFormat("zh-CN", {
        year: "numeric",
        month: "numeric",
        day: "numeric",
        hour: "numeric",
        minute: "numeric",
        hour12: false,
        timeZone: "Asia/Shanghai",
      });
      setCurrentTimeStr(formatter.format(now));
    };

    updateTime();
    const interval = setInterval(updateTime, 1000 * 60); // 每分钟更新
    return () => clearInterval(interval);
  }, []);

  // 获取数据
  useEffect(() => {
    const fetchData = async () => {
      setLoading(true);
      try {
        const now = new Date();
        const utc = now.getTime() + now.getTimezoneOffset() * 60000;
        const bjTime = new Date(utc + 3600000 * 8);
        
        const jsDay = bjTime.getDay();
        const dbDay = (jsDay + 6) % 7; // 转换逻辑

        const handles = ACCOUNTS.map(a => a.handle);
        
        // 获取图表数据 (根据选择的 category)
        const { data: chartDataRes, error: chartError } = await supabase
          .from("user_activity_profiles")
          .select("handle, hour, probability")
          .in("handle", handles)
          .eq("day_of_week", dbDay)
          .eq("category", category) // Filter by category
          .order("hour", { ascending: true });

        if (chartError) throw chartError;

        // 处理图表数据
        const processedChartData: ProbabilityData[] = Array.from({ length: 24 }, (_, i) => {
          const row: ProbabilityData = { hour: i };
          handles.forEach(h => row[h] = 0);
          return row;
        });

        if (chartDataRes) {
          chartDataRes.forEach((item: any) => {
            const hourIndex = item.hour;
            if (hourIndex >= 0 && hourIndex < 24) {
              if (handles.includes(item.handle)) {
                processedChartData[hourIndex][item.handle] = item.probability;
              }
            }
          });
        }

        setChartData(processedChartData);

      } catch (err) {
        console.error("Failed to fetch speech probability:", err);
      } finally {
        setLoading(false);
      }
    };

    fetchData();
  }, [category]); // Re-fetch when category changes

  const handleSelectAll = (checked: boolean) => {
    if (checked) {
      setSelectedAccounts(ACCOUNTS.map(a => a.handle));
    } else {
      setSelectedAccounts([]);
    }
  };

  const handleAccountToggle = (handle: string, checked: boolean) => {
    if (checked) {
      setSelectedAccounts(prev => [...prev, handle]);
    } else {
      setSelectedAccounts(prev => prev.filter(h => h !== handle));
    }
  };

  return (
    <div className="container mx-auto p-6 max-w-6xl space-y-8">
      {/* 标题与时间 */}
      <div className="flex flex-col md:flex-row justify-between items-start md:items-center gap-4">
        <div>
          <h1 className="text-3xl font-bold tracking-tight">币安系发言概率</h1>
          <p className="text-gray-500 mt-1">
            预测币安系相关账号的推文活跃度
          </p>
        </div>
        <Card className="bg-slate-50 border-slate-200 shadow-sm">
          <CardContent className="p-4 flex items-center gap-3">
            <Clock className="h-5 w-5 text-indigo-600" />
            <div className="flex flex-col">
              <span className="text-xs text-gray-500 font-medium uppercase">Current Time (UTC+8)</span>
              <span className="text-xl font-mono font-bold text-slate-800">
                {currentTimeStr || "--/--/-- --:--"}
              </span>
            </div>
          </CardContent>
        </Card>
      </div>

      {/* 账号选择区域 */}
      <Card>
        <CardHeader className="pb-3">
            <CardTitle className="text-base font-medium">Select Accounts</CardTitle>
        </CardHeader>
        <CardContent>
            <div className="flex flex-wrap gap-6">
                <div className="flex items-center space-x-2 border-r pr-6 mr-2">
                    <Checkbox 
                        id="select-all" 
                        checked={selectedAccounts.length === ACCOUNTS.length}
                        onCheckedChange={(c) => handleSelectAll(c as boolean)}
                    />
                    <Label htmlFor="select-all" className="font-bold cursor-pointer">Select All</Label>
                </div>
                {ACCOUNTS.map(account => (
                    <div key={account.handle} className="flex items-center space-x-2">
                        <Checkbox 
                            id={account.handle} 
                            checked={selectedAccounts.includes(account.handle)}
                            onCheckedChange={(c) => handleAccountToggle(account.handle, c as boolean)}
                        />
                        <Label 
                            htmlFor={account.handle} 
                            className="cursor-pointer flex items-center gap-2"
                        >
                            <span className="w-2 h-2 rounded-full" style={{ backgroundColor: account.color }} />
                            {account.name} <span className="text-xs text-gray-400 font-normal">({account.handle})</span>
                        </Label>
                    </div>
                ))}
            </div>
        </CardContent>
      </Card>

      {/* 图表区域 */}
      <Card>
        <CardHeader>
          <div className="flex flex-col md:flex-row justify-between items-start md:items-center gap-4">
            <div>
              <CardTitle className="flex items-center gap-2">
                <MessageSquare className="h-5 w-5 text-indigo-600" />
                24H 发言概率趋势 (UTC+8)
              </CardTitle>
              <CardDescription>
                展示今日每小时的推文发布概率。
              </CardDescription>
            </div>
            <div className="w-full md:w-48">
              <Select value={category} onValueChange={setCategory}>
                <SelectTrigger>
                  <SelectValue placeholder="Select Category" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">全部 (All)</SelectItem>
                  <SelectItem value="post">原创 (Post)</SelectItem>
                  <SelectItem value="retweet">转发 (Retweet)</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>
        </CardHeader>
        <CardContent>
          <div className="h-[500px] w-full">
            {loading ? (
                <div className="h-full w-full flex items-center justify-center bg-slate-50 rounded-lg">
                    <Loader2 className="h-10 w-10 animate-spin text-gray-300" />
                </div>
            ) : (
                <ResponsiveContainer width="100%" height="100%">
                <LineChart
                    data={chartData}
                    margin={{
                    top: 20,
                    right: 30,
                    left: 0,
                    bottom: 0,
                    }}
                >
                    <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="#e2e8f0" />
                    <XAxis 
                        dataKey="hour" 
                        tickFormatter={(h) => `${h}:00`} 
                        stroke="#94a3b8"
                        fontSize={12}
                        tickLine={false}
                        axisLine={false}
                    />
                    <YAxis 
                        tickFormatter={(val) => `${(val * 100).toFixed(0)}%`} 
                        stroke="#94a3b8"
                        fontSize={12}
                        tickLine={false}
                        axisLine={false}
                        domain={[0, 'auto']} 
                    />
                    <Tooltip 
                        formatter={(value: number) => [`${(value * 100).toFixed(2)}%`, '概率']}
                        labelFormatter={(label) => `${label}:00 - ${label + 1}:00`}
                        contentStyle={{ borderRadius: '8px', border: 'none', boxShadow: '0 4px 6px -1px rgb(0 0 0 / 0.1)' }}
                    />
                    <Legend verticalAlign="top" height={36}/>
                    
                    {ACCOUNTS.map(account => (
                        selectedAccounts.includes(account.handle) && (
                            <Line
                                key={account.handle}
                                type="monotone"
                                name={`${account.name} (${account.handle})`}
                                dataKey={account.handle}
                                stroke={account.color}
                                strokeWidth={3}
                                dot={{ r: 4, fill: account.color, strokeWidth: 0 }}
                                activeDot={{ r: 6 }}
                            />
                        )
                    ))}
                </LineChart>
                </ResponsiveContainer>
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
