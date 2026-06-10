import { useCallback, useEffect, useState } from "react";
import { supabase } from "./supabaseClient";

export type TagValue = "顶尖" | "高手" | "特殊策略" | "待观察" | "排除";

export function useTags() {
  const [tags, setTags] = useState<Record<string, TagValue>>({});

  useEffect(() => {
    if (!supabase) return;
    supabase
      .from("address_tags")
      .select("address,tag")
      .then(({ data }) => {
        if (!data) return;
        const m: Record<string, TagValue> = {};
        for (const row of data) {
          if (typeof row.address === "string") {
            m[row.address.toLowerCase()] = row.tag as TagValue;
          }
        }
        setTags(m);
      });
  }, []);

  const setTag = useCallback(async (address: string, tag: TagValue | null, email: string) => {
    if (!supabase) return;
    const normalizedAddress = address.toLowerCase();
    if (tag === null) {
      setTags((prev) => {
        const next = { ...prev };
        delete next[normalizedAddress];
        return next;
      });
      await supabase.from("address_tags").delete().eq("address", normalizedAddress);
    } else {
      setTags((prev) => ({ ...prev, [normalizedAddress]: tag }));
      await supabase.from("address_tags").upsert(
        { address: normalizedAddress, tag, updated_by: email, updated_at: new Date().toISOString() },
        { onConflict: "address" }
      );
    }
  }, []);

  return { tags, setTag };
}
