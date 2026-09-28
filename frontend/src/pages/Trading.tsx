import { useState } from "react";
import { errorText, post } from "../api";
import { KillSwitch, ModePill } from "../components/Layout";
import { Alert, Badge, Button, Card, Confirm, KV, Modal, Toggle, useToast } from "../components/ui";
import { ago } from "../format";
import { useApi } from "../hooks";
import { useLive } from "../live";
import type { ReadinessItem } from "../types";

interface Readiness { ready: boolean; items: ReadinessItem[]; passed: number; total: number }

export function ReadinessList({ r }: { r: Readiness | null }) {
  if (!r) return null;
  return (
    <ul className="ready">
      {r.items.map((i) => (
        <li key={i.key} className={i.ok ? "ok" : "no"}>
          <span className="ci">{i.ok ? "☑" : "☐"}</span>
          <div>
            <div className="rl">{i.label}{!i.required && <span className="muted"> (optional)</span>}</div>
            <div className="rd small">{i.detail}</div>
            {!i.ok && <div className="rh small">→ {i.how}</div>}
          </div>
        </li>
      ))}
    </ul>
  );
}

function LiveWizard({ open, onClose, onDone }: { open: boolean; onClose: () => void; onDone: () => void }) {
  const [step, setStep] = useState(1);
  const [token, setToken] = useState("");
  const [text, setText] = useState("");
  const [ack, setAck] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const reset = () => { setStep(1); setToken(""); setText(""); setAck(false); setErr(null); };
  const close = () => { reset(); onClose(); };
  return (
    <Modal open={open} onClose={close} title="Enable LIVE trading">
      <div className="stack">
        <ol className="steps">
          <li className={step >= 1 ? "on" : ""}>Checklist</li><li className={step >= 2 ? "on" : ""}>Confirm</li><li className={step >= 3 ? "on" : ""}>Enable</li>
        </ol>
        {step === 1 && (
          <>
            <Alert tone="red">Live mode sends <b>real orders</b> with <b>real funds</b> to Binance USDⓈ-M futures. Leverage can cause rapid losses.
              Kestrel cannot predict Bitcoin; past paper results do not guarantee future results.</Alert>
            <p>You will be asked for your password (and 2FA code). The readiness checklist must pass.</p>
            <div className="row end">
              <Button kind="ghost" onClick={close}>Cancel</Button>
              <Button kind="danger" busy={busy} onClick={async () => {
                setBusy(true); setErr(null);
                try {
                  const r = await post<{ token: string }>("/api/trading/live/unlock");
                  setToken(r.token); setStep(2);
                } catch (e) { setErr(errorText(e)); } finally { setBusy(false); }
              }}>Continue</Button>
            </div>
          </>
        )}
        {step >= 2 && (
          <>
            <label>Type <b className="mono">ENABLE LIVE TRADING</b> to confirm
              <input value={text} onChange={(e) => setText(e.target.value)} autoComplete="off" autoCapitalize="characters" spellCheck={false} />
            </label>
            <label className="checkrow"><input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} />
              I understand this can lose money, that the kill switch is my emergency stop, and that no profit is promised.</label>
            <div className="row end">
              <Button kind="ghost" onClick={close}>Cancel</Button>
              <Button kind="danger" busy={busy} disabled={text !== "ENABLE LIVE TRADING" || !ack} onClick={async () => {
                setBusy(true); setErr(null);
                try {
                  await post("/api/trading/live/enable", { token, confirmation: text, acknowledge_risk: ack });
                  setStep(3); onDone();
                } catch (e) { setErr(errorText(e)); } finally { setBusy(false); }
              }}>Enable live trading</Button>
            </div>
          </>
        )}
        {step === 3 && <Alert tone="red">LIVE mode enabled. The strategy is <b>disarmed</b> — arm it explicitly on the Trading page.</Alert>}
        {err && <div className="alert a-red">{err}</div>}
      </div>
    </Modal>
  );
}

