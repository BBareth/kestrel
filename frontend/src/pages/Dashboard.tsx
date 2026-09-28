import { useMemo, useState } from "react";
import { Link } from "react-router";
import { Badge, Card, Checklist, KV, Meter, Stat, Tabs } from "../components/ui";
import { PriceChart, type Candle, type MarkerSpec, type PriceLineSpec } from "../components/charts";
import { compact, fundingPct, num, pct, pnlClass, price, signed, titleCase, usd, ago } from "../format";
import { useApi } from "../hooks";
import { useLive } from "../live";
import type { PositionRow, Trade } from "../types";

interface Dash {
  mode: string;
  account: { equity?: number; available?: number; unrealized?: number; wallet?: number; today_pnl: number; today_trades: number;
    win_rate: number | null; closed_trades: number };
  position: PositionRow | null;
  trade: Trade | null;
  last_signal: { direction: string; status: string; ts: string; confidence: number } | null;
  last_trade: Trade | null;
}

const TREND_TONE: Record<number, "green" | "red" | "gray"> = { 2: "green", 1: "green", 0: "gray", [-1]: "red", [-2]: "red" };

export function TrendChips({ tfs }: { tfs?: Record<string, { trend_score: number; trend: string }> }) {
  if (!tfs) return <span className="muted">—</span>;
  return (
    <div className="chips">
      {["5m", "15m", "1h", "4h"].map((tf) => tfs[tf] && (
        <Badge key={tf} tone={TREND_TONE[tfs[tf].trend_score] || "gray"}>
          {tf} {tfs[tf].trend_score > 0 ? "▲" : tfs[tf].trend_score < 0 ? "▼" : "▬"} {tfs[tf].trend}
        </Badge>
      ))}
    </div>
  );
}

export function SignalPanel({ compactView = false }: { compactView?: boolean }) {
  const { evaluation: ev } = useLive();
  const [side, setSide] = useState<"long" | "short">("long");
  if (!ev) return <div className="muted">Waiting for the first 5-minute evaluation…</div>;
  const s = ev.setup;
  const tone = ev.decision === "LONG" ? "green" : ev.decision === "SHORT" ? "red" : "gray";
  return (
    <div className="signal">
      <div className="sig-top">
        <div className={`sig-dec t-${tone}`}>{ev.decision === "NO_TRADE" ? "NO TRADE" : ev.decision}</div>
        <div className="sig-meta">
          <div className="muted small">Confidence</div>
          <div className="mono big-num">{s ? `${s.confidence.toFixed(0)}%` : "—"}</div>
          {s && <Meter value={s.confidence} tone={tone === "gray" ? "blue" : tone} />}
        </div>
      </div>
      {s ? (
        <KV rows={[
          ["Entry", price(s.entry)], ["Stop", price(s.stop), "down"], ["TP1", price(s.tp1), "up"], ["TP2", price(s.tp2), "up"],
          ["Risk/Reward", num(s.rr, 2)],
        ]} />
      ) : (
        <p className="muted small">{ev.summary.replace(/^NO TRADE — /, "")}</p>
      )}
      {s && (
        <ul className="reasons">
          {s.reasons.map((r, i) => <li key={i}>{r}</li>)}
          {s.warnings.map((w, i) => <li key={`w${i}`} className="warn">{w}</li>)}
        </ul>
      )}
      {!compactView && (
        <details className="more">
          <summary>Strategy checklist · regime {titleCase(ev.regime)} · {ago(ev.evaluated_at)}</summary>
          <Tabs value={side} onChange={setSide} items={[{ value: "long", label: "Long" }, { value: "short", label: "Short" }]} />
          <Checklist checks={side === "long" ? ev.long_checks : ev.short_checks} />
        </details>
      )}
      <p className="fine">NO TRADE is a normal, frequent outcome. Signals are not predictions.</p>
    </div>
  );
}

