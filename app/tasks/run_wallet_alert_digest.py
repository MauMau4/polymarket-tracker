"""Flush the daily per-wallet WATCH_WALLET digest (decisions/2026-07-19.md,
item 1b). Wallets flagged `alert_digest=True` have their WATCH_WALLET alerts
persisted with sent=False all day (app/services/alerts/engine.py) instead of
pinged in real time; this job aggregates the trailing 24h of those rows per
wallet into one Telegram message each and sends nothing for a wallet with no
qualifying activity that day (no daily heartbeat, same convention as the WARN
digest in app/services/alerts/system_alerts.py).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.db.models import Alert, Wallet
from app.db.session import get_session_factory
from app.logging import get_logger
from app.services.alerts import telegram

logger = get_logger(__name__)


def _short_wallet(address: str) -> str:
    if len(address) <= 10:
        return address
    return f"{address[:6]}...{address[-4:]}"


async def flush_wallet_alert_digests() -> dict:
    factory = get_session_factory()
    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=24)

    async with factory() as session:
        digest_wallets = (
            await session.execute(select(Wallet.wallet).where(Wallet.alert_digest == True))  # noqa: E712
        ).scalars().all()

        sent_count = 0
        skipped_empty = 0

        for wallet in digest_wallets:
            rows = (
                await session.execute(
                    select(Alert.payload)
                    .where(
                        Alert.wallet == wallet,
                        Alert.alert_type == "WATCH_WALLET",
                        Alert.created_at >= cutoff,
                    )
                    .order_by(Alert.created_at)
                )
            ).all()

            if not rows:
                skipped_empty += 1
                continue

            notionals = [float(p.get("notional_usd") or 0.0) for (p,) in rows]
            count = len(notionals)
            net_notional = sum(notionals)
            largest = max(notionals)

            text = (
                f"📊 WALLET DIGEST: {_short_wallet(wallet)} — {count} trade(s) today, "
                f"net notional ${net_notional:,.0f}, largest single trade ${largest:,.0f}"
            )
            sent = await telegram.send_message(text)
            if sent:
                sent_count += 1

    result = {
        "digest_wallets": len(digest_wallets),
        "sent": sent_count,
        "skipped_empty": skipped_empty,
    }
    logger.info("wallet_alert_digest_complete", **result)
    return result
