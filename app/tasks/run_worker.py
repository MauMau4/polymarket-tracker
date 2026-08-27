"""
APScheduler worker process — entry point for all scheduled background jobs.

Runs as a separate process from both the web app and the WS ingestor.

Schedule:
  market_discovery         every 5 min
  genre_discovery          every 30 min (no-op until genres enabled)
  market_resolution        every 30 min
  market_resolver          every 6 hours
  stranded_positions_check every 6 hours
  gamma_canary             every 6 hours
  freshness_watchdog       hourly
  scheduler_liveness       every 5 min (heartbeat write; checked by the ingestor process)
  price_snapshot_48h       every 6 hours
  price_snapshot_24h       every 3 hours
  score_recompute          daily 00:00 UTC
  trade_retention          daily 02:00 UTC
  epsilon_close            daily 02:30 UTC
  warn_digest              daily 08:00 UTC (sends nothing if no WARN alerts queued)
  wallet_alert_digest      daily 08:10 UTC (sends nothing per wallet with no activity)
  dune_backfill            nightly 01:00 UTC  (ENABLE_DUNE_BACKFILL=true)
  alchemy_reconcile        hourly  (ENABLE_ALCHEMY_RECONCILIATION=true)

Start: python run_worker.py
"""
import asyncio
import sys

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.config import get_settings
from app.logging import setup_logging, get_logger
from app.services.scheduling.jobs import (
    job_alchemy_reconciliation,
    job_dune_backfill,
    job_epsilon_close,
    job_freshness_watchdog,
    job_gamma_canary,
    job_genre_discovery,
    job_market_discovery,
    job_market_resolution,
    job_market_resolver,
    job_outcome_result_consistency_check,
    job_price_snapshot_24h,
    job_price_snapshot_48h,
    job_scheduler_liveness,
    job_score_history_retention,
    job_score_recompute,
    job_sports_discovery,
    job_stranded_positions_check,
    job_trade_retention,
    job_wallet_alert_digest,
    job_warn_digest,
)

logger = get_logger(__name__)


