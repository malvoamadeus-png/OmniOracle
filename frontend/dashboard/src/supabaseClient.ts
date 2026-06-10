import { createClient } from "@supabase/supabase-js";

const url = import.meta.env.VITE_SUPABASE_URL as string | undefined;
const anonKey = import.meta.env.VITE_SUPABASE_ANON_KEY as string | undefined;

export const supabaseConfig = {
  url,
  anonKey,
  ok: Boolean(url && anonKey)
};

export const supabase = supabaseConfig.ok ? createClient(url!, anonKey!) : null;
