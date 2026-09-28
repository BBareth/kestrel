import { BrowserRouter, Navigate, Route, Routes } from "react-router";
import { AuthProvider } from "./auth";
import { Layout } from "./components/Layout";
import { LiveProvider } from "./live";
import { Notifications, Settings, Strategy } from "./pages/Config";
import Dashboard from "./pages/Dashboard";
import { AI, Backtest, Performance } from "./pages/Insights";
import { History, Orders, Positions, Signals, TradeDetail } from "./pages/Lists";
import { Security, System } from "./pages/SystemSecurity";
import Trading from "./pages/Trading";

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <LiveProvider>
          <Layout>
            <Routes>
              <Route path="/" element={<Navigate to="/dashboard" replace />} />
              <Route path="/dashboard" element={<Dashboard />} />
              <Route path="/trading" element={<Trading />} />
              <Route path="/positions" element={<Positions />} />
              <Route path="/orders" element={<Orders />} />
              <Route path="/signals" element={<Signals />} />
              <Route path="/strategy" element={<Strategy />} />
              <Route path="/ai" element={<AI />} />
              <Route path="/performance" element={<Performance />} />
              <Route path="/backtest" element={<Backtest />} />
              <Route path="/history" element={<History />} />
              <Route path="/history/:id" element={<TradeDetail />} />
              <Route path="/notifications" element={<Notifications />} />
              <Route path="/settings" element={<Settings />} />
              <Route path="/system" element={<System />} />
              <Route path="/security" element={<Security />} />
              <Route path="*" element={<Navigate to="/dashboard" replace />} />
            </Routes>
          </Layout>
        </LiveProvider>
      </AuthProvider>
    </BrowserRouter>
  );
}
