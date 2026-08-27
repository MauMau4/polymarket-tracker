"""
APScheduler job definitions — run from run_worker.py.

Schedule:
  market_discovery    every 5 minutes
  market_resolution   every 30 minutes
  score_recompute     daily at 00:00 UTC
  trade_retention     daily at 02:00 UTC
  dune_backfill       nightly at 01:00 UTC  (ENABLE_DUNE_BACKFILL=true)
  alchemy_reconcile   hourly  (ENABLE_ALCHEMY_RECONCILIATION=true)
"""
from app.logging import get_logger

logger = get_logger(__name__)


async def job_market_discovery() -> None:
    """Sync market metadata and active asset subscriptions from Gamma API."""
    try:
        from app.services.discovery.refresh import run_discovery
        result = await run_discovery()
        logger.info(
            "scheduled_discovery_complete",
            markets_upserted=result.markets_upserted,
            tokens_upserted=result.tokens_upserted,
            active_assets=len(result.active_asset_ids),
        )
    except Exception as exc:
        logger.error("scheduled_discovery_error", error=str(exc), exc_info=True)


async def job_sports_discovery() -> None:
    """Discover current sports game markets via tag-based event endpoint."""
    try:
        from app.services.discovery.refresh import run_sports_discovery
        result = await run_sports_discovery()
        logger.info(
            "scheduled_sports_discovery_complete",
            total_found=result["total_found"],
            total_upserted=result["total_upserted"],
            total_tokens=result["total_tokens"],
        )
    except Exception as exc:
        logger.error("scheduled_sports_discovery_error", error=str(exc), exc_info=True)


async def job_genre_discovery() -> None:
    """Discover markets for enabled coverage-discovery genres (politics,
    elections, etc. — decisions/2026-07-19.md item 4). A no-op until the
    operator adds genres to settings.enabled_genre_tags_csv."""
    try:
        from app.tasks.run_genre_discovery import run_genre_discovery
        result = await run_genre_discovery()
        logger.info(
            "scheduled_genre_discovery_complete",
            enabled_genres=result["enabled_genres"],
            total_found=result["total_found"],
            total_upserted=result["total_upserted"],
        )
    except Exception as exc:
        logger.error("scheduled_genre_discovery_error", error=str(exc), exc_info=True)


async def job_market_resolution() -> None:
    """Check for newly resolved markets and update wallet aggregate stats."""
    try:
        from app.services.markets.resolution import check_and_process_resolved_markets
        result = await check_and_process_resolved_markets()
        logger.info("scheduled_resolution_complete", **result)
    except Exception as exc:
        logger.error("scheduled_resolution_error", error=str(exc), exc_info=True)
        from app.services.alerts.system_alerts import send_system_alert
        await send_system_alert("WARN", "market_resolution", f"resolution job failed: {exc}")



async def job_score_recompute() -> None:
    """Recompute Skill Scores for all tracked wallets."""
    try:
        from app.tasks.run_score_refresh import refresh_all_scores
        await refresh_all_scores()
    except Exception as exc:
        logger.error("scheduled_score_recompute_error", error=str(exc), exc_info=True)


async def job_trade_retention() -> None:
    """Purge trades older than TRADE_RETENTION_DAYS for resolved markets."""
    try:
        from app.services.maintenance.retention import purge_old_trades
        purged = await purge_old_trades()
        logger.info("scheduled_retention_complete", purged=purged)
    except Exception as exc:
        logger.error("scheduled_retention_error", error=str(exc), exc_info=True)


async def job_score_history_retention() -> None:
    """Purge wallet_score_history rows older than 7 days."""
    try:
        from app.services.maintenance.retention import purge_old_score_history
        purged = await purge_old_score_history()
        logger.info("scheduled_score_history_retention_complete", purged=purged)
    except Exception as exc:
        logger.error("scheduled_score_history_retention_error", error=str(exc), exc_info=True)


async def job_dune_backfill() -> None:
    """Backfill historical trades for newly seen wallets via Dune Analytics."""
    try:
        from app.config import get_settings
        if not get_settings().enable_dune_backfill:
            return
        from app.tasks.run_dune_backfill import run_dune_backfill
        await run_dune_backfill()
    except Exception as exc:
        logger.error("scheduled_dune_backfill_error", error=str(exc), exc_info=True)


async def job_alchemy_reconciliation() -> None:
    """Resolve unattributed trades via Alchemy Polygon RPC."""
    try:
        from app.config import get_settings
        if not get_settings().enable_alchemy_reconciliation:
            return
        from app.tasks.run_alchemy_reconciliation import run_alchemy_reconciliation
        await run_alchemy_reconciliation()
    except Exception as exc:
        logger.error("scheduled_alchemy_reconciliation_error", error=str(exc), exc_info=True)


async def job_stranded_positions_check() -> None:
    """Close wallet_cost_basis rows stranded in resolved markets."""
    try:
        from app.tasks.run_stranded_positions_check import run_stranded_positions_check
        result = await run_stranded_positions_check()
        logger.info("scheduled_stranded_check_complete", **result)
    except Exception as exc:
        logger.error("scheduled_stranded_check_error", error=str(exc), exc_info=True)


async def job_market_resolver() -> None:
    """Resolve or deactivate markets past their end_date (volume >= 10k only)."""
    try:
        from app.tasks.run_market_resolver import run_market_resolver
        result = await run_market_resolver()
        logger.info("scheduled_market_resolver_complete", **result)
    except Exception as exc:
        logger.error("scheduled_market_resolver_error", error=str(exc), exc_info=True)
        from app.services.alerts.system_alerts import send_system_alert
        await send_system_alert("WARN", "market_resolver", f"resolver job failed: {exc}")


