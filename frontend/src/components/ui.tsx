import { useEffect, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";

export function Card({ title, right, children, className = "", pad = true }: {
  title?: ReactNode; right?: ReactNode; children: ReactNode; className?: string; pad?: boolean;
}) {
  return (
    <section className={`card ${className}`}>
      {(title || right) && (
        <header className="card-h">
          <h2>{title}</h2>
          {right && <div className="card-r">{right}</div>}
        </header>
      )}
      <div className={pad ? "card-b" : ""}>{children}</div>
    </section>
  );
}

export function Stat({ label, value, sub, cls = "", big = false }: {
  label: string; value: ReactNode; sub?: ReactNode; cls?: string; big?: boolean;
}) {
  return (
    <div className={`stat ${big ? "stat-big" : ""}`}>
      <div className="stat-l">{label}</div>
      <div className={`stat-v mono ${cls}`}>{value}</div>
      {sub !== undefined && <div className="stat-s">{sub}</div>}
    </div>
  );
}

export function KV({ rows }: { rows: [ReactNode, ReactNode, string?][] }) {
  return (
    <dl className="kv">
      {rows.map(([k, v, c], i) => (
        <div key={i} className="kv-row">
          <dt>{k}</dt>
          <dd className={`mono ${c || ""}`}>{v}</dd>
        </div>
      ))}
    </dl>
  );
}

type Tone = "green" | "red" | "amber" | "blue" | "violet" | "gray";
export function Badge({ tone = "gray", children, solid = false }: { tone?: Tone; children: ReactNode; solid?: boolean }) {
  return <span className={`badge b-${tone} ${solid ? "solid" : ""}`}>{children}</span>;
}

export function Dot({ status }: { status: string | undefined | null }) {
  const s = status || "unknown";
  const tone = s === "ok" ? "green" : s === "degraded" || s === "stale" || s === "unknown" ? "amber" : s === "disabled" ? "gray" : "red";
  return <span className={`dot d-${tone}`} aria-label={s} />;
}

export function statusTone(s: string | null | undefined): Tone {
  switch (s) {
    case "ok": case "connected": case "executed": case "done": case "closed": case "FILLED": return "green";
    case "degraded": case "stale": case "candidate": case "approved": case "pending": case "running": case "queued": case "NEW": case "unsafe": return "amber";
    case "rejected_risk": case "rejected_ai": case "ai_unavailable": case "execution_failed": case "failed": case "down": case "invalid": case "error": return "red";
    default: return "gray";
  }
}

export function Button({ children, onClick, kind = "default", disabled, busy, type = "button", small, title }: {
  children: ReactNode; onClick?: () => void; kind?: "default" | "primary" | "danger" | "ghost" | "success";
  disabled?: boolean; busy?: boolean; type?: "button" | "submit"; small?: boolean; title?: string;
}) {
  return (
    <button type={type} className={`btn btn-${kind} ${small ? "btn-sm" : ""}`} onClick={onClick} disabled={disabled || busy} title={title}>
      {busy ? <span className="spin" /> : null}
      {children}
    </button>
  );
}

export function Toggle({ checked, onChange, disabled, label }: {
  checked: boolean; onChange: (v: boolean) => void; disabled?: boolean; label?: string;
}) {
  return (
    <label className={`toggle ${disabled ? "dis" : ""}`}>
      <input type="checkbox" checked={checked} disabled={disabled} onChange={(e) => onChange(e.target.checked)} />
      <span className="track"><span className="thumb" /></span>
      {label && <span className="toggle-l">{label}</span>}
    </label>
  );
}

export function Modal({ open, onClose, title, children, wide = false }: {
  open: boolean; onClose: () => void; title: ReactNode; children: ReactNode; wide?: boolean;
}) {
  useEffect(() => {
    if (!open) return;
    const fn = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", fn);
    return () => window.removeEventListener("keydown", fn);
  }, [open, onClose]);
  if (!open) return null;
  // Portal to <body>: ancestors with backdrop-filter (the sticky header) would otherwise
  // become the containing block for position:fixed and clip the dialog.
  return createPortal(
    <div className="modal-bg" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className={`modal ${wide ? "wide" : ""}`} role="dialog" aria-modal="true">
        <header className="modal-h">
          <h3>{title}</h3>
          <button className="x" onClick={onClose} aria-label="Close">×</button>
        </header>
        <div className="modal-b">{children}</div>
      </div>
    </div>,
    document.body,
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

export function Loading() {
  return <div className="empty"><span className="spin" /> Loading…</div>;
}

export function ErrorBox({ error }: { error: string | null | undefined }) {
  if (!error) return null;
  return <div className="alert a-red">{error}</div>;
}

export function Alert({ tone = "amber", children }: { tone?: Tone; children: ReactNode }) {
  return <div className={`alert a-${tone}`}>{children}</div>;
}

export function Tabs<T extends string>({ value, onChange, items }: {
  value: T; onChange: (v: T) => void; items: { value: T; label: ReactNode }[];
}) {
  return (
    <div className="tabs" role="tablist">
      {items.map((it) => (
        <button key={it.value} role="tab" aria-selected={value === it.value} className={value === it.value ? "on" : ""}
          onClick={() => onChange(it.value)}>
          {it.label}
        </button>
      ))}
    </div>
  );
}

export function Meter({ value, max = 100, tone = "blue" }: { value: number; max?: number; tone?: Tone }) {
  const w = Math.max(0, Math.min(100, (value / max) * 100));
  return <div className="meter"><div className={`meter-f f-${tone}`} style={{ width: `${w}%` }} /></div>;
}

export function Checklist({ checks }: { checks: { name: string; passed: boolean; detail: string }[] }) {
  return (
    <ul className="checks">
      {checks.map((c, i) => (
        <li key={i} className={c.passed ? "ok" : "no"}>
          <span className="ci">{c.passed ? "✓" : "✕"}</span>
          <span className="cn">{c.name}</span>
          <span className="cd">{c.detail}</span>
        </li>
      ))}
    </ul>
  );
}

export function Confirm({ open, title, body, confirmLabel, kind = "danger", onConfirm, onClose }: {
  open: boolean; title: string; body: ReactNode; confirmLabel: string; kind?: "danger" | "primary";
  onConfirm: () => Promise<void> | void; onClose: () => void;
}) {
  const [busy, setBusy] = useState(false);
  return (
    <Modal open={open} onClose={onClose} title={title}>
      <div className="stack">
        <div>{body}</div>
        <div className="row end">
          <Button kind="ghost" onClick={onClose}>Cancel</Button>
          <Button kind={kind} busy={busy} onClick={async () => {
            setBusy(true);
            try { await onConfirm(); onClose(); } finally { setBusy(false); }
          }}>{confirmLabel}</Button>
        </div>
      </div>
    </Modal>
  );
}

export function useToast() {
  const [msg, setMsg] = useState<{ text: string; tone: Tone } | null>(null);
  useEffect(() => {
    if (!msg) return;
    const id = window.setTimeout(() => setMsg(null), 4500);
    return () => window.clearTimeout(id);
  }, [msg]);
  const node = msg ? createPortal(<div className={`toast t-${msg.tone}`} role="status">{msg.text}</div>, document.body) : null;
  return { toast: (text: string, tone: Tone = "green") => setMsg({ text, tone }), node };
}
