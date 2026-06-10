export type AccessRole = "basic" | "advanced";

export function useAccess() {
  return {
    role: "advanced" as AccessRole,
    isBasic: true,
    isAdvanced: true,
    configured: true,
    loadingAccess: false,
    refresh: async () => undefined,
    signOut: async () => undefined,
  };
}
