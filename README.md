<p align="center">
  <img src="./docs/logo.svg" width="96" height="96" alt="">
</p>

<h1 align="center">Kestrel</h1>

<p align="center">
  A self-hosted, paper-first trading desk for Binance BTCUSDT perpetual futures —
  risk engine first, AI second, full audit trail always.
</p>

<p align="center">
  <a href="https://github.com/BBareth/kestrel/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/BBareth/kestrel/actions/workflows/ci.yml/badge.svg"></a>
  <a href="./LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-green.svg"></a>
  <img alt="Python 3.13" src="https://img.shields.io/badge/python-3.13-3776ab?logo=python&logoColor=white">
  <img alt="PostgreSQL 17" src="https://img.shields.io/badge/postgres-17-336791?logo=postgresql&logoColor=white">
  <img alt="Paper trading by default" src="https://img.shields.io/badge/default-PAPER%20TRADING-1fc27e">
</p>

---

Kestrel watches **BTCUSDT perpetual** on Binance, reads 1m/5m/15m/1h/4h, and looks for one
conservative setup: a meaningful level broken on volume, retested, and held. Every candidate goes
through a **risk engine** that sizes the position from the stop distance and enforces loss limits,
cooldowns and caps; only then may an **AI layer** (OpenAI, strict JSON) veto it. If everything
agrees, Kestrel opens the position and immediately places a **verified exchange-side stop-loss and
take-profits** — or closes the position and halts if it cannot.

```
market data → technical analysis → strategy → risk engine → AI confirmation (veto only)
  → trade decision → execution engine → Binance / paper venue → position monitor
  → notifications + database (every step audited)
```

- **Paper first.** Starts in PAPER TRADING on real market data with fees, slippage, funding and
  liquidation simulated. Live trading needs a 15-item readiness checklist, two-factor authentication
  and a typed confirmation — and a Binance key that cannot withdraw and is IP-restricted.
- **Same code, two venues.** Paper and live share the strategy, risk engine, execution engine,
  monitor and reconciliation; only the exchange adapter differs.
- **Fails safe.** Stale data, clock skew, API error bursts, an unverifiable stop, an unknown
  position, a risk calculation that throws: no trade, halt, or flatten — never "probably fine".
- **One-tap kill switch** in every screen, from the shell, and — if the engine is down — executed
  by the API itself.
- **iPhone app.** Installable PWA with Web Push for trades, stops, targets, risk and system events.
- **Explains itself.** Every evaluation shows its checklist; every trade has an audit trail from
  signal to P&L; every AI call is stored with its tokens and cost.

<p align="center">
  <img src="./docs/screenshot-dashboard.png" alt="Dashboard: BTCUSDT price, market state with trend per timeframe, the latest signal (NO TRADE with its reason), position, account and a candlestick chart with EMA 7/25/99 and support/resistance levels." width="860">
  <br>
  <em>Dashboard — NO TRADE is the most common answer, and it says why.</em>
</p>

<p align="center">
  <img src="./docs/screenshot-trading.png" alt="Trading page: mode, strategy arm switch, the kill switch, and the live-trading readiness checklist." width="860">
  <br>
  <em>Trading — kill switch and the checklist that gates live mode.</em>
</p>

<p align="center">
  <img src="./docs/screenshot-backtest.png" alt="Backtest of the default strategy over 30 days: 14 trades, −3.55 %, equity and drawdown curves, P&L by day and R distribution." width="860">
  <br>
  <em>Backtest — shown as it came out: the default strategy lost 3.5 % over this month. It is not tuned to look good.</em>
</p>

<p align="center">
  <img src="./docs/screenshot-mobile.png" alt="The dashboard on an iPhone-sized screen with the mode badge, kill switch and bottom tab bar." width="300">
  <br>
  <em>On the iPhone, as a Home Screen app.</em>
</p>

> [!WARNING]
> **Trading BTC perpetual futures involves substantial risk. Leverage can result in rapid losses.**
> Past performance — paper, backtest or live — does not guarantee future results. Kestrel cannot
> predict Bitcoin and does not promise profit. It prioritises capital preservation, risk control,
> reliability and auditability over trade frequency; **NO TRADE is a valid and frequent outcome.**

> [!IMPORTANT]
> Kestrel is built for a **trusted LAN** (remote access through a VPN). Do not expose it to the
> internet. See [SECURITY.md](./SECURITY.md) for the trust model.

## At a glance

