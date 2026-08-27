"""Alert engine — evaluates rules, deduplicates via Redis, persists, and dispatches."""
import uuid
from datetime import datetime, timezone

import redis.asyncio as redis_async
from sqlalchemy import select

from app.config import Settings, get_settings
from app.db.models import Alert, Market, Wallet
from app.db.session import get_session_factory
from app.logging import get_logger
from app.schemas.trade import NormalizedTrade
from app.services.alerts import formatter, rules, telegram
from app.services.wallets import profiler

logger = get_logger(__name__)

_DEDUPE_PREFIX = "alert_dedupe"


class AlertEngine:
    def __init__(self) -> None:
        self._settings: Settings = get_settings()
        self._redis: redis_async.Redis | None = None

    async def _get_redis(self) -> redis_async.Redis:
        if self._redis is None:
            self._redis = redis_async.from_url(self._settings.redis_url)
        return self._redis

    async def _is_deduped(self, dedupe_key: str) -> bool:
        try:
            r = await self._get_redis()
            exists = await r.exists(f"{_DEDUPE_PREFIX}:{dedupe_key}")
            return bool(exists)
        except Exception as exc:
            logger.warning("dedupe_redis_check_error", error=str(exc))
            return False  # fail open — allow the alert through

    async def _mark_deduped(self, dedupe_key: str) -> None:
        try:
            r = await self._get_redis()
            await r.setex(
                f"{_DEDUPE_PREFIX}:{dedupe_key}",
                self._settings.default_alert_cooldown_seconds,
                "1",
            )
        except Exception as exc:
            logger.warning("dedupe_redis_mark_error", error=str(exc))

    async def _get_wallet(self, wallet_address: str | None) -> Wallet | None:
        if not wallet_address:
            return None
        try:
            factory = get_session_factory()
            async with factory() as session:
                result = await session.execute(
                    select(Wallet).where(Wallet.wallet == wallet_address)
                )
                return result.scalar_one_or_none()
        except Exception as exc:
            logger.warning("engine_wallet_fetch_error", wallet=wallet_address, error=str(exc))
            return None

    async def _get_market_info(
        self, market_id: str | None
    ) -> tuple[str | None, "datetime | None", str | None]:
        if not market_id:
            return None, None, None
        try:
            factory = get_session_factory()
            async with factory() as session:
                result = await session.execute(
                    select(Market.question, Market.end_date, Market.category).where(
                        Market.market_id == market_id
                    )
                )
                row = result.one_or_none()
                if row is None:
                    return None, None, None
                return row.question, row.end_date, row.category
        except Exception as exc:
            logger.warning("engine_market_fetch_error", market_id=market_id, error=str(exc))
            return None, None, None

    async def _persist_alert(
        self,
        alert_id: str,
        decision,
        trade: NormalizedTrade,
        message: str,
        sent: bool,
    ) -> None:
        try:
            factory = get_session_factory()
            async with factory() as session:
                alert = Alert(
                    id=alert_id,
                    alert_type=decision.alert_type,
                    severity=decision.severity,
                    wallet=trade.wallet,
                    market_id=trade.market_id,
                    asset_id=trade.asset_id,
                    title=f"{decision.alert_type} — {trade.market_id or 'unknown'}",
                    message=message,
                    payload=decision.payload,
                    dedupe_key=decision.dedupe_key,
                    sent=sent,
                    sent_ts=datetime.now(tz=timezone.utc) if sent else None,
                )
                session.add(alert)
                await session.commit()
            logger.info(
                "alert_persisted",
                alert_id=alert_id,
                alert_type=decision.alert_type,
                sent=sent,
            )
        except Exception as exc:
            logger.error("engine_persist_alert_error", error=str(exc))

    async def _ensure_wallet_profile(self, trade: NormalizedTrade) -> None:
        if not trade.wallet:
            return
        try:
            factory = get_session_factory()
            async with factory() as session:
                await profiler.ensure_wallet(session, trade.wallet, trade.ts)
                await session.commit()
        except Exception as exc:
            logger.warning("engine_wallet_profile_error", wallet=trade.wallet, error=str(exc))

    async def _process_inner(self, trade: NormalizedTrade) -> None:
        await self._ensure_wallet_profile(trade)
        wallet = await self._get_wallet(trade.wallet)
        decision = rules.evaluate_trade(trade, wallet, self._settings)

        if not decision.should_alert:
            return

        market_question, market_end_date, market_category = await self._get_market_info(trade.market_id)

        # Skip non-WATCH_WALLET alerts for Sports markets
        if decision.alert_type != "WATCH_WALLET" and market_category == "Sports":
            return

        message = formatter.format_alert(decision, trade, wallet, market_question, market_end_date)

        # Digest routing (decisions/2026-07-19.md item 1b): a wallet flagged
        # alert_digest gets its WATCH_WALLET alerts batched into one daily
        # Telegram message (app/tasks/run_wallet_alert_digest.py) instead of a
        # real-time ping. Persisted immediately (sent=False) so the digest job
        # can aggregate from the alerts table; dedupe is intentionally skipped
        # here — the digest wants every qualifying trade counted, not
        # collapsed by the 15-minute per-market cooldown bucket that real-time
        # pings use to avoid spam.
        if decision.payload.get("is_digest"):
            alert_id = str(uuid.uuid4())
            await self._persist_alert(alert_id, decision, trade, message, sent=False)
            logger.info(
                "alert_digested",
                alert_type=decision.alert_type,
                wallet=trade.wallet,
                market_id=trade.market_id,
            )
            return

        if await self._is_deduped(decision.dedupe_key):
            logger.info(
                "alert_deduped",
                alert_type=decision.alert_type,
                dedupe_key=decision.dedupe_key,
            )
            return

        sent = await telegram.send_message(message)

        alert_id = str(uuid.uuid4())
        await self._persist_alert(alert_id, decision, trade, message, sent)
        await self._mark_deduped(decision.dedupe_key)

        logger.info(
            "alert_dispatched",
            alert_type=decision.alert_type,
            severity=decision.severity,
            wallet=trade.wallet,
            market_id=trade.market_id,
            sent=sent,
        )

    async def process(self, trade: NormalizedTrade) -> None:
        """Evaluate a normalized trade for alerting. Never raises."""
        try:
            await self._process_inner(trade)
        except Exception as exc:
            logger.error(
                "alert_engine_unhandled_error",
                error=str(exc),
                asset_id=trade.asset_id,
                exc_info=True,
            )


# Module-level singleton
_engine_instance: AlertEngine | None = None


def get_alert_engine() -> AlertEngine:
    global _engine_instance
    if _engine_instance is None:
        _engine_instance = AlertEngine()
    return _engine_instance
