# Trading behaviour

## Modes

| | PAPER (default) | LIVE |
|---|---|---|
| Market data | real Binance production data | same |
| Strategy / risk / AI / execution / monitor | same code | same code |
| Venue | Kestrel paper exchange | Binance USDⓈ-M (or testnet if `BINANCE_TESTNET=true`) |
| Switching | — | multi-step unlock, see [LIVE_TRADING.md](LIVE_TRADING.md) |

The mode is shown on every page (green **PAPER TRADING** pill / red pulsing **LIVE TRADING** pill and a
red banner). Kestrel never switches to live by itself; the kill switch switches LIVE → PAPER.

## Paper realism

* Entries fill at the ask (long) / bid (short) plus 2 bps slippage; taker fee 0.05 %.
* Stops trigger on the **mark price** and fill at the worse of trigger and current price plus
  7 bps (2 + 5) — gaps hurt as they do live. Take-profits fill at market after triggering.
* Funding is charged/credited at each funding timestamp on the position notional.
* Isolated margin: margin must be available; liquidation at the isolated liquidation price.
* "Would immediately trigger" and reduce-only rules are enforced like on Binance.
* All parameters live under **Strategy → Execution**. The paper account survives restarts; reset it
  on the Trading page.

## Order lifecycle (every trade)

1. Pre-checks against the venue: no position, no stale Kestrel orders, no open DB trade, margin,
   isolated margin mode, leverage confirmed, fresh price, slippage vs signal ≤ 8 bps, stop and TP still
   valid at the current price, reward:risk and risk budget still satisfied.
2. MARKET entry with a deterministic client id (`kst<salt>-<trade>-E0`). A timeout/503 is treated as
   *unknown* and resolved by querying the order and the position — never by re-sending.
3. Fill verified; the venue position is the source of truth for size and entry price.
4. **Stop-loss**: reduce-only `STOP_MARKET` (Algo Order API, `workingType=MARK_PRICE`,
   `priceProtect=true`), then verified among open orders. Up to 3 attempts.
   **If it cannot be verified, the position is closed immediately, trading is halted and a critical
   notification is sent.**
5. **Take-profits**: TP1 (default 50 % at 1.5 R) and TP2 (rest at 3 R, capped before the next opposing
   1 h level), reduce-only `TAKE_PROFIT_MARKET`, verified. If they cannot be placed: close the position
   (default) or keep it stop-protected and manage the TP in software (setting).
6. Monitoring: TP1 fill → stop moved to breakeven + fees (new stop placed and verified *before* the old
   one is cancelled); a missing/undersized stop is re-placed; failure → flatten + halt.
7. Close: remaining orders cancelled, final order states synced, P&L computed from the trade's own
   fills (realised P&L − commissions + funding), notification sent.

Every step is written to the trade's audit trail (History → trade → *Audit trail*).

## Kill switch / STOP ALL TRADING

Engaging it (UI button, CLI `kill`, or API):

* disables the strategy and blocks new orders immediately (state written before anything else),
* cancels Kestrel's open orders,
* **closes open positions at market** (default; *Settings → Safety → Kill switch: open positions* can be
  set to `keep_protected`, which leaves the position with its stop and take-profit),
* switches LIVE → PAPER (default),
* sends a critical notification, logs everything, and marks the readiness item "kill switch tested".

If the engine is not running, the API executes the kill switch itself on the live venue. Releasing it
requires your password; the strategy stays disarmed until you arm it again.

## Automatic protection

| Condition | Reaction |
|---|---|
| Market data older than 30 s | new entries paused (auto-resumes) |
| … for longer than 10 min | **halt** (needs a human) |
| Clock skew vs Binance > 1 s | entries paused |
| ≥ 8 Binance API errors in 5 min | **halt** |
| Live credentials not "connected" | entries paused; halt if LIVE |
| Risk calculation throws | **halt**, no trade |
| Stop-loss missing and cannot be restored | flatten + **halt** + critical alert |
| Position liquidated | **halt** |
| Unknown position on the venue | **halt** + policy: `protect` (adopt + ensure a stop, default), `flatten`, `halt_only` |
| Daily loss limit reached | no entries until next UTC day + alert |
| Weekly loss limit / losing streak | no entries until next week / cooldown + alert |
| Database unavailable | nothing new can be recorded ⇒ no new trades; exchange-side stops stay active |

Thresholds are in *Settings → Safety*. A halt is cleared on the Trading page (password required) after
reviewing System → events.

## What happens if something breaks while a live position is open?

The stop-loss and take-profits sit **on Binance** from the moment the entry is confirmed. So:

* **Kestrel / the host / the network dies** → the exchange still executes the stop or target. On
  restart, reconciliation settles the trade from Binance fills, cancels leftovers, and never re-sends orders.
* **Binance API unreachable while monitoring** → alert "Binance connection lost"; no new trades;
  the position stays protected; monitoring resumes automatically.
* **Stop order disappears** (cancelled manually, expired) → re-placed within seconds; if that fails the
  position is closed and trading halts.
* **Entry timeout** → resolved by client-id lookup; exactly one position results (tested).
* **Kestrel restarted mid-entry** → pending entry resolved: filled → protected; not filled → cancelled.
* **Position appears that Kestrel didn't open** → halt + protect it with a stop (default policy).

All of these are automated tests (`backend/tests/test_execution.py`, `test_engine.py`).

## Notifications sent

Trade opened · stop-loss hit · TP1/TP2 hit · trade closed (other) · setup detected · risk (daily limit,
streak, halts, emergencies, kill switch) · system (stale data, Binance connection lost/restored,
protective order restored) · live mode enabled/disabled · AI (optional: "AI suggests closing").
