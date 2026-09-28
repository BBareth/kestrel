import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router";
import { errorText, post } from "../api";
import { PriceChart, type Candle, type MarkerSpec, type PriceLineSpec } from "../components/charts";
import { Badge, Card, Checklist, Confirm, Empty, ErrorBox, KV, Loading, Modal, Tabs, statusTone, useToast } from "../components/ui";
import { ago, dt, num, pct, pnlClass, price, signed, titleCase } from "../format";
import { useApi } from "../hooks";
import { useLive } from "../live";
import type { PositionRow, Trade } from "../types";
import { SignalPanel } from "./Dashboard";

const dirBadge = (d: string | null) => d ? <Badge tone={d === "LONG" ? "green" : "red"} solid>{d}</Badge> : null;

// ----------------------------------------------------------------------------- positions
export function Positions() {
  const { data, error, reload } = useApi<{ positions: PositionRow[]; trades: Trade[] }>("/api/positions", 3000);
  const { ticker } = useLive();
  const [closing, setClosing] = useState<Trade | null>(null);
  const { toast, node } = useToast();
  if (!data) return error ? <ErrorBox error={error} /> : <Loading />;
  return (
    <div className="stack">
      {data.trades.length === 0 && <Card><Empty>No open positions. NO TRADE is a valid outcome.</Empty></Card>}
      {data.trades.map((t) => {
        const p = data.positions.find((x) => x.mode === t.mode);
        const mark = ticker?.mark_price ?? p?.mark_price;
        const d = t.direction === "LONG" ? 1 : -1;
        const upnl = p?.unrealized_pnl ?? (mark && t.entry_price ? d * (mark - t.entry_price) * t.remaining_qty : null);
        return (
          <Card key={t.id} title={<>#{t.id} {dirBadge(t.direction)} <Badge tone={t.mode === "live" ? "red" : "green"}>{t.mode.toUpperCase()}</Badge> <Badge tone={statusTone(t.status)}>{t.status}</Badge></>}
            right={<button className="btn btn-danger btn-sm" onClick={() => setClosing(t)}>Close at market</button>}>
            <div className="grid three">
              <KV rows={[["Entry", price(t.entry_price)], ["Current (mark)", price(mark)], ["Unrealized P&L", signed(upnl, 2, " USDT"), pnlClass(upnl)],
                ["Size", `${num(t.remaining_qty, 3)} / ${num(t.quantity, 3)} BTC`]]} />
              <KV rows={[["Stop-loss", price(t.stop_price), "down"], ["TP1", t.tp1_price ? `${price(t.tp1_price)}${t.tp1_filled ? " ✓" : ""}` : "—", "up"],
                ["TP2", price(t.tp2_price), "up"], ["Risk", signed(-(t.risk_usdt ?? 0), 2, " USDT")]]} />
              <KV rows={[["Leverage", `${t.leverage}× isolated`], ["Liquidation", price(p?.liquidation_price ?? t.liquidation_price)],
                ["Opened", dt(t.opened_at)], ["Origin", t.origin]]} />
            </div>
            <p className="small muted">{t.reason}</p>
            <Link to={`/history/${t.id}`} className="small">Full audit trail →</Link>
          </Card>
        );
      })}
      <Confirm open={!!closing} onClose={() => setClosing(null)} title="Close position at market?" confirmLabel="Close position"
        body={closing && <>Closes trade #{closing.id} ({closing.direction}, {num(closing.remaining_qty, 3)} BTC) with a reduce-only market order, then cancels its stop and take-profit.</>}
        onConfirm={async () => {
          try { await post(`/api/trading/close/${closing!.id}`); toast("Position closed"); } catch (e) { toast(errorText(e), "red"); }
          reload();
        }} />
      {node}
    </div>
  );
}

// ----------------------------------------------------------------------------- orders
interface OrderRow { id: number; trade_id: number | null; mode: string; kind: string; client_order_id: string; side: string; type: string;
  quantity: number; trigger_price: number | null; status: string; filled_qty: number; avg_price: number | null; created_at: string; error: string | null; conditional: boolean }

export function Orders() {
  const [mode, setMode] = useState<"all" | "paper" | "live">("all");
  const [active, setActive] = useState(false);
  const q = `/api/orders?limit=300${mode !== "all" ? `&mode=${mode}` : ""}${active ? "&active=true" : ""}`;
  const { data, error } = useApi<OrderRow[]>(q, 5000);
  return (
    <Card title="Orders" right={<div className="row">
      <Tabs value={mode} onChange={setMode} items={[{ value: "all", label: "All" }, { value: "paper", label: "Paper" }, { value: "live", label: "Live" }]} />
      <label className="checkrow small"><input type="checkbox" checked={active} onChange={(e) => setActive(e.target.checked)} /> open only</label></div>}>
      <ErrorBox error={error} />
      {!data ? <Loading /> : data.length === 0 ? <Empty>No orders yet.</Empty> : (
        <div className="table-wrap">
          <table className="tbl">
            <thead><tr><th>Time</th><th>Trade</th><th>Kind</th><th>Side</th><th>Type</th><th className="r">Qty</th><th className="r">Trigger</th><th className="r">Avg fill</th><th>Status</th><th>Mode</th></tr></thead>
            <tbody>{data.map((o) => (
              <tr key={o.id} title={o.error || o.client_order_id}>
                <td>{dt(o.created_at)}</td><td>{o.trade_id ? <Link to={`/history/${o.trade_id}`}>#{o.trade_id}</Link> : "—"}</td>
                <td>{o.kind}</td><td className={o.side === "BUY" ? "up" : "down"}>{o.side}</td><td className="small">{o.type}{o.conditional ? " (algo)" : ""}</td>
                <td className="r mono">{num(o.quantity, 3)}</td><td className="r mono">{price(o.trigger_price)}</td><td className="r mono">{price(o.avg_price)}</td>
                <td><Badge tone={statusTone(o.status)}>{o.status}</Badge></td><td>{o.mode}</td>
              </tr>))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

// ----------------------------------------------------------------------------- signals
interface SignalRow { id: number; ts: string; mode: string; direction: string; status: string; confidence: number; entry: number; stop: number;
  tp1: number; tp2: number; rr: number; reasons: string[]; rejection: string | null; trade_id: number | null; strategy_version: number | null;
  risk_result?: { checks?: { name: string; passed: boolean; detail: string }[] } }

export function Signals() {
  const { data, error } = useApi<SignalRow[]>("/api/signals?limit=200", 15000);
  const [sel, setSel] = useState<number | null>(null);
  const detail = useApi<SignalRow & { ai: { decision: string; confidence: number; response: { reasoning: string; risk_flags: string[] } | null; error: string | null } | null }>(sel ? `/api/signals/${sel}` : null);
  return (
    <div className="grid two">
      <Card title="Latest evaluation"><SignalPanel /></Card>
      <Card title="Signal history" className="span2-lg">
        <ErrorBox error={error} />
        {!data ? <Loading /> : data.length === 0 ? <Empty>No setups detected yet. The strategy is selective — most 5-minute evaluations end in NO TRADE.</Empty> : (
          <div className="table-wrap">
            <table className="tbl click">
              <thead><tr><th>Time</th><th>Dir</th><th className="r">Conf.</th><th className="r">Entry</th><th className="r">R:R</th><th>Outcome</th><th>Why</th></tr></thead>
              <tbody>{data.map((s) => (
                <tr key={s.id} onClick={() => setSel(s.id)}>
                  <td>{dt(s.ts)}</td><td>{dirBadge(s.direction)}</td><td className="r mono">{num(s.confidence, 0)}</td>
                  <td className="r mono">{price(s.entry)}</td><td className="r mono">{num(s.rr, 2)}</td>
                  <td><Badge tone={statusTone(s.status)}>{titleCase(s.status)}</Badge></td>
                  <td className="small clip">{s.trade_id ? `trade #${s.trade_id}` : s.rejection || "—"}</td>
                </tr>))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
      <Modal open={!!sel} onClose={() => setSel(null)} title={`Signal #${sel}`} wide>
        {!detail.data ? <Loading /> : (
          <div className="stack">
            <KV rows={[["Direction", dirBadge(detail.data.direction)], ["Status", titleCase(detail.data.status)], ["Confidence", num(detail.data.confidence, 0)],
              ["Entry / Stop", `${price(detail.data.entry)} / ${price(detail.data.stop)}`], ["TP1 / TP2", `${price(detail.data.tp1)} / ${price(detail.data.tp2)}`],
              ["R:R", num(detail.data.rr, 2)], ["Strategy version", detail.data.strategy_version ?? "—"]]} />
            <h4>Reasons</h4><ul className="reasons">{detail.data.reasons.map((r, i) => <li key={i}>{r}</li>)}</ul>
            {detail.data.rejection && <div className="alert a-amber">{detail.data.rejection}</div>}
            {detail.data.risk_result?.checks && <><h4>Risk engine</h4><Checklist checks={detail.data.risk_result.checks} /></>}
            {detail.data.ai && <><h4>AI analysis</h4><KV rows={[["Decision", detail.data.ai.decision], ["Confidence", detail.data.ai.confidence]]} />
              <p className="small">{detail.data.ai.response?.reasoning || detail.data.ai.error}</p>
              {detail.data.ai.response?.risk_flags?.length ? <ul className="reasons">{detail.data.ai.response.risk_flags.map((f, i) => <li key={i} className="warn">{f}</li>)}</ul> : null}</>}
          </div>
        )}
      </Modal>
    </div>
  );
}

// ----------------------------------------------------------------------------- history (journal + events)
export function History() {
  const [tab, setTab] = useState<"journal" | "events">("journal");
  const [mode, setMode] = useState<"paper" | "live">("paper");
  const trades = useApi<Trade[]>(`/api/trades?mode=${mode}&limit=500`, 30000);
  const [kind, setKind] = useState<"all" | "system" | "risk" | "audit">("all");
  const events = useApi<{ type: string; ts: string; level: string; title: string; message: string }[]>(tab === "events" ? `/api/history/events?kind=${kind}&limit=300` : null, 15000);
  const nav = useNavigate();
  return (
    <Card title="History" right={<Tabs value={tab} onChange={setTab} items={[{ value: "journal", label: "Trade journal" }, { value: "events", label: "Event log" }]} />}>
      {tab === "journal" ? (
        <>
          <div className="row"><Tabs value={mode} onChange={setMode} items={[{ value: "paper", label: "Paper" }, { value: "live", label: "Live" }]} /></div>
          {!trades.data ? <Loading /> : trades.data.length === 0 ? <Empty>No trades yet.</Empty> : (
            <div className="table-wrap">
              <table className="tbl click">
                <thead><tr><th>#</th><th>Opened</th><th>Dir</th><th className="r">Entry</th><th className="r">Exit</th><th className="r">Qty</th><th className="r">Lev</th>
                  <th className="r">Fees</th><th className="r">Funding</th><th className="r">P&L</th><th className="r">P&L %</th><th className="r">R</th><th>Exit</th><th>AI</th><th>v</th><th>Status</th></tr></thead>
                <tbody>{trades.data.map((t) => (
                  <tr key={t.id} onClick={() => nav(`/history/${t.id}`)}>
                    <td>{t.id}</td><td>{dt(t.opened_at || undefined)}</td><td>{dirBadge(t.direction)}</td>
                    <td className="r mono">{price(t.entry_price)}</td><td className="r mono">{price(t.exit_price)}</td><td className="r mono">{num(t.quantity, 3)}</td>
                    <td className="r mono">{t.leverage}×</td><td className="r mono">{num(t.fees, 2)}</td><td className="r mono">{signed(t.funding, 2)}</td>
                    <td className={`r mono ${pnlClass(t.realized_pnl)}`}>{t.status === "closed" ? signed(t.realized_pnl, 2) : "—"}</td>
                    <td className={`r mono ${pnlClass(t.pnl_pct)}`}>{pct(t.pnl_pct, 2)}</td><td className="r mono">{signed(t.r_multiple, 2)}</td>
                    <td className="small">{titleCase(t.exit_reason)}</td><td className="small">{t.ai_decision ? `${t.ai_decision} ${t.ai_confidence ?? ""}` : "—"}</td>
                    <td className="small">{t.strategy_version ?? "—"}</td><td><Badge tone={statusTone(t.status)}>{t.status}</Badge></td>
                  </tr>))}
                </tbody>
              </table>
            </div>
          )}
        </>
      ) : (
        <>
          <Tabs value={kind} onChange={setKind} items={[{ value: "all", label: "All" }, { value: "system", label: "System" }, { value: "risk", label: "Risk" }, { value: "audit", label: "Audit" }]} />
          {!events.data ? <Loading /> : (
            <ul className="timeline">{events.data.map((e, i) => (
              <li key={i} className={`lv-${e.level}`}><span className="tl-t">{dt(e.ts)}</span><Badge tone={e.level === "critical" || e.level === "error" ? "red" : e.level === "warning" ? "amber" : "gray"}>{e.type}</Badge>
                <b>{titleCase(e.title)}</b><span className="small">{e.message}</span></li>))}
            </ul>
          )}
        </>
      )}
    </Card>
  );
}

interface TradeDetailT { trade: Trade; events: { ts: string; step: string; message: string }[]; orders: OrderRow[];
  signal: SignalRow | null; ai: { decision: string; confidence: number; model: string; cost_usd: number; response: { reasoning: string; risk_flags: string[]; invalidation: string; market_regime: string } | null } | null;
  candles: Candle[] }

export function TradeDetail() {
  const { id } = useParams();
  const { data, error } = useApi<TradeDetailT>(`/api/trades/${id}`, 10000);
  if (!data) return error ? <ErrorBox error={error} /> : <Loading />;
  const t = data.trade;
  const lines: PriceLineSpec[] = [];
  if (t.entry_price) lines.push({ price: t.entry_price, color: "#4c8dff", title: "Entry" });
  if (t.initial_stop) lines.push({ price: t.initial_stop, color: "#f0465a", title: "SL" });
  if (t.stop_price && t.stop_price !== t.initial_stop) lines.push({ price: t.stop_price, color: "#f0465a", title: "SL moved", dashed: true });
  if (t.tp1_price) lines.push({ price: t.tp1_price, color: "#1fc27e", title: "TP1", dashed: true });
  if (t.tp2_price) lines.push({ price: t.tp2_price, color: "#1fc27e", title: "TP2" });
  const markers: MarkerSpec[] = [];
  if (t.opened_at) { const s = Math.floor(new Date(t.opened_at).getTime() / 1000); markers.push({ time: s - (s % 300), position: t.direction === "LONG" ? "belowBar" : "aboveBar", color: "#4c8dff", shape: t.direction === "LONG" ? "arrowUp" : "arrowDown", text: "entry" }); }
  if (t.closed_at) { const s = Math.floor(new Date(t.closed_at).getTime() / 1000); markers.push({ time: s - (s % 300), position: "aboveBar", color: "#8b95a5", shape: "circle", text: titleCase(t.exit_reason) }); }
  return (
    <div className="grid two">
      <Card title={<>Trade #{t.id} {dirBadge(t.direction)} <Badge tone={t.mode === "live" ? "red" : "green"}>{t.mode.toUpperCase()}</Badge></>} className="span2" pad={false}>
        {data.candles.length ? <PriceChart candles={data.candles} lines={lines} markers={markers} height={320} /> : <Empty>No chart data for this period.</Empty>}
      </Card>
      <Card title="Journal">
        <KV rows={[["Opened", dt(t.opened_at)], ["Closed", dt(t.closed_at)], ["Entry / Exit", `${price(t.entry_price)} / ${price(t.exit_price)}`],
          ["Quantity", `${num(t.quantity, 3)} BTC @ ${t.leverage}×`], ["Stop / TP", `${price(t.initial_stop)} / ${price(t.tp1_price)} / ${price(t.tp2_price)}`],
          ["Fees", num(t.fees, 2)], ["Funding", signed(t.funding, 2)], ["Net P&L", signed(t.realized_pnl, 2, " USDT"), pnlClass(t.realized_pnl)],
          ["P&L %", pct(t.pnl_pct)], ["R multiple", signed(t.r_multiple, 2)], ["Exit reason", titleCase(t.exit_reason)],
          ["Strategy version", t.strategy_version ?? "—"], ["Confidence", num(t.confidence, 0)], ["AI", t.ai_decision ? `${t.ai_decision} (${t.ai_confidence})` : "—"],
          ["Market regime", titleCase(t.market_regime)]]} />
        <h4>Reasons</h4><ul className="reasons">{(t.reasons || []).map((r, i) => <li key={i}>{r}</li>)}</ul>
        {data.ai?.response && <><h4>AI view</h4><p className="small">{data.ai.response.reasoning}</p><p className="small muted">Invalidation: {data.ai.response.invalidation}</p></>}
      </Card>
      <Card title="Audit trail">
        <ol className="audit">{data.events.map((e, i) => <li key={i}><span className="tl-t">{dt(e.ts)}</span><b>{titleCase(e.step)}</b><span className="small">{e.message}</span></li>)}</ol>
      </Card>
      <Card title="Orders" className="span2">
        <div className="table-wrap"><table className="tbl">
          <thead><tr><th>Kind</th><th>Side</th><th>Type</th><th className="r">Qty</th><th className="r">Trigger</th><th className="r">Fill</th><th>Status</th><th>Client id</th></tr></thead>
          <tbody>{data.orders.map((o) => <tr key={o.id} title={o.error || ""}><td>{o.kind}</td><td>{o.side}</td><td className="small">{o.type}</td><td className="r mono">{num(o.quantity, 3)}</td>
            <td className="r mono">{price(o.trigger_price)}</td><td className="r mono">{price(o.avg_price)}</td><td><Badge tone={statusTone(o.status)}>{o.status}</Badge></td><td className="small mono">{o.client_order_id}</td></tr>)}</tbody>
        </table></div>
        <p className="muted small">Updated {ago(new Date().toISOString())}</p>
      </Card>
    </div>
  );
}
