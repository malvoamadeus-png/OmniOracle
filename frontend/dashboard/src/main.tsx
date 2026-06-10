import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { Analytics } from "@vercel/analytics/react";
import { App } from "./App";
import { CopytradeDailyCompareApp } from "./copytradeDailyCompareApp";
import { CopytradeLeaderPnlApp } from "./copytradeLeaderPnlApp";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<Navigate to="/" replace />} />
        <Route path="/" element={<App />} />
        <Route path="/daily-compare" element={<CopytradeDailyCompareApp />} />
        <Route path="/leader-attribution" element={<CopytradeLeaderPnlApp />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
      <Analytics />
    </BrowserRouter>
  </React.StrictMode>
);
