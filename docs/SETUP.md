# Setup

## Requirements

* Docker Engine 24+ with Compose v2 (tested: Docker 29.8 / Compose 5.5 on Debian 13, Proxmox LXC CT100)
* ~1.5 GB RAM for the stack, ~2 GB disk (images) + database growth (candles ≈ 30 MB/year)
* Outbound HTTPS to `fapi.binance.com`, `fstream.binance.com`, `api.binance.com` (key-permission check),
  `api.openai.com`, and the push services (`web.push.apple.com`, FCM, Mozilla)
* Accurate clock (NTP). Kestrel pauses trading if skew vs Binance exceeds 1 s.

## Ports (chosen to avoid existing services on CT100)

| Port | Use |
|---|---|
| 8443/tcp | HTTPS web app + API (Caddy, internal CA) |
| 8089/tcp | HTTP: `/kestrel-ca.crt` download, `/healthz`, redirect to HTTPS |

Change them with `KESTREL_HTTPS_PORT` / `KESTREL_HTTP_PORT` in `.env` (and update `ALLOWED_ORIGINS`).

## First installation

```bash
# on the development machine
scripts/deploy.sh                          # or KESTREL_SSH=root@host KESTREL_DIR=/path scripts/deploy.sh
# on the server
cd /opt/kestrel
docker compose exec api python -m app.cli create-admin <username>
```

`scripts/deploy.sh` copies the tree, runs `scripts/init-env.sh` (creates `.env` with a random
PostgreSQL password and `AUTH_SECRET`; mode 600; never overwrites an existing file), builds the
images, runs `scripts/gen-vapid.sh` (Web Push keys) and starts everything.

The password must be 12+ characters. Then sign in at `https://<host>:8443` and enable two-factor
authentication under **Security** (required for live trading).

## `.env` reference

| Variable | Meaning |
|---|---|
| `POSTGRES_PASSWORD` | DB password (hex only — it is embedded in the database URL) |
| `AUTH_SECRET` | ≥ 32 chars; signs CSRF tokens and encrypts TOTP secrets. Changing it invalidates 2FA enrolments. |
| `KESTREL_HOST` | IP/hostname the TLS certificate is issued for (e.g. `192.168.1.178`) |
| `ALLOWED_ORIGINS` | Origins allowed to make state-changing requests (`https://192.168.1.178:8443,...`) |
| `COOKIE_SECURE` | `true` in production (cookies only over HTTPS) |
| `SESSION_TTL_HOURS` / `SESSION_IDLE_HOURS` | absolute / idle session lifetime (default 14 d / 3 d) |
| `OPENAI_API_KEY` | optional; enables the AI layer |
| `BINANCE_API_KEY` / `BINANCE_API_SECRET` | optional; enable live trading (after the checklist) |
| `BINANCE_TESTNET` | `true` routes live orders to `demo-fapi.binance.com` |
| `VAPID_PUBLIC_KEY` / `VAPID_PRIVATE_KEY` | Web Push keys (`scripts/gen-vapid.sh`) |
| `VAPID_SUBJECT` | `mailto:` or `https:` contact that push services may use |
| `PAPER_STARTING_BALANCE` | simulated USDT balance (default 10,000) |
| `BACKUP_RETENTION_DAYS` / `BACKUP_HOUR_UTC` | backup schedule |

After editing `.env`: `docker compose up -d` (recreates affected containers).

If the host already has an OpenAI key for another service, copy it server-side into
`/opt/kestrel/.env` as `OPENAI_API_KEY` (e.g. with `sed`/`grep` on the host) rather than pasting it
through a chat or terminal session that records output.

## Operator CLI

```bash
docker compose exec api python -m app.cli create-admin [username]
echo "$PW" | docker compose exec -T api python -m app.cli create-admin alice --password-stdin
docker compose exec api python -m app.cli reset-password <username>   # revokes all sessions
docker compose exec api python -m app.cli disable-totp <username>     # lost phone; forces PAPER
docker compose exec api python -m app.cli kill                        # emergency stop
docker compose exec api python -m app.cli status
docker compose run --rm --no-deps api python -m app.cli gen-secret
```

## Remote access

The app is LAN-only by design. From outside, connect the phone to the existing **WireGuard** VPN
(CT101) and use the same URL. Push notifications arrive anywhere without the VPN — they are delivered
by Apple's push service; only opening the app needs the VPN. Do **not** port-forward 8443/8089. If you
ever publish Kestrel on the internet, put it behind strong authentication (2FA is already enforced for
sensitive actions) and add the Swiss legal pages (Impressum / Datenschutz).

## Resource usage (measured on CT100)

`docker stats` on 2026-09-28: engine ≈ 96 MB / 3–6 % of one core (WebSocket parsing), api ≈ 77 MB,
db ≈ 54 MB, web ≈ 12 MB, backup < 1 MB between runs. A 30-day backtest takes ~9 s in one worker process.
