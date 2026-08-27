"""Volume-distribution sizing report for metadata-only-discovered genre
markets (decisions/2026-07-19.md item 4, follow-up to run_genre_discovery.py).
Read-only — makes no writes and does not touch settings.enabled_genre_tags
or settings.enable_volume_ranked_subscription.

Discovery and WS subscription are deliberately decoupled: run_genre_discovery
ingests Market rows only (metadata-only, no Token rows, no subscription).
This report answers the separate, explicitly-gated follow-up question —
IF a top-N-by-volume slice of the newly-discovered universe were later
subscribed (Token rows added + the existing volume-ranked subscription cap,
app/services/discovery/refresh.py::get_subscription_asset_ids, sized to
include it), what would trades/day and DB growth look like at various N?

Same volume-proportional projection method and caveats as
run_genre_discovery_projection.py: Polymarket exposes no trade-frequency
field, so trades/day is a projection scaled from the real, measured current
baseline by real, measured volume ratios — never a fabricated number, but
also never presented as more than an estimate.

Run: python -m app.tasks.run_genre_volume_distribution politics elections
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone

from app.db.session import get_session_factory
from app.logging import get_logger, setup_logging
from app.services.discovery.refresh import _MICRO_LIFECYCLE_SLUG_PATTERN
from app.services.polymarket.gamma_client import fetch_sports_markets_by_tag
from app.tasks.run_genre_discovery import GENRE_TAG_SLUGS
from app.tasks.run_genre_discovery_projection import _current_baseline, _measure_bytes_per_row

logger = get_logger(__name__)

# Candidate subscription sizes to project trades/day for. Not a declared
# grid in the pathfinder sense — just a practical spread of options for the
# operator to pick an N from against their stated trades/day envelope.
_CANDIDATE_N_VALUES = [100, 250, 500, 1000, 1500, 2000, 3000, 5000]


async def _collect_genre_market_volumes(genres: list[str]) -> list[tuple[str, float]]:
    """Live census (market_id, volume) for the given genres, deduped across
    any overlapping tags, excluding micro-lifecycle slugs, sorted by volume
    descending."""
    seen: dict[str, float] = {}
    for genre in genres:
        for tag_slug in GENRE_TAG_SLUGS[genre]:
            markets = await fetch_sports_markets_by_tag(tag_slug)
            for m in markets:
                if _MICRO_LIFECYCLE_SLUG_PATTERN.search(m.slug or ""):
                    continue
                seen[m.market_id] = m.volume or 0.0
    return sorted(seen.items(), key=lambda kv: -kv[1])


def _deciles(sorted_desc: list[tuple[str, float]]) -> list[dict]:
    """Decile 1 = highest-volume 10% of markets (list is sorted descending)."""
    n = len(sorted_desc)
    volumes = [v for _, v in sorted_desc]
    total = sum(volumes)
    deciles = []
    for i in range(10):
        lo = i * n // 10
        hi = (i + 1) * n // 10
        bucket = volumes[lo:hi]
        bucket_sum = sum(bucket)
        deciles.append(
            {
                "decile": i + 1,
                "n_markets": len(bucket),
                "volume_sum_usd": round(bucket_sum, 0),
                "volume_min_usd": round(min(bucket), 2) if bucket else 0.0,
                "volume_max_usd": round(max(bucket), 2) if bucket else 0.0,
                "pct_of_total_volume": round(100 * bucket_sum / total, 2) if total else 0.0,
            }
        )
    return deciles


async def run_volume_distribution(genres: list[str]) -> dict:
    factory = get_session_factory()
    async with factory() as session:
        baseline = await _current_baseline(session)
        bytes_per_trade_row = await _measure_bytes_per_row(session, "trades")

    ranked = await _collect_genre_market_volumes(genres)
    total_markets = len(ranked)
    total_volume = sum(v for _, v in ranked)
    current_trades_per_day = baseline["trades_per_day_current"]
    current_volume = baseline["active_subscribed_volume_usd"]

    top_n_projections = []
    for n in _CANDIDATE_N_VALUES:
        if n > total_markets:
            continue
        top_slice_volume = sum(v for _, v in ranked[:n])
        ratio = (top_slice_volume / current_volume) if current_volume else None
        projected_new_trades_per_day = current_trades_per_day * ratio if ratio is not None else None
        combined_trades_per_day = (
            current_trades_per_day + projected_new_trades_per_day
            if projected_new_trades_per_day is not None
            else None
        )
        top_n_projections.append(
            {
                "n": n,
                "volume_usd": round(top_slice_volume, 0),
                "volume_share_of_new_universe_pct": (
                    round(100 * top_slice_volume / total_volume, 1) if total_volume else None
                ),
                "projected_new_trades_per_day": (
                    round(projected_new_trades_per_day, 1) if projected_new_trades_per_day is not None else None
                ),
                "projected_combined_trades_per_day": (
                    round(combined_trades_per_day, 1) if combined_trades_per_day is not None else None
                ),
                "combined_vs_current_multiple": (
                    round(combined_trades_per_day / current_trades_per_day, 2)
                    if combined_trades_per_day and current_trades_per_day
                    else None
                ),
                "projected_ongoing_db_growth_mb_per_month": (
                    round(projected_new_trades_per_day * bytes_per_trade_row * 30 / 1_000_000, 2)
                    if projected_new_trades_per_day is not None
                    else None
                ),
            }
        )

    return {
        "genres": genres,
        "total_markets": total_markets,
        "total_volume_usd": total_volume,
        "deciles": _deciles(ranked),
        "baseline_trades_per_day_current": round(current_trades_per_day, 1),
        "baseline_active_subscribed_volume_usd": current_volume,
        "top_n_projections": top_n_projections,
    }


async def main() -> None:
    setup_logging()
    genres = sys.argv[1:] or ["politics", "elections"]
    print(f"=== Genre Volume Distribution / Subscription Sizing — {datetime.now(tz=timezone.utc).isoformat()} ===")
    print(f"Genres: {genres}\n")

    result = await run_volume_distribution(genres)

    print(f"Total markets in universe : {result['total_markets']}")
    print(f"Total volume (lifetime)   : ${result['total_volume_usd']:,.0f}")
    print(f"Baseline trades/day (current, measured) : {result['baseline_trades_per_day_current']}")
    print()
    print("Volume deciles (1 = highest-volume 10% of markets):")
    for d in result["deciles"]:
        print(
            f"  decile {d['decile']:>2}  n={d['n_markets']:>5}  "
            f"sum=${d['volume_sum_usd']:>14,.0f}  "
            f"range=[${d['volume_min_usd']:,.0f}, ${d['volume_max_usd']:,.0f}]  "
            f"{d['pct_of_total_volume']:>5.1f}% of total"
        )
    print()
    print("Top-N-by-volume subscription sizing (volume-proportional PROJECTION, not measured):")
    print(f"{'N':>6} {'volume':>16} {'%univ':>7} {'new tr/d':>10} {'combined tr/d':>14} {'xcurrent':>9} {'DB MB/mo':>10}")
    for p in result["top_n_projections"]:
        print(
            f"{p['n']:>6} ${p['volume_usd']:>14,.0f} {p['volume_share_of_new_universe_pct']:>6.1f}% "
            f"{p['projected_new_trades_per_day']:>10.1f} {p['projected_combined_trades_per_day']:>14.1f} "
            f"{p['combined_vs_current_multiple']:>8.2f}x {p['projected_ongoing_db_growth_mb_per_month']:>10.1f}"
        )


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop)
    else:
        asyncio.run(main())
