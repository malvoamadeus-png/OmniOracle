import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { Analytics } from "@vercel/analytics/react";
import { App } from "./App";
import { CopytradeDailyCompareApp } from "./copytradeDailyCompareApp";
import { CopytradeLeaderPnlApp } from "./copytradeLeaderPnlApp";
import { LoginPage } from "./LoginPage";
import { useAccess, type AccessRole } from "./useAccess";

type RequiredRole = "basic" | "advanced";

function hasRequiredRole(role: AccessRole | null, required: RequiredRole): boolean {
  if (!role) return false;
  if (required === "basic") return role === "basic" || role === "advanced";
  return role === "advanced";
}

function buildLoginPath(nextPath: string, required: RequiredRole): string {
  const q = new URLSearchParams();
  q.set("required", required);
  q.set("next", nextPath);
  return `/login?${q.toString()}`;
}

function RoleRoute(props: { required: RequiredRole; children: React.ReactNode }) {
  const { role, loadingAccess } = useAccess();
  const location = useLocation();

  if (loadingAccess) {
    return <div style={{ padding: 16, fontFamily: "system-ui" }}>加载中...</div>;
  }

  if (!hasRequiredRole(role, props.required)) {
    const nextPath = `${location.pathname}${location.search}${location.hash}`;
    return <Navigate to={buildLoginPath(nextPath, props.required)} replace />;
  }

  return <>{props.children}</>;
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route
          path="/"
          element={
            <RoleRoute required="basic">
              <App />
            </RoleRoute>
          }
        />
        <Route
          path="/daily-compare"
          element={
            <RoleRoute required="advanced">
              <CopytradeDailyCompareApp />
            </RoleRoute>
          }
        />
        <Route
          path="/leader-attribution"
          element={
            <RoleRoute required="advanced">
              <CopytradeLeaderPnlApp />
            </RoleRoute>
          }
        />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
      <Analytics />
    </BrowserRouter>
  </React.StrictMode>
);
