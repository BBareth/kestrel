import { useState } from "react";
import { errorText, post } from "../api";
import { useAuth } from "../auth";
import { Alert, Badge, Button, Card, Dot, Empty, ErrorBox, KV, Loading, Modal, statusTone, useToast } from "../components/ui";
import { ago, dt, titleCase } from "../format";
import { useApi } from "../hooks";
import { useLive } from "../live";

interface Comp { status: string; detail: string | null; updated_at: string | null; last_ok_at?: string | null }
interface Health { version: string; components: Record<string, Comp>; trading: { mode: string; kill_switch: boolean; halted: boolean; halt_reason: string | null; strategy_enabled: boolean };
  engine: { uptime_s: number; clock_skew_ms: number | null; guards: string[]; market: { ws_connected: boolean; reconnects: number; data_age_s: number | null } } | null;
  last_market_update: string | null; last_evaluation: string | null; last_trade: { id: number; opened_at: string; direction: string; status: string } | null;
  last_signal: { id: number; ts: string; direction: string; status: string } | null;
  reconcile: { paper: { actions: string[]; at: string } | null; live: { actions: string[]; at: string } | null } }

const LABELS: Record<string, string> = { database: "Database", engine: "Trading engine", binance: "Binance", market_ws: "Market WebSocket",
  openai: "OpenAI", notifications: "Notifications", strategy: "Strategy", trading: "Trading", backup: "Backups" };

export function System() {
  const h = useApi<Health>("/api/system/health", 5000);
  const ev = useApi<{ id: number; ts: string; level: string; component: string; event: string; message: string; resolved: boolean }[]>("/api/system/events?limit=100", 15000);
  const { connected } = useLive();
  if (!h.data) return h.error ? <ErrorBox error={h.error} /> : <Loading />;
  const d = h.data;
  return (
    <div className="grid two">
      <Card title="System health" className="span2" right={<span className="small muted">v{d.version}</span>}>
        <div className="health">
          {Object.entries(d.components).map(([k, c]) => (
            <div key={k} className={`hc hc-${statusTone(c.status)}`}>
              <div className="hc-h"><Dot status={c.status} /><b>{LABELS[k] || k}</b></div>
              <div className="small">{c.detail || c.status}</div>
              <div className="small muted">{c.updated_at ? `updated ${ago(c.updated_at)}` : ""}</div>
            </div>
          ))}
          <div className="hc"><div className="hc-h"><Dot status={connected ? "ok" : "degraded"} /><b>UI live stream</b></div><div className="small">{connected ? "connected" : "reconnecting"}</div></div>
          <div className={`hc ${d.trading.mode === "live" ? "hc-red" : "hc-green"}`}><div className="hc-h"><b>Trading: {d.trading.mode.toUpperCase()}</b></div>
            <div className="small">{d.trading.kill_switch ? "kill switch engaged" : d.trading.halted ? `halted: ${d.trading.halt_reason}` : d.trading.strategy_enabled ? "strategy armed" : "strategy disarmed"}</div></div>
        </div>
      </Card>
      <Card title="Activity">
        <KV rows={[["Last market update", ago(d.last_market_update)], ["Last evaluation", ago(d.last_evaluation)],
          ["Last signal", d.last_signal ? `${d.last_signal.direction} · ${titleCase(d.last_signal.status)} · ${ago(d.last_signal.ts)}` : "none"],
          ["Last trade", d.last_trade ? `#${d.last_trade.id} ${d.last_trade.direction} · ${d.last_trade.status} · ${ago(d.last_trade.opened_at)}` : "none"],
          ["Engine uptime", d.engine ? `${Math.round(d.engine.uptime_s / 60)} min` : "—"],
          ["Clock skew vs Binance", d.engine?.clock_skew_ms != null ? `${d.engine.clock_skew_ms} ms` : "—"],
          ["WebSocket reconnects", d.engine?.market.reconnects ?? "—"],
          ["Entry guards", d.engine?.guards.length ? d.engine.guards.join("; ") : "none"]]} />
      </Card>
      <Card title="Reconciliation">
        {(["paper", "live"] as const).map((m) => (
          <div key={m} className="stack-sm"><b>{m.toUpperCase()}</b>
            <div className="small muted">{d.reconcile[m] ? `last run ${ago(d.reconcile[m]!.at)}` : "not run"}</div>
            <ul className="small">{(d.reconcile[m]?.actions || []).slice(0, 6).map((a, i) => <li key={i}>{a}</li>)}</ul></div>
        ))}
      </Card>
      <Card title="System events" className="span2" right={ev.data?.some((e) => e.level === "critical" && !e.resolved) ?
        <Button small onClick={async () => { try { await post("/api/system/events/ack"); ev.reload(); } catch { /* reauth cancelled */ } }}>Acknowledge critical events</Button> : undefined}>
        {!ev.data ? <Loading /> : ev.data.length === 0 ? <Empty>No events.</Empty> : (
          <ul className="timeline">{ev.data.map((e) => <li key={e.id} className={`lv-${e.level}`}><span className="tl-t">{dt(e.ts)}</span>
            <Badge tone={e.level === "critical" || e.level === "error" ? "red" : e.level === "warning" ? "amber" : "gray"}>{e.component}</Badge>
            <b>{titleCase(e.event)}</b><span className="small">{e.message}</span></li>)}</ul>
        )}
      </Card>
    </div>
  );
}

