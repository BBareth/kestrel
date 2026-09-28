import { useEffect, useMemo, useState } from "react";
import { del, errorText, get, post, put } from "../api";
import { useAuth } from "../auth";
import { Alert, Badge, Button, Card, Confirm, Empty, ErrorBox, KV, Loading, Tabs, Toggle, useToast } from "../components/ui";
import { ago, dt } from "../format";
import { useApi } from "../hooks";
import type { ParamSpec } from "../types";

type Params = Record<string, number | string | boolean>;

const same = (a: Params[string], b: Params[string]) => String(a) === String(b);
function normalize(specs: ParamSpec[], draft: Params, keys: string[]): Params {
  const byKey = Object.fromEntries(specs.map((p) => [p.key, p]));
  return Object.fromEntries(keys.map((k) => {
    const t = byKey[k]?.type;
    const v = draft[k];
    return [k, (t === "int" || t === "float") && typeof v === "string" && v.trim() !== "" && !Number.isNaN(Number(v)) ? Number(v) : v];
  }));
}

function ParamField({ spec, value, onChange, dirty }: { spec: ParamSpec; value: Params[string]; onChange: (v: Params[string]) => void; dirty: boolean }) {
  const id = `p-${spec.key}`;
  let input;
  if (spec.type === "bool") {
    input = <Toggle checked={!!value} disabled={spec.locked} onChange={onChange} />;
  } else if (spec.type === "enum") {
    input = <select id={id} value={String(value)} disabled={spec.locked} onChange={(e) => onChange(e.target.value)}>
      {spec.options.map((o) => <option key={o} value={o}>{o}</option>)}</select>;
  } else if (spec.type === "int" || spec.type === "float") {
    input = <input id={id} type="number" inputMode="decimal" value={String(value)} disabled={spec.locked}
      min={spec.min ?? undefined} max={spec.max ?? undefined} step={spec.step ?? (spec.type === "int" ? 1 : "any")}
      onChange={(e) => onChange(e.target.value)} />;
  } else {
    input = <input id={id} value={String(value)} disabled={spec.locked} onChange={(e) => onChange(e.target.value)} />;
  }
  return (
    <div className={`pf ${dirty ? "dirty" : ""}`}>
      <div className="pf-h">
        <label htmlFor={id}>{spec.label}</label>
        {spec.locked && <Badge>locked</Badge>}
        {spec.risk_sensitive && <Badge tone="amber">risk</Badge>}
      </div>
      <div className="pf-in">{input}{spec.unit && <span className="unit">{spec.unit}</span>}</div>
      <div className="pf-help">{spec.help}{(spec.min != null || spec.max != null) && spec.type !== "bool" &&
        <span className="muted"> Range {spec.min}–{spec.max}; default {String(spec.default)}.</span>}</div>
    </div>
  );
}

const GROUPS: { key: string; label: string }[] = [
  { key: "strategy", label: "Strategy" }, { key: "risk", label: "Risk" }, { key: "ai", label: "AI" },
  { key: "schedule", label: "Schedule" }, { key: "execution", label: "Execution" },
];

interface Cfg { version: number; params: Params; defaults: Params; catalogue: { params: ParamSpec[]; safety: ParamSpec[] };
  created_at: string; created_by: string; note: string | null; risk_reviewed: { at: string; ok: boolean } | null }

