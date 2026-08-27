"""Seed a small, synthetic sample dataset for local development and testing.

Run against a fresh, migrated database:

    DATABASE_URL=postgresql+psycopg://poly:poly@localhost:5432/polymarket python -m scripts.seed_sample_data

All wallet addresses here are synthetic (clearly-fake 0x... strings), not
real on-chain addresses — this script fabricates trades, and attaching
fabricated activity to a real wallet would misrepresent that wallet's
actual history. Markets/questions are illustrative, not live Polymarket
markets.

Covers: an open market with in-progress positions, a resolved market that
exercises the `resolved => closed` constraint (migration 0033) and the
outcome_result stamping (migration 0034), and both open and closed wallet
positions.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.db.models.market import Market
from app.db.models.token import Token
from app.db.models.trade import AttributionStatus, Trade
from app.db.models.wallet import Wallet
from app.db.models.wallet_closed_position import WalletClosedPosition
from app.db.models.wallet_position import WalletPosition

NOW = datetime.now(timezone.utc)

WALLETS = [f"0xSAMPLE000000000000000000000000000000{i:02d}" for i in range(1, 7)]


async def seed(session) -> None:
    # --- Markets -----------------------------------------------------
    resolved_market = Market(
        market_id="sample-market-championship-2026",
        condition_id="0xsample000000000000000000000000000000000000000000000000000001",
        slug="sample-championship-winner-2026",
        question="Will Sample Team A win the 2026 Sample Championship?",
        event_title="Sample Championship 2026",
        category="sports",
        subcategory="sample-league",
        active=False,
        closed=True,
        resolved=True,
        resolution="Yes",
        resolved_at=NOW - timedelta(days=2),
        end_date=NOW - timedelta(days=3),
        volume=125_000.0,
        outcome_result_stamped_resolution="Yes",
    )
    open_market_a = Market(
        market_id="sample-market-election-2026",
        condition_id="0xsample000000000000000000000000000000000000000000000000000002",
        slug="sample-election-outcome-2026",
        question="Will Sample Candidate X win the 2026 Sample Election?",
        event_title="Sample Election 2026",
        category="politics",
        subcategory="sample-region",
        active=True,
        closed=False,
        resolved=False,
        end_date=NOW + timedelta(days=30),
        volume=48_000.0,
    )
    open_market_b = Market(
        market_id="sample-market-btc-updown",
        condition_id="0xsample000000000000000000000000000000000000000000000000000003",
        slug="sample-btc-up-or-down-today",
        question="Will Sample-Coin be up in 24h?",
        event_title="Sample-Coin Up or Down",
        category="crypto",
        subcategory="daily",
        active=True,
        closed=False,
        resolved=False,
        end_date=NOW + timedelta(hours=6),
        volume=9_500.0,
    )
    session.add_all([resolved_market, open_market_a, open_market_b])
    await session.flush()

    # --- Tokens (Yes/No per market) -----------------------------------
    tokens = {
        ("resolved", "Yes"): Token(asset_id="sample-asset-resolved-yes", market_id=resolved_market.market_id, outcome="Yes"),
        ("resolved", "No"): Token(asset_id="sample-asset-resolved-no", market_id=resolved_market.market_id, outcome="No"),
        ("election", "Yes"): Token(asset_id="sample-asset-election-yes", market_id=open_market_a.market_id, outcome="Yes"),
        ("election", "No"): Token(asset_id="sample-asset-election-no", market_id=open_market_a.market_id, outcome="No"),
        ("btc", "Up"): Token(asset_id="sample-asset-btc-up", market_id=open_market_b.market_id, outcome="Up"),
        ("btc", "Down"): Token(asset_id="sample-asset-btc-down", market_id=open_market_b.market_id, outcome="Down"),
    }
    session.add_all(tokens.values())

    # --- Wallets -------------------------------------------------------
    session.add_all(
        Wallet(
            wallet=w,
            first_seen=NOW - timedelta(days=20),
            last_seen=NOW - timedelta(hours=i),
            watch_status=None,
            markets_traded_count=2,
            wallet_score=0.0,
            score_confidence="low",
        )
        for i, w in enumerate(WALLETS)
    )
    await session.flush()

    # --- Trades ----------------------------------------------------------
    trades = []

    # Resolved market: each wallet bought "Yes" early, market resolved Yes.
    for i, w in enumerate(WALLETS[:4]):
        trades.append(
            Trade(
                external_trade_id=f"sample-trade-resolved-buy-{i}",
                ts=NOW - timedelta(days=10, hours=i),
                market_id=resolved_market.market_id,
                asset_id="sample-asset-resolved-yes",
                outcome="Yes",
                price=0.40 + i * 0.02,
                size=100.0,
                notional_usd=(0.40 + i * 0.02) * 100.0,
                side="BUY",
                wallet=w,
                attribution_status=AttributionStatus.attributed,
                source="ws_market",
                raw_payload={"sample": True},
                outcome_result=1,
            )
        )

    # One organic SELL before resolution (wallet 0 trims its position).
    trades.append(
        Trade(
            external_trade_id="sample-trade-resolved-sell-0",
            ts=NOW - timedelta(days=5),
            market_id=resolved_market.market_id,
            asset_id="sample-asset-resolved-yes",
            outcome="Yes",
            price=0.55,
            size=40.0,
            notional_usd=0.55 * 40.0,
            side="SELL",
            wallet=WALLETS[0],
            attribution_status=AttributionStatus.attributed,
            source="ws_market",
            raw_payload={"sample": True},
            outcome_result=1,
        )
    )

    # Open election market: two wallets holding open positions.
    for i, w in enumerate(WALLETS[2:5]):
        trades.append(
            Trade(
                external_trade_id=f"sample-trade-election-buy-{i}",
                ts=NOW - timedelta(days=2, hours=i),
                market_id=open_market_a.market_id,
                asset_id="sample-asset-election-yes",
                outcome="Yes",
                price=0.30 + i * 0.03,
                size=60.0,
                notional_usd=(0.30 + i * 0.03) * 60.0,
                side="BUY",
                wallet=w,
                attribution_status=AttributionStatus.attributed,
                source="ws_market",
                raw_payload={"sample": True},
            )
        )

    # High-frequency crypto market: several small unattributed prints.
    for i in range(4):
        trades.append(
            Trade(
                external_trade_id=f"sample-trade-btc-{i}",
                ts=NOW - timedelta(minutes=30 * i),
                market_id=open_market_b.market_id,
                asset_id="sample-asset-btc-up",
                outcome="Up",
                price=0.50 + (i % 2) * 0.01,
                size=25.0,
                notional_usd=(0.50 + (i % 2) * 0.01) * 25.0,
                side="BUY" if i % 2 == 0 else "SELL",
                wallet=None,
                attribution_status=AttributionStatus.unresolved,
                source="ws_market",
                raw_payload={"sample": True},
            )
        )

    session.add_all(trades)

    # --- Wallet positions ------------------------------------------------
    positions = []
    for i, w in enumerate(WALLETS[:4]):
        shares_sold = 40.0 if i == 0 else 0.0
        positions.append(
            WalletPosition(
                wallet=w,
                asset_id="sample-asset-resolved-yes",
                market_id=resolved_market.market_id,
                selection="Yes",
                total_shares_bought=100.0,
                total_shares_sold=shares_sold,
                remaining_shares=0.0,  # resolution closes out remaining shares
                avg_entry_price=0.40 + i * 0.02,
                total_cost_basis=(0.40 + i * 0.02) * 100.0,
                realized_pnl=(1.0 - (0.40 + i * 0.02)) * (100.0 - shares_sold)
                + (0.55 - (0.40 + i * 0.02)) * shares_sold,
                status="Closed",
                opened_at=NOW - timedelta(days=10, hours=i),
                closed_at=resolved_market.resolved_at,
            )
        )
    for i, w in enumerate(WALLETS[2:5]):
        positions.append(
            WalletPosition(
                wallet=w,
                asset_id="sample-asset-election-yes",
                market_id=open_market_a.market_id,
                selection="Yes",
                total_shares_bought=60.0,
                total_shares_sold=0.0,
                remaining_shares=60.0,
                avg_entry_price=0.30 + i * 0.03,
                total_cost_basis=(0.30 + i * 0.03) * 60.0,
                realized_pnl=0.0,
                status="Open",
                opened_at=NOW - timedelta(days=2, hours=i),
                closed_at=None,
            )
        )
    session.add_all(positions)

    # --- Wallet closed positions -------------------------------------------
    closed_positions = []
    # Organic sell exit (wallet 0 trimmed 40 shares before resolution).
    closed_positions.append(
        WalletClosedPosition(
            wallet=WALLETS[0],
            asset_id="sample-asset-resolved-yes",
            market_id=resolved_market.market_id,
            entry_price=0.40,
            exit_price=0.55,
            shares_sold=40.0,
            total_shares_at_time=100.0,
            position_fraction=0.40,
            cost_basis=0.40 * 40.0,
            conviction_weight=0.40,
            log_roi=0.3185,  # ln(0.55/0.40)
            realized_pnl=(0.55 - 0.40) * 40.0,
            is_resolved=False,
            closed_at=NOW - timedelta(days=5),
            closed_at_source="observed_exit",
        )
    )
    # Resolution closes out the remaining shares for all four wallets.
    for i, w in enumerate(WALLETS[:4]):
        remaining = 60.0 if i == 0 else 100.0
        entry = 0.40 + i * 0.02
        closed_positions.append(
            WalletClosedPosition(
                wallet=w,
                asset_id="sample-asset-resolved-yes",
                market_id=resolved_market.market_id,
                entry_price=entry,
                exit_price=1.0,
                shares_sold=remaining,
                total_shares_at_time=100.0,
                position_fraction=remaining / 100.0,
                cost_basis=entry * remaining,
                conviction_weight=remaining / 100.0,
                log_roi=(0.0 if entry <= 0 else __import__("math").log(1.0 / entry)),
                realized_pnl=(1.0 - entry) * remaining,
                is_resolved=True,
                closed_at=resolved_market.resolved_at,
                closed_at_source="gamma_settlement",
            )
        )
    session.add_all(closed_positions)

    await session.commit()


async def main() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        await seed(session)
    await engine.dispose()
    print("Sample data seeded.")


if __name__ == "__main__":
    asyncio.run(main())
