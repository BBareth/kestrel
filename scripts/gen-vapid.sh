#!/bin/sh
# Fills VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY in .env if they are empty (idempotent).
# Needs the backend image (docker compose build api). Keys are never printed.
set -eu
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "no .env — run scripts/init-env.sh first"; exit 1; }
if grep -q '^VAPID_PRIVATE_KEY=.\+' .env; then
  echo "VAPID keys already present — leaving them (changing them invalidates every push subscription)"
  exit 0
fi
umask 077
KV=$(docker compose run --rm --no-deps -T api python -m app.cli gen-vapid 2>/dev/null)
PUB=$(printf '%s\n' "$KV" | sed -n 's/^VAPID_PUBLIC_KEY=//p')
PRIV=$(printf '%s\n' "$KV" | sed -n 's/^VAPID_PRIVATE_KEY=//p')
[ -n "$PUB" ] && [ -n "$PRIV" ] || { echo "key generation failed"; exit 1; }
awk -v pub="$PUB" -v priv="$PRIV" '
  /^VAPID_PUBLIC_KEY=/ {print "VAPID_PUBLIC_KEY=" pub; next}
  /^VAPID_PRIVATE_KEY=/ {print "VAPID_PRIVATE_KEY=" priv; next}
  {print}' .env > .env.tmp && mv .env.tmp .env && chmod 600 .env
echo "VAPID keys written to .env"
