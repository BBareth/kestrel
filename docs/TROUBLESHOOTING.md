# Troubleshooting

Start with **System** (component health, last updates, reconciliation, events) and the logs:
`docker compose logs --since 30m engine api`.

| Symptom | Cause / fix |
|---|---|
| Browser/iPhone warns about the certificate | CA not installed/trusted — [PWA.md](PWA.md) steps 1–3. The certificate is issued for `KESTREL_HOST`; use exactly that IP/hostname. |
| iPhone: "Enable notifications" disabled | Open the app from the **Home Screen icon**, not Safari. iOS 16.4+ required. |
| Test notification "not delivered: no subscribed devices" | Enable notifications on the device first. "VAPID keys missing" → `sh scripts/gen-vapid.sh && docker compose up -d`. |
| Banner "Live connection lost" | The SSE stream dropped (app backgrounded / Wi-Fi change). It reconnects automatically; check `docker compose ps api`. |
| "Entries paused: market data stale" | Binance WebSocket/REST unreachable from CT100. Check internet/DNS; `docker compose logs engine | grep -i websocket`. After 10 min it becomes a halt. |
| "TRADING HALTED: …" | A fail-safe fired. Read System → events, fix the cause, then Trading → *Clear halt* (password). |
| Readiness "No critical errors" failing | Review System → events → *Acknowledge critical events*. |
| Binance status **invalid** (`-2015`) | Wrong key/secret, key deleted, or the public IP changed and no longer matches the key's IP restriction. |
| Binance status **unsafe** | The key allows withdrawals/transfers or has no IP restriction, or the account is in hedge / multi-assets mode — see LIVE_TRADING.md. |
| `-4120 STOP_ORDER_SWITCH_ALGO` in logs | Would mean conditional orders were sent to the legacy endpoint — Kestrel uses the Algo Order API; report it as a bug. |
| `418` / `429` from Binance | Rate limited/banned temporarily; the supervisor halts after 8 errors in 5 min. Wait, then clear the halt. |
| Clock skew guard | `timedatectl` on the Proxmox host — enable NTP. |
| AI "unavailable" / "budget exhausted" | Check the AI page (last error, spend). Default policy is NO TRADE; switch to deterministic in Strategy → AI if you prefer. |
| Engine keeps restarting | `docker compose logs engine` — at start it needs Binance REST for the backfill and retries with back-off. |
| "another engine instance holds the trading lock" | Two engines against one database — stop the duplicate (by design only one may trade). |
| Web UI 502 | `api` not healthy: `docker compose ps`, `docker compose logs api`. |
| Forgot password | `docker compose exec api python -m app.cli reset-password <user>` |
| Lost 2FA phone | use a recovery code, or `python -m app.cli disable-totp <user>` (forces PAPER) |
| Backup status down | `docker compose logs backup`; disk space `df -h /opt/kestrel`. |
| Restore a backup | README → Restore. |

## Useful commands

```bash
docker compose exec api python -m app.cli status
docker compose exec -T db psql -U kestrel -c "select component,status,detail,updated_at from component_status"
docker compose exec -T db psql -U kestrel -c "select id,mode,direction,status,exit_reason,realized_pnl from trades order by id desc limit 10"
docker compose exec -T db psql -U kestrel -c "select ts,level,event,message from system_events order by id desc limit 20"
```