export function Strategy() {
  const cfg = useApi<Cfg>("/api/strategy/config");
  const versions = useApi<{ version: number; created_at: string; created_by: string; note: string | null; is_active: boolean }[]>("/api/strategy/versions");
  const [draft, setDraft] = useState<Params>({});
  const [group, setGroup] = useState("strategy");
  const [note, setNote] = useState("");
  const [errs, setErrs] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [resetOpen, setResetOpen] = useState(false);
  const { toast, node } = useToast();
  useEffect(() => { if (cfg.data) setDraft(cfg.data.params); }, [cfg.data]);
  const specs = useMemo(() => (cfg.data?.catalogue.params || []).filter((p) => p.group === group), [cfg.data, group]);
  if (!cfg.data) return cfg.error ? <ErrorBox error={cfg.error} /> : <Loading />;
  const changed = Object.keys(draft).filter((k) => !same(draft[k], cfg.data!.params[k]));
  const save = async () => {
    setBusy(true); setErrs(null);
    try {
      const body = normalize(cfg.data!.catalogue.params, draft, changed);
      const r = await put<{ version: number }>("/api/strategy/config", { params: body, note: note || null });
      toast(`Saved as version ${r.version}`); setNote(""); cfg.reload(); versions.reload();
    } catch (e) { setErrs(errorText(e)); } finally { setBusy(false); }
  };
  return (
    <div className="grid two">
      <Card title={<>Strategy &amp; risk configuration <Badge tone="blue">v{cfg.data.version}</Badge></>} className="span2"
        right={<Button kind="ghost" small onClick={() => setResetOpen(true)}>Reset to safe defaults</Button>}>
        <div className="stack">
          <p className="muted small">Breakout–retest intraday strategy on the 5m chart with 15m/1h/4h context. Every change creates a new
            strategy version; trades record the version they used. The risk limits are enforced in code with hard caps the UI cannot exceed.</p>
          <Tabs value={group} onChange={setGroup} items={GROUPS.map((g) => ({ value: g.key, label: g.label }))} />
          <div className="pgrid">
            {specs.map((s) => <ParamField key={s.key} spec={s} value={draft[s.key]} dirty={!same(draft[s.key], cfg.data!.params[s.key])}
              onChange={(v) => setDraft({ ...draft, [s.key]: v })} />)}
          </div>
          {group === "risk" && (
            <Alert tone={cfg.data.risk_reviewed?.ok ? "green" : "amber"}>
              {cfg.data.risk_reviewed?.ok ? <>Risk limits reviewed {ago(cfg.data.risk_reviewed.at)}.</> : <>Risk limits have not been reviewed since the last change (required for live trading).</>}
              {" "}<Button small onClick={async () => { await post("/api/strategy/risk-reviewed"); cfg.reload(); toast("Recorded"); }}>I have reviewed these limits</Button>
            </Alert>
          )}
          {group === "ai" && <p className="muted small">The AI is an analysis/veto layer only. It cannot change size, leverage, stops or any risk limit, and it is never asked about setups the risk engine rejected.</p>}
          <ErrorBox error={errs} />
          <div className="row wrap end sticky-save">
            <span className="muted small">{changed.length ? `${changed.length} unsaved change(s)` : "No changes"}</span>
            <input placeholder="Change note (optional)" value={note} onChange={(e) => setNote(e.target.value)} className="note" />
            <Button kind="ghost" disabled={!changed.length} onClick={() => setDraft(cfg.data!.params)}>Discard</Button>
            <Button kind="primary" disabled={!changed.length} busy={busy} onClick={save}>Save new version</Button>
          </div>
        </div>
      </Card>
      <Card title="Versions" className="span2">
        {!versions.data ? <Loading /> : (
          <div className="table-wrap"><table className="tbl"><thead><tr><th>Version</th><th>Created</th><th>By</th><th>Note</th><th /></tr></thead>
            <tbody>{versions.data.map((v) => <tr key={v.version}><td>v{v.version} {v.is_active && <Badge tone="green">active</Badge>}</td><td>{dt(v.created_at)}</td>
              <td>{v.created_by}</td><td className="small">{v.note}</td>
              <td>{!v.is_active && <Button small kind="ghost" onClick={async () => { try { await post(`/api/strategy/versions/${v.version}/activate`); cfg.reload(); versions.reload(); toast(`Restored v${v.version}`); } catch (e) { toast(errorText(e), "red"); } }}>Restore</Button>}</td></tr>)}
            </tbody></table></div>
        )}
      </Card>
      <Confirm open={resetOpen} onClose={() => setResetOpen(false)} title="Reset to safe defaults?" kind="primary" confirmLabel="Reset"
        body="All strategy, risk, AI, schedule and execution parameters return to the conservative defaults (0.5% risk, 3× max leverage, 2% daily loss, min R:R 2.0, AI confirmation required). A new version is created."
        onConfirm={async () => { await post("/api/strategy/reset"); cfg.reload(); versions.reload(); toast("Reset to safe defaults"); }} />
      {node}
    </div>
  );
}

interface SettingsT { safety: Params; catalogue: ParamSpec[]; symbol: string; paper_starting_balance: number; testnet: boolean }

