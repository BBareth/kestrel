#!/bin/sh
# Daily PostgreSQL backup loop (runs in the postgres image).
#   - custom-format pg_dump, verified with pg_restore --list
#   - retention: BACKUP_RETENTION_DAYS (default 14)
#   - reports success/failure into component_status so the UI and the
#     live-readiness checklist can see it
set -u
RET="${BACKUP_RETENTION_DAYS:-14}"
HOUR="${BACKUP_HOUR_UTC:-3}"
DIR=/backups
mkdir -p "$DIR"

report() {  # $1=status $2=detail
  psql -q -v ON_ERROR_STOP=1 -v st="$1" -v dt="$2" >/dev/null 2>&1 <<'SQL' || true
INSERT INTO component_status (component, status, detail, data, updated_at, last_ok_at)
VALUES ('backup', :'st', :'dt', '{}'::jsonb, now(), CASE WHEN :'st' = 'ok' THEN now() ELSE NULL END)
ON CONFLICT (component) DO UPDATE SET status = EXCLUDED.status, detail = EXCLUDED.detail,
  updated_at = now(), last_ok_at = COALESCE(EXCLUDED.last_ok_at, component_status.last_ok_at);
SQL
}

run_backup() {
  ts=$(date -u +%Y%m%d-%H%M%S)
  f="$DIR/kestrel-$ts.dump"
  if pg_dump -Fc -Z 6 -f "$f.tmp" && pg_restore --list "$f.tmp" >/dev/null 2>&1; then
    mv "$f.tmp" "$f"
    find "$DIR" -name 'kestrel-*.dump' -type f -mtime +"$RET" -delete
    n=$(ls "$DIR"/kestrel-*.dump 2>/dev/null | wc -l)
    size=$(du -h "$f" | cut -f1)
    echo "$(date -u +%FT%TZ) backup ok: $f ($size), $n kept"
    report ok "last backup $ts UTC ($size), $n kept, retention ${RET}d"
  else
    rm -f "$f.tmp"
    echo "$(date -u +%FT%TZ) BACKUP FAILED" >&2
    report down "backup failed at $ts UTC"
  fi
}

# Wait for the database and the schema (the migrate service creates it).
until pg_isready -q; do sleep 3; done
until psql -tAc "SELECT 1 FROM component_status LIMIT 1" >/dev/null 2>&1 || \
      psql -tAc "SELECT to_regclass('public.component_status')" 2>/dev/null | grep -q component_status; do
  sleep 5
done

latest=$(ls -t "$DIR"/kestrel-*.dump 2>/dev/null | head -1)
if [ -z "$latest" ] || [ -n "$(find "$latest" -mmin +1440 2>/dev/null)" ]; then
  run_backup
else
  report ok "last backup $(basename "$latest") (status restored after restart)"
fi

while true; do
  now=$(date -u +%s)
  next=$(( (now / 86400) * 86400 + HOUR * 3600 ))
  [ "$next" -le "$now" ] && next=$((next + 86400))
  sleep $((next - now))
  run_backup
done
