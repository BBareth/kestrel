export const isNum = (x: unknown): x is number => typeof x === "number" && Number.isFinite(x);

export function usd(x: unknown, digits?: number): string {
  if (!isNum(x)) return "—";
  const d = digits ?? (Math.abs(x) >= 1000 ? 1 : 2);
  return "$" + x.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
}

export function price(x: unknown): string {
  if (!isNum(x)) return "—";
  return x.toLocaleString("en-US", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
}

export function num(x: unknown, digits = 2): string {
  if (!isNum(x)) return "—";
  return x.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

export function signed(x: unknown, digits = 2, suffix = ""): string {
  if (!isNum(x)) return "—";
  const s = x > 0 ? "+" : x < 0 ? "−" : "";
  return s + Math.abs(x).toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits }) + suffix;
}

export function pct(x: unknown, digits = 2): string {
  return signed(x, digits, "%");
}

export function pnlClass(x: unknown): string {
  return !isNum(x) || x === 0 ? "" : x > 0 ? "up" : "down";
}

export function compact(x: unknown): string {
  if (!isNum(x)) return "—";
  return Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 2 }).format(x);
}

export function dt(x: unknown): string {
  if (!x) return "—";
  const d = new Date(String(x));
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString(undefined, { month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

export function ago(x: unknown): string {
  if (!x) return "never";
  const t = new Date(String(x)).getTime();
  if (Number.isNaN(t)) return "—";
  const s = Math.round((Date.now() - t) / 1000);
  if (s < 5) return "just now";
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export function fundingPct(x: unknown): string {
  return isNum(x) ? `${(x * 100).toFixed(4)}%` : "—";
}

export function titleCase(s: string | null | undefined): string {
  return (s || "").replace(/_/g, " ").replace(/\b\w/g, (m) => m.toUpperCase());
}
