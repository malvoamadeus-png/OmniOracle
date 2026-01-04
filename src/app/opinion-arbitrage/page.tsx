import React from 'react';
import fs from 'fs';
import path from 'path';
import { createClient } from '@supabase/supabase-js';
import { OpinionArbitrageList, Event } from '@/components/OpinionArbitrageList';

// --- Supabase Config ---
const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL || "";
const supabaseAnonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY || "";
const supabase = createClient(supabaseUrl, supabaseAnonKey);

async function getArbitrageData(): Promise<Event[]> {
  try {
    // 1. Try to fetch from Supabase first
    const { data, error } = await supabase
      .from('opinion_arbitrage')
      .select('raw_data')
      .order('id', { ascending: true });

    if (!error && data && data.length > 0) {
      return data.map(row => row.raw_data as Event);
    }
    
    // 2. Fallback to local file if Supabase fails or is empty
    // Path relative to dashboard root: ../data/Opinion/arbitrage_opportunities.json
    const filePath = path.resolve(process.cwd(), '../data/Opinion/arbitrage_opportunities.json');
    
    if (!fs.existsSync(filePath)) {
      console.warn(`File not found at: ${filePath}. Checking fallback location...`);
      // Fallback to src/data if copied there
      const fallbackPath = path.resolve(process.cwd(), 'src/data/Opinion/arbitrage_opportunities.json');
      if (fs.existsSync(fallbackPath)) {
          const fileContent = fs.readFileSync(fallbackPath, 'utf-8');
          return JSON.parse(fileContent) as Event[];
      }
      return [];
    }

    const fileContent = fs.readFileSync(filePath, 'utf-8');
    return JSON.parse(fileContent) as Event[];
  } catch (error) {
    console.error("Error reading arbitrage data:", error);
    return [];
  }
}

export default async function OpinionArbitragePage() {
  const events = await getArbitrageData();

  return (
    <div className="container mx-auto p-6">
      <OpinionArbitrageList initialEvents={events} />
    </div>
  );
}
