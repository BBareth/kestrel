# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [1.1.0] — 2026-09-28

### Added
- **In plain words** panel at the top of the dashboard: a one-sentence summary of what Kestrel is
  doing (waiting, which side is closer to a setup, a setup found, or an open trade with its exits),
  and side-by-side *Go LONG* / *Go SHORT* ladders with the six steps each setup needs, which are
  met, and what is missing — translated from the strategy's own checklist, no rules of its own.
- *Explain everything* switch (on by default, remembered per browser): one-line explanations under
  every market figure, the signal, the account and a key for the chart.

### Changed
- The repository is public. Security reports go through GitHub private vulnerability reporting.

## [1.0.0] — 2026-09-28

First release. Paper trading by default; live trading behind a checklist-gated unlock.

### Added
- Market data for Binance BTCUSDT perpetual: REST backfill plus the 2026 WebSocket layout
  (`/market` for klines 1m–4h, mark price, 24h ticker and liquidations; `/public` for the book
  ticker), with a REST fallback when the streams go quiet and freshness tracking for every input.
- Breakout–retest strategy on 5m with 15m/1h/4h context, a full pass/fail checklist on every
  evaluation, and every parameter editable, explained and versioned in the UI.
- Risk engine: position size from equity × risk ÷ stop distance (fees included, rounded down);
  daily/weekly loss limits, losing-streak and per-loss cooldowns, trades per hour/day, one position,
  funding, spread, slippage, liquidation-distance and trading-window gates; hard caps in code.
- AI confirmation layer on the OpenAI Responses API with a strict JSON schema, re-validated locally.
  It is only consulted after the risk engine approves, can only veto, has a daily budget, and falls
  back by explicit policy (default: no trade).
- Execution engine shared by paper and live: venue-side pre-checks, idempotent client ids,
  unknown-status resolution by query, Binance Algo Order API for the reduce-only stop-loss and
  take-profits, verification of every protective order, emergency flatten + halt when a stop cannot
  be verified, place-new-then-cancel-old stop moves, breakeven after TP1.
- Position monitor and reconciliation (startup and periodic): resumes interrupted entries, settles
  trades closed while offline, adopts or flattens unknown positions by policy, cancels orphans,
  never re-sends blindly.
- Paper exchange: book-side fills, slippage, fees, mark-triggered stops with gap slippage, funding,
  isolated-margin liquidation; state survives restarts.
- Kill switch in the UI, the CLI and — if the engine is down — executed by the API itself.
- Safety supervisor: stale data, clock skew, API error bursts, unsafe credentials.
- Binance key safety check (withdrawals disabled, IP restriction, futures, one-way, single-asset).
- 15-item live-readiness checklist and a multi-step live unlock with step-up authentication.
- Backtester on the same strategy and risk engine, with a conservative fill model.
- Performance analytics: P&L, ROI, win rate, profit factor, drawdown, Sharpe-like, long/short, R
  distribution, equity and drawdown curves.
- Full audit trail per trade, plus system, risk and audit event logs.
- Authentication: argon2id, server-side sessions, CSRF, rate limiting, TOTP 2FA with recovery codes.
- React PWA: installable on iPhone, offline shell, Web Push (VAPID) with per-category preferences.
- Docker Compose deployment with Caddy (internal CA), PostgreSQL 17, daily verified backups,
  hardened containers, health checks.
- 149 automated tests (SQLite and PostgreSQL), including failure injection for the
  "something goes wrong while a live position is open" cases.

[1.1.0]: https://github.com/BBareth/kestrel/releases/tag/v1.1.0
[1.0.0]: https://github.com/BBareth/kestrel/releases/tag/v1.0.0