export function Settings() {
  const s = useApi<SettingsT>("/api/settings");
  const [draft, setDraft] = useState<Params>({});
  const [err, setErr] = useState<string | null>(null);
  const [pw, setPw] = useState({ current: "", next: "", again: "" });
  const { toast, node } = useToast();
  const sessions = useApi<{ id: number; created_at: string; last_seen_at: string; ip: string; user_agent: string; current: boolean }[]>("/api/auth/sessions");
  useEffect(() => { if (s.data) setDraft(s.data.safety); }, [s.data]);
  if (!s.data) return s.error ? <ErrorBox error={s.error} /> : <Loading />;
  const changed = Object.keys(draft).filter((k) => !same(draft[k], s.data!.safety[k]));
  return (
    <div className="grid two">
      <Card title="Safety" className="span2">
        <div className="pgrid">
          {s.data.catalogue.map((spec) => <ParamField key={spec.key} spec={spec} value={draft[spec.key]} dirty={!same(draft[spec.key], s.data!.safety[spec.key])}
            onChange={(v) => setDraft({ ...draft, [spec.key]: v })} />)}
        </div>
        <ErrorBox error={err} />
        <div className="row end">
          <Button kind="primary" disabled={!changed.length} onClick={async () => {
            setErr(null);
            try { await put("/api/settings/safety", normalize(s.data!.catalogue, draft, changed)); s.reload(); toast("Safety settings saved"); }
            catch (e) { setErr(errorText(e)); }
          }}>Save safety settings</Button>
        </div>
      </Card>
      <Card title="Change password">
        <form className="stack" onSubmit={async (e) => {
          e.preventDefault();
          if (pw.next !== pw.again) { toast("Passwords do not match", "red"); return; }
          try { await post("/api/auth/password", { current: pw.current, new: pw.next }); setPw({ current: "", next: "", again: "" }); toast("Password changed; other sessions signed out"); }
          catch (e2) { toast(errorText(e2), "red"); }
        }}>
          <label>Current password<input type="password" autoComplete="current-password" value={pw.current} onChange={(e) => setPw({ ...pw, current: e.target.value })} /></label>
          <label>New password (12+ characters)<input type="password" autoComplete="new-password" value={pw.next} onChange={(e) => setPw({ ...pw, next: e.target.value })} /></label>
          <label>Repeat<input type="password" autoComplete="new-password" value={pw.again} onChange={(e) => setPw({ ...pw, again: e.target.value })} /></label>
          <Button type="submit">Change password</Button>
        </form>
      </Card>
      <Card title="Active sessions">
        {!sessions.data ? <Loading /> : sessions.data.length === 0 ? <Empty>None</Empty> : (
          <ul className="list">{sessions.data.map((x) => <li key={x.id}><div><b>{x.current ? "This device" : x.ip}</b> <span className="small muted">{(x.user_agent || "").slice(0, 60)}</span>
            <div className="small muted">last seen {ago(x.last_seen_at)}</div></div>
            {!x.current && <Button small kind="ghost" onClick={async () => { await del(`/api/auth/sessions/${x.id}`); sessions.reload(); }}>Revoke</Button>}</li>)}</ul>
        )}
      </Card>
      <Card title="Instance">
        <KV rows={[["Symbol", s.data.symbol], ["Paper starting balance", `${s.data.paper_starting_balance} USDT`], ["Binance venue", s.data.testnet ? "testnet" : "mainnet"]]} />
      </Card>
      {node}
    </div>
  );
}

