import { useState, type ReactNode } from "react";
import { price } from "../format";
import type { Check, Evaluation, PositionRow, Trade, TradingState } from "../types";

/*
 * Plain-language reading of the latest strategy evaluation: where the long and the short
 * setup stand, step by step, and what is still missing. It only re-describes the checks the
 * strategy already ran (long_checks / short_checks); it adds no rules of its own.
 */

type Side = "long" | "short";

const STEPS: { key: string; checks: string[]; title: string; want: Record<Side, string> }[] = [
  {
    key: "trend", checks: ["data", "1h trend", "4h trend", "15m trend"], title: "Bigger trend allows it",
    want: {
      long: "The 1-hour and 4-hour trends are not pointing down, and the 15-minute trend isn't against it.",
      short: "The 1-hour and 4-hour trends are not pointing up, and the 15-minute trend isn't against it.",
    },
  },
  {
    key: "momentum", checks: ["volatility", "5m momentum", "RSI"], title: "Short-term momentum",
    want: {
      long: "On the 5-minute chart the yellow line (EMA 7) is above the blue line (EMA 25), buyers are in control (RSI) but not overheated, and price moves a normal amount.",
      short: "On the 5-minute chart the yellow line (EMA 7) is below the blue line (EMA 25), sellers are in control (RSI) but not exhausted, and price moves a normal amount.",
    },
  },
  {
    key: "breakout", checks: ["breakout", "breakout volume"], title: "Breakout with volume",
    want: {
      long: "Price closes clearly above a resistance line (red dashed R on the chart) with a green candle and higher-than-normal volume.",
      short: "Price closes clearly below a support line (green dashed S on the chart) with a red candle and higher-than-normal volume.",
    },
  },
  {
    key: "retest", checks: ["retest", "retest held"], title: "Retest holds",
    want: {
      long: "Price comes back down to that old ceiling and bounces off it — it now acts as a floor.",
      short: "Price comes back up to that old floor and gets rejected — it now acts as a ceiling.",
    },
  },
  {
    key: "entry", checks: ["confirmation", "not extended"], title: "Entry candle",
    want: {
      long: "A green 5-minute candle closes above the yellow line, and price hasn't already run far away from the level.",
      short: "A red 5-minute candle closes below the yellow line, and price hasn't already run far away from the level.",
    },
  },
  {
    key: "plan", checks: ["stop distance", "room to target", "reward:risk", "confidence", "signal cooldown"], title: "Trade plan makes sense",
    want: {
      long: "The stop-loss fits under the retest, the targets pay at least 2× the risk, and nothing big is in the way.",
      short: "The stop-loss fits above the retest, the targets pay at least 2× the risk, and nothing big is in the way.",
    },
  },
];

