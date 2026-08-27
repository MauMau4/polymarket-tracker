"""
One-shot: recompute markets_traded_count for wallet 0x29a3 via update_wallet_activity().
"""
import asyncio
import selectors

from sqlalchemy import select

from app.db.session import get_session_factory
from app.db.models.wallet import Wallet
from app.services.wallets.profiler import update_wallet_activity
from app.logging import setup_logging

setup_logging()

WALLET = "0x29a3ba125bbbc251e5522ed3d90488d5c63ee7c6"


async def main() -> None:
    factory = get_session_factory()

    async with factory() as session:
        row = (await session.execute(
            select(Wallet.markets_traded_count).where(Wallet.wallet == WALLET)
        )).scalar_one_or_none()
        before = row if row is not None else "NOT FOUND"
        print(f"markets_traded_count BEFORE: {before}")

        await update_wallet_activity(session, WALLET)
        await session.commit()

    async with factory() as session:
        after = (await session.execute(
            select(Wallet.markets_traded_count).where(Wallet.wallet == WALLET)
        )).scalar_one_or_none()
        print(f"markets_traded_count AFTER:  {after}")


if __name__ == "__main__":
    asyncio.run(main(), loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()))
