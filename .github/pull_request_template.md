## What this changes

<!-- One or two sentences. Link the issue it closes, if any. -->

## Why

<!-- The problem, not the patch. What was wrong or missing before? -->

## Safety

<!-- Does this touch risk limits, protective orders, halts, the kill switch, the live unlock or auth?
     If it loosens anything, say why and which failure-injection test covers it. -->

## How it was verified

<!-- Delete what does not apply. -->

- [ ] `cd backend && pytest -q` passes (and on PostgreSQL)
- [ ] `alembic check` reports no drift (schema changes ship a migration)
- [ ] `cd frontend && npm run build` passes
- [ ] `docker compose build` succeeds
- [ ] Ran it in PAPER mode against live market data — describe what you watched:
- [ ] Strategy change: checked on a date range it was **not** tuned on

## Notes for the reviewer

<!-- Trade-offs, things you are unsure about, follow-ups you deliberately left out. -->
