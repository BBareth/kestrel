import { useState, type ReactNode } from "react";
import { NavLink, useLocation } from "react-router";
import { errorText, post } from "../api";
import { useAuth } from "../auth";
import { pct, price } from "../format";
import { useLive } from "../live";
import { Button, Confirm, useToast } from "./ui";

export const NAV: { to: string; label: string; icon: string; primary?: boolean }[] = [
  { to: "/dashboard", label: "Dashboard", icon: "◧", primary: true },
  { to: "/trading", label: "Trading", icon: "⏻", primary: true },
  { to: "/positions", label: "Positions", icon: "▤", primary: true },
  { to: "/orders", label: "Orders", icon: "≣" },
  { to: "/signals", label: "Signals", icon: "⚑", primary: true },
  { to: "/strategy", label: "Strategy", icon: "⚙" },
  { to: "/ai", label: "AI", icon: "✦" },
  { to: "/performance", label: "Performance", icon: "↗" },
  { to: "/backtest", label: "Backtest", icon: "⟲" },
  { to: "/history", label: "History", icon: "☰" },
  { to: "/notifications", label: "Notifications", icon: "◔" },
  { to: "/settings", label: "Settings", icon: "⚒" },
  { to: "/system", label: "System", icon: "◉" },
  { to: "/security", label: "Security", icon: "⛨" },
];

export function ModePill({ big = false }: { big?: boolean }) {
  const { trading } = useLive();
  const live = trading?.mode === "live";
  return (
    <span className={`mode-pill ${live ? "live" : "paper"} ${big ? "big" : ""}`} title={live ? "Real orders on Binance" : "Simulated orders, real market data"}>
      <span className="mp-dot" />{live ? "LIVE TRADING" : "PAPER TRADING"}
    </span>
  );
}

export function KillSwitch({ size = "sm" }: { size?: "sm" | "lg" }) {
  const { trading, refreshTrading } = useLive();
  const [open, setOpen] = useState(false);
  const { toast, node } = useToast();
  const engaged = !!trading?.kill_switch;
  return (
    <>
      <button className={`kill ${size} ${engaged ? "engaged" : ""}`} onClick={() => setOpen(true)} title="Stop all trading now">
        <span className="kill-ico">■</span>{engaged ? "KILLED" : size === "lg" ? "KILL SWITCH — STOP ALL TRADING" : "KILL"}
      </button>
      <Confirm open={open} onClose={() => setOpen(false)} title="STOP ALL TRADING?" confirmLabel="Engage kill switch"
        body={<div className="stack">
          <p>This immediately disables the strategy and blocks new orders. Depending on <b>Settings → Safety</b> it also
            closes open positions at market (default) and cancels open orders. In LIVE mode it switches back to PAPER.</p>
          <p className="muted">All logs are preserved. Re-arming requires your password.</p>
        </div>}
        onConfirm={async () => {
          try {
            const r = await post<{ message: string; engine?: { actions?: string[] } }>("/api/trading/kill", { reason: "kill switch (UI)" });
            toast(r.engine?.actions?.join("; ") || r.message, "red");
          } catch (e) {
            toast(errorText(e), "red");
          }
          refreshTrading();
        }} />
      {node}
    </>
  );
}

function Banners() {
  const { trading, engine, connected, lastMessageAt } = useLive();
  const out: ReactNode[] = [];
  if (trading?.mode === "live") out.push(<div key="live" className="banner b-live">● LIVE TRADING — orders use real funds on Binance</div>);
  if (trading?.kill_switch) out.push(<div key="kill" className="banner b-red">■ KILL SWITCH ENGAGED — no new trades. Re-arm on the Trading page.</div>);
  if (trading?.halted) out.push(<div key="halt" className="banner b-red">⚠ TRADING HALTED — {trading.halt_reason}</div>);
  if (engine?.guards?.length) out.push(<div key="guard" className="banner b-amber">⏸ Entries paused: {engine.guards.join("; ")}</div>);
  const stale = lastMessageAt && Date.now() - lastMessageAt > 15000;
  if (!connected || stale) out.push(<div key="conn" className="banner b-amber">Live connection lost — reconnecting…</div>);
  return <>{out}</>;
}

export function Layout({ children }: { children: ReactNode }) {
  const { ticker, engine } = useLive();
  const { me, logout } = useAuth();
  const [more, setMore] = useState(false);
  const loc = useLocation();
  const chg = ticker?.change_24h_pct;
  return (
    <div className="shell">
      <aside className="side">
        <div className="brand"><img src="/icons/icon-192.png" alt="" width={28} height={28} /><div><b>KESTREL</b><span>BTCUSDT · PERP</span></div></div>
        <nav>
          {NAV.map((n) => (
            <NavLink key={n.to} to={n.to} className={({ isActive }) => (isActive ? "on" : "")}>
              <span className="ni">{n.icon}</span>{n.label}
            </NavLink>
          ))}
        </nav>
        <div className="side-foot">
          <div className="muted small">{me?.username} · v{engine?.version || "—"}</div>
          <button className="linkish" onClick={logout}>Sign out</button>
        </div>
      </aside>
      <div className="main">
        <header className="top">
          <div className="top-l">
            <div className="brand-m"><img src="/icons/icon-192.png" alt="" width={24} height={24} /></div>
            <div className="tick">
              <span className="tick-s">BTCUSDT</span>
              <span className="tick-p mono">{price(ticker?.price)}</span>
              <span className={`tick-c mono ${chg && chg > 0 ? "up" : chg && chg < 0 ? "down" : ""}`}>{pct(chg)}</span>
            </div>
          </div>
          <div className="top-r">
            <ModePill />
            <KillSwitch />
          </div>
        </header>
        <Banners />
        <main className="content" key={loc.pathname}>{children}</main>
      </div>
      <nav className="tabbar">
        {NAV.filter((n) => n.primary).map((n) => (
          <NavLink key={n.to} to={n.to} className={({ isActive }) => (isActive ? "on" : "")}>
            <span className="ni">{n.icon}</span><span>{n.label}</span>
          </NavLink>
        ))}
        <button className={more ? "on" : ""} onClick={() => setMore(true)}><span className="ni">⋯</span><span>More</span></button>
      </nav>
      {more && (
        <div className="sheet-bg" onClick={() => setMore(false)}>
          <div className="sheet" onClick={(e) => e.stopPropagation()}>
            <div className="sheet-grab" />
            <div className="sheet-grid">
              {NAV.filter((n) => !n.primary).map((n) => (
                <NavLink key={n.to} to={n.to} onClick={() => setMore(false)}><span className="ni">{n.icon}</span>{n.label}</NavLink>
              ))}
            </div>
            <Button kind="ghost" onClick={logout}>Sign out {me?.username}</Button>
          </div>
        </div>
      )}
    </div>
  );
}
