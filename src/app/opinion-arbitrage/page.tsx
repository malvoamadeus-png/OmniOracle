import React from 'react';
import { createClient } from '@supabase/supabase-js';
import { OpinionArbitrageList, Event } from '@/components/OpinionArbitrageList';

// --- Supabase Config ---
const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL || "";
const supabaseAnonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY || "";
const supabase = createClient(supabaseUrl, supabaseAnonKey);

// Revalidate every 60 seconds
export const revalidate = 60;

async function getArbitrageData(): Promise<Event[]> {
  try {
    const { data, error } = await supabase
      .from('opinion_arbitrage')
      .select('raw_data')
      .order('id', { ascending: true });

    if (error) {
        console.error("Supabase fetch error:", error);
        return [];
    }

    if (data && data.length > 0) {
      return data.map(row => row.raw_data as Event);
    }
    
    return [];
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
