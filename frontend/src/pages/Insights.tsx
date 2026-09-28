import { useState } from "react";
import { Link } from "react-router";
import { errorText, post } from "../api";
import { BarsChart, HBars, LineChart } from "../components/charts";
import { Alert, Badge, Button, Card, Empty, ErrorBox, KV, Loading, Meter, Modal, Stat, Tabs, statusTone, useToast } from "../components/ui";
import { dt, num, pct, pnlClass, signed, titleCase, usd } from "../format";
import { useApi } from "../hooks";

// ----------------------------------------------------------------------------- AI
interface Usage { configured: boolean; mode: string; model: string; budget_usd: number; today_usd: number; today_calls: number; week_usd: number;
  week_calls: number; month_usd: number; month_calls: number; budget_exceeded: boolean; invalid_30d: number; status: string; detail: string | null;
  by_trigger: { trigger: string; calls: number; cost_usd: number }[]; per_day: { day: string; cost_usd: number }[] }
interface Analysis { id: number; ts: string; trigger: string; model: string; valid: boolean; error: string | null; decision: string | null; confidence: number | null;
  market_regime: string | null; input_tokens: number; output_tokens: number; reasoning_tokens: number; cost_usd: number; latency_ms: number;
  response: { reasoning: string; risk_flags: string[]; invalidation: string; recommended_action: string } | null }

export function AI() {
  const usage = useApi<Usage>("/api/ai/usage", 30000);
  const list = useApi<Analysis[]>("/api/ai/analyses?limit=100", 30000);
  const [sel, setSel] = useState<Analysis | null>(null);
  const [busy, setBusy] = useState(false);
  const { toast, node } = useToast();
  const u = usage.data;
  return (
    <div className="grid two">
      <Card title="AI analysis layer" right={u && <Badge tone={statusTone(u.status)}>{u.status}</Badge>}>
        {!u ? <Loading /> : (
          <div className="stack">
            {!u.configured && <Alert tone="amber">No OpenAI key configured — strategy runs deterministic-only per your AI policy.</Alert>}
            <KV rows={[["Mode", u.mode], ["Model", u.model], ["Status", u.detail || u.status], ["Invalid responses (30d)", u.invalid_30d]]} />
            <p className="muted small">The model receives structured market data (OHLCV, indicators, levels, funding, open interest, the candidate
              and the risk decision) and must answer in a strict JSON schema. It can veto a setup; it can never size, place or modify orders.</p>
            <Button busy={busy} onClick={async () => { setBusy(true); try { await post("/api/ai/analyze"); toast("Analysis requested"); list.reload(); usage.reload(); } catch (e) { toast(errorText(e), "red"); } finally { setBusy(false); } }}>
              Analyze market now</Button>
          </div>
        )}
      </Card>
      <Card title="Cost control">
        {!u ? <Loading /> : (
          <div className="stack">
            <div className="stats4">
              <Stat label="Today" value={usd(u.today_usd, 3)} sub={`${u.today_calls} calls`} />
              <Stat label="7 days" value={usd(u.week_usd, 3)} sub={`${u.week_calls} calls`} />
              <Stat label="30 days" value={usd(u.month_usd, 3)} sub={`${u.month_calls} calls`} />
              <Stat label="Daily budget" value={usd(u.budget_usd, 2)} sub={u.budget_exceeded ? "EXCEEDED" : "ok"} cls={u.budget_exceeded ? "down" : ""} />
            </div>
            <Meter value={u.today_usd} max={Math.max(u.budget_usd, 0.0001)} tone={u.budget_exceeded ? "red" : "blue"} />
            <HBars items={u.by_trigger.map((b) => ({ label: `${b.trigger} ($${b.cost_usd.toFixed(3)})`, value: b.calls }))} />
            {u.per_day.length > 1 && <BarsChart data={u.per_day.map((d) => [Date.parse(d.day) / 1000, d.cost_usd])} height={140} />}
            <p className="muted small">Estimates from token usage × list prices. The model is only called for setups that already passed the risk engine,
              regime changes, position reviews and optional periodic reads.</p>
          </div>
        )}
      </Card>
      <Card title="Analyses" className="span2">
        {!list.data ? <Loading /> : list.data.length === 0 ? <Empty>No AI analyses yet.</Empty> : (
          <div className="table-wrap"><table className="tbl click"><thead><tr><th>Time</th><th>Trigger</th><th>Decision</th><th className="r">Conf.</th><th>Regime</th><th className="r">Tokens</th><th className="r">Cost</th><th className="r">Latency</th></tr></thead>
            <tbody>{list.data.map((a) => <tr key={a.id} onClick={() => setSel(a)}><td>{dt(a.ts)}</td><td>{a.trigger}</td>
              <td>{a.valid ? <Badge tone={a.decision === "LONG" ? "green" : a.decision === "SHORT" ? "red" : "gray"}>{a.decision}</Badge> : <Badge tone="red">invalid</Badge>}</td>
              <td className="r mono">{a.confidence ?? "—"}</td><td className="small">{titleCase(a.market_regime)}</td>
              <td className="r mono">{a.input_tokens + a.output_tokens}</td><td className="r mono">{usd(a.cost_usd, 4)}</td><td className="r mono">{(a.latency_ms / 1000).toFixed(1)}s</td></tr>)}</tbody></table></div>
        )}
      </Card>
      <Modal open={!!sel} onClose={() => setSel(null)} title={`AI analysis #${sel?.id}`} wide>
        {sel && <div className="stack">
          <KV rows={[["Model", sel.model], ["Decision", sel.decision || "—"], ["Confidence", sel.confidence ?? "—"], ["Recommended action", sel.response?.recommended_action || "—"],
            ["Tokens in/out (reasoning)", `${sel.input_tokens} / ${sel.output_tokens} (${sel.reasoning_tokens})`], ["Cost", usd(sel.cost_usd, 4)]]} />
          {sel.response ? <><h4>Reasoning</h4><p>{sel.response.reasoning}</p><h4>Risk flags</h4>
            <ul className="reasons">{sel.response.risk_flags.map((f, i) => <li className="warn" key={i}>{f}</li>)}</ul>
            <p className="small muted">Invalidation: {sel.response.invalidation}</p></> : <div className="alert a-red">{sel.error}</div>}
        </div>}
      </Modal>
      {node}
    </div>
  );
}

