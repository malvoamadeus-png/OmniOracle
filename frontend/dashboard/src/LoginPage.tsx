import React, { useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import type { AccessRole } from "./useAccess";
import { useAccess } from "./useAccess";

type RequiredRole = "basic" | "advanced";

type LoginResponse = {
  ok: boolean;
  role?: AccessRole;
  redirectTo?: string;
  error?: string;
};

function sanitizeNextPath(raw: string | null): string {
  if (!raw || !raw.startsWith("/")) return "/";
  if (raw.startsWith("//")) return "/";
  return raw;
}

function hasRequiredRole(role: AccessRole | null, required: RequiredRole): boolean {
  if (!role) return false;
  if (required === "basic") return role === "basic" || role === "advanced";
  return role === "advanced";
}

export function LoginPage() {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const { role, configured, loadingAccess, refresh } = useAccess();
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const requiredRole = useMemo<RequiredRole>(() => {
    return searchParams.get("required") === "advanced" ? "advanced" : "basic";
  }, [searchParams]);

  const nextPath = useMemo(() => sanitizeNextPath(searchParams.get("next")), [searchParams]);

  useEffect(() => {
    if (loadingAccess) return;
    if (!hasRequiredRole(role, requiredRole)) return;
    navigate(nextPath, { replace: true });
  }, [role, requiredRole, nextPath, navigate, loadingAccess]);

  const onSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!password.trim()) {
      setError("请输入密码。");
      return;
    }

    setSubmitting(true);
    setError(null);
    try {
      const res = await fetch("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({
          password,
          required: requiredRole,
          next: nextPath,
        }),
      });
      const data = (await res.json()) as LoginResponse;
      if (!res.ok || !data.ok) {
        setError(data.error ?? "密码错误，请重试。");
        return;
      }
      await refresh();
      window.location.assign(data.redirectTo || nextPath || "/");
    } catch {
      setError("登录请求失败，请稍后再试。");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      style={{
        minHeight: "100vh",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: 16,
        background: "linear-gradient(135deg, #f4f7ff 0%, #f7fbf5 100%)",
        fontFamily: "system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial",
      }}
    >
      <div
        style={{
          width: "100%",
          maxWidth: 420,
          border: "1px solid #e5e9f2",
          borderRadius: 12,
          background: "#fff",
          padding: 20,
          boxShadow: "0 8px 24px rgba(22, 34, 64, 0.08)",
        }}
      >
        <h2 style={{ margin: 0, fontSize: 22 }}>PolySport 登录</h2>
        <div style={{ marginTop: 8, color: "#555", fontSize: 13 }}>
          {requiredRole === "advanced"
            ? "请输入高级密码（密码2）访问归因看板和跟单分析。"
            : "请输入密码（密码1 或 密码2）访问 PolySport 看板。"}
        </div>
        {!configured ? (
          <div
            style={{
              marginTop: 12,
              padding: 10,
              border: "1px solid #f5d39c",
              borderRadius: 8,
              background: "#fff9ef",
              color: "#8a5a00",
              fontSize: 12,
            }}
          >
            服务端密码未配置，请先在 Vercel 环境变量中设置。
          </div>
        ) : null}
        <form onSubmit={onSubmit} style={{ marginTop: 16, display: "grid", gap: 10 }}>
          <input
            type="password"
            placeholder="输入访问密码"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoFocus
            style={{
              width: "100%",
              padding: "10px 12px",
              borderRadius: 8,
              border: "1px solid #d9dee8",
              fontSize: 14,
              boxSizing: "border-box",
            }}
          />
          <button
            type="submit"
            disabled={submitting}
            style={{
              border: "1px solid #2f66d3",
              background: submitting ? "#9db8ea" : "#2f66d3",
              color: "#fff",
              fontSize: 14,
              borderRadius: 8,
              padding: "10px 12px",
              cursor: submitting ? "default" : "pointer",
            }}
          >
            {submitting ? "登录中..." : "登录"}
          </button>
        </form>
        {error ? (
          <div
            style={{
              marginTop: 10,
              color: "#9f1d1d",
              border: "1px solid #f3b4b4",
              background: "#fff2f2",
              borderRadius: 8,
              padding: "8px 10px",
              fontSize: 12,
            }}
          >
            {error}
          </div>
        ) : null}
      </div>
    </div>
  );
}
