# Enabling live trading — safely

Live trading uses **real money**. BTC perpetual futures with leverage can lose money quickly. Kestrel
cannot predict Bitcoin; a positive paper or backtest record does not guarantee future results. Only use
money you can afford to lose.

## 1. Prepare Binance

1. Create a **dedicated sub-account** for Kestrel and transfer only the capital you intend to risk into
   its USDⓈ-M futures wallet.
2. Futures → Preferences: **Position mode: One-way**, **Asset mode: Single-Asset**.
3. API Management → Create API key (system-generated HMAC):
   * enable **Reading** and **Futures**,
   * **do NOT enable Withdrawals, Universal/Internal transfer or Margin**,
   * **Restrict access to trusted IPs only** → the homelab's public IP
     (`ssh root@192.168.1.178 curl -s https://ifconfig.me`).
4. Optional dry run: create a key on the futures testnet (`demo-fapi.binance.com`) first and set
   `BINANCE_TESTNET=true`.

## 2. Configure Kestrel

```bash
ssh root@192.168.1.178
cd /opt/kestrel && nano .env          # BINANCE_API_KEY=… and BINANCE_API_SECRET=…
docker compose up -d                  # recreates api + engine with the key
```

In the app: **Security → Verify now**. Status must read **connected (paper trading)** with
withdrawals `false`, IP restricted `true`, futures `true`, hedge mode `false`, multi-assets `false`.

## 3. Complete the Live Trading Readiness checklist (Trading page)

| Item | How |
|---|---|
| ☑ Binance API connected | step 2 |
| ☑ Withdrawal permission disabled | Binance key settings |
| ☑ IP restriction configured | Binance key settings |
| ☑ One-way, single-asset margin | Binance futures preferences |
| ☑ Paper trading completed | ≥ 10 closed paper trades over ≥ 7 days (Settings → Safety) |
| ☑ Risk limits reviewed | Strategy → Risk → "I have reviewed these limits" |
| ☑ Stop-loss tested on Binance | Security → *Run stop-loss self-test* (places and cancels a far-away reduce-only stop) |
| ☑ Kill switch tested | press KILL once (paper), release it |
| ☑ Database backups running | automatic (backup container) |
| ☑ Notifications tested | Notifications → Send test notification (must be delivered) |
| ☑ Reconciliation tested | Security → *Run live reconciliation check* |
| ☑ Two-factor authentication | Security → Set up authenticator |
| ☑ No critical errors | System → acknowledge reviewed events / clear halts |
| ☑ Trading engine running · Clock synchronised | automatic |

The unlock button stays disabled until every required item passes.

## 4. Unlock

Trading → **Enable LIVE trading…** → read the warning → re-enter password + 2FA code → type
**`ENABLE LIVE TRADING`** → tick the risk acknowledgement → *Enable live trading*.

Kestrel now shows the red **LIVE TRADING** banner, sends a "Live trading enabled" notification,
reconciles the Binance account (any unknown position halts trading), and keeps the **strategy disarmed**.
Arm it explicitly with the *Strategy armed* toggle (password again).

## 5. Operating

* Start small: keep 0.5 % risk (or lower) and 3× max leverage.
* Watch the first trades: History → trade → audit trail should show *SL submitted … verified* and
  *TP submitted … verified* within a second of the fill; the orders must also be visible in the Binance app.
* Changing risk parameters or safety settings while live requires re-authentication.

## 6. Stop

* **KILL** button (UI) or `docker compose exec api python -m app.cli kill` → strategy off, orders
  cancelled, positions closed at market (default), mode back to PAPER.
* *Return to PAPER* on the Trading page (only when flat).
* Last resort: close the position in the Binance app and delete the API key. Stopping the containers
  (`docker compose stop`) does **not** close positions, but their exchange-side stop and take-profit
  remain active.