async def job_price_snapshot_48h() -> None:
    """Capture T-48 price snapshots for markets closing within 48 hours."""
    try:
        from app.tasks.run_price_snapshot import take_snapshot
        result = await take_snapshot(48)
        logger.info("scheduled_price_snapshot_48h_complete", **result)
    except Exception as exc:
        logger.error("scheduled_price_snapshot_48h_error", error=str(exc), exc_info=True)


async def job_price_snapshot_24h() -> None:
    """Capture T-24 price snapshots for markets closing within 24 hours."""
    try:
        from app.tasks.run_price_snapshot import take_snapshot
        result = await take_snapshot(24)
        logger.info("scheduled_price_snapshot_24h_complete", **result)
    except Exception as exc:
        logger.error("scheduled_price_snapshot_24h_error", error=str(exc), exc_info=True)


async def job_epsilon_close() -> None:
    """Close residual sub-share dust left by the $20 min-notional trade filter."""
    try:
        from app.services.wallets.epsilon_close import run_epsilon_close
        result = await run_epsilon_close()
        logger.info("scheduled_epsilon_close_complete", **result)
    except Exception as exc:
        logger.error("scheduled_epsilon_close_error", error=str(exc), exc_info=True)
        from app.services.alerts.system_alerts import send_system_alert
        await send_system_alert("WARN", "epsilon_close", f"epsilon-close job failed: {exc}")


async def job_gamma_canary() -> None:
    """Validate the Gamma API contract hasn't drifted (deprecation, schema)."""
    try:
        from app.tasks.run_gamma_canary import run_gamma_canary
        result = await run_gamma_canary()
        logger.info(
            "scheduled_gamma_canary_complete",
            ok=result["ok"],
            deprecated_endpoints=result["deprecated_endpoints"],
        )
    except Exception as exc:
        logger.error("scheduled_gamma_canary_error", error=str(exc), exc_info=True)


async def job_freshness_watchdog() -> None:
    """Check that trades/discovery/resolver are still actually producing output."""
    try:
        from app.tasks.run_freshness_watchdog import run_freshness_watchdog
        result = await run_freshness_watchdog()
        logger.info("scheduled_freshness_watchdog_complete", ok=result["ok"], problems=result["problems"])
    except Exception as exc:
        logger.error("scheduled_freshness_watchdog_error", error=str(exc), exc_info=True)
        # CRIT, not WARN: a crash here (as opposed to a clean run that finds
        # problems, which already CRITs on its own) means the watchdog can't
        # even reach its dependencies — exactly the catastrophic-dependency
        # signal that must survive a DB/Redis outage. WARN is queued in Redis
        # and would be lost in precisely that scenario (decisions/2026-07-23.md).
        from app.services.alerts.system_alerts import send_system_alert
        await send_system_alert("CRIT", "freshness_watchdog", f"freshness watchdog crashed: {exc}")


async def job_outcome_result_consistency_check() -> None:
    """trades.outcome_result must always agree with markets.resolution — see
    app/tasks/run_outcome_result_consistency_check.py (decisions/2026-08-06.md,
    punch list item 0). The check function itself already CRITs on both a
    real mismatch and its own crash, so this wrapper only needs to log."""
    try:
        from app.tasks.run_outcome_result_consistency_check import check_outcome_result_consistency
        result = await check_outcome_result_consistency()
        logger.info(
            "scheduled_outcome_result_consistency_complete",
            ok=result["ok"],
            mismatched_trades=result.get("mismatched_trades"),
        )
    except Exception as exc:
        logger.error("scheduled_outcome_result_consistency_error", error=str(exc), exc_info=True)


async def job_warn_digest() -> None:
    """Flush the day's queued WARN-severity system alerts as one Telegram
    message. Sends nothing if the queue is empty — no daily heartbeat."""
    try:
        from app.services.alerts.system_alerts import flush_warn_digest
        result = await flush_warn_digest()
        logger.info("scheduled_warn_digest_complete", sent=result["sent"], count=result["count"])
    except Exception as exc:
        logger.error("scheduled_warn_digest_error", error=str(exc), exc_info=True)


async def job_wallet_alert_digest() -> None:
    """Flush the trailing-24h WATCH_WALLET digest for alert_digest wallets.
    Sends nothing for a wallet with no qualifying activity that day."""
    try:
        from app.tasks.run_wallet_alert_digest import flush_wallet_alert_digests
        result = await flush_wallet_alert_digests()
        logger.info("scheduled_wallet_alert_digest_complete", **result)
    except Exception as exc:
        logger.error("scheduled_wallet_alert_digest_error", error=str(exc), exc_info=True)


async def job_scheduler_liveness() -> None:
    """Write a Redis heartbeat proving the scheduler itself is still alive.

    This job's own CRIT alerts can't cover the scheduler dying outright — if
    the scheduler process dies, this job stops running too. The heartbeat it
    writes is instead checked by the separate `run_ws_ingestor.py` process
    (see `run_scheduler_liveness_check.check_scheduler_liveness`), which
    keeps running even if the worker dies and can therefore actually raise
    the alert. See decisions/2026-07-18.md.
    """
    try:
        from app.tasks.run_scheduler_liveness_check import record_heartbeat
        await record_heartbeat()
    except Exception as exc:
        logger.error("scheduled_scheduler_liveness_error", error=str(exc), exc_info=True)