// ----------------------------------------------------------------------------- performance
interface Perf { trades: number; pnl: number; roi_pct: number | null; win_rate: number | null; loss_rate: number | null; profit_factor: number | null;
  max_drawdown_pct: number; max_drawdown_usdt: number; avg_trade: number | null; avg_win: number | null; avg_loss: number | null; avg_r: number | null;
  fees: number; funding: number; sharpe_like: number | null; best_trade: { id: number; pnl: number } | null; worst_trade: { id: number; pnl: number } | null;
  long: { trades: number; pnl: number; win_rate: number | null; profit_factor: number | null }; short: { trades: number; pnl: number; win_rate: number | null; profit_factor: number | null };
  equity_curve: [number, number][]; drawdown_curve: [number, number][]; pnl_by_day: { day: string; pnl: number }[]; r_distribution: { bucket: string; count: number; negative: boolean }[];
  exit_reasons: Record<string, number>; starting_equity: number; disclaimer: string }
  // r_distribution is an ordered list: { bucket, count, negative }

export function PerfBody({ m }: { m: Perf }) {
  return (
    <div className="stack">
      <div className="stats6">
        <Stat label="P&L" value={signed(m.pnl, 2)} cls={pnlClass(m.pnl)} sub={`ROI ${pct(m.roi_pct)}`} />
        <Stat label="Trades" value={m.trades} sub={`fees ${num(m.fees, 2)} · funding ${signed(m.funding, 2)}`} />
        <Stat label="Win rate" value={m.win_rate != null ? `${m.win_rate}%` : "—"} sub={`loss ${m.loss_rate ?? "—"}%`} />
        <Stat label="Profit factor" value={num(m.profit_factor, 2)} sub={`avg R ${signed(m.avg_r, 2)}`} />
        <Stat label="Max drawdown" value={`${num(m.max_drawdown_pct, 2)}%`} cls="down" sub={signed(m.max_drawdown_usdt, 2)} />
        <Stat label="Sharpe-like" value={num(m.sharpe_like, 2)} sub="daily, annualised" />
        <Stat label="Avg trade" value={signed(m.avg_trade, 2)} cls={pnlClass(m.avg_trade)} />
        <Stat label="Avg win / loss" value={`${signed(m.avg_win, 1)} / ${signed(m.avg_loss, 1)}`} />
        <Stat label="Best / worst" value={`${signed(m.best_trade?.pnl, 1)} / ${signed(m.worst_trade?.pnl, 1)}`} />
      </div>
      <div className="grid two">
        <div><h4>Equity curve</h4>{m.equity_curve.length > 1 ? <LineChart data={m.equity_curve} baseline={m.starting_equity} /> : <Empty>Not enough data.</Empty>}</div>
        <div><h4>Drawdown %</h4>{m.drawdown_curve.length > 1 ? <LineChart data={m.drawdown_curve} color="#f0465a" /> : <Empty>Not enough data.</Empty>}</div>
        <div><h4>P&L by day</h4>{m.pnl_by_day.length ? <BarsChart data={m.pnl_by_day.map((d) => [Date.parse(d.day) / 1000, d.pnl])} /> : <Empty>No closed trades.</Empty>}</div>
        <div><h4>Win/loss distribution (R)</h4><HBars items={m.r_distribution.map((b) => ({ label: b.bucket, value: b.count, tone: b.negative ? "red" : "green" }))} /></div>
        <div><h4>Long vs short</h4>
          <table className="tbl"><thead><tr><th /><th className="r">Trades</th><th className="r">P&L</th><th className="r">Win %</th><th className="r">PF</th></tr></thead>
            <tbody>{(["long", "short"] as const).map((k) => <tr key={k}><td>{k.toUpperCase()}</td><td className="r mono">{m[k].trades}</td>
              <td className={`r mono ${pnlClass(m[k].pnl)}`}>{signed(m[k].pnl, 2)}</td><td className="r mono">{m[k].win_rate ?? "—"}</td><td className="r mono">{num(m[k].profit_factor, 2)}</td></tr>)}</tbody></table></div>
        <div><h4>Exit reasons</h4><HBars items={Object.entries(m.exit_reasons).map(([k, v]) => ({ label: titleCase(k), value: v }))} /></div>
      </div>
      <p className="fine">{m.disclaimer || "Past performance does not guarantee future results."}</p>
    </div>
  );
}