export function MarketChart({ height = 380, trade }: { height?: number; trade?: Trade | null }) {
  const [interval, setInterval] = useState<"1m" | "5m" | "15m" | "1h" | "4h">("5m");
  const { data } = useApi<{ candles: Candle[] }>(`/api/market/candles?interval=${interval}&limit=400`, 30000);
  const { ticker, evaluation } = useLive();
  const trades = useApi<Trade[]>("/api/trades?limit=40", 60000);
  const live: Candle | null = interval === "5m" && ticker?.forming_5m
    ? [ticker.forming_5m.open_time / 1000, ticker.forming_5m.open, ticker.forming_5m.high, ticker.forming_5m.low,
      ticker.price ?? ticker.forming_5m.close, ticker.forming_5m.volume] : null;
  const lines = useMemo<PriceLineSpec[]>(() => {
    const out: PriceLineSpec[] = [];
    (evaluation?.levels || []).slice(0, 4).forEach((l) => out.push({ price: l.price, color: l.kind === "resistance" ? "#f0465a88" : "#1fc27e88",
      title: `${l.kind === "resistance" ? "R" : "S"}${l.touches > 1 ? "×" + l.touches : ""}`, dashed: true }));
    if (trade?.entry_price) out.push({ price: trade.entry_price, color: "#4c8dff", title: "Entry" });
    if (trade?.stop_price) out.push({ price: trade.stop_price, color: "#f0465a", title: "SL" });
    if (trade?.tp1_price && !trade.tp1_filled) out.push({ price: trade.tp1_price, color: "#1fc27e", title: "TP1" });
    if (trade?.tp2_price) out.push({ price: trade.tp2_price, color: "#1fc27e", title: "TP2" });
    return out;
  }, [evaluation?.levels, trade]);
  const markers = useMemo<MarkerSpec[]>(() => {
    const m: MarkerSpec[] = [];
    const first = data?.candles?.[0]?.[0] ?? 0;
    for (const t of trades.data || []) {
      if (!t.opened_at || t.status === "cancelled") continue;
      const o = Math.floor(new Date(t.opened_at).getTime() / 1000);
      if (o >= first) m.push({ time: o - (o % 300), position: t.direction === "LONG" ? "belowBar" : "aboveBar",
        color: t.direction === "LONG" ? "#1fc27e" : "#f0465a", shape: t.direction === "LONG" ? "arrowUp" : "arrowDown", text: `${t.direction[0]} #${t.id}` });
      if (t.closed_at) {
        const c = Math.floor(new Date(t.closed_at).getTime() / 1000);
        if (c >= first) m.push({ time: c - (c % 300), position: t.direction === "LONG" ? "aboveBar" : "belowBar", color: "#8b95a5",
          shape: "circle", text: `exit ${signed(t.realized_pnl, 0)}` });
      }
    }
    return interval === "5m" ? m : [];
  }, [trades.data, data, interval]);
  return (
    <div>
      <div className="chart-bar">
        <Tabs value={interval} onChange={setInterval} items={(["1m", "5m", "15m", "1h", "4h"] as const).map((v) => ({ value: v, label: v }))} />
        <div className="legend small"><i style={{ background: "#f2b33d" }} />EMA7 <i style={{ background: "#4c8dff" }} />EMA25 <i style={{ background: "#b07bff" }} />EMA99</div>
      </div>
      <PriceChart candles={data?.candles || []} live={live} lines={lines} markers={markers} height={height} />
    </div>
  );
}

