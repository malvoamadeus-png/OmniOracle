import { useCallback, useEffect, useState } from "react";

export type AccessRole = "basic" | "advanced";

type SessionResponse = {
  role: AccessRole | null;
  configured?: boolean;
};

export function useAccess() {
  const [role, setRole] = useState<AccessRole | null>(null);
  const [configured, setConfigured] = useState<boolean>(true);
  const [loadingAccess, setLoadingAccess] = useState(true);

  const refresh = useCallback(async () => {
    setLoadingAccess(true);
    try {
      const res = await fetch("/api/session", { credentials: "include", cache: "no-store" });
      if (!res.ok) {
        setRole(null);
        setConfigured(false);
        return;
      }
      const data = (await res.json()) as SessionResponse;
      setRole(data.role ?? null);
      setConfigured(data.configured ?? true);
    } catch {
      setRole(null);
      setConfigured(false);
    } finally {
      setLoadingAccess(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const signOut = useCallback(async () => {
    try {
      await fetch("/api/logout", { method: "POST", credentials: "include" });
    } catch {
      // Ignore transient network errors and still refresh local state.
    }
    await refresh();
  }, [refresh]);

  const isBasic = role === "basic" || role === "advanced";
  const isAdvanced = role === "advanced";

  return { role, isBasic, isAdvanced, configured, loadingAccess, refresh, signOut };
}
