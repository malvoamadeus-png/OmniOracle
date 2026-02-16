import { createClient } from "@supabase/supabase-js";
import type { SupabaseClient } from "@supabase/supabase-js";

const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL ?? "";
const supabaseAnonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY ?? "";

export const supabaseConfigOk = Boolean(supabaseUrl && supabaseAnonKey);

let _supabase: SupabaseClient | null = null;

function getSupabase(): SupabaseClient {
  if (_supabase) return _supabase;
  if (!supabaseUrl || !supabaseAnonKey) {
    throw new Error(
      "Supabase client is not configured. Missing NEXT_PUBLIC_SUPABASE_URL or NEXT_PUBLIC_SUPABASE_ANON_KEY."
    );
  }
  _supabase = createClient(supabaseUrl, supabaseAnonKey);
  return _supabase;
}

export const supabase = new Proxy(
  {},
  {
    get(_target, prop) {
      return (getSupabase() as any)[prop];
    },
  }
) as SupabaseClient;

if (!supabaseConfigOk && typeof window !== "undefined") {
  console.error(
    "Supabase client is not configured. Missing NEXT_PUBLIC_SUPABASE_URL or NEXT_PUBLIC_SUPABASE_ANON_KEY."
  );
}
