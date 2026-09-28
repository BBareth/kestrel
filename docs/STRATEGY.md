# Strategy: breakout–retest v1 (`breakout_retest_v1`)

A deliberately conservative intraday setup on the **5-minute** chart, filtered by 15m / 1h / 4h context.
It is not a prediction model. Most evaluations end in **NO TRADE**, and the UI shows exactly which check
failed.

## Features (per timeframe, all causal)

EMA 7 / 25 / 99 (SMA-seeded), Wilder RSI 14, Wilder ATR 14, 20-bar volume average, session VWAP (UTC day),
EMA-25 slope, and swing pivots (a swing high/low that dominates 3 bars on each side — it is only *known*
3 bars later, so there is no look-ahead). Swings closer than 0.3 ATR are clustered into
support/resistance levels with a touch count.

**Trend score** per timeframe (−2 … +2): +2 strong bullish = close > EMA7 > EMA25 > EMA99, EMA25 rising,
close > EMA99 by 0.5 ATR; +1 bullish = close and EMA25 above EMA99; 0 neutral; mirrored for bearish.

**Regime** (for display and AI triggers): high volatility, quiet, trending up/down, ranging.

## LONG rules (SHORT is the exact mirror)

| # | Check | Default |
|---|---|---|
| 1 | 1h trend score ≥ 0, 4h ≥ −1 ("not strongly bearish") | `long_min_1h_trend`, `long_min_4h_trend` |
| 2 | 15m trend neutral or bullish | `require_15m_alignment` |
| 3 | 5m ATR between 0.05 % and 1.2 % of price | volatility band |
| 4 | EMA7 > EMA25 on 5m; RSI 50–72 | momentum |
| 5 | A *meaningful* resistance (≥ 2 touches or the look-back extreme, last 96 bars) was broken within the last 12 bars by a **bullish** candle closing ≥ 0.1 ATR above it | breakout |
| 6 | Breakout volume ≥ 1.3 × the prior 20-bar average | volume confirmation |
| 7 | Price pulled back to within 0.3 ATR of the level, wicks pierced ≤ 0.35 ATR, **every close held above the level** | retest holds |
| 8 | Last candle bullish and above EMA7; price ≤ 1 ATR above the level (no chasing) | confirmation |
| 9 | Stop below the retest low − 0.25 ATR; widened to ≥ 0.6 ATR; rejected if > 2.5 ATR | stop |
| 10 | TP1 = 1.5 R (50 %), TP2 = 3 R capped just before the next opposing 1 h level; blended R:R ≥ 2.0 | targets |
| 11 | Deterministic confidence ≥ 60 | score |
| 12 | Same direction not signalled in the last 6 bars | cooldown |

**Confidence** starts at 50: +10 1h aligned, +5 4h aligned, +5 15m aligned, up to +15 for volume,
+5 RSI in the sweet spot, +5 above VWAP, +5 level with ≥ 3 touches, +5 R:R ≥ 2.5, +5 rising open interest;
−10 crowded funding against the trade, −5 falling OI, −5 extended entry.

After a setup the risk engine decides whether and how big (see [TRADING.md](TRADING.md)), then the AI
may veto it.

## Example output

```
LONG · Confidence 78
- 1h trend bullish
- 15m trend strong bullish
- 5m resistance breakout at 84,210.0 (3 touches)
- volume +34% vs average
- successful retest
- RSI 58
- R:R 2.3
- price above VWAP
- funding neutral (0.0050%)
- AI confirms LONG (72)
```

## Parameters

Every parameter is editable under **Strategy** with an explanation, range and default; edits create a
new version (trades record the version they used) and can be restored. **Reset to safe defaults**
restores the table below. Values outside the ranges are rejected by the server, and the risk engine
applies absolute ceilings in code (risk ≤ 2 %, leverage ≤ 10×, daily loss ≤ 10 %) even if a database row
were edited by hand.

### Default risk parameters

| Parameter | Default |
|---|---|
| Risk per trade | **0.5 %** of equity (hard cap 2 %) |
| Max leverage | **3×**, isolated (hard cap 10×) |
| Max position size | 10,000 USDT notional |
| Max margin usage | 50 % of available balance |
| Max daily loss | **2 %** (UTC day) |
| Max weekly loss | **5 %** (ISO week) |
| Max consecutive losses | 3 → 240 min cooldown |
| Cooldown after a loss | 30 min |
| Max trades | 2 / hour, 6 / day |
| Max open positions | **1** (one-way mode, fixed) |
| Min reward:risk | **2.0** |
| Max funding | 0.05 % per interval |
| Max spread / slippage | 3 bps / 8 bps |
| Liquidation buffer | liquidation ≥ 3 × stop distance away |
| Mandatory stop-loss | always (locked) |

Position size = equity × risk % ÷ (stop distance + round-trip fees + stop slippage), rounded **down** to
the exchange step, then capped by max notional and margin. It is never "buy $100 worth".

### AI parameters

| Parameter | Default |
|---|---|
| AI confirmation | `required` (off / advisory / required) |
| AI min confidence | 65 |
| If AI unavailable | `no_trade` (or deterministic) |
| If budget exceeded | `no_trade` (or deterministic) |
| Daily AI budget | 1.00 USD |
| Model / reasoning effort | `gpt-6-sol` / low |
| Regime-change analysis | on, ≥ 60 min apart |
| Position review | every 60 min (advisory only) |
| Periodic market read | every 240 min |

### Schedule

Entry window 00:00–24:00 UTC, weekends allowed, no entries ±10 min around funding timestamps
(00:00/08:00/16:00 UTC). Open positions are always managed.

## How the AI is used

* Only **after** the strategy found a setup **and** the risk engine approved it (so the model never sees,
  and can never rescue, a rejected trade).
* Input is structured JSON: price, OHLCV for 5m/15m/1h/4h, indicator values, trend scores, regime,
  support/resistance, funding, open interest, liquidations, account, open position, recent trades,
  the full strategy checklist, the candidate and the risk decision.
* Output must match a strict JSON schema (`decision`, `confidence`, `market_regime`, `reasoning`,
  `risk_flags`, `invalidation`, `recommended_action`), requested via OpenAI Structured Outputs and
  re-validated locally with `jsonschema`. Anything else counts as "unavailable".
* In `required` mode the model must answer the same direction with confidence ≥ 65 and
  `recommended_action = ENTER`; otherwise NO TRADE. It cannot change size, leverage, stops or limits,
  and cannot override the kill switch, cooldowns or halts.
* Calls are cached per bar/level; every call's tokens and estimated cost are recorded (AI page).
  Measured on the deployment: one analysis ≈ 4.3 k tokens ≈ **$0.01** with `gpt-6-sol`.
