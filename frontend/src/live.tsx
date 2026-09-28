import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { get } from "./api";
import type { EngineState, Evaluation, Ticker, TradingState } from "./types";

interface LiveData {
  ticker: Ticker | null;
  evaluation: Evaluation | null;
  engine: EngineState | null;
  trading: TradingState | null;
  accounts: Record<string, { equity: number; wallet: number; available: number; unrealized: number }> | null;
  connected: boolean;
  lastMessageAt: number;
  refreshTrading: () => void;
}

const Ctx = createContext<LiveData | null>(null);

export function LiveProvider({ children }: { children: ReactNode }) {
  const [ticker, setTicker] = useState<Ticker | null>(null);
  const [evaluation, setEvaluation] = useState<Evaluation | null>(null);
  const [engine, setEngine] = useState<EngineState | null>(null);
  const [trading, setTrading] = useState<TradingState | null>(null);
  const [accounts, setAccounts] = useState<LiveData["accounts"]>(null);
  const [connected, setConnected] = useState(false);
  const [lastMessageAt, setLast] = useState(0);
  const esRef = useRef<EventSource | null>(null);

  const refreshTrading = useCallback(() => {
    get<TradingState>("/api/trading/state").then(setTrading).catch(() => {});
  }, []);

  useEffect(() => {
    let closed = false;
    let retry: number | undefined;
    const open = () => {
      if (closed) return;
      const es = new EventSource("/api/stream", { withCredentials: true });
      esRef.current = es;
      const on = <T,>(name: string, fn: (v: T) => void) =>
        es.addEventListener(name, (ev) => {
          try {
            fn(JSON.parse((ev as MessageEvent).data));
            setLast(Date.now());
          } catch {
            /* ignore malformed frame */
          }
        });
      on<Ticker>("ticker", setTicker);
      on<Evaluation>("evaluation", setEvaluation);
      on<EngineState>("engine", setEngine);
      on<TradingState>("trading", setTrading);
      on<LiveData["accounts"]>("accounts", setAccounts);
      es.onopen = () => setConnected(true);
      es.onerror = () => {
        setConnected(false);
        es.close();
        retry = window.setTimeout(open, 3000);
      };
    };
    open();
    refreshTrading();
    const onVis = () => {
      if (document.visibilityState === "visible" && esRef.current?.readyState !== EventSource.OPEN) {
        esRef.current?.close();
        window.clearTimeout(retry);
        open();
      }
    };
    document.addEventListener("visibilitychange", onVis);
    return () => {
      closed = true;
      window.clearTimeout(retry);
      esRef.current?.close();
      document.removeEventListener("visibilitychange", onVis);
    };
  }, [refreshTrading]);

  return (
    <Ctx.Provider value={{ ticker, evaluation, engine, trading, accounts, connected, lastMessageAt, refreshTrading }}>
      {children}
    </Ctx.Provider>
  );
}

export function useLive(): LiveData {
  const v = useContext(Ctx);
  if (!v) throw new Error("useLive outside LiveProvider");
  return v;
}
