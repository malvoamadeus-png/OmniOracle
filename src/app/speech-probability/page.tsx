"use client";

import React, { useState, useEffect } from "react";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { supabase } from "@/lib/supabase";
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Legend } from "recharts";
import { Loader2, MessageSquare, Clock } from "lucide-react";

interface ProbabilityData {
  hour: number;
  cz: number;
  heyi: number;
}

interface CurrentProb {
  cz: { current: number; next: number };
  heyi: { current: number; next: number };
}

export default function SpeechProbabilityPage() {
  const [loading, setLoading] = useState(true);
  const [currentTimeStr, setCurrentTimeStr] = useState("");
  const [chartData, setChartData] = useState<ProbabilityData[]>([]);
  const [currentProbs, setCurrentProbs] = useState<CurrentProb>({
    cz: { current: 0, next: 0 },
    heyi: { current: 0, next: 0 },
  });

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
        // 1. 获取当前北京时间的星期几
        // JS getDay(): 0(Sun), 1(Mon)...6(Sat)
        // DB day_of_week: 0(Mon)...6(Sun)
        const now = new Date();
        // 简单处理时区，假设用户本地时间不是太离谱，或者直接用 UTC+8 计算
        // 更严谨的做法是获取 UTC 时间并加 8 小时
        const utc = now.getTime() + now.getTimezoneOffset() * 60000;
        const bjTime = new Date(utc + 3600000 * 8);
        
        const jsDay = bjTime.getDay();
        const dbDay = (jsDay + 6) % 7; // 转换逻辑
        const currentHour = bjTime.getHours();

        // 2. 查询 Supabase
        const handles = ["@cz_binance", "@heyibinance"];
        const { data, error } = await supabase
          .from("user_activity_profiles")
          .select("handle, hour, probability")
          .in("handle", handles)
          .eq("day_of_week", dbDay)
          .order("hour", { ascending: true });

        if (error) throw error;

        // 3. 处理数据
        const processedData: ProbabilityData[] = Array.from({ length: 24 }, (_, i) => ({
          hour: i,
          cz: 0,
          heyi: 0,
        }));

        const probs = {
            cz: { current: 0, next: 0 },
            heyi: { current: 0, next: 0 },
        };

        if (data) {
          data.forEach((item: any) => {
            const hourIndex = item.hour;
            if (hourIndex >= 0 && hourIndex < 24) {
              const prob = item.probability; // 已经是小数，如 0.1234
              if (item.handle === "@cz_binance") {
                processedData[hourIndex].cz = prob;
              } else if (item.handle === "@heyibinance") {
                processedData[hourIndex].heyi = prob;
              }
            }
          });
          
           // 提取当前和下一小时概率
           const nextHour = (currentHour + 1) % 24;
           probs.cz.current = processedData[currentHour]?.cz || 0;
           probs.cz.next = processedData[nextHour]?.cz || 0;
           probs.heyi.current = processedData[currentHour]?.heyi || 0;
           probs.heyi.next = processedData[nextHour]?.heyi || 0;
        }

        setChartData(processedData);
        setCurrentProbs(probs);

      } catch (err) {
        console.error("Failed to fetch speech probability:", err);
      } finally {
        setLoading(false);
      }
    };

    fetchData();
  }, []);

  const formatPercent = (val: number) => `${(val * 100).toFixed(1)}%`;

  return (
    <div className="container mx-auto p-6 max-w-6xl space-y-8">
      {/* 标题与时间 */}
      <div className="flex flex-col md:flex-row justify-between items-start md:items-center gap-4">
        <div>
          <h1 className="text-3xl font-bold tracking-tight">二圣发言概率</h1>
          <p className="text-gray-500 mt-1">
            基于历史数据预测 @cz_binance 与 @heyibinance 的推文发布概率
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

      {/* 实时概率卡片 */}
      <div className="grid gap-6 md:grid-cols-2">
        {/* CZ Card */}
        <Card className="border-l-4 border-l-yellow-500 shadow-md hover:shadow-lg transition-shadow">
          <CardHeader className="pb-2">
            <div className="flex justify-between items-center">
                <CardTitle className="text-xl flex items-center gap-2">
                    <span className="text-2xl">🔶</span> CZ (@cz_binance)
                </CardTitle>
            </div>
          </CardHeader>
          <CardContent>
            {loading ? (
               <div className="h-24 flex items-center justify-center">
                 <Loader2 className="h-8 w-8 animate-spin text-gray-300" />
               </div>
            ) : (
                <div className="grid grid-cols-2 gap-4 mt-2">
                    <div className="space-y-1">
                        <span className="text-sm text-gray-500">当前小时概率</span>
                        <div className="text-3xl font-bold text-slate-800">
                            {formatPercent(currentProbs.cz.current)}
                        </div>
                    </div>
                    <div className="space-y-1">
                        <span className="text-sm text-gray-500">下一小时概率</span>
                        <div className="text-3xl font-bold text-slate-400">
                            {formatPercent(currentProbs.cz.next)}
                        </div>
                    </div>
                </div>
            )}
          </CardContent>
        </Card>

        {/* Heyi Card */}
        <Card className="border-l-4 border-l-gray-800 shadow-md hover:shadow-lg transition-shadow">
          <CardHeader className="pb-2">
            <div className="flex justify-between items-center">
                <CardTitle className="text-xl flex items-center gap-2">
                    <span className="text-2xl">👩🏻‍💼</span> He Yi (@heyibinance)
                </CardTitle>
            </div>
          </CardHeader>
          <CardContent>
            {loading ? (
               <div className="h-24 flex items-center justify-center">
                 <Loader2 className="h-8 w-8 animate-spin text-gray-300" />
               </div>
            ) : (
                <div className="grid grid-cols-2 gap-4 mt-2">
                    <div className="space-y-1">
                        <span className="text-sm text-gray-500">当前小时概率</span>
                        <div className="text-3xl font-bold text-slate-800">
                            {formatPercent(currentProbs.heyi.current)}
                        </div>
                    </div>
                    <div className="space-y-1">
                        <span className="text-sm text-gray-500">下一小时概率</span>
                        <div className="text-3xl font-bold text-slate-400">
                            {formatPercent(currentProbs.heyi.next)}
                        </div>
                    </div>
                </div>
            )}
          </CardContent>
        </Card>
      </div>

      {/* 图表区域 */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <MessageSquare className="h-5 w-5 text-indigo-600" />
            24H 发言概率趋势 (UTC+8)
          </CardTitle>
          <CardDescription>
            展示今日每小时的推文发布概率。
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div className="h-[400px] w-full">
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
                    
                    <Line
                        type="monotone"
                        name="CZ (@cz_binance)"
                        dataKey="cz"
                        stroke="#eab308" // Yellow-500
                        strokeWidth={3}
                        dot={{ r: 4, fill: "#eab308", strokeWidth: 0 }}
                        activeDot={{ r: 6 }}
                    />
                    <Line
                        type="monotone"
                        name="He Yi (@heyibinance)"
                        dataKey="heyi"
                        stroke="#1f2937" // Gray-800
                        strokeWidth={3}
                        dot={{ r: 4, fill: "#1f2937", strokeWidth: 0 }}
                        activeDot={{ r: 6 }}
                    />
                </LineChart>
                </ResponsiveContainer>
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