export function Notifications() {
  const n = useApi<{ items: { id: number; ts: string; category: string; title: string; body: string; severity: string; delivered: number; failed: number; suppressed: boolean; error: string | null }[];
    vapid_public_key: string | null; configured: boolean; prefs: Record<string, boolean>; categories: Record<string, string>;
    devices: { id: number; user_agent: string; created_at: string; endpoint_host: string; last_success_at: string | null; failure_count: number }[] }>("/api/notifications", 15000);
  const [state, setState] = useState<{ supported: boolean; permission: string; subscribed: boolean; standalone: boolean; ios: boolean }>();
  const [busy, setBusy] = useState(false);
  const { toast, node } = useToast();
  const refreshState = async () => {
    const ios = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
    const standalone = window.matchMedia("(display-mode: standalone)").matches || (navigator as unknown as { standalone?: boolean }).standalone === true;
    const supported = "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
    let subscribed = false;
    if (supported) {
      const reg = await navigator.serviceWorker.getRegistration();
      subscribed = !!(await reg?.pushManager.getSubscription());
    }
    setState({ supported, permission: "Notification" in window ? Notification.permission : "unsupported", subscribed, standalone, ios });
  };
  useEffect(() => { refreshState(); }, []);
  const enable = async () => {
    setBusy(true);
    try {
      if (!n.data?.vapid_public_key) throw new Error("Server has no VAPID key configured");
      const perm = await Notification.requestPermission();
      if (perm !== "granted") throw new Error("Permission was not granted");
      const reg = await navigator.serviceWorker.ready;
      const key = Uint8Array.from(atob(n.data.vapid_public_key.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((n.data.vapid_public_key.length + 3) % 4)), (c) => c.charCodeAt(0));
      const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
      await post("/api/notifications/subscribe", sub.toJSON());
      toast("Notifications enabled on this device");
    } catch (e) { toast(errorText(e), "red"); } finally { setBusy(false); refreshState(); n.reload(); }
  };
  const disable = async () => {
    const reg = await navigator.serviceWorker.getRegistration();
    const sub = await reg?.pushManager.getSubscription();
    if (sub) { await post("/api/notifications/unsubscribe", { endpoint: sub.endpoint }); await sub.unsubscribe(); }
    toast("Notifications disabled on this device"); refreshState(); n.reload();
  };
  const blocked = state && state.ios && !state.standalone;
  return (
    <div className="grid two">
      <Card title="This device">
        <div className="stack">
          {!n.data?.configured && <Alert tone="red">The server has no VAPID keys — push is not configured (see docs/NOTIFICATIONS.md).</Alert>}
          {blocked && <Alert tone="amber"><b>iPhone/iPad:</b> Web Push only works after installing Kestrel to the Home Screen (iOS 16.4+).
            In Safari tap <b>Share → Add to Home Screen</b>, open Kestrel from the Home Screen icon, then enable notifications here.</Alert>}
          {state && !state.supported && !blocked && <Alert tone="amber">This browser does not support Web Push (needs HTTPS, a service worker and the Push API).</Alert>}
          <KV rows={[["Permission", state?.permission || "—"], ["Subscribed", state?.subscribed ? "yes" : "no"], ["Installed app", state?.standalone ? "yes" : "no (browser tab)"]]} />
          <div className="row wrap">
            {!state?.subscribed ? <Button kind="primary" busy={busy} disabled={!state?.supported || !n.data?.configured} onClick={enable}>Enable notifications</Button>
              : <Button onClick={disable}>Disable notifications</Button>}
            <Button onClick={async () => { try { const r = await post<{ delivered: number; error: string | null }>("/api/notifications/test"); toast(r.delivered ? `Delivered to ${r.delivered} device(s)` : `Not delivered: ${r.error}`, r.delivered ? "green" : "red"); n.reload(); } catch (e) { toast(errorText(e), "red"); } }}>
              Send test notification</Button>
          </div>
        </div>
      </Card>
      <Card title="Notification types">
        {!n.data ? <Loading /> : (
          <div className="stack">
            {Object.entries(n.data.categories).map(([k, label]) => (
              <Toggle key={k} checked={!!n.data!.prefs[k]} label={label}
                onChange={async (v) => { await put("/api/notifications/prefs", { [k]: v }); n.reload(); }} />
            ))}
            <p className="muted small">Critical alerts (emergencies, halts, kill switch) are always delivered.</p>
          </div>
        )}
      </Card>
      <Card title="Registered devices">
        {!n.data ? <Loading /> : n.data.devices.length === 0 ? <Empty>No devices subscribed.</Empty> : (
          <ul className="list">{n.data.devices.map((d) => <li key={d.id}><div><b>{d.endpoint_host}</b> <span className="small muted">{d.user_agent.slice(0, 50)}</span>
            <div className="small muted">added {dt(d.created_at)} · last delivery {ago(d.last_success_at)}{d.failure_count ? ` · ${d.failure_count} failures` : ""}</div></div>
            <Button small kind="ghost" onClick={async () => { await del(`/api/notifications/devices/${d.id}`); n.reload(); }}>Remove</Button></li>)}</ul>
        )}
      </Card>
      <Card title="History" className="span2">
        {!n.data ? <Loading /> : n.data.items.length === 0 ? <Empty>No notifications yet.</Empty> : (
          <div className="table-wrap"><table className="tbl"><thead><tr><th>Time</th><th>Type</th><th>Message</th><th>Delivery</th></tr></thead>
            <tbody>{n.data.items.map((x) => <tr key={x.id}><td>{dt(x.ts)}</td><td><Badge tone={x.severity === "critical" ? "red" : x.severity === "warning" ? "amber" : "gray"}>{x.category}</Badge></td>
              <td><b>{x.title}</b><div className="small muted">{x.body}</div></td>
              <td className="small">{x.suppressed ? `suppressed (${x.error})` : x.delivered ? `✓ ${x.delivered}` : x.error || "—"}</td></tr>)}</tbody></table></div>
        )}
      </Card>
      {node}
    </div>
  );
}

export function useMeTotp() {
  const { me } = useAuth();
  return !!me?.totp_enabled;
}

export async function fetchJSON<T>(p: string): Promise<T> { return get<T>(p); }
