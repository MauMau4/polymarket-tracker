"""Hand-computed fixture wallets for score_wallet (PRD §5.1 thresholds).

Every expected number in this file is computed by hand in the comments —
per pathfinder/CLAUDE.md rule 7 ("never fabricate results"), nothing here is
eyeballed from running the code first.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from pathfinder.config import load_config
from pathfinder.scoring.engine import score_wallet
from tests.scoring.conftest import add_wallet, add_wcp

pytestmark = pytest.mark.asyncio

CONFIG = load_config()
AS_OF = datetime(2026, 6, 1, tzinfo=timezone.utc)


async def _seed_uniform(db_session, wallet, n, entry, exit_, shares=100.0, closed_at_source="observed_exit"):
    for i in range(n):
        await add_wcp(
            db_session,
            wallet=wallet,
            closed_at=AS_OF - timedelta(days=1, hours=i),
            entry_price=entry,
            exit_price=exit_,
            shares_sold=shares,
            closed_at_source=closed_at_source,
        )


# ---------------------------------------------------------------------------
# min_closed_positions boundary: exactly 20 positions
# ---------------------------------------------------------------------------
# 20 identical positions: entry=0.30, exit=0.80, shares=100
#   cost_basis = 30, realized_pnl = 50, ROI = 50/30 = 1.6667  (>> 0.15 min)
#   win_rate = 1.0 (all wins), implied_win_rate = mean(entry) = 0.30 (1.0 > 0.30 OK)
#   concentration_ratio = 50 / (20*50) = 0.05  (<< 0.60 cap)
async def test_exactly_20_positions_qualifies(db_session):
    wallet = "0x20pos"
    await add_wallet(db_session, wallet=wallet)
    await _seed_uniform(db_session, wallet, 20, entry=0.30, exit_=0.80)

    score = await score_wallet(db_session, wallet, AS_OF, CONFIG)

    assert score.n_positions == 20
    assert score.qualified is True
    assert score.disqual_reasons == []
    assert score.mean_roi == pytest.approx(Decimal("50") / Decimal("30"))


async def test_19_positions_disqualified_min_closed_positions(db_session):
    wallet = "0x19pos"
    await add_wallet(db_session, wallet=wallet)
    await _seed_uniform(db_session, wallet, 19, entry=0.30, exit_=0.80)

    score = await score_wallet(db_session, wallet, AS_OF, CONFIG)

    assert score.n_positions == 19
    assert score.qualified is False
    assert "min_closed_positions" in score.disqual_reasons


# ---------------------------------------------------------------------------
# concentration_cap boundary: exactly 60%
# ---------------------------------------------------------------------------
# All entry/exit/shares below are exact in IEEE-754 binary (powers-of-2
# fractions and integer share counts) so the arithmetic that matters for the
# boundary assertion (concentration_ratio == 0.6 exactly) isn't polluted by
# float rounding noise from the underlying Float DB columns.
#
# 1 "big" position: entry=0.1875, exit=0.8125, shares=96 -> pnl=0.625*96=60, cost_basis=18
# 20 "small" positions: entry=0.5, exit=0.625, shares=16 -> pnl=0.125*16=2, cost_basis=8
#   total_abs_pnl = 60 + 20*2 = 100
#   concentration_ratio = 60 / 100 = 0.60 exactly -> must NOT disqualify (only >0.60 does)
#   mean_roi = (60/18 + 20*(2/8)) / 21 = (3.3333... + 5) / 21 = 0.3968...  (> 0.15 OK)
#   win_rate = 1.0 (all wins); implied = (0.1875 + 20*0.5)/21 = 0.4851...  (1.0 > implied OK)
async def test_concentration_exactly_60_pct_qualifies(db_session):
    wallet = "0xconc60"
    await add_wallet(db_session, wallet=wallet)
    await add_wcp(db_session, wallet=wallet, closed_at=AS_OF - timedelta(days=1),
                  entry_price=0.1875, exit_price=0.8125, shares_sold=96.0)
    await _seed_uniform(db_session, wallet, 20, entry=0.5, exit_=0.625, shares=16.0)

    score = await score_wallet(db_session, wallet, AS_OF, CONFIG)

    assert score.n_positions == 21
    assert score.concentration_ratio == Decimal("0.6")
    assert "concentration_cap" not in score.disqual_reasons
    assert score.qualified is True


# Same shape, big position raised to pnl=62 -> ratio 62/102 = 0.6078... > 0.60 -> disqualifies.
# entry=0.25, exit=0.75, shares=124 -> pnl=0.5*124=62, cost_basis=31.
async def test_concentration_just_above_60_pct_disqualifies(db_session):
    wallet = "0xconc61"
    await add_wallet(db_session, wallet=wallet)
    await add_wcp(db_session, wallet=wallet, closed_at=AS_OF - timedelta(days=1),
                  entry_price=0.25, exit_price=0.75, shares_sold=124.0)
    await _seed_uniform(db_session, wallet, 20, entry=0.5, exit_=0.625, shares=16.0)

    score = await score_wallet(db_session, wallet, AS_OF, CONFIG)

    assert score.concentration_ratio > Decimal("0.6")
    assert "concentration_cap" in score.disqual_reasons
    assert score.qualified is False


# ---------------------------------------------------------------------------
# disputed/unverifiable-market exclusion via closed_at_source
# ---------------------------------------------------------------------------
# 25 trusted positions: entry=0.40, exit=0.48, shares=100 -> pnl=8, cost_basis=40, ROI=0.20
#   mean_roi (trusted only) = 0.20 (> 0.15 OK); win_rate=1.0; implied=0.40 (OK);
#   concentration = 8/(25*8) = 0.04 (OK) -> qualifies on trusted data alone.
# 5 rows tagged closed_at_source='end_date_proxy' (audit's tag for unverifiable/
# drifted resolution status, e.g. markets 630963/2332744): entry=0.90, exit=0.0,
# shares=100 -> pnl=-90, ROI=-1.0 (total loss).
#   If these leaked in: mean_roi = (25*0.20 + 5*(-1.0))/30 = (5.0-5.0)/30 = 0.0 < 0.15
#   -> would flip qualification. Excluding them is what keeps this a pass.
async def test_end_date_proxy_rows_excluded_from_scoring(db_session):
    wallet = "0xdisputed"
    await add_wallet(db_session, wallet=wallet)
    await _seed_uniform(db_session, wallet, 25, entry=0.40, exit_=0.48, closed_at_source="observed_exit")
    for i in range(5):
        await add_wcp(
            db_session,
            wallet=wallet,
            closed_at=AS_OF - timedelta(days=2, hours=i),
            entry_price=0.90,
            exit_price=0.0,
            closed_at_source="end_date_proxy",
        )

    score = await score_wallet(db_session, wallet, AS_OF, CONFIG)

    assert score.n_positions == 25  # the 5 end_date_proxy rows are not counted
    assert score.mean_roi == pytest.approx(Decimal("0.2"))
    assert "min_mean_roi" not in score.disqual_reasons
    assert score.qualified is True


# ---------------------------------------------------------------------------
# win_rate vs implied_win_rate edge case
# ---------------------------------------------------------------------------
# 20 positions, entry=0.50 for all (implied_win_rate = 0.50 exactly).
# Wins: entry=0.5, exit=0.9 -> pnl=40, cost_basis=50, ROI=0.8
# Losses: entry=0.5, exit=0.3 -> pnl=-20, cost_basis=50, ROI=-0.4
async def test_win_rate_equal_to_implied_disqualifies(db_session):
    # 10 wins / 10 losses -> win_rate = 0.5 == implied_win_rate = 0.5 (tie, must NOT pass)
    #   mean_roi = (10*0.8 + 10*(-0.4))/20 = 4/20 = 0.20 (> 0.15, isolates this check)
    #   concentration: abs pnls 10x40 + 10x20, total=600, max=40, ratio=0.0667 (OK)
    wallet = "0xtie"
    await add_wallet(db_session, wallet=wallet)
    await _seed_uniform(db_session, wallet, 10, entry=0.5, exit_=0.9)
    for i in range(10):
        await add_wcp(db_session, wallet=wallet, closed_at=AS_OF - timedelta(days=1, hours=20 + i),
                      entry_price=0.5, exit_price=0.3)

    score = await score_wallet(db_session, wallet, AS_OF, CONFIG)

    assert score.win_rate == pytest.approx(Decimal("0.5"))
    assert score.implied_win_rate == pytest.approx(Decimal("0.5"))
    assert "skill_vs_implied" in score.disqual_reasons
    assert score.qualified is False


async def test_win_rate_marginally_above_implied_qualifies(db_session):
    # 11 wins / 9 losses -> win_rate = 0.55 > implied_win_rate = 0.50
    #   mean_roi = (11*0.8 + 9*(-0.4))/20 = (8.8-3.6)/20 = 0.26 (> 0.15)
    #   concentration: abs pnls 11x40+9x20=620, max=40, ratio=0.0645 (OK)
    wallet = "0xmargin"
    await add_wallet(db_session, wallet=wallet)
    await _seed_uniform(db_session, wallet, 11, entry=0.5, exit_=0.9)
    for i in range(9):
        await add_wcp(db_session, wallet=wallet, closed_at=AS_OF - timedelta(days=1, hours=20 + i),
                      entry_price=0.5, exit_price=0.3)

    score = await score_wallet(db_session, wallet, AS_OF, CONFIG)

    assert score.win_rate > score.implied_win_rate
    assert "skill_vs_implied" not in score.disqual_reasons
    assert score.qualified is True


# ---------------------------------------------------------------------------
# sybil exclusion
# ---------------------------------------------------------------------------
async def test_sybil_flagged_wallet_disqualified_even_if_metrics_pass(db_session):
    wallet = "0xsybil"
    await add_wallet(db_session, wallet=wallet, suspected_sybil=True)
    await _seed_uniform(db_session, wallet, 20, entry=0.30, exit_=0.80)

    score = await score_wallet(db_session, wallet, AS_OF, CONFIG)

    assert "sybil_flagged" in score.disqual_reasons
    assert score.qualified is False
