# Polymarket Whale Tracker

A real-time ingestion and analysis system for [Polymarket](https://polymarket.com)
prediction-market data. It streams live trades over WebSocket, attributes them
to wallets, detects "whale" activity and other alertable patterns, tracks
every wallet's positions and realized P&L, and scores wallets on trading
skill — all served through a FastAPI JSON API and a server-rendered dashboard.

`pathfinder/` is a research module built on top of the tracker that tests
whether following historically-skilled wallets produces a statistically
followable edge, using point-in-time wallet scoring and an event-study /
matched-control methodology. It is research-only — no order-execution code
exists in this codebase.

## Architecture

Six Docker Compose services, deliberately separated by failure mode and
resource profile rather than bundled into one process:

| Service | Runs | Why separate |
|---|---|---|
| `app` | `app.tasks.run_ws_ingestor` | The WebSocket connection is long-lived and stateful; a crash or reconnect storm here shouldn't touch the API or scheduled jobs. |
| `worker` | `app.tasks.run_worker` (APScheduler) | Discovery, resolution detection, retention, scoring, and watchdog jobs run on independent schedules — isolated from the request-serving path so a slow job never blocks API latency. |
| `api` | `uvicorn app.main:app` | Serves the JSON API and dashboard; stateless, restartable independently of ingestion. |
| `pathfinder-booklog` | `pathfinder.booklog.daemon` | Polls order-book snapshots on its own cadence for a market subset — a research data feed, not part of the tracker's live path. |
| `db` | PostgreSQL 16 | System of record. |
| `redis` | Redis 7 | Alert dedupe/cooldown state, live-price cache, WARN-alert digest queue. |

## Technical highlights

- **A schema-level invariant added after the ORM-level one was bypassed.**
  `markets.resolved ⇒ closed` was always enforced inside `_upsert_market()`,
  but several one-off repair scripts wrote `Market.resolved` directly via raw
  ORM attribute assignment, bypassing that chokepoint entirely and producing
  markets resolved with no real winner. Migration `0033` adds a `CHECK
  (NOT resolved OR closed)` constraint (`NOT VALID`, so it applies to all new
  writes immediately without requiring the pre-existing violations to be
  cleaned up first) — moving enforcement from "every caller must remember"
  to "the database won't allow it."

- **Recompute-on-change instead of stamp-once.** `trades.outcome_result` and
  `wallet_closed_positions.realized_pnl` used to be derived once, at first
  resolution, from whatever `markets.resolution` held at that instant — if a
  resolution was later corrected, every already-stamped row silently kept the
  old, wrong value. Migration `0034` adds
  `markets.outcome_result_stamped_resolution` plus a partial index on rows
  where it no longer matches the live `resolution`; the resolution job
  treats that mismatch as a re-stamp candidate on every cycle, so a
  correction event propagates to the trades/positions it already touched
  instead of only affecting future ones.

- **Page-scoped live pricing with a Redis cache**, not whole-window
  re-fetching. An earlier version of the dashboard fetched live Gamma prices
  for every market in the current filter window (hundreds of markets) on
  every page load, regardless of which ~25-row page was actually requested.
  The fix scopes the live fetch (and its Redis cache key) to just the
  displayed page, computing pagination from the DB-filtered list first —
  cutting a measured cold load from ~2.5s to ~1.3s independent of which page
  is requested.

- **`REPEATABLE READ` on a paginated, concurrently-written endpoint.**
  Computing a total count and then fetching a page as two separate
  statements lets a commit land in between under concurrent writers, so the
  count and the returned rows describe different snapshots. The markets
  list endpoint pins the transaction's isolation level up front so both
  statements see the same snapshot.

- **Escalating some scheduled checks to CRIT instead of WARN, deliberately.**
  Most operational alerts queue as WARN in Redis and flush once daily as a
  digest. The freshness watchdog, the outcome-result consistency check, and
  the scheduler-liveness check all fire CRIT (immediate push) instead,
  because the WARN queue is itself Redis-backed — exactly the kind of
  outage these checks exist to catch would also silently drop the alert
  meant to report it.

- **Point-in-time wallet scoring with an enforced no-lookahead invariant**
  (`pathfinder/scoring/`). The same `score_wallet`/`score_universe` code
  path backs both research and (eventually) live qualification, and a
  dedicated test seeds a future-dated position and asserts it never
  influences a score computed before it existed — the discipline the
  event-study results depend on.

## Running it

```bash
cp .env.example .env        # fill in credentials as needed; safe defaults otherwise
docker compose up -d db redis
docker compose run --rm api alembic upgrade head
docker compose up -d
```

The API/dashboard is then at `http://localhost:8000` (HTTP Basic Auth —
`DASHBOARD_USERNAME`/`DASHBOARD_PASSWORD` in `.env`).

To populate a database with a small illustrative dataset instead of waiting
on live ingestion:

```bash
docker compose run --rm api python -m scripts.seed_sample_data
```

## Testing

```bash
docker compose exec db psql -U poly -d polymarket -c "CREATE DATABASE polymarket_test;"
docker compose run --rm -e DATABASE_URL=postgresql+psycopg://poly:poly@db:5432/polymarket_test \
  api alembic upgrade head
docker compose run --rm -e PATHFINDER_TEST_DATABASE_URL=postgresql+psycopg://poly:poly@db:5432/polymarket_test \
  api sh -c "pip install pytest pytest-asyncio pytest-mock respx && pytest -q"
```

(`pyproject.toml`'s `dev` extra lists the four test-only packages; the
Docker image only installs the base runtime dependencies, so they're
installed on top for this run rather than baked into the image.)

**Verified: 444 tests pass, 0 failures, against a clean migrated database.**
Nearly all of them run against a real Postgres instance (`tests/conftest.py`)
rather than a mocked session — the project's own history includes bugs that
mocked tests missed (see the `resolved ⇒ closed` invariant above), so
integration coverage against real Postgres semantics is treated as
load-bearing, not optional. A handful of unit tests mock the
Polymarket/Alchemy/Dune HTTP clients directly (`respx`) and don't touch the
database at all. No test in the suite requires a live external API.

## Status and limitations

- **Wallet-level attribution for WebSocket-observed trades is currently
  disabled by design, not broken.** The endpoint used to match a live trade
  event to a wallet address ignores the asset/time-window filters passed to
  it and matches by price alone against a platform-wide feed — verified
  directly (a bogus asset ID returns identical results to a real one; an
  arbitrary past time window still returns current trades). The normalizer
  now writes `wallet=NULL, attribution_status=unresolved` for any candidate
  that isn't independently confirmed via a correctly wallet-scoped lookup.
  In practice this means wallet-level analytics are currently reliable only
  for a small explicitly-watched set of wallets, not the general trading
  population, pending a rework of the underlying matching strategy.
- **Market discovery has known coverage gaps** in some long-tail market
  genres (e.g. very-high-frequency single-outcome markets), so wallet
  activity concentrated in those genres is undercounted rather than absent.
- **`pathfinder/`** ships its scoring engine, event-cluster tooling, and
  event-study/markout research pipeline with their test suites; the
  strategy design documents (requirements and architecture writeups) are
  kept private and not included in this copy. The research pipeline has run
  its first full evaluation gate and, per its own kill criteria, found no
  statistically separable edge in the tested sample — a live execution
  layer does not exist.
