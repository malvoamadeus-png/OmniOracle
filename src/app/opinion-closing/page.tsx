import React from 'react';
import { createClient } from '@supabase/supabase-js';
import { OpinionClosingList, ClosingMarket } from '@/components/OpinionClosingList';

// --- Supabase Config ---
const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL || "";
const supabaseAnonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY || "";
const supabase = createClient(supabaseUrl, supabaseAnonKey);

// Revalidate every 60 seconds to ensure freshness without rebuilding
export const revalidate = 60; 

async function getClosingData(): Promise<ClosingMarket[]> {
  try {
    const { data, error } = await supabase
      .from('opinion_closing_markets')
      .select('raw_data')
      .limit(100); 

    if (error) {
        console.error("Supabase fetch error:", error);
        return [];
    }

    if (data && data.length > 0) {
      return data.map(row => row.raw_data as ClosingMarket);
    }

    return [];
  } catch (error) {
    console.error("Error fetching closing markets data:", error);
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