/** What a failed check means, in plain words. `detail` carries the live numbers. */
function explainFailure(c: Check, side: Side): string {
  const up = side === "long";
  const d = c.detail;
  switch (c.name) {
    case "data": return "Kestrel is still collecting enough price history.";
    case "1h trend": return up
      ? `The 1-hour trend is down (${d}). Kestrel only buys when the hourly trend is flat or up.`
      : `The 1-hour trend is up (${d}). Kestrel only sells short when the hourly trend is flat or down.`;
    case "4h trend": return up
      ? `The 4-hour trend is strongly down (${d}). Buying against it is too risky.`
      : `The 4-hour trend is strongly up (${d}). Shorting against it is too risky.`;
    case "15m trend": return `The 15-minute trend points the other way (${d}).`;
    case "volatility": return `Price is moving too little or too much for this strategy right now (${d}).`;
    case "5m momentum": return up
      ? `Short-term momentum is down: ${d}. Wait for the yellow line to cross above the blue one.`
      : `Short-term momentum is up: ${d}. Wait for the yellow line to cross below the blue one.`;
    case "RSI": return up
      ? `Buying pressure (RSI) is outside the range Kestrel buys in: ${d}.`
      : `Selling pressure (RSI) is outside the range Kestrel shorts in: ${d}.`;
    case "breakout": return d.startsWith("no ")
      ? (up
        ? "No resistance line has been broken recently. Watch for price closing above a red dashed R line."
        : "No support line has been broken recently. Watch for price closing below a green dashed S line.")
      : `The break was too weak — it has to close clearly ${up ? "above" : "below"} the level with a ${up ? "green" : "red"} candle (${d}).`;
    case "breakout volume": return `Price broke the level, but on too little volume (${d}). Weak breakouts often fail, so Kestrel skips them.`;
    case "retest": return d.includes("just happened")
      ? "The breakout just happened. Now Kestrel waits for price to come back and test the level."
      : `Price hasn't come back to the broken level yet (${d}). Kestrel waits for the pullback instead of chasing.`;
    case "retest held": return up
      ? "Price fell back below the level — the breakout failed."
      : "Price climbed back above the level — the breakdown failed.";
    case "confirmation": return up
      ? `Waiting for a green candle that closes above the yellow line (${d}).`
      : `Waiting for a red candle that closes below the yellow line (${d}).`;
    case "not extended": return `Price already ran too far from the level (${d}) — entering now would be chasing.`;
    case "stop distance": return `The stop-loss would have to sit too far away (${d}).`;
    case "room to target": return `A bigger 1-hour level is in the way before the first target (${d}).`;
    case "reward:risk": return `The possible profit is too small compared with the risk (${d}).`;
    case "confidence": return `Everything lined up, but the setup score is too low (${d}).`;
    case "signal cooldown": return "A signal in this direction fired a few minutes ago; Kestrel waits before repeating it.";
    default: return d || c.name;
  }
}

type StepState = "done" | "blocked" | "open";

function assess(checks: Check[]) {
  const byName = new Map(checks.map((c) => [c.name, c]));
  const states: StepState[] = STEPS.map((s) => {
    const cs = s.checks.map((n) => byName.get(n)).filter((c): c is Check => !!c);
    if (cs.some((c) => !c.passed)) return "blocked";
    // Every check of the step must have run: the strategy stops early after some failures.
    const needed = s.checks.filter((n) => n !== "data");
    return needed.every((n) => byName.has(n)) ? "done" : "open";
  });
  const firstFail = checks.find((c) => !c.passed) || null;
  const firstFailStep = firstFail ? STEPS.findIndex((s) => s.checks.includes(firstFail.name)) : -1;
  return { states, done: states.filter((s) => s === "done").length, firstFail, firstFailStep };
}

