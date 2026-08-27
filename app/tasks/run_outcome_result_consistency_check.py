"""
Standing consistency check: trades.outcome_result must always agree with
markets.resolution (decisions/2026-08-06.md, punch list item 0).

This defect sat undetected for months because nothing ever asserted this
invariant — the check was one query (V3/V4,
scripts/verify_worldcup_outcome_result.sql) and it had never been run until a
throwaway calibration query happened to surface it. This job is that query,
scheduled, so the next occurrence (a resolution.py bug the propagation fix
didn't anticipate, a future one-off script bypassing the chokepoint the way
run_market_resolver.py / run_market_status_refresh.py did) pages instead of
sitting silent for months again.

CRIT (not WARN), same reasoning as run_freshness_watchdog.py
(decisions/2026-07-23.md): the WARN queue is Redis-backed and can be lost in
exactly the outages that matter — a data-integrity finding needs to survive
whatever infrastructure trouble might be contributing to it. Same convention
as the freshness watchdog's own substantive-finding path (as opposed to its
crash path, which is a separate CRIT for a separate reason).

Read-only. Does not fix anything — that's the one-off script
(app/tasks/run_fix_outcome_result_stamp_20260806.py) plus the resolution.py
chokepoint fix, both already in place. This job only asserts the invariant
holds and pages if it doesn't.

Excludes resolved=True/closed=False markets (the known ~30-row
ck_markets_resolved_implies_closed violation, migration 0033 / punch list
item 13 — those `run_force_resolve_*` scripts are the reason these rows
exist at all). Any attempted fix-write to one of them fails with
IntegrityError (Postgres re-checks that NOT VALID constraint on any UPDATE to
an already-violating row), so this check must not page on something the
standard remediation path is structurally unable to fix — that's item 13's
job, separately. 0 of them show a mismatch as of 2026-08-06; excluded anyway
as a standing guard against a future one.

Manual run:
  python -m app.tasks.run_outcome_result_consistency_check

Scheduled:
  outcome_result_consistency_check  hourly  (registered in run_worker.py)
"""
import asyncio
import selectors

from sqlalchemy import and_, func as sqlfunc, or_, select

from app.db.session import get_session_factory
from app.db.models.market import Market
from app.db.models.trade import Trade
from app.logging import setup_logging, get_logger

logger = get_logger(__name__)

_MISMATCH = or_(
    and_(Trade.outcome == Market.resolution, Trade.outcome_result == 0),
    and_(Trade.outcome.is_distinct_from(Market.resolution), Trade.outcome_result == 1),
)


async def check_outcome_result_consistency() -> dict:
    """V4 (scripts/verify_worldcup_outcome_result.sql), as a job. Returns
    {"ok": bool, "mismatched_trades": int, "mismatched_markets": int,
    "mismatched_staked_usd": float}. CRITs and never raises on a real finding;
    a query/DB failure also CRITs, same as freshness_watchdog's crash path."""
    try:
        factory = get_session_factory()
        async with factory() as session:
            row = (await session.execute(
                select(
                    sqlfunc.count().label("n"),
                    sqlfunc.count(sqlfunc.distinct(Trade.market_id)).label("n_markets"),
                    sqlfunc.coalesce(sqlfunc.sum(Trade.price * Trade.size), 0).label("staked"),
                )
                .select_from(Trade)
                .join(Market, Market.market_id == Trade.market_id)
                .where(
                    Trade.side == "BUY",
                    Trade.outcome_result.is_not(None),
                    Market.resolution.is_not(None),
                    Market.closed == True,  # noqa: E712 — see module docstring re: item 13
                    Trade.outcome.is_not(None),
                    _MISMATCH,
                )
            )).one()
    except Exception as exc:
        logger.error("outcome_result_consistency_check_error", error=str(exc), exc_info=True)
        from app.services.alerts.system_alerts import send_system_alert
        await send_system_alert(
            "CRIT", "outcome_result_consistency",
            f"consistency check crashed and could not run: {exc}",
        )
        return {"ok": False, "checked": False, "reason": str(exc)}

    n = row.n
    result = {
        "ok": n == 0,
        "checked": True,
        "mismatched_trades": n,
        "mismatched_markets": row.n_markets,
        "mismatched_staked_usd": float(row.staked or 0),
    }

    if n > 0:
        logger.critical("outcome_result_mismatch_detected", **result)
        from app.services.alerts.system_alerts import send_system_alert
        await send_system_alert(
            "CRIT", "outcome_result_consistency",
            f"{n} trades across {row.n_markets} markets have outcome_result out of "
            f"sync with markets.resolution (${result['mismatched_staked_usd']:,.2f} staked). "
            f"Run: python -m app.tasks.run_fix_outcome_result_stamp_20260806 --live",
        )
    else:
        logger.info("outcome_result_consistency_ok")

    return result


async def main() -> None:
    setup_logging()
    result = await check_outcome_result_consistency()
    print(f"ok={result['ok']}  checked={result.get('checked')}  "
          f"mismatched_trades={result.get('mismatched_trades')}  "
          f"mismatched_markets={result.get('mismatched_markets')}  "
          f"staked=${result.get('mismatched_staked_usd', 0):,.2f}")


if __name__ == "__main__":
    if __import__("sys").platform == "win32":
        asyncio.run(main(), loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()))
    else:
        asyncio.run(main())
