# Backtesting

**Backtest** page: choose symbol (BTCUSDT), execution timeframe (5m default, or 15m), date range
(up to 365 days), strategy version (default: active), starting balance and risk per trade.

## What it does

* Downloads Binance klines for the primary and context timeframes (plus 600 bars of warm-up) and the
  historical funding rates; caches them in the database, so re-runs are fast.
* Walks the primary timeframe bar by bar. Indicators are computed once on the full series, but the
  strategy only ever reads values at or before the current bar, pivots are only used once confirmed, and
  higher-timeframe context uses the last **closed** candle — no look-ahead (covered by
  `test_no_lookahead_evaluation_before_breakout`).
* Uses the **same** strategy code and the **same** risk engine (daily/weekly limits, streaks,
  cooldowns, trades/hour, funding window, sizing) as live trading.
* Fills: entry at the signal bar close + slippage + taker fee; stops/targets checked on later bars'
  high/low; **if a bar touches both, the stop is assumed first**; stops fill at the stop (or the open if
  it gapped) minus stop slippage; funding applied at each timestamp; isolated liquidation.
* Runs in a separate worker process (the API stays responsive).

## Output

Total return, win/loss rate, profit factor, max drawdown, average trade / win / loss, number of trades,
Sharpe-like metric (daily P&L, annualised with √365), long vs short, exit reasons, R-multiple
distribution, P&L by day, equity curve, every trade, setup/rejection counts.

## Limitations — read before trusting a number

* **AI confirmation is not simulated** (a model cannot be replayed faithfully on the past).
* Historical spread is unknown; a constant 0.5 bps is assumed. Queue position and partial fills are ignored.
* Intra-bar ordering is unknown; the conservative stop-first rule biases results downwards.
* **Overfitting:** changing parameters until a backtest looks good fits the noise of that period. Treat
  any improvement you get by tuning as suspect; confirm it out-of-sample (a different date range) and in
  paper trading before believing it. Past performance does not guarantee future results.

## Reference run (for honesty)

2026-08-28 → 2026-09-27, 5m, default parameters, 10,000 USDT: 8,925 bars, 15 trades, **−350 USDT
(−3.5 %)**, win rate 20 %, profit factor 0.08, max drawdown −3.55 %, compute 8.7 s. The default strategy
lost money in that month; the risk limits kept the loss small. This is why Kestrel starts in paper mode
and requires a paper track record before live trading. Kestrel was **not** tuned to make this number look
better.