function Ladder({ side, ev }: { side: Side; ev: Evaluation }) {
  const checks = side === "long" ? ev.long_checks : ev.short_checks;
  const a = assess(checks);
  const up = side === "long";
  const complete = ev.decision === (up ? "LONG" : "SHORT");
  return (
    <div className={`ladder ${up ? "l-long" : "l-short"}`}>
      <div className="ladder-h">
        <div>
          <div className="ladder-t">{up ? "Go LONG" : "Go SHORT"}</div>
          <div className="muted small">{up ? "buy — profits if the price rises" : "sell — profits if the price falls"}</div>
        </div>
        <div className="ladder-n mono">{complete ? "ready" : `${a.done}/${STEPS.length}`}</div>
      </div>
      <ol className="gsteps">
        {STEPS.map((s, i) => {
          const st = a.states[i];
          const isBlocker = i === a.firstFailStep;
          return (
            <li key={s.key} className={`st-${st} ${isBlocker ? "st-now" : ""}`}>
              <span className="si" aria-label={st}>{st === "done" ? "✓" : st === "blocked" ? "✕" : i + 1}</span>
              <div>
                <div className="sn">{s.title}</div>
                <div className="sw">{s.want[side]}</div>
                {isBlocker && a.firstFail && <div className="sb">Missing: {explainFailure(a.firstFail, side)}</div>}
              </div>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

function headline(ev: Evaluation | null, trading: TradingState | null, pos: PositionRow | null | undefined,
  trade: Trade | null | undefined): { tone: string; title: string; body: ReactNode } {
  const paper = (trading?.mode || "paper") === "paper";
  const money = paper ? " (paper money, nothing real)" : " (REAL money)";
  if (pos && pos.direction) {
    const long = pos.direction === "LONG";
    return {
      tone: long ? "green" : "red",
      title: `Kestrel is in a ${pos.direction} trade${money}.`,
      body: <>It {long ? "bought and profits if the price rises" : "sold short and profits if the price falls"}.
        It exits by itself: stop-loss at <b className="mono">{price(trade?.stop_price)}</b> if it goes wrong,
        {trade?.tp1_price && !trade.tp1_filled ? <> first target at <b className="mono">{price(trade.tp1_price)}</b>,</> : null}
        {" "}final target at <b className="mono">{price(trade?.tp2_price)}</b>. You don't need to do anything.</>,
    };
  }
  if (trading?.kill_switch) return { tone: "red", title: "Trading is stopped (kill switch).", body: "Kestrel will not open trades until you resume on the Trading page." };
  if (trading?.halted) return { tone: "amber", title: "Trading is paused by a safety check.", body: trading.halt_reason || "See the System page for the reason." };
  if (trading && !trading.strategy_enabled) return { tone: "amber", title: "The strategy is switched off.", body: "Kestrel still watches the market but won't open trades. Turn it on under Trading." };
  if (!ev) return { tone: "gray", title: "Waiting for the first 5-minute candle to close…", body: "" };
  if (ev.decision !== "NO_TRADE") {
    const long = ev.decision === "LONG";
    return {
      tone: long ? "green" : "red",
      title: `${ev.decision} setup found — all six steps are met.`,
      body: <>Kestrel wants to {long ? "buy (profit if the price rises)" : "sell short (profit if the price falls)"}{money}.
        The risk limits and the AI check run next; if they agree it enters by itself with the stop-loss and targets shown under Signal.</>,
    };
  }
  const l = assess(ev.long_checks).done;
  const s = assess(ev.short_checks).done;
  const nearer = l === s ? null : l > s ? "LONG" : "SHORT";
  return {
    tone: "gray",
    title: "No trade right now — Kestrel is waiting.",
    body: <>A trade needs all six steps below on one side. {nearer
      ? <>Closer to a setup: <b>{nearer}</b> ({Math.max(l, s)} of 6 steps met).</>
      : <>Both sides are equally far away ({l} of 6 steps met).</>} This is the normal state — setups are rare, and waiting is part of the strategy.</>,
  };
}

export function SetupGuide({ ev, trading, pos, trade }: {
  ev: Evaluation | null; trading: TradingState | null; pos?: PositionRow | null; trade?: Trade | null;
}) {
  const h = headline(ev, trading, pos, trade);
  return (
    <div className="guide">
      <div className={`guide-head g-${h.tone}`}>
        <div className="guide-title">{h.title}</div>
        {h.body && <div className="guide-body">{h.body}</div>}
      </div>
      {ev && (
        <div className="ladders">
          <Ladder side="long" ev={ev} />
          <Ladder side="short" ev={ev} />
        </div>
      )}
      <p className="fine">
        These are Kestrel's own rules, checked on every closed 5-minute candle. They describe when <i>Kestrel</i> trades — not a
        prediction and not financial advice. Kestrel opens and closes trades itself; you don't have to act on this.
      </p>
    </div>
  );
}

/** Dashboard-wide "show explanations" switch, remembered per browser. */
export function useExplain(): [boolean, (v: boolean) => void] {
  const [on, setOn] = useState<boolean>(() => {
    try { return localStorage.getItem("kestrel.explain") !== "0"; } catch { return true; }
  });
  const set = (v: boolean) => {
    setOn(v);
    try { localStorage.setItem("kestrel.explain", v ? "1" : "0"); } catch { /* storage unavailable */ }
  };
  return [on, set];
}

/** A KV label with an optional one-line explanation under it. */
export function Term({ label, hint, show }: { label: string; hint: string; show: boolean }) {
  return <span className="term">{label}{show && <span className="term-h">{hint}</span>}</span>;
}
