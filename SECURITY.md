# Security

Kestrel can move real money. The full hardening description, the audit results and the incident
checklist are in [docs/SECURITY.md](docs/SECURITY.md); this page is the trust model.

## Trust model

**The web app is LAN-only by design.** Only Caddy publishes ports (8443 HTTPS, 8089 for the CA
download). PostgreSQL and the API are not reachable from the network. Remote access goes through the
homelab VPN. Do not port-forward it, and do not put it behind a public tunnel without re-reading
this file.

**Everyone who can sign in can trade.** There is one role. Sign-in needs a password (argon2id); every
action that raises risk — enabling live mode, arming the strategy in live mode, changing risk limits
while live, releasing the kill switch, clearing a halt — needs the password and the TOTP code again.
Two-factor authentication is required before live trading can be unlocked. The kill switch never
asks for anything.

**Anyone with root on the Docker host owns the instance.** `.env` holds every secret and the Caddy
volume holds the CA key the phones trust.

## Secrets

| Secret | Exposure if leaked | Response |
| --- | --- | --- |
| `BINANCE_API_KEY` / `BINANCE_API_SECRET` | Trading on the account (no withdrawals: Kestrel refuses keys that allow them, and the key must be IP-restricted). | Delete the key in Binance API Management, create a new one. |
| `OPENAI_API_KEY` | Spend on the OpenAI account (shared with openGym). | Revoke in the OpenAI dashboard. |
| `AUTH_SECRET` | Forge CSRF tokens (still needs a session) and decrypt stored TOTP secrets. | Rotate, restart, re-enrol 2FA. |
| `POSTGRES_PASSWORD` | Full database access — only from the internal Docker network. | Rotate in `.env` and in PostgreSQL. |
| `VAPID_PRIVATE_KEY` | Send push notifications to subscribed devices. | Regenerate (devices must re-subscribe). |
| Caddy CA key (volume `kestrel_caddy-data`) | Impersonate any website to phones that trust the CA. | Remove the profile from the phones; delete the volume. |

None of these are sent to the browser, stored in the database or written to logs (a redaction filter
scrubs them and common credential patterns from every log line).

## Reporting a problem

Please report vulnerabilities privately through GitHub:
[Report a vulnerability](https://github.com/BBareth/kestrel/security/advisories/new) (Security tab →
*Report a vulnerability*). Do not open a public issue for a security problem, and never paste API
keys, logs with account data or `.env` contents anywhere.
