#!/usr/bin/env bash
# End-to-end smoke test of freshly built images (CI runs this; it also runs on the homelab).
#
# Needs kestrel-backend:latest and kestrel-web:latest. Brings up db, migrate, api, web and backup
# as a throwaway compose project with its own secrets, volumes and localhost-only ports, probes it,
# and tears everything down again. The engine is left out on purpose: Binance answers HTTP 451 to
# US-hosted CI runners, and nothing here needs market data.
#
#   SMOKE_HTTPS_PORT=18443 SMOKE_HTTP_PORT=18089 bash scripts/smoke-test.sh
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT="${SMOKE_PROJECT:-kestrel-smoke}"
HTTPS_PORT="${SMOKE_HTTPS_PORT:-8443}"
HTTP_PORT="${SMOKE_HTTP_PORT:-8089}"
B="https://localhost:${HTTPS_PORT}"
WORK="$(mktemp -d)"
mkdir -p "$WORK/backups"

cat > "$WORK/smoke.env" <<EOF
POSTGRES_PASSWORD=$(openssl rand -hex 16)
AUTH_SECRET=$(openssl rand -hex 24)
KESTREL_HOST=localhost
KESTREL_BIND=127.0.0.1
KESTREL_HTTPS_PORT=${HTTPS_PORT}
KESTREL_HTTP_PORT=${HTTP_PORT}
ALLOWED_ORIGINS=${B}
COOKIE_SECURE=true
EOF
# Point every service at the throwaway env file and backup directory, never at a real .env.
cat > "$WORK/override.yml" <<EOF
services:
  api: { env_file: !override ["$WORK/smoke.env"] }
  engine: { env_file: !override ["$WORK/smoke.env"] }
  migrate: { env_file: !override ["$WORK/smoke.env"] }
  backup:
    volumes: !override ["$WORK/backups:/backups", "./deploy/backup.sh:/backup.sh:ro"]
EOF
C=(docker compose -p "$PROJECT" -f docker-compose.yml -f "$WORK/override.yml" --env-file "$WORK/smoke.env")

cleanup() { "${C[@]}" down -v --remove-orphans >/dev/null 2>&1 || true; rm -rf "$WORK"; }
on_error() { echo "::error::smoke test failed at line $1"; "${C[@]}" logs --tail 60 || true; }
trap cleanup EXIT
trap 'on_error $LINENO' ERR

check() {  # check <description> <command...>
  local what="$1"; shift
  if "$@"; then echo "ok   $what"; else echo "FAIL $what"; return 1; fi
}
code() { curl -sk -o /dev/null -w '%{http_code}' "$@"; }

"${C[@]}" up -d --no-build db migrate api web backup

for i in $(seq 1 60); do
  if curl -fsk "$B/api/health" 2>/dev/null | grep -q '"status":"ok"'; then echo "healthy after ~$((i * 2))s"; break; fi
  if [ "$i" -eq 60 ]; then echo "::error::stack did not become healthy"; false; fi
  sleep 2
done

# The PWA shell is served, client-side routes fall through to it, and the PWA assets exist.
check "SPA shell"                      bash -c "curl -fsk '$B/' | grep -q 'id=\"root\"'"
check "client route falls back to SPA" bash -c "curl -fsk '$B/dashboard' | grep -q 'id=\"root\"'"
check "manifest"                       bash -c "curl -fsk '$B/manifest.webmanifest' | grep -q '\"display\": \"standalone\"'"
check "service worker with precache"   bash -c "curl -fsk '$B/sw.js' | grep -q 'const PRECACHE = \\['"
# Hardening headers.
HDR="$(curl -sk -D - -o /dev/null "$B/")"
check "CSP header"                     grep -qi "content-security-policy: default-src 'self'" <<<"$HDR"
check "X-Frame-Options DENY"           grep -qi "x-frame-options: DENY" <<<"$HDR"
check "HSTS header"                    grep -qi "strict-transport-security" <<<"$HDR"
# Plain HTTP only serves the CA and redirects.
check "CA certificate download"        bash -c "curl -fs 'http://localhost:${HTTP_PORT}/kestrel-ca.crt' | grep -q 'BEGIN CERTIFICATE'"
check "HTTP redirects to HTTPS"        test "$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:${HTTP_PORT}/dashboard")" = 302
# Nothing but login/health without a session.
check "dashboard needs a session"      test "$(code "$B/api/dashboard")" = 401
check "kill switch needs a session"    test "$(code -X POST "$B/api/trading/kill" -H 'content-type: application/json' -d '{}')" = 401
check "unknown user rejected"          test "$(code -X POST "$B/api/auth/login" -H 'content-type: application/json' -d '{"username":"nobody","password":"wrong-password-123"}')" = 401

# Account, login, CSRF.
PW="Smoke-$(openssl rand -hex 12)"
echo "$PW" | "${C[@]}" exec -T api python -m app.cli create-admin smoker --password-stdin >/dev/null
JAR="$WORK/jar"
check "login"                          bash -c "curl -fsk -c '$JAR' -H 'content-type: application/json' -d '{\"username\":\"smoker\",\"password\":\"$PW\"}' '$B/api/auth/login' | grep -q '\"ok\":true'"
CSRF="$(awk '$6 == "kestrel_csrf" {print $7}' "$JAR")"
check "session cookie is HttpOnly"     grep -q '^#HttpOnly_.*kestrel_session' "$JAR"
check "state change without CSRF → 403" test "$(code -b "$JAR" -X POST "$B/api/trading/strategy" -H 'content-type: application/json' -d '{"enabled":false}')" = 403
check "foreign Origin → 403"           test "$(code -b "$JAR" -X POST "$B/api/trading/strategy" -H 'content-type: application/json' -H "X-CSRF-Token: $CSRF" -H 'Origin: https://evil.example' -d '{"enabled":false}')" = 403
check "state change with CSRF → 200"   test "$(code -b "$JAR" -X POST "$B/api/trading/strategy" -H 'content-type: application/json' -H "X-CSRF-Token: $CSRF" -H "Origin: $B" -d '{"enabled":false}')" = 200
check "starts in PAPER mode"           bash -c "curl -fsk -b '$JAR' '$B/api/trading/state' | grep -q '\"mode\":\"paper\"'"
check "strategy config v1 = defaults"  bash -c "curl -fsk -b '$JAR' '$B/api/strategy/config' | grep -q '\"risk_per_trade_pct\":0.5'"
check "live unlock refused (no keys)"  test "$(code -b "$JAR" -X POST "$B/api/trading/live/unlock" -H "X-CSRF-Token: $CSRF" -H "Origin: $B")" = 409

# The backup job reports into the database.
for i in $(seq 1 45); do
  if "${C[@]}" exec -T db psql -U kestrel -tAc "select status from component_status where component='backup'" 2>/dev/null | grep -q ok; then
    echo "ok   backup ran and reported"; break
  fi
  if [ "$i" -eq 45 ]; then echo "::error::backup did not report"; false; fi
  sleep 2
done
check "backup file written"            bash -c "ls '$WORK'/backups/kestrel-*.dump >/dev/null"

echo "smoke test passed"
