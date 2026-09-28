# Security

## Secrets

* Secrets exist **only** in `/opt/kestrel/.env` (mode 600, root) and in the containers' environment.
  They are never written to the database, never sent to the browser and never logged:
  every log record passes a redaction filter that removes the literal values of all configured
  secrets plus patterns (Binance signatures, `X-MBX-APIKEY`, `sk-…` keys, bearer tokens, `password=`).
* `.env`, `.env.*`, backups and databases are in `.gitignore`/`.dockerignore`; only `.env.example`
  (no values) is tracked.
* The database stores the Binance **connection status** and a 4-character key hint (`…ab12`), never the key.
* TOTP secrets are encrypted at rest (Fernet, key derived from `AUTH_SECRET` via HKDF); recovery codes
  and session tokens are stored as SHA-256 hashes.
* Anyone with root on the Docker host can read `.env` (e.g. `docker inspect`). Protect the host.

## Binance API key requirements (enforced)

Kestrel refuses LIVE mode unless the key check (spot SAPI `GET /sapi/v1/account/apiRestrictions`,
repeated every 10 minutes) reports:

| Check | Required |
|---|---|
| `enableWithdrawals` | **false** |
| `ipRestrict` | **true** (restricted to the homelab's public IP) |
| `enableFutures` | true |
| internal / universal transfer | disabled |
| Position mode | One-way (`dualSidePosition=false`) |
| Asset mode | Single-Asset (`multiAssetsMargin=false`) — isolated margin requires it |

A key that becomes unsafe while LIVE halts trading. The UI shows the status as
**missing / invalid / unsafe / connected (paper trading) / live trading**.

**IP restriction:** find the public IP with `ssh root@192.168.1.178 curl -s https://ifconfig.me`, then
Binance → API Management → Edit restrictions → *Restrict access to trusted IPs only* → add it. If the
ISP changes the IP, Binance rejects requests (`-2015`) and Kestrel reports "invalid" — update the
restriction. Use a **dedicated sub-account** for Kestrel so manual trading never mixes with it.

## Web authentication

* Username + password (argon2id, 64 MiB, t=3), minimum 12 characters; constant-time dummy verification
  for unknown users; lockout for 15 min after 8 failures; 10 login attempts / 5 min / IP.
* Server-side sessions: random 256-bit token in an `HttpOnly; Secure; SameSite=Strict` cookie, SHA-256 at
  rest, absolute (14 d) and idle (3 d) expiry, revocable per device, all revoked on password change.
* **CSRF:** every state-changing request needs the per-session token in `X-CSRF-Token` (double submit,
  compared in constant time) *and* an allowed `Origin`; cookies are `SameSite=Strict`.
* **Step-up re-authentication** (password + TOTP, valid 5 min) for: live unlock/enable, arming the
  strategy in live mode, risk/safety changes in live mode, releasing the kill switch, clearing a halt,
  2FA/password changes, paper reset, stop-loss self-test, acknowledging critical events.
* **TOTP 2FA** (RFC 6238) with 8 single-use recovery codes; required by the live-readiness checklist.
* The kill switch never requires re-authentication — it only reduces risk.
* Rate limit: 600 API requests/min/session; request bodies ≤ 256 KB (API) / 1 MB (proxy).

Only two routes are reachable without a session: `POST /api/auth/login` and `GET /api/health`
(returns only `{"status","db"}`); this was verified programmatically across all 64 routes.

## Transport and browser hardening

* TLS by Caddy with its **internal CA** (see [PWA.md](PWA.md)); HTTP only serves the CA certificate and redirects.
* Headers: strict CSP (`script-src 'self'`, no inline scripts, `connect-src 'self'`,
  `frame-ancestors 'none'`, `object-src 'none'`), HSTS, `X-Frame-Options: DENY`, `nosniff`,
  `Referrer-Policy: same-origin`, `Permissions-Policy`, `Cross-Origin-Opener-Policy`; no `Server` header.
* No CORS middleware: the API is same-origin only. OpenAPI/docs are disabled in production.
* React escapes all output; the only raw HTML is the server-generated TOTP QR SVG (segno; contains paths only).

> **CA trust caveat.** Installing Caddy's root CA on the iPhone makes the phone trust certificates that
> CA signs **for any domain**. Its private key lives in the `kestrel_caddy-data` Docker volume on CT100.
> Anyone who obtains it could impersonate websites to that phone. Keep the host locked down, or use a
> publicly trusted certificate instead (see PWA.md → alternatives), and remove the profile if you stop
> using Kestrel.

## Container hardening

* Backend runs as UID 10001, read-only root filesystem, `tmpfs /tmp`, `cap_drop: ALL`,
  `no-new-privileges`, memory limits; pip/setuptools removed from the runtime image.
* Caddy: read-only root, only `NET_BIND_SERVICE`, `no-new-privileges`; Caddy is compiled from the
  v2.11.4 tag with Go 1.26.8 and updated x/crypto, x/net, x/text, grpc modules.
* PostgreSQL and the backup container live on an `internal: true` network with no internet route and
  no published ports.
* JSON-file log rotation (10 MB × 5 per container).

## Security audit (2026-09-28)

| Check | Tool | Result |
|---|---|---|
| Python static analysis | bandit 1.9.4 | 0 findings after fixes (asserts → explicit checks, silent excepts → logged, heartbeat `/tmp` documented) |
| Python dependencies | pip-audit | 0 known vulnerabilities |
| JS dependencies | npm audit | 0 vulnerabilities |
| Container images | Trivy (HIGH/CRITICAL, fixable) | backend 0, web 0 — after rebuilding Caddy (was 17 Go CVEs in the official image) and removing pip (vendored msgpack) / old setuptools |
| Hard-coded secrets | regex scan of the tree | none (only a fake string inside the redaction test) |
| SQL injection | review | ORM / bound parameters only; backup script uses psql variables |
| Command injection | review | no `subprocess`, `os.system`, `eval` in the application |
| Auth bypass | route walk | only `login` and `health` unauthenticated |
| XSS / CSRF / cookies / CORS | review + tests | see above; covered by `tests/test_api.py` |

Re-run: `bandit -r backend/app`, `pip-audit -r backend/requirements.txt`, `npm audit` (frontend),
`docker run --rm -v /var/run/docker.sock:/var/run/docker.sock aquasec/trivy image --severity HIGH,CRITICAL --ignore-unfixed kestrel-web:latest`.

## Reporting / incident checklist

1. Press **KILL** (or `python -m app.cli kill`).
2. In Binance, **delete the API key** (a leaked key without withdrawal rights can still trade).
3. `docker compose exec api python -m app.cli reset-password <user>`; rotate `AUTH_SECRET` (re-enrol 2FA).
4. Review **Security → Audit log** and **History → Event log**.