export function Performance() {
  const [period, setPeriod] = useState<"today" | "7d" | "30d" | "all">("all");
  const [mode, setMode] = useState<"paper" | "live">("paper");
  const { data, error } = useApi<Perf>(`/api/performance?mode=${mode}&period=${period}`, 60000);
  return (
    <Card title="Performance" right={<div className="row wrap">
      <Tabs value={mode} onChange={setMode} items={[{ value: "paper", label: "Paper" }, { value: "live", label: "Live" }]} />
      <Tabs value={period} onChange={setPeriod} items={[{ value: "today", label: "Today" }, { value: "7d", label: "7 days" }, { value: "30d", label: "30 days" }, { value: "all", label: "All time" }]} /></div>}>
      <ErrorBox error={error} />
      {!data ? <Loading /> : <PerfBody m={data} />}
      <p className="small"><Link to="/backtest">Run a backtest →</Link></p>
    </Card>
  );
}

// ----------------------------------------------------------------------------- backtest
interface Run { id: number; status: string; created_at: string; finished_at: string | null; error: string | null;
  params: { start: string; end: string; timeframe: string; starting_balance: number; risk_per_trade_pct: number | null; strategy_version: number };
  summary?: { trades: number; pnl: number; roi_pct: number; win_rate: number; profit_factor: number; max_drawdown_pct: number } }
interface RunDetail extends Run { result: { metrics: Perf; equity_curve: [number, number][]; trades: { direction: string; entry: number; exit: number; opened_at: string; pnl: number; r: number; exit_reason: string }[];
  counts: Record<string, number>; rejections: Record<string, number>; final_equity: number; warnings: string[]; elapsed_s: number; wall_s?: number } | null }

