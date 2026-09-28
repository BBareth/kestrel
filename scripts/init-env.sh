#!/bin/sh
# Creates /opt/kestrel/.env on the server with freshly generated secrets.
# Run once, on the server, from the project directory:   sh scripts/init-env.sh
# Existing .env files are never overwritten.
set -eu
cd "$(dirname "$0")/.."
if [ -f .env ]; then
  echo ".env already exists — not touching it"
  exit 0
fi
umask 077
HOST_IP="${KESTREL_HOST:-$(hostname -I | awk '{print $1}')}"
PGPW=$(openssl rand -hex 24)
AUTH=$(openssl rand -base64 48 | tr -d '\n/+=' | cut -c1-56)
sed -e "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=${PGPW}|" \
    -e "s|^AUTH_SECRET=.*|AUTH_SECRET=${AUTH}|" \
    -e "s|^KESTREL_HOST=.*|KESTREL_HOST=${HOST_IP}|" \
    -e "s|^ALLOWED_ORIGINS=.*|ALLOWED_ORIGINS=https://${HOST_IP}:8443,https://localhost:8443|" \
    .env.example > .env
chmod 600 .env
echo "wrote .env for ${HOST_IP} (mode 600). Next: docker compose build && sh scripts/gen-vapid.sh"
