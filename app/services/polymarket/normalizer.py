"""
Trade event normalizer and wallet attribution pipeline.

Flow:
  WS event (last_trade_price only — price_change is order-book noise, not fills)
    → extract asset_id, price, timestamp
    → CLOB attribution (always first — wallet needed for bypass check)
    → watched wallets bypass all filters and go directly to persist
    → notional minimum filter for non-watched wallets
    → build NormalizedTrade with attribution_status
    → persist to DB
    → post-processing: sybil cluster detection, unwind detection
"""
import asyncio
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select as _sa_select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Trade, Wallet, AttributionStatus
from app.db.models.market import Market
from app.db.session import get_session_factory
from app.logging import get_logger
from app.schemas.trade import NormalizedTrade
from app.services.polymarket import clob_client
from app.services.polymarket.websocket_client import parse_events

logger = get_logger(__name__)

# In-process cache: market_ids confirmed to exist in the DB this session.
# Avoids a SELECT on every trade for already-known markets.
_known_market_ids: set[str] = set()


async def _ensure_market_exists(market_id: str) -> None:
    """
    Guarantee a Market row exists for market_id.
    Runs in its own session so failures never affect the calling trade commit.
    Fetches metadata from the Gamma API when the row is absent.
    """
    if not market_id or market_id in _known_market_ids:
        return
    factory = get_session_factory()
    try:
        async with factory() as session:
            existing = (await session.execute(
                _sa_select(Market.market_id).where(Market.market_id == market_id)
            )).scalar_one_or_none()
            if existing:
                _known_market_ids.add(market_id)
                return

        # Not in DB — fetch from Gamma and upsert
        from app.services.polymarket.gamma_client import fetch_market_by_id
        from app.services.discovery.refresh import _upsert_market
        info = await fetch_market_by_id(market_id)
        if info is None:
            logger.warning("market_ensure_not_found_in_gamma", market_id=market_id)
            return
        async with factory() as session:
            await _upsert_market(session, info)
            await session.commit()
        _known_market_ids.add(market_id)
        logger.info("market_ensured_via_gamma", market_id=market_id)
    except Exception as exc:
        logger.warning("market_ensure_error", market_id=market_id, error=str(exc))

_SUPPORTED_EVENT_TYPES = {"last_trade_price"}


def _parse_timestamp(raw: str | int | float | None) -> datetime:
    if raw is None:
        return datetime.now(tz=timezone.utc)
    try:
        value = float(raw)
        if value > 1e10:  # milliseconds — divide before passing to fromtimestamp
            value /= 1000.0
        return datetime.fromtimestamp(value, tz=timezone.utc)
    except (ValueError, TypeError, OSError):
        return datetime.now(tz=timezone.utc)


def _extract_trade_events(event: dict) -> list[dict]:
    """
    Extract individual trade events from a WS message.

    Only last_trade_price events are actual fills. price_change events are
    order-book level changes (orders placed/amended/canceled, NOT trades) —
    treating them as trades fabricated phantom Trade rows and fired whale
    alerts on unfilled orders, so they are deliberately ignored.
    """
    event_type = event.get("event_type", "")

    if event_type == "last_trade_price":
        return [event]

    if event_type not in {"book", "tick_size_change", "price_change"}:
        if event_type:
            logger.debug("ws_unknown_event_type", event_type=event_type)

    return []


async def _attribute_trade(
    asset_id: str,
    price: float,
    event_ts: datetime,
    settings,
):
    """
    Attempt wallet attribution via the Data API.
    Returns (wallet, tx_hash, attribution_status, match_object | None).
    """
    try:
        match = await asyncio.wait_for(
            clob_client.find_matching_trade(
                asset_id=asset_id,
                price=price,
                event_ts=event_ts,
                window_seconds=120,  # ±2 min covers Data API indexing lag
            ),
            timeout=max(settings.wallet_attribution_timeout_seconds, 15),
        )
    except asyncio.TimeoutError:
        logger.warning("attribution_timeout", asset_id=asset_id)
        return None, None, "unresolved", None
    except Exception as exc:
        logger.error("attribution_error", asset_id=asset_id, error=str(exc))
        return None, None, "unresolved", None

    if match is None:
        return None, None, "unresolved", None

    wallet = match.maker_address
    tx_hash = match.transaction_hash
    status = "attributed" if wallet else "unresolved"
    return wallet, tx_hash, status, match


