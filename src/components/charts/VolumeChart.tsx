"use client";

import React from 'react';
import { BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from 'recharts';
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

// Custom Tooltip for the chart
const CustomTooltip = ({ active, payload, label }: any) => {
  if (active && payload && payload.length) {
    return (
      <div className="bg-white p-4 border rounded-lg shadow-lg text-sm">
        <p className="font-bold mb-2 max-w-[300px]">{label}</p>
        <div className="space-y-1">
          <p className="text-indigo-600">
            Opinion Vol: ${parseFloat(payload[0].value).toLocaleString()}
          </p>
          <p className="text-blue-600">
            Polymarket Vol: ${parseFloat(payload[1].value).toLocaleString()}
          </p>
        </div>
      </div>
    );
  }
  return null;
};

interface VolumeChartProps {
  data: {
    name: string;
    opinionVol: number;
    polyVol: number;
  }[];
}

export function VolumeChart({ data }: VolumeChartProps) {
  if (!data || data.length === 0) return null;

  return (
    <Card>
      <CardHeader>
        <CardTitle>Top Volume Comparison (24h)</CardTitle>
      </CardHeader>
      <CardContent className="h-[500px]">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart
            layout="vertical"
            data={data}
            margin={{ top: 5, right: 30, left: 20, bottom: 5 }}
          >
            <CartesianGrid strokeDasharray="3 3" horizontal={false} />
            <XAxis type="number" tickFormatter={(value) => `$${value.toLocaleString()}`} />
            <YAxis 
              dataKey="name" 
              type="category" 
              width={250} 
              tick={{ fontSize: 12 }}
              interval={0}
            />
            <Tooltip content={<CustomTooltip />} />
            <Bar dataKey="opinionVol" name="Opinion Vol" fill="#818cf8" radius={[0, 4, 4, 0]} barSize={10} />
            <Bar dataKey="polyVol" name="Polymarket Vol" fill="#60a5fa" radius={[0, 4, 4, 0]} barSize={10} />
          </BarChart>
        </ResponsiveContainer>
      </CardContent>
    </Card>
  );
}