export default function Trading() {
  const { trading, engine, refreshTrading } = useLive();
  const readiness = useApi<Readiness>("/api/trading/readiness", 20000);
  const state = useApi<{ live_possible: boolean; testnet: boolean; engine_alive: boolean }>("/api/trading/state", 10000);
  const [wizard, setWizard] = useState(false);
  const [confirmLeave, setConfirmLeave] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const { toast, node } = useToast();
  const act = async (key: string, fn: () => Promise<unknown>, ok: string) => {
    setBusy(key);
    try { await fn(); toast(ok); } catch (e) { toast(errorText(e), "red"); } finally { setBusy(null); refreshTrading(); readiness.reload(); }
  };
  const live = trading?.mode === "live";
  return (
    <div className="grid two">
      <Card title="Trading mode" right={<ModePill big />}>
        <div className="stack">
          <KV rows={[
            ["Mode", live ? <b className="down">LIVE — real orders</b> : <b className="up">PAPER — simulated fills, real market data</b>],
            ["Strategy", trading?.strategy_enabled ? <Badge tone="green">ARMED</Badge> : <Badge>DISARMED</Badge>],
            ["Kill switch", trading?.kill_switch ? <Badge tone="red" solid>ENGAGED</Badge> : <Badge tone="green">off</Badge>],
            ["Halt", trading?.halted ? <span className="down">{trading.halt_reason}</span> : "none"],
            ["Entry guards", engine?.guards?.length ? engine.guards.join("; ") : "all clear"],
            ["Engine", state.data?.engine_alive ? `running · up ${Math.round((engine?.uptime_s || 0) / 60)} min` : <span className="down">not responding</span>],
            ["Execution venue", live ? (state.data?.testnet ? "Binance Futures TESTNET" : "Binance Futures (mainnet)") : "Kestrel paper exchange"],
          ]} />
          <div className="row wrap">
            <Toggle checked={!!trading?.strategy_enabled} disabled={!!trading?.kill_switch || !!trading?.halted || busy === "arm"}
              label={trading?.strategy_enabled ? "Strategy armed (auto-trading)" : "Strategy disarmed"}
              onChange={(v) => act("arm", () => post("/api/trading/strategy", { enabled: v }), v ? "Strategy armed" : "Strategy disarmed")} />
          </div>
          {trading?.kill_switch && (
            <Button kind="primary" busy={busy === "resume"} onClick={() => act("resume", () => post("/api/trading/resume"), "Kill switch released — strategy still disarmed")}>
              Release kill switch</Button>
          )}
          {trading?.halted && (
            <Button kind="primary" busy={busy === "halt"} onClick={() => act("halt", () => post("/api/trading/halt/clear"), "Halt cleared")}>
              Clear halt (after reviewing System)</Button>
          )}
          <div className="row wrap">
            {!live ? (
              <Button kind="danger" disabled={!state.data?.live_possible} onClick={() => setWizard(true)}
                title={state.data?.live_possible ? "" : "No Binance credentials configured"}>Enable LIVE trading…</Button>
            ) : (
              <Button kind="default" onClick={() => setConfirmLeave(true)}>Return to PAPER</Button>
            )}
            <Button kind="ghost" busy={busy === "eval"} onClick={() => act("eval", () => post("/api/trading/evaluate"), "Evaluation complete")}>Evaluate now</Button>
          </div>
          {!state.data?.live_possible && <p className="muted small">Live trading is unavailable until Binance API credentials are configured (see Security).</p>}
        </div>
      </Card>

      <Card title="Emergency stop">
        <div className="stack center">
          <KillSwitch size="lg" />
          <p className="muted small">Stops the strategy, blocks new orders, cancels orders and (by default) flattens positions.
            Behaviour is configurable in Settings → Safety. {trading?.kill_engaged_at && <>Last engaged {ago(trading.kill_engaged_at)}.</>}</p>
          <p className="muted small">Shell fallback: <code>docker compose exec api python -m app.cli kill</code></p>
        </div>
      </Card>

      <Card title={<>Live trading readiness {readiness.data && <Badge tone={readiness.data.ready ? "green" : "amber"}>{readiness.data.passed}/{readiness.data.total}</Badge>}</>}
        className="span2">
        <ReadinessList r={readiness.data} />
      </Card>

      <Card title="Paper account">
        <div className="stack">
          <p className="muted small">Reset the simulated account balance. Only possible while no paper trade is open. History is kept.</p>
          <Button busy={busy === "reset"} onClick={() => act("reset", () => post("/api/trading/paper/reset", { balance: 10000 }), "Paper account reset to 10,000 USDT")}>
            Reset paper balance to 10,000 USDT</Button>
        </div>
      </Card>

      <LiveWizard open={wizard} onClose={() => setWizard(false)} onDone={() => { refreshTrading(); readiness.reload(); }} />
      <Confirm open={confirmLeave} onClose={() => setConfirmLeave(false)} title="Return to paper trading?" confirmLabel="Return to PAPER" kind="primary"
        body="If a live position is open you must close it first (or use the kill switch)."
        onConfirm={() => act("leave", () => post("/api/trading/live/disable", { close_positions: false }), "Back to PAPER trading")} />
      {node}
    </div>
  );
}