def _build_normalized_trade(
    event: dict,
    asset_id: str,
    market_id: str,
    wallet: str | None,
    tx_hash: str | None,
    attribution_status: str,
    clob_match,
    outcome: str | None = None,
    trade_type: str | None = None,
) -> NormalizedTrade:
    raw_price = event.get("price") or (clob_match.price if clob_match else None)
    raw_size = event.get("size") or (clob_match.size if clob_match else None)

    try:
        price = Decimal(str(raw_price)) if raw_price is not None else None
    except Exception:
        price = None

    try:
        size = Decimal(str(raw_size)) if raw_size is not None else None
    except Exception:
        size = None

    notional_usd = (price * size).quantize(Decimal("0.01")) if price and size else None

    side = event.get("side") or (clob_match.side if clob_match else None)
    # outcome is resolved from the tokens table by the caller — do NOT use clob_match.outcome
    # because the Data API outcome field is unreliable (can carry labels from other markets).
    ts = _parse_timestamp(event.get("timestamp"))

    return NormalizedTrade(
        ts=ts,
        market_id=market_id,
        asset_id=asset_id,
        outcome=outcome,
        price=price,
        size=size,
        notional_usd=notional_usd,
        side=side,
        wallet=wallet,
        tx_hash=tx_hash,
        attribution_status=attribution_status,
        trade_type=trade_type,
        source="ws_market",
        raw_payload=event,
    )


def _finalize_attribution(
    wallet: str | None,
    tx_hash: str | None,
    attribution_status: str,
    is_watched: bool,
    verification_confirmed: bool,
) -> tuple[str | None, str | None, str]:
    """
    Decide whether a find_matching_trade() candidate may be trusted and
    written to trades.wallet/tx_hash.

    find_matching_trade()'s Data API query is unscoped by asset or time
    (decisions/2026-08-11.md, item 0c: asset/startTs/endTs are all silently
    ignored) — it returns whichever trade anywhere on the platform has the
    closest price, not a real match. For a non-watched candidate there is no
    independent check at all, so the raw result is a price-coincidence
    guess and must not be written as attribution. Only a watched-wallet
    candidate that additionally passes verify_wallet_attribution's
    wallet-scoped confirmation (query params: user + asset/startTs/endTs —
    user is honored even though asset/time are not) is trusted.
    """
    if not wallet or not is_watched or not verification_confirmed:
        return None, None, "unresolved"
    return wallet, tx_hash, attribution_status


async def _upsert_wallet(session: AsyncSession, wallet_address: str, ts: datetime) -> None:
    from sqlalchemy import select
    result = await session.execute(select(Wallet).where(Wallet.wallet == wallet_address))
    existing = result.scalar_one_or_none()
    if existing is None:
        session.add(Wallet(
            wallet=wallet_address,
            first_seen=ts,
            last_seen=ts,
        ))
        logger.info("wallet_created", wallet=wallet_address)
    else:
        existing.last_seen = ts


async def _persist_trade(normalized: NormalizedTrade) -> None:
    from app.services.wallets.exit_skill import (
        update_buy_position,
        write_sell_closed_position,
        close_cost_basis_if_resolved,
    )
    from app.services.wallets.positions import (
        update_position_on_buy,
        update_position_on_sell,
    )

    price_f = float(normalized.price) if normalized.price else None
    size_f = float(normalized.size) if normalized.size else None
    side = (normalized.side or "").upper()

    resolved_position_closed = False

    factory = get_session_factory()
    async with factory() as session:
        trade = Trade(
            ts=normalized.ts,
            market_id=normalized.market_id,
            asset_id=normalized.asset_id,
            outcome=normalized.outcome,
            price=price_f,
            size=size_f,
            notional_usd=float(normalized.notional_usd) if normalized.notional_usd else None,
            side=normalized.side,
            wallet=normalized.wallet,
            tx_hash=normalized.tx_hash,
            attribution_status=AttributionStatus(normalized.attribution_status),
            trade_type=normalized.trade_type,
            source=normalized.source,
            raw_payload=normalized.raw_payload,
        )
        session.add(trade)

        if normalized.wallet:
            await _upsert_wallet(session, normalized.wallet, normalized.ts)

        # Position cost basis tracking for exit skill computation
        if normalized.wallet and price_f and size_f and normalized.asset_id:
            if side == "BUY":
                await update_buy_position(
                    session, normalized.wallet, normalized.asset_id,
                    normalized.market_id or "", price_f, size_f,
                )
                await update_position_on_buy(
                    session, normalized.wallet, normalized.asset_id,
                    normalized.market_id or "", price_f, size_f,
                    normalized.ts, normalized.outcome,
                )
                # Close immediately if this market is already resolved.
                # Must run AFTER update_position_on_buy — it also closes the
                # wallet_positions row, which would otherwise be re-opened
                # here and stranded forever (the market never resolves again).
                resolved_position_closed = await close_cost_basis_if_resolved(
                    session,
                    normalized.wallet,
                    normalized.asset_id,
                    normalized.market_id or "",
                    normalized.ts,
                )
            elif side == "SELL":
                sell_pnl = await write_sell_closed_position(
                    session,
                    normalized.wallet,
                    normalized.asset_id,
                    normalized.market_id or "",
                    price_f,
                    size_f,
                    normalized.ts,
                )
                await update_position_on_sell(
                    session, normalized.wallet, normalized.asset_id,
                    size_f, sell_pnl, normalized.ts,
                    market_id=normalized.market_id or "",
                    price=price_f or 0.0,
                )

        await session.commit()
        logger.info(
            "trade_persisted",
            asset_id=normalized.asset_id,
            market_id=normalized.market_id,
            notional_usd=str(normalized.notional_usd),
            attribution=normalized.attribution_status,
            wallet=normalized.wallet,
        )

    # A late BUY that closed against an already-resolved market gets picked up
    # by the next scheduled rescore (30-min resolution job / daily recompute).
    # Spawning a full global rescore per trade caused unbounded concurrent
    # recomputes racing each other's writes.
    if resolved_position_closed:
        logger.info(
            "late_buy_closed_resolved_position",
            wallet=normalized.wallet,
            market_id=normalized.market_id,
        )

    # Guarantee the market row exists — runs in a separate session so a Gamma
    # API failure or unique-constraint race never rolls back the trade above.
    if normalized.market_id:
        await _ensure_market_exists(normalized.market_id)