async def main() -> None:
    setup_logging()
    settings = get_settings()

    scheduler = AsyncIOScheduler(timezone="UTC")

    # Market discovery — every 5 minutes
    scheduler.add_job(
        job_market_discovery,
        trigger=IntervalTrigger(minutes=5),
        id="market_discovery",
        name="Market Discovery",
        max_instances=1,
        coalesce=True,
    )

    # Sports game market discovery — every 30 minutes
    scheduler.add_job(
        job_sports_discovery,
        trigger=IntervalTrigger(minutes=30),
        id="sports_discovery",
        name="Sports Market Discovery",
        max_instances=1,
        coalesce=True,
    )

    # Coverage-discovery genre expansion — every 30 minutes (no-op until
    # genres are enabled via settings.enabled_genre_tags_csv)
    scheduler.add_job(
        job_genre_discovery,
        trigger=IntervalTrigger(minutes=30),
        id="genre_discovery",
        name="Genre Market Discovery",
        max_instances=1,
        coalesce=True,
    )

    # Market resolution detection — every 30 minutes
    scheduler.add_job(
        job_market_resolution,
        trigger=IntervalTrigger(minutes=30),
        id="market_resolution",
        name="Market Resolution Detection",
        max_instances=1,
        coalesce=True,
    )

    # Score recomputation — daily at 00:00 UTC
    scheduler.add_job(
        job_score_recompute,
        trigger=CronTrigger(hour=0, minute=0),
        id="score_recompute",
        name="Score Recomputation",
        max_instances=1,
        coalesce=True,
    )

    # Trade retention purge — daily at 02:00 UTC
    scheduler.add_job(
        job_trade_retention,
        trigger=CronTrigger(hour=2, minute=0),
        id="trade_retention",
        name="Trade Retention Purge",
        max_instances=1,
        coalesce=True,
    )

    # Score history retention purge — daily at 02:15 UTC
    scheduler.add_job(
        job_score_history_retention,
        trigger=CronTrigger(hour=2, minute=15),
        id="score_history_retention",
        name="Score History Retention Purge",
        max_instances=1,
        coalesce=True,
    )

    # Epsilon-close: residual sub-share dust from the $20 min-notional filter — daily at 02:30 UTC
    scheduler.add_job(
        job_epsilon_close,
        trigger=CronTrigger(hour=2, minute=30),
        id="epsilon_close",
        name="Epsilon Close",
        max_instances=1,
        coalesce=True,
    )

    # WARN-severity system alert digest — daily at 08:00 UTC (sends nothing if empty)
    scheduler.add_job(
        job_warn_digest,
        trigger=CronTrigger(hour=8, minute=0),
        id="warn_digest",
        name="System Alert WARN Digest",
        max_instances=1,
        coalesce=True,
    )

    # Per-wallet WATCH_WALLET digest — daily at 08:10 UTC (sends nothing for
    # a digest wallet with no qualifying activity that day)
    scheduler.add_job(
        job_wallet_alert_digest,
        trigger=CronTrigger(hour=8, minute=10),
        id="wallet_alert_digest",
        name="Wallet Alert Digest",
        max_instances=1,
        coalesce=True,
    )

    # Market auto-resolver — every 6 hours
    scheduler.add_job(
        job_market_resolver,
        trigger=IntervalTrigger(hours=6),
        id="market_resolver",
        name="Market Auto-Resolver",
        max_instances=1,
        coalesce=True,
    )

    # Stranded positions safety net — every 6 hours
    scheduler.add_job(
        job_stranded_positions_check,
        trigger=IntervalTrigger(hours=6),
        id="stranded_positions_check",
        name="Stranded Positions Check",
        max_instances=1,
        coalesce=True,
    )

    # Gamma API contract canary — every 6 hours (catches endpoint deprecation
    # and schema drift before it silently corrupts discovery/resolution data)
    scheduler.add_job(
        job_gamma_canary,
        trigger=IntervalTrigger(hours=6),
        id="gamma_canary",
        name="Gamma Contract Canary",
        max_instances=1,
        coalesce=True,
    )

    # Freshness watchdog — hourly (symptom-based: trades/discovery/resolver
    # actually still producing output, complements the contract canary above)
    scheduler.add_job(
        job_freshness_watchdog,
        trigger=IntervalTrigger(hours=1),
        id="freshness_watchdog",
        name="Freshness Watchdog",
        max_instances=1,
        coalesce=True,
    )

    # outcome_result/markets.resolution consistency check — hourly, same
    # cadence as the freshness watchdog. decisions/2026-08-06.md, punch list
    # item 0: this invariant went unchecked for months; this is the standing
    # version of the V3/V4 query that finally surfaced it.
    scheduler.add_job(
        job_outcome_result_consistency_check,
        trigger=IntervalTrigger(hours=1),
        id="outcome_result_consistency_check",
        name="Outcome Result Consistency Check",
        max_instances=1,
        coalesce=True,
    )

    # Scheduler liveness heartbeat — every 5 min, checked by the separate
    # ingestor process (this job can't detect its own scheduler's death)
    scheduler.add_job(
        job_scheduler_liveness,
        trigger=IntervalTrigger(minutes=5),
        id="scheduler_liveness",
        name="Scheduler Liveness Heartbeat",
        max_instances=1,
        coalesce=True,
    )

    # Price snapshots — T-48 every 6 hours, T-24 every 3 hours
    scheduler.add_job(
        job_price_snapshot_48h,
        trigger=IntervalTrigger(hours=6),
        id="price_snapshot_48h",
        name="Price Snapshot T-48",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        job_price_snapshot_24h,
        trigger=IntervalTrigger(hours=3),
        id="price_snapshot_24h",
        name="Price Snapshot T-24",
        max_instances=1,
        coalesce=True,
    )

    # Dune backfill — nightly at 01:00 UTC (feature-flagged)
    if settings.enable_dune_backfill:
        scheduler.add_job(
            job_dune_backfill,
            trigger=CronTrigger(hour=1, minute=0),
            id="dune_backfill",
            name="Dune Backfill",
            max_instances=1,
            coalesce=True,
        )

    # Alchemy reconciliation — hourly (feature-flagged)
    if settings.enable_alchemy_reconciliation:
        scheduler.add_job(
            job_alchemy_reconciliation,
            trigger=IntervalTrigger(hours=1),
            id="alchemy_reconciliation",
            name="Alchemy Reconciliation",
            max_instances=1,
            coalesce=True,
        )

    scheduler.start()

    logger.info(
        "worker_started",
        jobs=[j.id for j in scheduler.get_jobs()],
        dune_backfill=settings.enable_dune_backfill,
        alchemy_reconciliation=settings.enable_alchemy_reconciliation,
    )

    # Run an immediate discovery pass on startup so the DB is fresh
    try:
        await job_market_discovery()
    except Exception as exc:
        logger.error("worker_startup_discovery_failed", error=str(exc))

    # Run sports discovery immediately after general discovery
    try:
        await job_sports_discovery()
    except Exception as exc:
        logger.error("worker_startup_sports_discovery_failed", error=str(exc))

    # Run genre discovery immediately too (no-op if no genres are enabled)
    try:
        await job_genre_discovery()
    except Exception as exc:
        logger.error("worker_startup_genre_discovery_failed", error=str(exc))

    # Run the Gamma contract canary immediately on startup so a broken
    # deploy is caught within seconds, not up to 6 hours later
    try:
        await job_gamma_canary()
    except Exception as exc:
        logger.error("worker_startup_gamma_canary_failed", error=str(exc))

    try:
        # Keep the event loop alive; scheduler handles the rest
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        scheduler.shutdown(wait=False)
        logger.info("worker_shutdown")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop)
    else:
        asyncio.run(main())