interface Sec { binance: { status: string; display_status: string; environment: string; configured: boolean; testnet: boolean; key_hint: string | null;
  permissions: Record<string, unknown>; problems: string[]; last_checked_at: string | null }; openai_configured: boolean; vapid_configured: boolean;
  mode: string; totp_enabled: boolean; audit: { id: number; ts: string; username: string; action: string; target: string | null; ip: string | null }[]; ip_guidance: string }

const STATUS_TONE: Record<string, "green" | "red" | "amber" | "gray"> = { connected: "green", missing: "gray", invalid: "red", unsafe: "red", error: "amber", unchecked: "amber" };

export function Security() {
  const s = useApi<Sec>("/api/security/status", 30000);
  const { refresh } = useAuth();
  const [busy, setBusy] = useState<string | null>(null);
  const [totp, setTotp] = useState<{ secret: string; qr_svg: string } | null>(null);
  const [code, setCode] = useState("");
  const [recovery, setRecovery] = useState<string[] | null>(null);
  const [recon, setRecon] = useState<string[] | null>(null);
  const { toast, node } = useToast();
  const run = async (key: string, fn: () => Promise<void>) => { setBusy(key); try { await fn(); } catch (e) { toast(errorText(e), "red"); } finally { setBusy(null); s.reload(); } };
  if (!s.data) return s.error ? <ErrorBox error={s.error} /> : <Loading />;
  const b = s.data.binance;
  const perm = (k: string) => (b.permissions?.[k] === undefined ? "—" : String(b.permissions[k]));
  return (
    <div className="grid two">
      <Card title="Binance API credentials" right={<Badge tone={STATUS_TONE[b.status] || "gray"} solid>{b.display_status.toUpperCase()}</Badge>}>
        <div className="stack">
          <KV rows={[["Status", b.status], ["Environment", b.testnet ? "TESTNET (demo-fapi)" : "mainnet"], ["Key", b.key_hint || "not configured"],
            ["Trading mode", s.data.mode.toUpperCase()], ["Withdrawals enabled", perm("enableWithdrawals"), b.permissions?.enableWithdrawals ? "down" : ""],
            ["IP restricted", perm("ipRestrict"), b.permissions?.ipRestrict === false ? "down" : ""], ["Futures enabled", perm("enableFutures")],
            ["Hedge mode", perm("hedge_mode")], ["Multi-assets mode", perm("multi_assets_mode")], ["Last checked", ago(b.last_checked_at)]]} />
          {b.problems.length > 0 && <Alert tone="red"><ul>{b.problems.map((p, i) => <li key={i}>{p}</li>)}</ul></Alert>}
          <p className="small muted">Keys live only in the server's environment (.env) and are never sent to this browser or stored in the database.
            Live trading is refused unless withdrawals are disabled and the key is IP-restricted. {s.data.ip_guidance}</p>
          <div className="row wrap">
            <Button busy={busy === "verify"} onClick={() => run("verify", async () => { await post("/api/security/binance/verify"); toast("Credentials re-checked"); })}>Verify now</Button>
            <Button busy={busy === "prot"} disabled={!b.configured} onClick={() => run("prot", async () => {
              const r = await post<{ ok: boolean; message?: string }>("/api/security/protection-test");
              toast(r.ok ? "Stop-loss self-test passed" : `Self-test failed: ${r.message || "see System"}`, r.ok ? "green" : "red");
            })}>Run stop-loss self-test</Button>
            <Button busy={busy === "rec"} disabled={!b.configured} onClick={() => run("rec", async () => {
              const r = await post<{ reconcile_live?: { actions: string[] } }>("/api/security/reconcile");
              setRecon(r.reconcile_live?.actions || ["no live report yet"]);
            })}>Run live reconciliation check</Button>
          </div>
          {recon && <Alert tone="blue"><b>Reconciliation:</b> {recon.join("; ") || "clean — no positions or orders"}</Alert>}
        </div>
      </Card>
      <Card title="Two-factor authentication" right={<Badge tone={s.data.totp_enabled ? "green" : "amber"}>{s.data.totp_enabled ? "ENABLED" : "OFF"}</Badge>}>
        <div className="stack">
          <p className="small muted">TOTP (authenticator app) protects sign-in and every sensitive action. Required for live trading.</p>
          {!s.data.totp_enabled ? (
            <Button kind="primary" busy={busy === "totp"} onClick={() => run("totp", async () => setTotp(await post("/api/auth/totp/setup")))}>Set up authenticator</Button>
          ) : (
            <Button kind="ghost" onClick={() => run("totpoff", async () => { await post("/api/auth/totp/disable"); await refresh(); toast("2FA disabled", "amber"); })}>Disable 2FA</Button>
          )}
          <KV rows={[["OpenAI key", s.data.openai_configured ? "configured (server-side)" : "not configured"], ["Web push (VAPID)", s.data.vapid_configured ? "configured" : "missing"]]} />
        </div>
      </Card>
      <Card title="Audit log" className="span2">
        <div className="table-wrap"><table className="tbl"><thead><tr><th>Time</th><th>User</th><th>Action</th><th>Target</th><th>IP</th></tr></thead>
          <tbody>{s.data.audit.map((a) => <tr key={a.id}><td>{dt(a.ts)}</td><td>{a.username || "system"}</td><td>{titleCase(a.action)}</td><td className="small">{a.target || ""}</td><td className="small mono">{a.ip || ""}</td></tr>)}</tbody></table></div>
      </Card>
      <Modal open={!!totp} onClose={() => { setTotp(null); setCode(""); }} title="Set up authenticator">
        {totp && !recovery && <div className="stack">
          <p>Scan with an authenticator app (1Password, Google Authenticator, iOS Passwords…), then enter the 6-digit code.</p>
          <div className="qr" dangerouslySetInnerHTML={{ __html: totp.qr_svg }} />
          <p className="small mono break">{totp.secret}</p>
          <label>Code<input inputMode="numeric" autoComplete="one-time-code" value={code} onChange={(e) => setCode(e.target.value)} /></label>
          <Button kind="primary" onClick={() => run("en", async () => { const r = await post<{ recovery_codes: string[] }>("/api/auth/totp/enable", { code }); setRecovery(r.recovery_codes); await refresh(); })}>Enable</Button>
        </div>}
        {recovery && <div className="stack">
          <Alert tone="amber">Save these one-time recovery codes somewhere safe. Each works once if you lose your phone. They will not be shown again.</Alert>
          <pre className="codes">{recovery.join("\n")}</pre>
          <Button kind="primary" onClick={() => { setTotp(null); setRecovery(null); setCode(""); }}>I saved them</Button>
        </div>}
      </Modal>
      {node}
    </div>
  );
}