class TradeNormalizer:
    """
    Stateful normalizer: holds asset_id → market_id and market_id → end_date lookups.
    Call process_ws_payload() for each raw WS message.
    """

    def __init__(self) -> None:
        self._asset_to_market: dict[str, str] = {}
        self._asset_to_outcome: dict[str, str | None] = {}
        self._market_end_dates: dict[str, datetime | None] = {}
        self._watched_wallets: set[str] = set()
        self._settings = get_settings()
        # Protects snapshot reads in _process_single against concurrent dict
        # replacement by _refresh_subscriptions (which runs between awaits).
        self._state_lock = asyncio.Lock()

    def update_asset_map(self, asset_id: str, market_id: str) -> None:
        self._asset_to_market[asset_id] = market_id

    def update_asset_outcome(self, asset_id: str, outcome: str | None) -> None:
        self._asset_to_outcome[asset_id] = outcome

    def update_market_end_date(self, market_id: str, end_date: datetime | None) -> None:
        self._market_end_dates[market_id] = end_date

    def load_asset_map(self, mapping: dict[str, str]) -> None:
        self._asset_to_market.update(mapping)
        logger.info("normalizer_asset_map_loaded", count=len(mapping))

    def load_asset_outcome_map(self, mapping: dict[str, str | None]) -> None:
        self._asset_to_outcome.update(mapping)
        logger.info("normalizer_outcome_map_loaded", count=len(mapping))

    def load_watched_wallets(self, wallets: set[str]) -> None:
        self._watched_wallets = {w.lower() for w in wallets}
        logger.info("normalizer_watched_wallets_loaded", count=len(self._watched_wallets))

    def _should_filter(self, notional_usd: float | None) -> bool:
        """Return True if this trade is below the minimum notional threshold."""
        if notional_usd is not None and notional_usd < self._settings.trade_minimum_notional_usd:
            logger.debug("trade_filtered_min_notional", notional_usd=notional_usd)
            return True
        return False

    async def process_ws_payload(self, raw: dict | list) -> None:
        for event in parse_events(raw):
            trade_events = _extract_trade_events(event)
            for trade_event in trade_events:
                await self._process_single(trade_event)

    async def _process_single(self, event: dict) -> None:
        asset_id = event.get("asset_id", "")
        if not asset_id:
            logger.warning("normalizer_missing_asset_id", event_type=event.get("event_type"))
            return

        # Snapshot shared state under lock before any await.
        # _refresh_subscriptions() can interleave during _attribute_trade()'s Data API
        # call and replace _asset_to_market, _asset_to_outcome, or _watched_wallets.
        # Snapshotting here guarantees the three values are consistent with each other.
        async with self._state_lock:
            market_id = self._asset_to_market.get(asset_id, "")
            outcome_label = self._asset_to_outcome.get(asset_id)
            watched_wallets = frozenset(self._watched_wallets)

        raw_price = event.get("price")
        try:
            price_float = float(raw_price) if raw_price is not None else None
        except (ValueError, TypeError):
            price_float = None

        raw_size = event.get("size")
        try:
            size_float = float(raw_size) if raw_size is not None else None
        except (ValueError, TypeError):
            size_float = None

        notional_pre = (price_float * size_float) if price_float and size_float else None
        ts = _parse_timestamp(event.get("timestamp"))

        # Price-level crossing alerts (decisions/2026-07-19.md item 3) — every
        # observed price tick, independent of trade size/attribution/side, so
        # a sub-$20 print in a thin book still moves the last-known price.
        # Runs before the min-notional filter and never blocks ingestion.
        if price_float is not None:
            try:
                from decimal import Decimal
                from app.db.session import get_session_factory
                from app.services.alerts.price_levels import evaluate_price_levels
                factory = get_session_factory()
                async with factory() as level_session:
                    await evaluate_price_levels(level_session, asset_id, Decimal(str(price_float)), ts)
            except Exception as exc:
                logger.error("normalizer_price_level_error", asset_id=asset_id, error=str(exc))

        # Attribution always runs first — wallet is needed before any filter decision
        wallet: str | None = None
        tx_hash: str | None = None
        attribution_status = "unresolved"
        clob_match = None

        if price_float is not None:
            wallet, tx_hash, attribution_status, clob_match = await _attribute_trade(
                asset_id=asset_id,
                price=price_float,
                event_ts=ts,
                settings=self._settings,
            )

        # Verification step for watched wallets only.
        # find_matching_trade() selects by closest price across all market participants,
        # so if two wallets trade at the same price in the same window it may pick the
        # wrong one.  The wallet-scoped endpoint can only return trades that actually
        # belong to the attributed wallet, so a match there confirms attribution.
        is_watched_candidate = bool(wallet) and wallet.lower() in watched_wallets
        verification_confirmed = False
        if is_watched_candidate and price_float is not None:
            try:
                verification_confirmed = await asyncio.wait_for(
                    clob_client.verify_wallet_attribution(
                        wallet=wallet,
                        asset_id=asset_id,
                        price=price_float,
                        event_ts=ts,
                        window_seconds=120,
                    ),
                    timeout=10.0,
                )
            except asyncio.TimeoutError:
                verification_confirmed = True  # fail open — don't discard on slow API
            except Exception as exc:
                logger.warning("ws_attribution_verify_error", asset_id=asset_id[:20], error=str(exc))
                verification_confirmed = True  # fail open — don't discard on transient error

            if not verification_confirmed:
                logger.warning(
                    "ws_attribution_rejected",
                    asset_id=asset_id[:20],
                    attributed_wallet=wallet[:12],
                    reason="not_in_clob_history",
                )

        # Only a watched-wallet candidate that passed verification above is
        # ever trusted (decisions/2026-08-11.md, item 0c) — everything else
        # from find_matching_trade() is a price-coincidence guess, not
        # attribution, and is discarded rather than written.
        wallet, tx_hash, attribution_status = _finalize_attribution(
            wallet, tx_hash, attribution_status, is_watched_candidate, verification_confirmed,
        )

        # Watched wallets bypass all filters — every trade is captured regardless of notional
        if (wallet or "").lower() in watched_wallets:
            logger.info(
                "watch_wallet_trade_captured",
                wallet=wallet,
                market_id=market_id,
                notional=notional_pre,
            )
        elif self._should_filter(notional_pre):
            return

        normalized = _build_normalized_trade(
            event=event,
            asset_id=asset_id,
            market_id=market_id,
            wallet=wallet,
            tx_hash=tx_hash,
            attribution_status=attribution_status,
            clob_match=clob_match,
            outcome=outcome_label,
        )

        try:
            await _persist_trade(normalized)
        except Exception as exc:
            logger.error(
                "normalizer_persist_error",
                asset_id=asset_id,
                error=str(exc),
                exc_info=True,
            )
            return

        # Alert evaluation — failure here never blocks ingestion
        try:
            from app.services.alerts.engine import get_alert_engine
            await get_alert_engine().process(normalized)
        except Exception as exc:
            logger.error("normalizer_alert_engine_error", asset_id=asset_id, error=str(exc))

        # Post-processing: sybil cluster detection and unwind detection
        if normalized.wallet and normalized.attribution_status == "attributed":
            try:
                from app.services.wallets.clustering import check_sybil_cluster
                await check_sybil_cluster(normalized)
            except Exception as exc:
                logger.error("normalizer_cluster_error", asset_id=asset_id, error=str(exc))

            try:
                from app.services.wallets.unwind import check_unwind
                await check_unwind(normalized)
            except Exception as exc:
                logger.error("normalizer_unwind_error", asset_id=asset_id, error=str(exc))
