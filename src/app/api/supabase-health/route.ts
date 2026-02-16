import { NextResponse } from "next/server";

export async function GET() {
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL || "";
  const key = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY || "";
  if (!url || !key) {
    return NextResponse.json(
      {
        ok: false,
        error: "Missing NEXT_PUBLIC_SUPABASE_URL or NEXT_PUBLIC_SUPABASE_ANON_KEY",
      },
      { status: 500 }
    );
  }

  const base = url.endsWith("/") ? url.slice(0, -1) : url;
  const endpoint = `${base}/rest/v1/grok_preseason_markets?select=market_slug&limit=1`;
  const resp = await fetch(endpoint, {
    headers: {
      apikey: key,
      Authorization: `Bearer ${key}`,
    },
    cache: "no-store",
  });

  const text = await resp.text();
  return NextResponse.json(
    {
      ok: resp.ok,
      status: resp.status,
      endpoint,
      body_preview: text.slice(0, 500),
    },
    { status: resp.ok ? 200 : 500 }
  );
}
