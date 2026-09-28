#!/usr/bin/env bash
# Ship the working tree to the Docker host and (re)build/start the stack.
#   scripts/deploy.sh                 # default host root@192.168.1.178, dir /opt/kestrel
#   KESTREL_SSH=root@host KESTREL_DIR=/srv/kestrel scripts/deploy.sh
# .env on the server is created once (secrets generated there) and never overwritten.
set -euo pipefail
HOST="${KESTREL_SSH:-root@192.168.1.178}"
DIR="${KESTREL_DIR:-/opt/kestrel}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

tar -C "$ROOT" -czf "$TMP/kestrel.tgz" \
  --exclude=.git --exclude=node_modules --exclude=.venv --exclude=dist --exclude=__pycache__ \
  --exclude=.pytest_cache --exclude=.env --exclude='.env.*' --exclude=backups --exclude='*.db' \
  backend frontend deploy scripts docs docker-compose.yml .env.example .dockerignore .gitignore README.md
scp -q "$TMP/kestrel.tgz" "$HOST:/tmp/kestrel.tgz"
ssh "$HOST" "set -e; mkdir -p '$DIR' && cd '$DIR' \
  && tar --no-same-owner --no-same-permissions -xzf /tmp/kestrel.tgz && rm -f /tmp/kestrel.tgz \
  && chmod 755 deploy/backup.sh scripts/*.sh && mkdir -p backups \
  && sh scripts/init-env.sh \
  && docker compose build --pull \
  && sh scripts/gen-vapid.sh \
  && docker compose up -d --remove-orphans && docker compose ps"