export default function Dashboard() {
  const { ticker, evaluation, accounts, trading } = useLive();
  const { data } = useApi<Dash>("/api/dashboard", 5000);
  const mode = trading?.mode || "paper";
  const acct = { ...(data?.account || {}), ...(accounts?.[mode] || {}) };
  const pos = data?.position;
  const tr = data?.trade;
  const tf = evaluation?.timeframes;
  const chg = ticker?.change_24h_pct;
  const upnl = pos?.unrealized_pnl;
  return (
    <div className="grid dash">
      <Card className="hero">
        <div className="hero-row">
          <div>
            <div className="muted small">BTCUSDT Perpetual · Binance USDⓈ-M</div>
            <div className="hero-price mono">{usd(ticker?.price, 1)}</div>
            <div className={`mono ${pnlClass(chg)}`}>{pct(chg)} <span className="muted">24h</span></div>
          </div>
          <div className="hero-stats">
            <Stat label="Mark" value={price(ticker?.mark_price)} />
            <Stat label="24h high" value={price(ticker?.high_24h)} />
            <Stat label="24h low" value={price(ticker?.low_24h)} />
            <Stat label="Spread" value={`${num(ticker?.spread_bps, 2)} bps`} />
          </div>
        </div>
      </Card>

      <Card title="Market" className="market">
        <KV rows={[
          ["Trend", <TrendChips tfs={tf} />],
          ["Regime", titleCase(evaluation?.regime) || "—"],
          ["Volatility (5m ATR)", tf?.["5m"] ? `${num(tf["5m"].atr, 1)} · ${num(tf["5m"].atr_pct, 3)}%` : "—"],
          ["RSI 5m / 1h", tf?.["5m"] ? `${num(tf["5m"].rsi, 1)} / ${num(tf["1h"]?.rsi, 1)}` : "—"],
          ["Funding", fundingPct(ticker?.funding_rate), (ticker?.funding_rate ?? 0) > 0 ? "" : ""],
          ["Volume 24h", `${compact(ticker?.volume_24h)} BTC · $${compact(ticker?.quote_volume_24h)}`],
          ["Open interest", `${compact(ticker?.open_interest)} BTC (${pct(ticker?.oi_change_1h_pct)} 1h)`],
          ["Liquidations 1h", ticker?.liquidations_1h ? `L $${compact(ticker.liquidations_1h.long_liquidations_usdt)} · S $${compact(ticker.liquidations_1h.short_liquidations_usdt)}` : "—"],
        ]} />
      </Card>

      <Card title="Signal" className="signal-card" right={<Link to="/signals" className="small">All signals →</Link>}>
        <SignalPanel />
      </Card>

      <Card title="Current position" className="pos" right={<Badge tone={mode === "live" ? "red" : "green"}>{mode.toUpperCase()}</Badge>}>
        {pos && pos.direction ? (
          <KV rows={[
            ["Direction", <Badge tone={pos.direction === "LONG" ? "green" : "red"} solid>{pos.direction}</Badge>],
            ["Size", `${num(pos.quantity, 3)} BTC`],
            ["Entry", price(pos.entry_price)],
            ["Current", price(ticker?.mark_price ?? pos.mark_price)],
            ["Unrealized P&L", signed(upnl, 2, " USDT"), pnlClass(upnl)],
            ["Stop", price(tr?.stop_price), "down"],
            ["Take profit", tr ? `${tr.tp1_price && !tr.tp1_filled ? price(tr.tp1_price) + " / " : ""}${price(tr.tp2_price)}` : "—", "up"],
            ["Leverage", pos.leverage ? `${pos.leverage}× isolated` : "—"],
            ["Liquidation", price(pos.liquidation_price)],
          ]} />
        ) : (
          <div className="empty small">Flat — no open position.{data?.last_trade && <> Last trade #{data.last_trade.id}: <span className={pnlClass(data.last_trade.realized_pnl)}>{signed(data.last_trade.realized_pnl, 2, " USDT")}</span></>}</div>
        )}
      </Card>

      <Card title="Account" className="acct">
        <div className="stats4">
          <Stat label="Equity" value={usd(acct.equity, 2)} />
          <Stat label="Available margin" value={usd(acct.available, 2)} />
          <Stat label="Today's P&L" value={signed(acct.today_pnl, 2)} cls={pnlClass(acct.today_pnl)} />
          <Stat label="Today's trades" value={acct.today_trades ?? "—"} />
          <Stat label="Win rate" value={acct.win_rate != null ? `${acct.win_rate}%` : "—"} sub={`${acct.closed_trades ?? 0} closed`} />
          <Stat label="Unrealized" value={signed(acct.unrealized, 2)} cls={pnlClass(acct.unrealized)} />
        </div>
      </Card>

      <Card title="Chart" className="chart-card" pad={false}>
        <MarketChart trade={tr} />
      </Card>
    </div>
  );
}
