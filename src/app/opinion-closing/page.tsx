import React from 'react';
import fs from 'fs';
import path from 'path';
import { OpinionClosingList, ClosingMarket } from '@/components/OpinionClosingList';

async function getClosingData(): Promise<ClosingMarket[]> {
  try {
    // 1. Fetch from local file
    // Path relative to dashboard root: ../data/Opinion/closing_markets.json
    const filePath = path.resolve(process.cwd(), '../data/Opinion/closing_markets.json');
    
    if (!fs.existsSync(filePath)) {
      console.warn(`File not found at: ${filePath}. Checking fallback location...`);
      const fallbackPath = path.resolve(process.cwd(), 'src/data/Opinion/closing_markets.json');
      if (fs.existsSync(fallbackPath)) {
          const fileContent = fs.readFileSync(fallbackPath, 'utf-8');
          return JSON.parse(fileContent) as ClosingMarket[];
      }
      return [];
    }

    const fileContent = fs.readFileSync(filePath, 'utf-8');
    return JSON.parse(fileContent) as ClosingMarket[];
  } catch (error) {
    console.error("Error reading closing markets data:", error);
    return [];
  }
}

export default async function OpinionClosingPage() {
  const markets = await getClosingData();

  return (
    <div className="min-h-screen bg-gray-50/30">
      <OpinionClosingList initialMarkets={markets} />
    </div>
  );
}
