"""Tests for the volume-ranked WS subscription selection (decisions/2026-07-18.md).

Real Postgres against polymarket_test, same fixture pattern as tests/scoring.
"""
from datetime import datetime, timezone

from app.db.models import Market, Token, Wallet, WalletPosition
from app.services.discovery.refresh import _MICRO_LIFECYCLE_SLUG_PATTERN, get_subscription_asset_ids


def _market(market_id: str, slug: str, volume: float | None) -> Market:
    return Market(
        market_id=market_id, slug=slug, question=slug, active=True, closed=False,
        resolved=False, volume=volume,
    )


def _token(asset_id: str, market_id: str) -> Token:
    return Token(asset_id=asset_id, market_id=market_id, outcome="Yes")


def test_micro_lifecycle_pattern_matches_updown_slugs():
    assert _MICRO_LIFECYCLE_SLUG_PATTERN.search("btc-updown-5m-1784334300")
    assert _MICRO_LIFECYCLE_SLUG_PATTERN.search("hype-updown-15m-1784334300")


def test_micro_lifecycle_pattern_does_not_match_normal_slugs():
    assert not _MICRO_LIFECYCLE_SLUG_PATTERN.search("nba-champion-2024-2025")
    assert not _MICRO_LIFECYCLE_SLUG_PATTERN.search("will-team-x-win-the-updown-league")


async def test_selects_top_n_by_volume_descending(db_session):
    for i in range(5):
        db_session.add(_market(f"m{i}", f"slug-{i}", volume=float(i)))
        db_session.add(_token(f"a{i}", f"m{i}"))
    await db_session.commit()

    asset_ids, diag = await get_subscription_asset_ids(db_session, top_n=2)

    # highest volume markets are m4 (vol=4) and m3 (vol=3)
    assert set(asset_ids) == {"a4", "a3"}
    assert diag["n_top_volume_selected"] == 2
    assert diag["n_candidate_markets"] == 5


async def test_null_volume_sorts_last_not_as_zero(db_session):
    db_session.add(_market("m_null", "slug-null", volume=None))
    db_session.add(_token("a_null", "m_null"))
    db_session.add(_market("m_zero", "slug-zero", volume=0.0))
    db_session.add(_token("a_zero", "m_zero"))
    await db_session.commit()

    asset_ids, _ = await get_subscription_asset_ids(db_session, top_n=1)

    # a market with real (even zero) volume outranks one with no volume data at all
    assert asset_ids == ["a_zero"]


async def test_excludes_micro_lifecycle_slugs_from_ranking(db_session):
    db_session.add(_market("m_updown", "btc-updown-5m-123", volume=999999.0))
    db_session.add(_token("a_updown", "m_updown"))
    db_session.add(_market("m_normal", "normal-market", volume=1.0))
    db_session.add(_token("a_normal", "m_normal"))
    await db_session.commit()

    asset_ids, diag = await get_subscription_asset_ids(db_session, top_n=5)

    assert "a_updown" not in asset_ids
    assert "a_normal" in asset_ids
    assert diag["n_excluded_micro_lifecycle"] == 1


async def test_watched_wallet_open_position_augments_regardless_of_rank(db_session):
    # Low-volume market that would never make the top-1 cut on its own
    db_session.add(_market("m_low", "low-volume-market", volume=0.01))
    db_session.add(_token("a_low", "m_low"))
    db_session.add(_market("m_high", "high-volume-market", volume=1000.0))
    db_session.add(_token("a_high", "m_high"))

    now = datetime.now(tz=timezone.utc)
    db_session.add(Wallet(wallet="0xwatched", first_seen=now, last_seen=now, watch_status="watch"))
    db_session.add(WalletPosition(
        wallet="0xwatched", asset_id="a_low", market_id="m_low",
        status="Open", opened_at=now,
    ))
    await db_session.commit()

    asset_ids, diag = await get_subscription_asset_ids(db_session, top_n=1)

    assert set(asset_ids) == {"a_high", "a_low"}
    assert diag["n_watched_augmented"] == 1


async def test_watched_wallet_closed_position_does_not_augment(db_session):
    db_session.add(_market("m_low", "low-volume-market", volume=0.01))
    db_session.add(_token("a_low", "m_low"))
    db_session.add(_market("m_high", "high-volume-market", volume=1000.0))
    db_session.add(_token("a_high", "m_high"))

    now = datetime.now(tz=timezone.utc)
    db_session.add(Wallet(wallet="0xwatched", first_seen=now, last_seen=now, watch_status="watch"))
    db_session.add(WalletPosition(
        wallet="0xwatched", asset_id="a_low", market_id="m_low",
        status="Closed", opened_at=now,
    ))
    await db_session.commit()

    asset_ids, diag = await get_subscription_asset_ids(db_session, top_n=1)

    assert asset_ids == ["a_high"]
    assert diag["n_watched_augmented"] == 0


async def test_non_watched_wallet_open_position_does_not_augment(db_session):
    db_session.add(_market("m_low", "low-volume-market", volume=0.01))
    db_session.add(_token("a_low", "m_low"))
    db_session.add(_market("m_high", "high-volume-market", volume=1000.0))
    db_session.add(_token("a_high", "m_high"))

    now = datetime.now(tz=timezone.utc)
    db_session.add(Wallet(wallet="0xnotwatched", first_seen=now, last_seen=now, watch_status=None))
    db_session.add(WalletPosition(
        wallet="0xnotwatched", asset_id="a_low", market_id="m_low",
        status="Open", opened_at=now,
    ))
    await db_session.commit()

    asset_ids, _ = await get_subscription_asset_ids(db_session, top_n=1)

    assert asset_ids == ["a_high"]
