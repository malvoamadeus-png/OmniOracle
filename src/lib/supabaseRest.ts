type SupabaseRestOptions = {
  table: string;
  select: string;
  params?: Record<string, string>;
};

export async function supabaseRest<T>({ table, select, params }: SupabaseRestOptions): Promise<T[]> {
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL || "";
  const key = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY || "";
  if (!url || !key) {
    throw new Error("Missing NEXT_PUBLIC_SUPABASE_URL or NEXT_PUBLIC_SUPABASE_ANON_KEY");
  }

  const base = url.endsWith("/") ? url.slice(0, -1) : url;
  const endpoint = new URL(`${base}/rest/v1/${table}`);
  endpoint.searchParams.set("select", select);
  if (params) {
    Object.entries(params).forEach(([k, v]) => endpoint.searchParams.set(k, v));
  }

  const resp = await fetch(endpoint.toString(), {
    headers: {
      apikey: key,
      Authorization: `Bearer ${key}`,
    },
    cache: "no-store",
  });
  if (!resp.ok) {
    const txt = await resp.text();
    throw new Error(`Supabase REST failed: ${resp.status} ${txt.slice(0, 500)}`);
  }
  return (await resp.json()) as T[];
}