| | |
|---|---|
| Web app / PWA | `https://<host>:8443` (homelab: https://192.168.1.178:8443) |
| CA certificate for iPhone | `http://<host>:8089/kestrel-ca.crt` |
| Stack | FastAPI + asyncio engine (Python 3.13), PostgreSQL 17, React 19 PWA, Caddy (TLS) |
| Default | PAPER, 10,000 USDT simulated, 0.5 % risk per trade, 3× max leverage, 2 % max daily loss |

Documentation: [Architecture](docs/ARCHITECTURE.md) · [Setup](docs/SETUP.md) ·
[Security](docs/SECURITY.md) · [Trading](docs/TRADING.md) · [Strategy](docs/STRATEGY.md) ·
[Backtesting](docs/BACKTESTING.md) · [PWA / iPhone](docs/PWA.md) ·
[Notifications](docs/NOTIFICATIONS.md) · [Live trading](docs/LIVE_TRADING.md) ·
[Troubleshooting](docs/TROUBLESHOOTING.md) · [Contributing](CONTRIBUTING.md) · [Changelog](CHANGELOG.md)

## Quick start

On a Docker host (Docker 24+, Compose v2):

```bash
git clone git@github.com:BBareth/kestrel.git /opt/kestrel && cd /opt/kestrel
sh scripts/init-env.sh          # creates .env (mode 600) with a random DB password + AUTH_SECRET
docker compose build
sh scripts/gen-vapid.sh         # web-push keys (idempotent)
docker compose up -d
docker compose exec api python -m app.cli create-admin <your-username>
```

Or from a development machine: `scripts/deploy.sh` (copies the tree to `root@192.168.1.178:/opt/kestrel`,
generates `.env` on the server, builds and starts). Then open `https://<host>:8443`, sign in and
enable two-factor authentication under **Security**.

## Commands

All server commands run in the project directory on the Docker host.

### Configure

```bash
nano .env                       # OpenAI / Binance / host settings — see .env.example
docker compose up -d            # recreates containers whose configuration changed
```

Strategy, risk, AI, schedule, notification and safety settings live in the web UI (**Strategy** and
**Settings**), versioned and validated against hard caps.

### Start / stop / restart / status

```bash
docker compose up -d                 # start
docker compose stop                  # stop (positions keep their exchange-side SL/TP)
docker compose restart engine        # restart one service
docker compose ps                    # status + health
docker compose exec api python -m app.cli status
```

### Update

```bash
scripts/deploy.sh                    # from the dev machine: ship code, rebuild, recreate
# or on the server after pulling:
git pull && docker compose build --pull && docker compose up -d
```

Migrations run automatically (`migrate` service) before `api` and `engine` start.

### Logs

```bash
docker compose logs -f engine        # trading engine (JSON lines, secrets redacted)
docker compose logs -f api
docker compose logs --since 1h web
docker compose logs backup
```

### Backup

The `backup` service writes a verified `pg_dump` every day at 03:00 UTC to `backups/`
(14-day retention; status on **System** and in the readiness checklist). Manual backup:

```bash
docker compose exec -T backup sh -c 'pg_dump -Fc -f /backups/kestrel-manual-$(date -u +%Y%m%d-%H%M%S).dump'
```

Keep a copy of `.env` somewhere safe as well — it holds the secrets and is not in the dumps.

### Restore

```bash
docker compose stop api engine backup
docker compose exec -T db pg_restore -U kestrel -d kestrel --clean --if-exists --no-owner < backups/<file>.dump
docker compose start api engine backup
```

### Tests

```bash
cd backend && pip install -r requirements-dev.txt && pytest -q            # SQLite
TEST_DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/db pytest -q   # PostgreSQL
cd frontend && npm ci && npm run build                                    # type-check + build
```

### Emergency stop

- The red **KILL** button at the top of every page → confirm.
- Shell: `docker compose exec api python -m app.cli kill`
- If the engine is down, the API performs the kill switch itself (flatten live positions, return to PAPER).

## iPhone

1. On the home Wi-Fi (or VPN) open `http://<host>:8089/kestrel-ca.crt` in Safari → Allow →
   Settings → *Profile Downloaded* → Install.
2. Settings → General → About → **Certificate Trust Settings** → trust "Caddy Local Authority".
3. Open `https://<host>:8443` → Share → **Add to Home Screen**.
4. Launch Kestrel from the Home Screen, sign in, **Notifications → Enable notifications** → Allow →
   *Send test notification*.

Details: [docs/PWA.md](docs/PWA.md), [docs/NOTIFICATIONS.md](docs/NOTIFICATIONS.md).

## Binance

Use a dedicated sub-account. Create an API key with **Futures enabled, withdrawals disabled,
IP-restricted** to the homelab's public IP; set One-way position mode and Single-Asset margin; put
the key in `.env`; `docker compose up -d`; **Security → Verify now**. Then work through the readiness
checklist — [docs/LIVE_TRADING.md](docs/LIVE_TRADING.md).

## License

[MIT](./LICENSE). The repository is private.