export function Backtest() {
  const today = new Date().toISOString().slice(0, 10);
  const monthAgo = new Date(Date.now() - 30 * 86400000).toISOString().slice(0, 10);
  const [form, setForm] = useState({ start: monthAgo, end: today, timeframe: "5m", starting_balance: 10000, risk_per_trade_pct: 0.5, strategy_version: "" });
  const runs = useApi<Run[]>("/api/backtest", 4000);
  const [sel, setSel] = useState<number | null>(null);
  const detail = useApi<RunDetail>(sel ? `/api/backtest/${sel}` : null, 4000);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const r = detail.data?.result;
  return (
    <div className="grid two">
      <Card title="New backtest">
        <form className="stack" onSubmit={async (e) => {
          e.preventDefault(); setErr(null); setBusy(true);
          try {
            const res = await post<{ id: number }>("/api/backtest", { ...form, strategy_version: form.strategy_version ? Number(form.strategy_version) : null });
            setSel(res.id); runs.reload();
          } catch (e2) { setErr(errorText(e2)); } finally { setBusy(false); }
        }}>
          <div className="row wrap">
            <label>Symbol<input value="BTCUSDT" disabled /></label>
            <label>Execution timeframe<select value={form.timeframe} onChange={(e) => setForm({ ...form, timeframe: e.target.value })}><option value="5m">5m (default)</option><option value="15m">15m</option></select></label>
          </div>
          <div className="row wrap">
            <label>From<input type="date" value={form.start} onChange={(e) => setForm({ ...form, start: e.target.value })} /></label>
            <label>To<input type="date" value={form.end} onChange={(e) => setForm({ ...form, end: e.target.value })} /></label>
          </div>
          <div className="row wrap">
            <label>Starting balance (USDT)<input type="number" value={form.starting_balance} onChange={(e) => setForm({ ...form, starting_balance: Number(e.target.value) })} /></label>
            <label>Risk per trade (%)<input type="number" step="0.05" value={form.risk_per_trade_pct} onChange={(e) => setForm({ ...form, risk_per_trade_pct: Number(e.target.value) })} /></label>
            <label>Strategy version<input placeholder="active" value={form.strategy_version} onChange={(e) => setForm({ ...form, strategy_version: e.target.value })} /></label>
          </div>
          <ErrorBox error={err} />
          <Button type="submit" kind="primary" busy={busy}>Run backtest</Button>
          <Alert tone="amber">Backtests are estimates on historical data with a conservative fill model; AI confirmation is not simulated.
            Tuning parameters until the backtest looks good overfits the past — it says little about the future.</Alert>
        </form>
      </Card>
      <Card title="Runs">
        {!runs.data ? <Loading /> : runs.data.length === 0 ? <Empty>No backtests yet.</Empty> : (
          <div className="table-wrap"><table className="tbl click"><thead><tr><th>#</th><th>Range</th><th>Status</th><th className="r">Trades</th><th className="r">P&L</th><th className="r">PF</th></tr></thead>
            <tbody>{runs.data.map((x) => <tr key={x.id} onClick={() => setSel(x.id)} className={sel === x.id ? "sel" : ""}><td>{x.id}</td>
              <td className="small">{x.params.start} → {x.params.end} · {x.params.timeframe} · v{x.params.strategy_version}</td>
              <td><Badge tone={statusTone(x.status)}>{x.status}</Badge></td><td className="r mono">{x.summary?.trades ?? "—"}</td>
              <td className={`r mono ${pnlClass(x.summary?.pnl)}`}>{signed(x.summary?.pnl, 0)}</td><td className="r mono">{num(x.summary?.profit_factor, 2)}</td></tr>)}</tbody></table></div>
        )}
      </Card>
      {sel && (
        <Card title={`Backtest #${sel}`} className="span2">
          {!detail.data ? <Loading /> : detail.data.status === "failed" ? <div className="alert a-red">{detail.data.error}</div>
            : !r ? <div className="empty"><span className="spin" /> {detail.data.status}… downloading data and simulating.</div> : (
              <div className="stack">
                <KV rows={[["Final equity", usd(r.final_equity, 2)], ["Bars", r.counts.bars], ["Setups / entries", `${r.counts.setups} / ${r.counts.entries}`],
                  ["Rejected by risk", `${r.counts.rejected_risk} (${Object.entries(r.rejections).map(([k, v]) => `${k}: ${v}`).join("; ") || "—"})`],
                  ["Compute time", `${r.elapsed_s}s`]]} />
                <PerfBody m={{ ...r.metrics, equity_curve: r.equity_curve, starting_equity: detail.data.params.starting_balance, disclaimer: r.warnings.join(" ") }} />
                <h4>Trades</h4>
                <div className="table-wrap"><table className="tbl"><thead><tr><th>Opened</th><th>Dir</th><th className="r">Entry</th><th className="r">Exit</th><th className="r">P&L</th><th className="r">R</th><th>Exit</th></tr></thead>
                  <tbody>{r.trades.slice().reverse().map((t, i) => <tr key={i}><td>{dt(t.opened_at)}</td><td>{t.direction}</td><td className="r mono">{num(t.entry, 1)}</td><td className="r mono">{num(t.exit, 1)}</td>
                    <td className={`r mono ${pnlClass(t.pnl)}`}>{signed(t.pnl, 2)}</td><td className="r mono">{signed(t.r, 2)}</td><td className="small">{titleCase(t.exit_reason)}</td></tr>)}</tbody></table></div>
              </div>
            )}
        </Card>
      )}
    </div>
  );
}
