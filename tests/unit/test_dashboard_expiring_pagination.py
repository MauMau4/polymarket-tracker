"""Regression test for the Dashboard "Expiring Soon" pagination bug
(decisions/2026-07-24.md, "[FIX] Dashboard Expiring Soon pager counted
markets that got filtered out before render").

Root cause: `dashboard_home` (app/ui/views.py) computed `expiring_total`/
`expiring_total_pages` from the full DB-filtered 72h window, but fetched
prices and applied the effectively-resolved (>=0.99)/min_price filters to
only the current page, AFTER pagination math. A page dominated by markets
that are effectively decided (e.g. an entire same-end_date negRisk book —
observed live: an F1 session's ~22 per-driver "fastest lap" legs going
effectively-decided together) could render blank or sparse while the pager
still counted every pre-filter row.

Fix: fetch prices for and filter the full window BEFORE computing
pagination, so total/total_pages always match what's actually shown.

This test seeds a 72h window where most markets are "effectively resolved"
(mocked live price >= 0.99) and only a handful are genuinely open, then
asserts the pager's total matches the filtered count and page 1 renders the
full filtered set (not blank, not truncated by stale pre-filter math)."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from starlette.requests import Request

from app.db.models import Market
from app.ui.views import dashboard_home


def _fake_request() -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [],
        "query_string": b"",
    }
    return Request(scope)


def _expiring_market(market_id: str, minutes_from_now: int) -> Market:
    return Market(
        market_id=market_id,
        slug=market_id,
        question=f"q-{market_id}",
        subcategory="test-sub",
        active=True,
        closed=False,
        resolved=False,
        end_date=datetime.now(tz=timezone.utc) + timedelta(minutes=minutes_from_now),
    )


class TestDashboardExpiringPagination:
    async def test_pager_total_matches_filtered_count_not_raw_window(self, db_session):
        # 30 markets that will look "effectively resolved" (decided), sorted
        # to occupy the front of the window (soonest end_date) — mirrors the
        # live F1 same-end_date negRisk book that triggered this bug, and
        # exceeds one page (25) so the old code's page 1 would be entirely
        # decided markets — plus 5 genuinely open markets later in the window.
        decided_ids = [f"test-decided-{i}" for i in range(30)]
        open_ids = [f"test-open-{i}" for i in range(5)]

        for i, mid in enumerate(decided_ids):
            db_session.add(_expiring_market(mid, minutes_from_now=10 + i))
        for i, mid in enumerate(open_ids):
            db_session.add(_expiring_market(mid, minutes_from_now=1000 + i))
        await db_session.commit()

        prices = {mid: {"Yes": 0.01, "No": 0.995} for mid in decided_ids}
        prices.update({mid: {"Yes": 0.5, "No": 0.5} for mid in open_ids})

        async def _fake_fetch(market_ids, out_volumes=None):
            return {mid: prices[mid] for mid in market_ids if mid in prices}

        with patch("app.ui.views.fetch_market_prices_batch", side_effect=_fake_fetch), \
             patch("app.ui.views._get_cached_prices", new=AsyncMock(return_value=(None, None))), \
             patch("app.ui.views._set_cached_prices", new=AsyncMock(return_value=None)):
            response = await dashboard_home(
                request=_fake_request(),
                session=db_session,
                expiring_page=1,
                category=None,
                subcategory=None,
                min_volume=0.0,
                min_price=0.0,
            )

        pagination = response.context["expiring_pagination"]
        rendered_ids = {m.market_id for m in response.context["expiring_markets"]}

        # Only the 5 genuinely-open markets should survive the filter, and
        # the pager must agree — not the raw 35-row/2-page window count that
        # the old code would have reported (with page 1 rendered blank,
        # since every one of the first 25 rows by end_date is "decided").
        assert pagination["total"] == 5
        assert pagination["total_pages"] == 1
        assert rendered_ids == set(open_ids)
        assert not (rendered_ids & set(decided_ids))
