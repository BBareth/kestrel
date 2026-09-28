# Architecture

## Services (docker compose project `kestrel`)

| Service | Image | Networks | Purpose |
|---|---|---|---|
| `db` | postgres:17-alpine | internal | All state: trades, orders, signals, AI analyses, audit, settings, candles |
| `migrate` | kestrel-backend | internal | One-shot `alembic upgrade head` before api/engine start |
| `engine` | kestrel-backend | internal + edge | The trading engine (single instance, PostgreSQL advisory lock) |
| `api` | kestrel-backend | internal + edge | FastAPI: auth, UI data, commands, SSE stream |
| `web` | kestrel-web (Caddy) | edge | TLS, static PWA, `/api` reverse proxy, CA download on :80 |
| `backup` | postgres:17-alpine | internal | Daily verified `pg_dump`, reports status to the DB |

`internal` is an `internal: true` network (no internet route); only `engine`, `api` and `web` sit on
`edge`. Only `web` publishes ports (8443 HTTPS, 8089 HTTP for the CA). PostgreSQL and the API are not
reachable from the LAN.

## Engine ↔ API

The engine and the API are separate processes that share only PostgreSQL:

* **Commands** (kill switch, manual close, analyze now, reconcile, protection self-test, paper reset)
  are rows in `commands`; the engine polls every second and writes the result back. The API waits for
  the result. If the engine is down, the kill switch is executed by the API itself.
* **Live state** (ticker, latest evaluation, engine status, account equity) is published by the
  engine into `live_state` every second; the API streams it to browsers over Server-Sent Events.
* The trading state (`mode`, `kill_switch`, `strategy_enabled`, `halted`) is a settings row both read.

Only one engine can trade: it holds `pg_try_advisory_lock(7072026)` for its lifetime; a second
instance waits instead of trading.

## Pipeline

```
Binance WS (/market: klines 1m–4h, markPrice@1s, ticker, forceOrder; /public: bookTicker)
   + REST (backfill, open interest, fallback when the socket is quiet)
        │
        ▼
MarketDataService ── closed candles per timeframe, mark/last/book, funding, OI, liquidations
        │ on every 5m close (+1.5 s so 15m/1h/4h closes land)
        ▼
features (EMA 7/25/99, RSI, ATR, VWAP, volume SMA, swing pivots) → TFView per timeframe
        ▼
BreakoutRetestStrategy.evaluate → Evaluation {decision, setup, long/short checklists, regime, levels}
        │ setup?
        ▼
RiskEngine.evaluate (gates, limits, sizing)  ──✗── signal "rejected_risk"
        ▼
AIService.confirm_setup (only after risk approved; veto only; budget + policy) ──✗── "rejected_ai" / "ai_unavailable"
        ▼
re-check trading state (kill switch might have been pressed meanwhile)
        ▼
ExecutionEngine.open_trade — venue-side pre-checks → MARKET entry → verify fill
        → STOP_MARKET (reduce-only, mark price) placed + VERIFIED → TP1/TP2 placed + verified
        (stop cannot be verified ⇒ flatten + halt + critical alert)
        ▼
PositionMonitor (1 s paper / 3 s live) — TP1 → stop to breakeven (place new, then cancel old),
        missing stop → re-place (or flatten + halt), flat → settle P&L from own fills + funding
        ▼
Reconciler (startup, every 60 s live / 5 min paper) — venue is the truth: resume, settle, adopt,
        cancel orphans, never duplicate
```

## Paper vs live

Everything above the `ExecutionAdapter` interface is shared. Only the adapter changes:

* `PaperExchange` — simulated venue fed by live Binance prices: book-side fills + slippage, taker fee,
  mark-price triggered reduce-only stops/TPs with extra stop slippage (gaps fill worse), funding at each
  funding timestamp, isolated-margin liquidation, "would immediately trigger" rejections, persisted
  state (survives restarts).
* `BinanceFuturesAdapter` — signed USDⓈ-M REST (v3 account/positions, Algo Order API for conditional
  orders, `clientOrderId`/`clientAlgoId` idempotency, unknown-status handling, server-time offset).

## Safety supervisor

Every 5 s the engine evaluates guards (auto-clearing: stale data, clock skew, live credentials not
connected) and halts (latching, need a human: data stale > N min, API error bursts, risk calculation
failure, unverifiable stop-loss, liquidation, unexpected positions). Every loop is wrapped so one
failing task never stops the others; failure inside the decision pipeline always means NO TRADE.

## Data model (main tables)

`users`, `sessions`, `settings`, `strategy_configs` (versioned), `exchange_connections` (status only —
no secrets), `candles`, `funding_rates`, `market_snapshots`, `signals`, `ai_analyses`, `trades`,
`trade_events` (per-trade audit trail), `orders`, `positions`, `equity_snapshots`, `risk_events`,
`system_events`, `audit_logs`, `notifications`, `push_subscriptions`, `component_status`,
`live_state`, `commands`, `backtest_runs`. Schema is managed by Alembic (`backend/migrations`).

## Source layout

```
backend/app/
  config.py            env settings (secrets live only here, from the environment)
  logging_setup.py     JSON logs + secret redaction
  core/params.py       every tunable parameter: default, range, hard cap, explanation
  core/store.py        data access  ·  core/readiness.py  live-trading checklist
  exchange/            base adapter, binance_rest, paper, credentials (key-permission check)
  market/              indicators, features, data_service (WS + REST)
  strategy/            base types, breakout_retest
  risk/engine.py       gates, loss limits, cooldowns, sizing
  ai/                  schema (strict JSON), provider (OpenAI Responses API), service (policy/budget/cache), context
  execution/           engine (entry/protection/flatten/settle), monitor (monitor + reconciler)
  notifications/       Web Push (VAPID)
  engine/              runner (orchestration, supervisor, commands), main (advisory lock)
  backtest/            engine (bar-by-bar, same strategy + risk), data (historical download/cache)
  performance/         metrics
  api/                 FastAPI app, auth/security, routes
  cli.py               create-admin, reset-password, disable-totp, kill, status, gen-vapid, gen-secret
frontend/src/          React PWA (pages/, components/, live SSE store, api client)
frontend/public/       manifest, service worker, icons, iOS splash screens
deploy/                Caddyfile, backup.sh, e2e overrides   ·   scripts/  deploy, init-env, gen-vapid, icons
```
