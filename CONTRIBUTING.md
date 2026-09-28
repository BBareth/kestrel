# Contributing

Kestrel is a personal project, published as-is. This file is the working agreement for changing it.

## Before a large change

Write down why it belongs. Kestrel is deliberately narrow: one symbol, one position, one engine,
paper first. A change that loosens a safety rule needs a stronger argument than a change that adds
one — and a change that makes the strategy look better on a backtest is not, by itself, an argument
(see [docs/BACKTESTING.md](docs/BACKTESTING.md) on overfitting).

## Getting set up

Python 3.12+ (3.13 in the image), Node 22+ (24 in the image), Docker.

```bash
# backend: API on :8000 against a local SQLite file
cd backend
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
export DATABASE_URL=sqlite+aiosqlite:///./dev.db AUTH_SECRET=dev-secret-dev-secret-dev-secret-32 \
       ENVIRONMENT=development COOKIE_SECURE=false ALLOWED_ORIGINS=http://localhost:5173
alembic upgrade head
python -m app.cli create-admin dev
uvicorn app.api.main:app --reload            # terminal 1
python -m app.engine.main                    # terminal 2 (public Binance data, paper only)

# frontend on :5173, proxying /api to :8000
cd frontend && npm install && npm run dev
```

Never put real Binance keys in a development environment. If you need the live path, use the futures
testnet (`BINANCE_TESTNET=true`) with a testnet key.

## Before opening a PR

```bash
cd backend && pytest -q                       # SQLite
TEST_DATABASE_URL=postgresql+asyncpg://… pytest -q   # PostgreSQL, as CI does
cd frontend && npm run build                  # type-check + build
docker compose build
```

CI runs the tests on PostgreSQL, `alembic check` (models and migrations must agree), bandit,
pip-audit and npm audit, builds both images and smoke-tests the stack: health, SPA routing,
security headers, CA download, unauthenticated requests rejected, login, CSRF enforcement and the
backup job.

A schema change needs a migration:
`DATABASE_URL=… alembic revision --autogenerate -m "…"`, then read the generated file.

## Style

Match what is there. A few things are deliberate:

- **Fail safe.** When the code cannot be sure — unknown order status, stale data, a risk calculation
  that throws, a stop that cannot be verified — the answer is no trade, a halt, or a flatten. Never
  "probably fine".
- **The venue is the source of truth.** Positions, orders and fills are read back from the exchange
  (or the paper venue); the database is corrected to match, never the other way round.
- **Nothing is re-sent blindly.** Every order has a deterministic client id; an ambiguous result is
  resolved by querying that id.
- **Protective orders are replaced, never removed first.** Place the new one, verify it, then cancel
  the old one.
- **Comments explain why, not what.** Most of them exist because something non-obvious bit once —
  keep that bar.
- **Every decision is auditable.** If the engine does something, a trade event, risk event or system
  event says so.
- **Tests for failure paths.** A safety change comes with a test in `tests/test_execution.py` or
  `tests/test_engine.py` that injects the failure (`tests/fakes.py` has a flaky venue).

## Releasing

1. Merge to `main` and let CI go green.
2. Bump `backend/app/__init__.py` and `frontend/package.json`, add a `CHANGELOG.md` entry.
3. Tag and push: `git tag -a vX.Y.Z -m "Kestrel X.Y.Z" && git push origin vX.Y.Z`.
4. **Publish images** pushes `ghcr.io/bbareth/kestrel-backend` and `ghcr.io/bbareth/kestrel-web`
   (linux/amd64). `latest` only moves when the tag is the newest semver in the repository.
5. Deploy to the homelab with `scripts/deploy.sh` (builds on the host; the GHCR images are for
   rollback and reference).

Do not re-run the publish workflow on an old tag to "refresh" it — cut a new tag instead.
