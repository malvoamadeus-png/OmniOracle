import { useCallback, useEffect, useState } from "react";
import { supabase } from "./supabaseClient";

export type TagValue = "高手" | "特殊策略" | "待观察" | "排除";

export function useTags() {
  const [tags, setTags] = useState<Record<string, TagValue>>({});
  const [loading, setLoading] = useState(true);
  const [saveError, setSaveError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!supabase) return;
    setLoading(true);
    const { data, error } = await supabase.from("address_tags").select("address,tag");
    if (error) {
      setSaveError(error.message);
      setLoading(false);
      return;
    }
    const m: Record<string, TagValue> = {};
    for (const row of data ?? []) {
      if (typeof row.address === "string") {
        m[row.address.toLowerCase()] = row.tag as TagValue;
      }
    }
    setTags(m);
    setLoading(false);
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const setTag = useCallback(async (address: string, tag: TagValue | null, updatedBy?: string) => {
    if (!supabase) return false;
    const normalizedAddress = address.toLowerCase();
    const prevTag = tags[normalizedAddress];

    setSaveError(null);
    if (tag === null) {
      setTags((prev) => {
        const next = { ...prev };
        delete next[normalizedAddress];
        return next;
      });
    } else {
      setTags((prev) => ({ ...prev, [normalizedAddress]: tag }));
    }

    try {
      if (tag === null) {
        const { error } = await supabase.from("address_tags").delete().eq("address", normalizedAddress);
        if (error) throw error;
      } else {
        const { error } = await supabase.from("address_tags").upsert(
          {
            address: normalizedAddress,
            tag,
            updated_by: updatedBy ?? null,
            updated_at: new Date().toISOString(),
          },
          { onConflict: "address" }
        );
        if (error) throw error;
      }
      return true;
    } catch (error: any) {
      setTags((prev) => {
        const next = { ...prev };
        if (prevTag) {
          next[normalizedAddress] = prevTag;
        } else {
          delete next[normalizedAddress];
        }
        return next;
      });
      setSaveError(error?.message ?? "标签保存失败");
      return false;
    }
  }, [tags]);

  return { tags, loading, saveError, refresh, setTag };
}
