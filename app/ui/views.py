"""FastAPI + Jinja2 dashboard views."""
import hashlib
import json as _json
import os
import time as _time
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_, distinct, select, func, or_, update as _sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import db_session
from app.db.models import Alert, Market, Trade, Wallet, WalletScoreHistory
from app.db.models.market_price_snapshot import MarketPriceSnapshot
from app.db.models.position import Position
from app.db.models.wallet_closed_position import WalletClosedPosition
from app.db.models.wallet_position import WalletPosition
from app.logging import get_logger
from app.services.polymarket.gamma_client import fetch_market_prices_batch
from app.services.wallets.tagging import VALID_TAGS, VALID_WATCH_STATUSES

router = APIRouter(tags=["dashboard"])
logger = get_logger(__name__)

_TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
templates = Jinja2Templates(directory=_TEMPLATES_DIR)


def _fmt_usd(value) -> str:
    if value is None:
        return "—"
    try:
        return f"${float(value):,.0f}"
    except (TypeError, ValueError):
        return "—"


def _fmt_number(value) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return "—"


def _fmt_selection(outcome: str | None, side: str | None) -> str:
    if outcome is None:
        return "—"
    return outcome


def _consistency_grade(skill_consistency) -> str:
    if skill_consistency is None:
        return "—"
    v = float(skill_consistency)
    if v < 0.05:
        return "A"
    if v < 0.15:
        return "B"
    if v < 0.30:
        return "C"
    return "D"


def _wallet_badges(wallet) -> list[str]:
    """Compute display badges for a wallet object.
    Priority: FARMER (suppresses INFORMED/SHARP) > INFORMED (suppresses SHARP) > SHARP.
    """
    badges: list[str] = []
    if getattr(wallet, "suspected_sybil", False):
        badges.append("SYBIL")
    # FARMER: manual_farmer overrides system computation when set
    manual_farmer = getattr(wallet, "manual_farmer", None)
    is_farmer = False
    if manual_farmer is True:
        badges.append("FARMER")
        is_farmer = True
    elif manual_farmer is None:
        if getattr(wallet, "has_farming_trades", False):
            badges.append("FARMER")
            is_farmer = True
    # elif manual_farmer is False: suppress FARMER badge
    if not is_farmer:
        tier = getattr(wallet, "score_confidence", "Unranked")
        win_rate = getattr(wallet, "win_rate", None)
        realized_pnl = getattr(wallet, "realized_pnl", None)
        weighted_log_roi = getattr(wallet, "_weighted_log_roi", None)

        # INFORMED: Established/Verified AND win_rate >= 50% AND pnl > 0 AND wlr >= ln(1.30)
        manual_informed = getattr(wallet, "manual_informed", None)
        is_informed = False
        if manual_informed is True:
            badges.append("INFORMED")
            is_informed = True
        elif manual_informed is None:
            if (
                tier in ("Established", "Verified")
                and win_rate is not None and win_rate >= 0.50
                and realized_pnl is not None and realized_pnl > 0
                and weighted_log_roi is not None and weighted_log_roi >= 0.262
            ):
                badges.append("INFORMED")
                is_informed = True
        # elif manual_informed is False: suppress INFORMED badge

        # SHARP: Developing/Established/Verified AND win_rate >= 40% AND pnl > 0 AND wlr >= ln(1.15)
        if not is_informed:
            if (
                tier in ("Developing", "Established", "Verified")
                and win_rate is not None and win_rate >= 0.40
                and realized_pnl is not None and realized_pnl > 0
                and weighted_log_roi is not None and weighted_log_roi >= 0.140
            ):
                badges.append("SHARP")
    return badges


async def _attach_roi(wallets, session: AsyncSession) -> None:
    """Batch-fetch SUM(cost_basis) per wallet from wallet_closed_positions,
    compute ROI = realized_pnl / cost_basis * 100, attach as _roi on each wallet object."""
    if not wallets:
        return
    wallet_addresses = [w.wallet for w in wallets]
    from sqlalchemy import text
    rows = (await session.execute(
        text("""
            SELECT wallet, SUM(cost_basis) AS total_cost
            FROM wallet_closed_positions
            WHERE wallet = ANY(:wallets)
            GROUP BY wallet
        """),
        {"wallets": wallet_addresses},
    )).all()
    cost_map = {row.wallet: float(row.total_cost) for row in rows if row.total_cost}
    for w in wallets:
        total_cost = cost_map.get(w.wallet, 0.0)
        pnl = w.realized_pnl if w.realized_pnl is not None else 0.0
        w._roi = (pnl / total_cost * 100) if total_cost > 0 else None


async def _attach_weighted_log_roi(wallets, session: AsyncSession) -> None:
    """Fetch the most recent weighted_log_roi from wallet_score_history for each wallet
    and attach it as _weighted_log_roi on the wallet object (used by _wallet_badges)."""
    if not wallets:
        return
    wallet_addresses = [w.wallet for w in wallets]
    # DISTINCT ON keeps the latest snapshot_ts row per wallet
    from sqlalchemy import text
    rows = (await session.execute(
        text("""
            SELECT DISTINCT ON (wallet) wallet,
                   (components->>'weighted_log_roi')::float AS weighted_log_roi
            FROM wallet_score_history
            WHERE wallet = ANY(:wallets)
            ORDER BY wallet, snapshot_ts DESC
        """),
        {"wallets": wallet_addresses},
    )).all()
    roi_map = {row.wallet: row.weighted_log_roi for row in rows}
    for w in wallets:
        w._weighted_log_roi = roi_map.get(w.wallet)


def _inject_side_into_action(message: str, payload: dict | None) -> str:
    """
    Inject Buy/Sell direction onto the Action line of a stored alert message.
    Handles legacy alerts saved before formatter.py was updated to include the side.
    No-ops if the Action line already contains the direction or if side is missing.
    """
    if not message or not payload:
        return message
    side = payload.get("side")
    if not side:
        return message
    direction = "Buy" if side.upper() == "BUY" else "Sell"
    marker = f" — {direction}"
    lines = message.split("\n")
    for i, line in enumerate(lines):
        if line.startswith("Action:") and marker not in line:
            lines[i] = line + marker
            break
    return "\n".join(lines)


def _fmt_volume(value) -> str:
    if value is None:
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    if v >= 1_000_000:
        return f"${v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"${v / 1_000:.1f}K"
    return f"${v:.0f}"


templates.env.filters["usd"] = _fmt_usd
templates.env.filters["number"] = _fmt_number
templates.env.filters["volume"] = _fmt_volume
templates.env.filters["consistency_grade"] = _consistency_grade
templates.env.globals["wallet_badges"] = _wallet_badges
templates.env.globals["fmt_selection"] = _fmt_selection
templates.env.globals["inject_side_into_action"] = _inject_side_into_action


# ──────────────────────────────────────────────────────────────────────────────
# Dashboard home
# ──────────────────────────────────────────────────────────────────────────────

_DASHBOARD_EXPIRING_PAGE_SIZE = 25
_PRICE_CACHE_TTL = 300  # seconds (5 minutes)


def _price_cache_key(market_ids: list[str]) -> str:
    digest = hashlib.md5(",".join(sorted(market_ids)).encode()).hexdigest()[:16]
    return f"dashboard_prices:{digest}"


async def _get_cached_prices(market_ids: list[str]) -> tuple[dict | None, int | None]:
    """Return (prices_dict, cached_at_unix) from Redis, or (None, None) on miss/error."""
    try:
        import redis.asyncio as redis_async
        from app.config import get_settings
        r = redis_async.from_url(get_settings().redis_url, decode_responses=True)
        try:
            raw = await r.get(_price_cache_key(market_ids))
        finally:
            await r.aclose()
        if raw:
            data = _json.loads(raw)
            return data["prices"], data.get("cached_at")
    except Exception:
        pass
    return None, None


async def _set_cached_prices(market_ids: list[str], prices: dict) -> None:
    """Write prices to Redis with TTL; silently ignores errors."""
    try:
        import redis.asyncio as redis_async
        from app.config import get_settings
        r = redis_async.from_url(get_settings().redis_url, decode_responses=True)
        payload = _json.dumps({"prices": prices, "cached_at": int(_time.time())})
        try:
            await r.setex(_price_cache_key(market_ids), _PRICE_CACHE_TTL, payload)
        finally:
            await r.aclose()
    except Exception:
        pass


@router.get("/", response_class=HTMLResponse)
async def dashboard_home(
    request: Request,
    session: AsyncSession = Depends(db_session),
    expiring_page: int = Query(1, ge=1),
    category: str | None = Query(None),
    subcategory: str | None = Query(None),
    min_volume: float = Query(0.0, ge=0.0),
    min_price: float = Query(0.0, ge=0.0, le=1.0),
):
    from app.db.models import Token

    now = datetime.now(tz=timezone.utc)

    # Markets closing within 72 hours
    cutoff_72h = now + timedelta(hours=72)

    def _base_expiring_where(stmt):
        stmt = stmt.where(
            Market.active == True,  # noqa: E712
            Market.resolved == False,  # noqa: E712
            Market.end_date >= now,
            Market.end_date <= cutoff_72h,
            Market.subcategory.isnot(None),
        )
        if category and category != "all":
            stmt = stmt.where(Market.category == category)
        if subcategory and subcategory != "all":
            stmt = stmt.where(Market.subcategory == subcategory)
        if min_volume > 0:
            # NULL volume treated as 0 — excluded when threshold active
            stmt = stmt.where(Market.volume >= min_volume)
        return stmt

    # Fetch all matching markets. Prices are fetched and the effectively-
    # resolved/min_price filters applied to this FULL window (see below)
    # BEFORE pagination is computed — closes the 2026-07-24 regression where
    # the pager counted markets that then got filtered out before render
    # (e.g. an entire same-end_date negRisk book — F1's ~22 per-driver
    # "fastest lap" legs — going effectively-decided together, leaving page 1
    # blank under a nonzero total). decisions/2026-07-17.md's page-only
    # scoping is superseded by this; see the price-fetch block below for why
    # it's still safe cost-wise.
    all_markets_result = await session.execute(
        _base_expiring_where(select(Market)).order_by(Market.end_date.asc())
    )
    all_markets = list(all_markets_result.scalars().all())

    # Distinct category/subcategory values within the 72h window (for dropdowns, unfiltered)
    cat_rows = (await session.execute(
        select(Market.category).where(
            Market.active == True,  # noqa: E712
            Market.resolved == False,  # noqa: E712
            Market.end_date >= now,
            Market.end_date <= cutoff_72h,
            Market.subcategory.isnot(None),
            Market.category.isnot(None),
        ).distinct().order_by(Market.category)
    )).scalars().all()
    expiring_categories = list(cat_rows)

    subcat_stmt = select(Market.subcategory).where(
        Market.active == True,  # noqa: E712
        Market.resolved == False,  # noqa: E712
        Market.end_date >= now,
        Market.end_date <= cutoff_72h,
        Market.subcategory.isnot(None),
    )
    if category and category != "all":
        subcat_stmt = subcat_stmt.where(Market.category == category)
    subcat_rows = (await session.execute(
        subcat_stmt.distinct().order_by(Market.subcategory)
    )).scalars().all()
    expiring_subcategories = list(subcat_rows)

    # Live prices — try Redis cache first, fall back to Gamma API. Priced for
    # the FULL window now, not just one page (see comment above), but this is
    # NOT a live per-request cost: _get_cached_prices/_set_cached_prices key
    # on the full sorted market_id list, same as before, so within one
    # _PRICE_CACHE_TTL (5 min) window every dashboard load — any page, any
    # user, this filter combination — shares one fetch. The real cost is
    # that a cache MISS now always prices the whole window (currently ~974
    # markets, via fetch_market_prices_batch's existing concurrency-capped
    # per-market lookups) rather than just whichever ~25-market page happened
    # to be requested — a deliberate trade for pager correctness. This window
    # will keep growing: sports/genre discovery now ingest high-cardinality
    # single-entity prop books (e.g. one F1 session becomes ~22 sibling
    # "fastest lap" markets instead of 1-2), so this cache is what keeps the
    # per-refresh Gamma call count bounded — it must never be bypassed to
    # re-fetch per page or per request (that's exactly the 595-request
    # regression commit 560f17f fixed).
    all_market_ids = [m.market_id for m in all_markets]
    live_prices: dict[str, dict[str, float]] = {}
    prices_cached_at: int | None = None
    if all_market_ids:
        cached, prices_cached_at = await _get_cached_prices(all_market_ids)
        if cached is not None:
            live_prices = cached
        else:
            fetched_volumes: dict[str, float] = {}
            try:
                live_prices = await fetch_market_prices_batch(
                    all_market_ids, out_volumes=fetched_volumes
                )
                await _set_cached_prices(all_market_ids, live_prices)
            except Exception:
                pass
            # Bulk-update markets.volume from freshly fetched data — only on
            # a cache miss (at most once per _PRICE_CACHE_TTL), never per
            # request.
            if fetched_volumes:
                try:
                    for mid, vol in fetched_volumes.items():
                        await session.execute(
                            _sa_update(Market)
                            .where(Market.market_id == mid)
                            .values(volume=vol)
                        )
                    await session.commit()
                except Exception:
                    await session.rollback()
            prices_cached_at = None  # fresh fetch — no age to show

    # Drop markets that have effectively resolved but not yet closed: a live
    # outcome price at/above the resolution threshold (same 0.99 heuristic as
    # gamma_client.derive_resolution) means the winner is decided even though
    # end_date hasn't passed and Gamma still reports the market as active.
    # Applied to the full window now, before pagination is computed — a
    # market anywhere in the window that's effectively resolved no longer
    # occupies a page slot or counts toward the total.
    _EFFECTIVELY_RESOLVED_PRICE = 0.99
    filtered_markets = [
        m for m in all_markets
        if not (
            (pd := live_prices.get(m.market_id))
            and max(pd.values(), default=0.0) >= _EFFECTIVELY_RESOLVED_PRICE
        )
    ]

    # min_price filter, same full-window scoping.
    if min_price > 0:
        filtered_markets = [
            m for m in filtered_markets
            if (pd := live_prices.get(m.market_id)) and max(pd.values(), default=0.0) >= min_price
        ]

    # Pagination on the fully-filtered list — total/total_pages now always
    # match what actually renders.
    expiring_total = len(filtered_markets)
    expiring_total_pages = max(1, (expiring_total + _DASHBOARD_EXPIRING_PAGE_SIZE - 1) // _DASHBOARD_EXPIRING_PAGE_SIZE)
    expiring_page = min(expiring_page, expiring_total_pages)
    expiring_offset = (expiring_page - 1) * _DASHBOARD_EXPIRING_PAGE_SIZE
    expiring_markets = filtered_markets[expiring_offset:expiring_offset + _DASHBOARD_EXPIRING_PAGE_SIZE]

    # Build per-market outcome price pairs for the displayed page only.
    expiring_prices: dict[str, dict] = {}
    for m in expiring_markets:
        api_prices = live_prices.get(m.market_id)
        if api_prices:
            pairs = [(label, price) for label, price in api_prices.items()]
            expiring_prices[m.market_id] = {"pairs": pairs, "live": True}
        else:
            # Fallback: outcome labels from tokens table, no price
            token_rows = (await session.execute(
                select(Token.outcome).where(
                    Token.market_id == m.market_id,
                    Token.outcome.isnot(None),
                )
            )).scalars().all()
            if token_rows:
                expiring_prices[m.market_id] = {"pairs": [(lbl, None) for lbl in token_rows], "live": False}

    def _expiring_url(p: int) -> str:
        parts = [f"expiring_page={p}"]
        if category and category != "all":
            parts.append(f"category={category}")
        if subcategory and subcategory != "all":
            parts.append(f"subcategory={subcategory}")
        if min_volume > 0:
            parts.append(f"min_volume={min_volume:.0f}")
        if min_price > 0:
            parts.append(f"min_price={min_price}")
        return "/?" + "&".join(parts)

    prices_age_min: int | None = None
    if prices_cached_at is not None:
        prices_age_min = max(0, int((_time.time() - prices_cached_at) / 60))

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "expiring_markets": expiring_markets,
            "expiring_prices": expiring_prices,
            "expiring_categories": expiring_categories,
            "expiring_subcategories": expiring_subcategories,
            "expiring_selected_category": category or "all",
            "expiring_selected_subcategory": subcategory or "all",
            "expiring_min_volume": min_volume,
            "expiring_min_price": min_price,
            "prices_age_min": prices_age_min,
            "expiring_pagination": {
                "page": expiring_page,
                "total_pages": expiring_total_pages,
                "total": expiring_total,
                "page_size": _DASHBOARD_EXPIRING_PAGE_SIZE,
                "offset": expiring_offset,
                "prev_url": _expiring_url(expiring_page - 1) if expiring_page > 1 else None,
                "next_url": _expiring_url(expiring_page + 1) if expiring_page < expiring_total_pages else None,
            },
        },
    )


# ──────────────────────────────────────────────────────────────────────────────
# Markets
# ──────────────────────────────────────────────────────────────────────────────

_MARKETS_PAGE_SIZE = 25


@router.get("/markets", response_class=HTMLResponse)
async def markets_page(
    request: Request,
    session: AsyncSession = Depends(db_session),
    q: str | None = Query(None),
    status: str | None = Query(None),
    category: str | None = Query(None),
    subcategory: str | None = Query(None),
    end_before: str | None = Query(None),
    end_after: str | None = Query(None),
    page: int = Query(1, ge=1),
):
    now_utc = datetime.now(tz=timezone.utc)

    # Pin every query in this request to one consistent snapshot. Under the
    # default READ COMMITTED isolation, count_stmt and data_stmt each get
    # their own snapshot — Market Discovery (5 min), Sports/Genre Discovery
    # and Market Resolution (30 min) all write active/resolved on this same
    # table on independent schedules, so a commit landing between the two
    # statements can make `total` and the returned rows describe different
    # populations (e.g. total_pages computed pre-commit, rows post-commit).
    # REPEATABLE READ fixes the snapshot at the first statement so both see
    # the same rows regardless of concurrent writers.
    await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})

    def _apply_filters(stmt):
        if status == "resolved":
            stmt = stmt.where(Market.resolved == True)  # noqa: E712
        elif status == "closed":
            stmt = stmt.where(Market.closed == True, Market.resolved == False)  # noqa: E712
        elif status == "all":
            pass
        else:
            # Default ACTIVE view: flags are source of truth; end_date is not used here
            # because resolved markets can have future end_dates.
            stmt = stmt.where(
                Market.active == True,  # noqa: E712
                Market.resolved == False,  # noqa: E712
            )

        if q:
            stmt = stmt.where(
                or_(
                    Market.question.ilike(f"%{q}%"),
                    Market.event_title.ilike(f"%{q}%"),
                )
            )
        if category and category != "all":
            stmt = stmt.where(Market.category == category)
        if subcategory and subcategory != "all":
            stmt = stmt.where(Market.subcategory == subcategory)
        if end_before:
            try:
                dt = datetime.strptime(end_before, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                stmt = stmt.where(Market.end_date <= dt)
            except ValueError:
                pass
        if end_after:
            try:
                dt = datetime.strptime(end_after, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                stmt = stmt.where(Market.end_date >= dt)
            except ValueError:
                pass
        return stmt

    ordering = [Market.active.desc(), Market.end_date.asc().nulls_last(), Market.market_id.asc()]

    count_stmt = _apply_filters(select(func.count()).select_from(Market))
    total = (await session.execute(count_stmt)).scalar_one()

    offset = (page - 1) * _MARKETS_PAGE_SIZE
    data_stmt = _apply_filters(select(Market)).order_by(*ordering).limit(_MARKETS_PAGE_SIZE).offset(offset)
    markets = (await session.execute(data_stmt)).scalars().all()
    total_pages = max(1, (total + _MARKETS_PAGE_SIZE - 1) // _MARKETS_PAGE_SIZE)

    cat_result = await session.execute(
        select(Market.category).where(Market.category.is_not(None)).distinct().order_by(Market.category)
    )
    categories = [row[0] for row in cat_result.all()]

    subcat_result = await session.execute(
        select(Market.subcategory).where(Market.subcategory.is_not(None)).distinct().order_by(Market.subcategory)
    )
    subcategories = [row[0] for row in subcat_result.all()]

    status_count_result = await session.execute(
        select(
            func.count().filter(Market.active == True, Market.resolved == False),   # noqa: E712
            func.count().filter(Market.closed == True, Market.resolved == False),  # noqa: E712
            func.count().filter(Market.resolved == True),  # noqa: E712
        ).select_from(Market)
    )
    sc = status_count_result.one()
    status_counts = {"active": sc[0], "closed": sc[1], "resolved": sc[2]}

    def _page_url(p: int) -> str:
        parts = []
        if q:
            parts.append(f"q={q}")
        if status and status != "active":
            parts.append(f"status={status}")
        if category:
            parts.append(f"category={category}")
        if subcategory:
            parts.append(f"subcategory={subcategory}")
        if end_after:
            parts.append(f"end_after={end_after}")
        if end_before:
            parts.append(f"end_before={end_before}")
        if p > 1:
            parts.append(f"page={p}")
        qs = "&".join(parts)
        return f"/markets?{qs}" if qs else "/markets"

    return templates.TemplateResponse(
        request, "markets.html",
        {
            "markets": markets,
            "categories": categories,
            "subcategories": subcategories,
            "status_counts": status_counts,
            "pagination": {
                "page": page, "total_pages": total_pages, "total": total,
                "page_size": _MARKETS_PAGE_SIZE, "offset": offset,
                "prev_url": _page_url(page - 1) if page > 1 else None,
                "next_url": _page_url(page + 1) if page < total_pages else None,
            },
            "filters": {
                "q": q or "", "status": status or "active", "category": category or "",
                "subcategory": subcategory or "", "end_before": end_before or "",
                "end_after": end_after or "",
            },
        },
    )


_MARKET_DETAIL_TRADES_PAGE_SIZE = 25


@router.get("/markets/{market_id}", response_class=HTMLResponse)
async def market_detail_page(
    market_id: str,
    request: Request,
    session: AsyncSession = Depends(db_session),
    trades_page: int = Query(1, ge=1),
    side: str | None = Query(None),
    selection: str | None = Query(None),
    sort: str | None = Query(None),
):
    market_result = await session.execute(select(Market).where(Market.market_id == market_id))
    market = market_result.scalar_one_or_none()

    # Distinct outcome values for Selection dropdown
    outcome_rows = (await session.execute(
        select(Trade.outcome).where(
            Trade.market_id == market_id,
            Trade.outcome.isnot(None),
        ).distinct().order_by(Trade.outcome)
    )).scalars().all()
    outcome_options = list(outcome_rows)

    def _base_trades_where(stmt):
        stmt = stmt.where(Trade.market_id == market_id)
        if side and side.upper() in ("BUY", "SELL"):
            stmt = stmt.where(Trade.side == side.upper())
        if selection and selection != "all":
            stmt = stmt.where(Trade.outcome == selection)
        return stmt

    trades_total = (await session.execute(
        _base_trades_where(select(func.count()).select_from(Trade))
    )).scalar_one()
    trades_offset = (trades_page - 1) * _MARKET_DETAIL_TRADES_PAGE_SIZE
    trades_total_pages = max(1, (trades_total + _MARKET_DETAIL_TRADES_PAGE_SIZE - 1) // _MARKET_DETAIL_TRADES_PAGE_SIZE)

    base_q = _base_trades_where(select(Trade))
    if sort == "asc":
        base_q = base_q.order_by(Trade.notional_usd.asc(), Trade.ts.desc())
    elif sort == "desc":
        base_q = base_q.order_by(Trade.notional_usd.desc(), Trade.ts.desc())
    else:
        base_q = base_q.order_by(Trade.ts.desc())

    trades_result = await session.execute(
        base_q.limit(_MARKET_DETAIL_TRADES_PAGE_SIZE).offset(trades_offset)
    )
    trades = trades_result.scalars().all()

    alerts_result = await session.execute(
        select(Alert).where(Alert.market_id == market_id).order_by(Alert.created_at.desc()).limit(20)
    )
    alerts = alerts_result.scalars().all()

    def _trades_url(p: int) -> str:
        parts = [f"trades_page={p}"]
        if side:
            parts.append(f"side={side}")
        if selection and selection != "all":
            parts.append(f"selection={selection}")
        if sort:
            parts.append(f"sort={sort}")
        return f"/markets/{market_id}?" + "&".join(parts)

    def _sort_url() -> str:
        next_sort = "asc" if sort == "desc" else "desc"
        parts = ["trades_page=1"]
        if side:
            parts.append(f"side={side}")
        if selection and selection != "all":
            parts.append(f"selection={selection}")
        parts.append(f"sort={next_sort}")
        return f"/markets/{market_id}?" + "&".join(parts)

    sort_indicator = "▼" if sort == "desc" else ("▲" if sort == "asc" else "")

    return templates.TemplateResponse(
        request, "market_detail.html",
        {
            "market": market,
            "trades": trades,
            "alerts": alerts,
            "outcome_options": outcome_options,
            "selected_side": side or "",
            "selected_selection": selection or "all",
            "sort": sort or "",
            "sort_indicator": sort_indicator,
            "sort_url": _sort_url(),
            "trades_pagination": {
                "page": trades_page,
                "total_pages": trades_total_pages,
                "total": trades_total,
                "page_size": _MARKET_DETAIL_TRADES_PAGE_SIZE,
                "offset": trades_offset,
                "prev_url": _trades_url(trades_page - 1) if trades_page > 1 else None,
                "next_url": _trades_url(trades_page + 1) if trades_page < trades_total_pages else None,
            },
        },
    )


# ──────────────────────────────────────────────────────────────────────────────
# Alert log
# ──────────────────────────────────────────────────────────────────────────────

_ALERTS_PAGE_SIZE = 10


@router.get("/alerts", response_class=HTMLResponse)
async def alerts_page(
    request: Request,
    session: AsyncSession = Depends(db_session),
    page: int = Query(1, ge=1),
):
    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=48)

    total_in_window = (await session.execute(
        select(func.count()).select_from(Alert).where(Alert.created_at >= cutoff)
    )).scalar_one()

    offset = (page - 1) * _ALERTS_PAGE_SIZE
    total_pages = max(1, (total_in_window + _ALERTS_PAGE_SIZE - 1) // _ALERTS_PAGE_SIZE)

    result = await session.execute(
        select(Alert)
        .where(Alert.created_at >= cutoff)
        .order_by(Alert.created_at.desc())
        .limit(_ALERTS_PAGE_SIZE)
        .offset(offset)
    )
    alerts = result.scalars().all()

    def _page_url(p: int) -> str:
        return f"/alerts?page={p}"

    return templates.TemplateResponse(
        request, "alerts.html",
        {
            "alerts": alerts,
            "total_in_window": total_in_window,
            "window_hours": 48,
            "pagination": {
                "page": page,
                "total_pages": total_pages,
                "total": total_in_window,
                "page_size": _ALERTS_PAGE_SIZE,
                "offset": offset,
                "prev_url": _page_url(page - 1) if page > 1 else None,
                "next_url": _page_url(page + 1) if page < total_pages else None,
            },
        },
    )


# ──────────────────────────────────────────────────────────────────────────────
# Wallets list
# ──────────────────────────────────────────────────────────────────────────────

_WALLETS_PAGE_SIZE = 50

_SORT_COLUMNS = {
    "score": Wallet.wallet_score,
    "confidence": Wallet.score_confidence,
    "markets": Wallet.markets_traded_count,
    "last_seen": Wallet.last_seen,
    "win_rate": Wallet.win_rate,
}


@router.get("/wallets", response_class=HTMLResponse)
async def wallets_page(
    request: Request,
    session: AsyncSession = Depends(db_session),
    page: int = Query(1, ge=1),
    q: str | None = Query(None),
    sort: str = Query("score"),
    order: str = Query("desc"),
    confidence: str | None = Query(None),
    watch_status: str | None = Query(None),
    suspected_sybil: str | None = Query(None),
    period: str = Query("24h"),
):
    stmt = select(Wallet)

    # Search by wallet address or explicit watch filter: both bypass time filter
    if q and q.strip():
        stmt = stmt.where(Wallet.wallet.ilike(f"%{q.strip()}%"))
    elif watch_status == "watched":
        pass  # show all watched wallets regardless of last_seen
    else:
        # Default: last 24 hours based on last_seen; "all" shows everything
        if period == "24h":
            cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=24)
            stmt = stmt.where(Wallet.last_seen >= cutoff)
        elif period == "7d":
            cutoff = datetime.now(tz=timezone.utc) - timedelta(days=7)
            stmt = stmt.where(Wallet.last_seen >= cutoff)
        # period == "all": no filter

    # Confidence filter
    if confidence and confidence != "all":
        stmt = stmt.where(Wallet.score_confidence == confidence)

    # Watch status filter
    if watch_status == "watched":
        stmt = stmt.where(Wallet.watch_status.in_(["watch", "high-interest"]))
    elif watch_status == "unwatched":
        stmt = stmt.where(or_(Wallet.watch_status.is_(None), Wallet.watch_status == "ignore"))

    # Sybil filter
    if suspected_sybil == "yes":
        stmt = stmt.where(Wallet.suspected_sybil == True)  # noqa: E712
    elif suspected_sybil == "no":
        stmt = stmt.where(Wallet.suspected_sybil == False)  # noqa: E712

    # Count before pagination
    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = (await session.execute(count_stmt)).scalar_one()
    total_pages = max(1, (total + _WALLETS_PAGE_SIZE - 1) // _WALLETS_PAGE_SIZE)
    offset = (page - 1) * _WALLETS_PAGE_SIZE

    # Sorting
    sort_col = _SORT_COLUMNS.get(sort, Wallet.wallet_score)
    if order == "asc":
        stmt = stmt.order_by(sort_col.asc().nulls_last(), Wallet.last_seen.desc())
    else:
        stmt = stmt.order_by(sort_col.desc().nulls_last(), Wallet.last_seen.desc())

    wallets_result = await session.execute(stmt.limit(_WALLETS_PAGE_SIZE).offset(offset))
    wallets = wallets_result.scalars().all()
    await _attach_weighted_log_roi(wallets, session)

    def _page_url(p: int) -> str:
        parts = [f"sort={sort}", f"order={order}", f"period={period}"]
        if q:
            parts.append(f"q={q}")
        if confidence:
            parts.append(f"confidence={confidence}")
        if watch_status:
            parts.append(f"watch_status={watch_status}")
        if suspected_sybil:
            parts.append(f"suspected_sybil={suspected_sybil}")
        if p > 1:
            parts.append(f"page={p}")
        return f"/wallets?{'&'.join(parts)}"

    def _sort_url(col: str) -> str:
        new_order = "asc" if (sort == col and order == "desc") else "desc"
        parts = [f"sort={col}", f"order={new_order}", f"period={period}"]
        if q:
            parts.append(f"q={q}")
        if confidence:
            parts.append(f"confidence={confidence}")
        if watch_status:
            parts.append(f"watch_status={watch_status}")
        if suspected_sybil:
            parts.append(f"suspected_sybil={suspected_sybil}")
        return f"/wallets?{'&'.join(parts)}"

    return templates.TemplateResponse(
        request, "wallets.html",
        {
            "wallets": wallets,
            "pagination": {
                "page": page, "total_pages": total_pages, "total": total,
                "page_size": _WALLETS_PAGE_SIZE, "offset": offset,
                "prev_url": _page_url(page - 1) if page > 1 else None,
                "next_url": _page_url(page + 1) if page < total_pages else None,
            },
            "filters": {
                "q": q or "",
                "sort": sort,
                "order": order,
                "period": period,
                "confidence": confidence or "",
                "watch_status": watch_status or "",
                "suspected_sybil": suspected_sybil or "",
            },
            "sort_url": _sort_url,
        },
    )


# ──────────────────────────────────────────────────────────────────────────────
# Rankings
# ──────────────────────────────────────────────────────────────────────────────

@router.get("/rankings", response_class=HTMLResponse)
async def rankings_page(
    request: Request,
    session: AsyncSession = Depends(db_session),
    sort: str = Query("score"),
    order: str = Query("desc"),
    page: int = Query(1, ge=1),
):
    _PAGE_SIZE = 25
    _MAX_RANKINGS = 100  # cap total wallets displayed across all pages

    _roi_subq = (
        select(func.coalesce(func.sum(WalletClosedPosition.cost_basis), 0.0))
        .where(WalletClosedPosition.wallet == Wallet.wallet)
        .scalar_subquery()
    )
    _roi_sort_expr = (Wallet.realized_pnl / func.nullif(_roi_subq, 0) * 100)

    _RANK_SORT = {
        "score": Wallet.wallet_score,
        "win_rate": Wallet.win_rate,
        "consistency": Wallet.resolved_trades_count,  # proxy: more positions = more consistent data
        "resolved": Wallet.resolved_trades_count,
        "markets": Wallet.markets_traded_count,
        "pnl": Wallet.realized_pnl,
        "roi": _roi_sort_expr,
    }

    # Qualification gates:
    #   • tier >= Developing (5+ closed positions)
    #   • not a farmer (manual_farmer=False overrides system tag)
    #   • win rate >= 30%
    #   • positive realized PnL
    #   • sybil-tagged wallets are included — coordinated wallets may be the most
    #     informed and should be surfaced, not hidden; SYBIL badge shown in table
    not_farmer = or_(
        Wallet.manual_farmer == False,  # noqa: E712  forced off
        and_(Wallet.manual_farmer.is_(None), Wallet.has_farming_trades == False),  # noqa: E712
    )
    stmt = select(Wallet).where(
        Wallet.score_confidence.in_(["Developing", "Established", "Verified"]),
        not_farmer,
        Wallet.win_rate >= 0.30,
        Wallet.realized_pnl > 0,
    )

    total_unfiltered = (await session.execute(
        select(func.count()).select_from(stmt.subquery())
    )).scalar_one()
    total = min(total_unfiltered, _MAX_RANKINGS)
    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    # Clamp offset so we never show rows beyond _MAX_RANKINGS
    offset = min((page - 1) * _PAGE_SIZE, max(0, _MAX_RANKINGS - _PAGE_SIZE))

    sort_col = _RANK_SORT.get(sort, Wallet.wallet_score)

    if order == "asc":
        stmt = stmt.order_by(sort_col.asc().nulls_last())
    else:
        stmt = stmt.order_by(sort_col.desc().nulls_last())

    wallets_result = await session.execute(stmt.limit(_PAGE_SIZE).offset(offset))
    wallets = wallets_result.scalars().all()
    await _attach_weighted_log_roi(wallets, session)
    await _attach_roi(wallets, session)

    # Assign ranks (global rank based on current page position)
    ranked_wallets = [(offset + i + 1, w) for i, w in enumerate(wallets)]

    def _sort_url(col: str) -> str:
        new_order = "asc" if (sort == col and order == "desc") else "desc"
        return f"/rankings?sort={col}&order={new_order}&page=1"

    def _page_url(p: int) -> str:
        return f"/rankings?sort={sort}&order={order}&page={p}"

    return templates.TemplateResponse(
        request, "rankings.html",
        {
            "ranked_wallets": ranked_wallets,
            "pagination": {
                "page": page, "total_pages": total_pages, "total": total,
                "page_size": _PAGE_SIZE, "offset": offset,
                "prev_url": _page_url(page - 1) if page > 1 else None,
                "next_url": _page_url(page + 1) if page < total_pages else None,
            },
            "filters": {"sort": sort, "order": order},
            "sort_url": _sort_url,
        },
    )


# ──────────────────────────────────────────────────────────────────────────────
# Wallet detail
# ──────────────────────────────────────────────────────────────────────────────

_WALLET_DETAIL_TRADES_PAGE_SIZE = 10
_WALLET_DETAIL_POSITIONS_PAGE_SIZE = 10
_WALLET_DETAIL_SCORE_HISTORY_PAGE_SIZE = 10
_WALLET_DETAIL_ALERTS_PAGE_SIZE = 10


async def _build_enriched_trades(
    session: AsyncSession,
    wallet_address: str,
    trades: list,
) -> tuple[list[dict], bool]:
    """Enrich a page of Trade ORM rows with FIFO classification, WCP cost basis, and missing-data flags."""
    wcp_result = await session.execute(
        select(WalletClosedPosition)
        .where(
            WalletClosedPosition.wallet == wallet_address,
        )
    )
    wcp_rows = wcp_result.scalars().all()
    _wcp_by_key: dict[tuple, WalletClosedPosition] = {}
    for w in wcp_rows:
        key = (w.asset_id, w.closed_at, round(float(w.shares_sold), 4))
        _wcp_by_key[key] = w

    def _find_wcp(asset_id: str, ts, size=None) -> WalletClosedPosition | None:
        if size is not None:
            exact = _wcp_by_key.get((asset_id, ts, round(float(size), 4)))
            if exact:
                return exact
        best = None
        best_delta = None
        # Normalise ts to UTC so naive/aware comparisons don't raise TypeError.
        ts_cmp = ts.replace(tzinfo=timezone.utc) if ts is not None and ts.tzinfo is None else ts
        for (aid, cat, _sz), wcp in _wcp_by_key.items():
            if aid != asset_id:
                continue
            try:
                cat_cmp = cat.replace(tzinfo=timezone.utc) if cat is not None and cat.tzinfo is None else cat
                delta = abs((cat_cmp - ts_cmp).total_seconds())
            except Exception:
                continue
            if delta <= 300 and (best_delta is None or delta < best_delta):
                best_delta = delta
                best = wcp
        return best

    _page_asset_ids = list({t.asset_id for t in trades if (t.side or "").upper() == "BUY"})
    _sell_totals: dict[str, float] = {}
    if _page_asset_ids:
        sell_totals_result = await session.execute(
            select(Trade.asset_id, func.sum(Trade.size))
            .where(
                Trade.wallet == wallet_address,
                Trade.side == "SELL",
                Trade.asset_id.in_(_page_asset_ids),
                Trade.size.is_not(None),
            )
            .group_by(Trade.asset_id)
        )
        _sell_totals = {r[0]: float(r[1] or 0.0) for r in sell_totals_result.all()}

    _older_buy_totals: dict[str, float] = {}
    if trades and _page_asset_ids:
        oldest_ts = min(t.ts for t in trades)
        older_buy_result = await session.execute(
            select(Trade.asset_id, func.sum(Trade.size))
            .where(
                Trade.wallet == wallet_address,
                Trade.side == "BUY",
                Trade.asset_id.in_(_page_asset_ids),
                Trade.size.is_not(None),
                Trade.ts < oldest_ts,
            )
            .group_by(Trade.asset_id)
        )
        _older_buy_totals = {r[0]: float(r[1] or 0.0) for r in older_buy_result.all()}

    _fifo_offset: dict[str, float] = {}

    def _fifo_result(asset_id: str, size, outcome_result) -> str:
        if size is None:
            return "OPEN" if outcome_result is None else ("WIN" if outcome_result == 1 else "LOSS")
        size_f = float(size)
        total_sells = _sell_totals.get(asset_id, 0.0)
        if total_sells <= 0:
            return "OPEN" if outcome_result is None else ("WIN" if outcome_result == 1 else "LOSS")
        consumed = _fifo_offset.get(asset_id, _older_buy_totals.get(asset_id, 0.0))
        new_consumed = consumed + size_f
        _fifo_offset[asset_id] = new_consumed
        if consumed >= total_sells:
            return "OPEN" if outcome_result is None else ("WIN" if outcome_result == 1 else "LOSS")
        return "CLOSED"

    def _enrich(t: Trade) -> dict:
        side = (t.side or "").upper()
        price = t.price
        size = t.size
        trade_value = (price * size) if price is not None and size is not None else None
        if side == "SELL":
            wcp = _find_wcp(t.asset_id, t.ts, t.size)
            missing_cost_basis = wcp is None
            cost_basis = wcp.cost_basis if wcp else None
            realized_pnl = wcp.realized_pnl if wcp else None
            roi_pct = (
                (wcp.exit_price - wcp.entry_price) / wcp.entry_price * 100
                if wcp and wcp.entry_price > 0 else None
            )
            result = "SOLD"
        else:
            missing_cost_basis = False
            cost_basis = trade_value
            realized_pnl = None
            roi_pct = None
            result = _fifo_result(t.asset_id, size, t.outcome_result)
        return {
            "ts": t.ts, "market_id": t.market_id, "outcome": t.outcome, "side": t.side,
            "price": price, "size": size, "cost_basis": cost_basis, "trade_value": trade_value,
            "realized_pnl": realized_pnl, "roi_pct": roi_pct, "result": result,
            "trade_type": t.trade_type, "outcome_result": t.outcome_result,
            "missing_cost_basis": missing_cost_basis,
        }

    trades_asc = list(reversed(trades))
    enriched = list(reversed([_enrich(t) for t in trades_asc]))
    has_missing = any(t["missing_cost_basis"] for t in enriched)
    return enriched, has_missing


def _make_pagination(
    page: int, total: int, page_size: int, full_url_fn, ajax_url_fn
) -> dict:
    total_pages = max(1, (total + page_size - 1) // page_size)
    offset = (page - 1) * page_size
    return {
        "page": page,
        "total_pages": total_pages,
        "total": total,
        "page_size": page_size,
        "offset": offset,
        "prev_url": full_url_fn(page - 1) if page > 1 else None,
        "next_url": full_url_fn(page + 1) if page < total_pages else None,
        "ajax_prev_url": ajax_url_fn(page - 1) if page > 1 else None,
        "ajax_next_url": ajax_url_fn(page + 1) if page < total_pages else None,
    }


@router.get("/wallets/{wallet_address}", response_class=HTMLResponse)
async def wallet_detail_page(
    wallet_address: str,
    request: Request,
    session: AsyncSession = Depends(db_session),
    trades_page: int = Query(1, ge=1),
    positions_page: int = Query(1, ge=1),
    score_history_page: int = Query(1, ge=1),
    alerts_page: int = Query(1, ge=1),
):
    wallet_result = await session.execute(select(Wallet).where(Wallet.wallet == wallet_address))
    wallet = wallet_result.scalar_one_or_none()

    # Full-page URL builder — preserves all table page states
    def _full_url(tp=None, pp=None, sp=None, ap=None) -> str:
        parts = []
        tp = tp if tp is not None else trades_page
        pp = pp if pp is not None else positions_page
        sp = sp if sp is not None else score_history_page
        ap = ap if ap is not None else alerts_page
        if tp > 1: parts.append(f"trades_page={tp}")
        if pp > 1: parts.append(f"positions_page={pp}")
        if sp > 1: parts.append(f"score_history_page={sp}")
        if ap > 1: parts.append(f"alerts_page={ap}")
        qs = "&".join(parts)
        return f"/wallets/{wallet_address}{'?' + qs if qs else ''}"

    # Trades
    trades_total = (await session.execute(
        select(func.count()).select_from(Trade).where(Trade.wallet == wallet_address)
    )).scalar_one()
    trades_offset = (trades_page - 1) * _WALLET_DETAIL_TRADES_PAGE_SIZE
    trades_result = await session.execute(
        select(Trade)
        .where(Trade.wallet == wallet_address)
        .order_by(Trade.ts.desc())
        .limit(_WALLET_DETAIL_TRADES_PAGE_SIZE)
        .offset(trades_offset)
    )
    raw_trades = trades_result.scalars().all()
    enriched_trades, trades_missing = await _build_enriched_trades(session, wallet_address, raw_trades)
    trades_pagination = _make_pagination(
        trades_page, trades_total, _WALLET_DETAIL_TRADES_PAGE_SIZE,
        lambda p: _full_url(tp=p),
        lambda p: f"/wallets/{wallet_address}/trades?page={p}",
    )

    # Positions
    positions_total = (await session.execute(
        select(func.count()).select_from(WalletPosition).where(WalletPosition.wallet == wallet_address)
    )).scalar_one()
    positions_offset = (positions_page - 1) * _WALLET_DETAIL_POSITIONS_PAGE_SIZE
    positions_result = await session.execute(
        select(WalletPosition)
        .where(WalletPosition.wallet == wallet_address)
        .order_by(WalletPosition.status.desc(), WalletPosition.opened_at.desc())
        .limit(_WALLET_DETAIL_POSITIONS_PAGE_SIZE)
        .offset(positions_offset)
    )
    wallet_positions = positions_result.scalars().all()
    positions_missing = any(p.avg_entry_price == 0.0 for p in wallet_positions)
    positions_estimated = any(p.estimated_entry for p in wallet_positions)
    positions_pagination = _make_pagination(
        positions_page, positions_total, _WALLET_DETAIL_POSITIONS_PAGE_SIZE,
        lambda p: _full_url(pp=p),
        lambda p: f"/wallets/{wallet_address}/positions?page={p}",
    )

    # Score history — 7 most recent rows
    score_history_result = await session.execute(
        select(WalletScoreHistory)
        .where(WalletScoreHistory.wallet == wallet_address)
        .order_by(WalletScoreHistory.snapshot_ts.desc())
        .limit(7)
    )
    score_history = score_history_result.scalars().all()
    score_history_total = len(score_history)
    score_history_pagination = _make_pagination(
        score_history_page, score_history_total, _WALLET_DETAIL_SCORE_HISTORY_PAGE_SIZE,
        lambda p: _full_url(sp=p),
        lambda p: f"/wallets/{wallet_address}/score-history?page={p}",
    )

    # Alerts (paginated)
    alerts_total = (await session.execute(
        select(func.count()).select_from(Alert).where(Alert.wallet == wallet_address)
    )).scalar_one()
    alerts_offset = (alerts_page - 1) * _WALLET_DETAIL_ALERTS_PAGE_SIZE
    alerts_result = await session.execute(
        select(Alert)
        .where(Alert.wallet == wallet_address)
        .order_by(Alert.created_at.desc())
        .limit(_WALLET_DETAIL_ALERTS_PAGE_SIZE)
        .offset(alerts_offset)
    )
    alerts = alerts_result.scalars().all()
    alerts_pagination = _make_pagination(
        alerts_page, alerts_total, _WALLET_DETAIL_ALERTS_PAGE_SIZE,
        lambda p: _full_url(ap=p),
        lambda p: f"/wallets/{wallet_address}/alerts?page={p}",
    )

    distinct_markets = (await session.execute(
        select(func.count(distinct(Trade.market_id))).where(Trade.wallet == wallet_address)
    )).scalar_one()

    # Closed-position provenance breakdown — see WalletClosedPosition.closed_at_source.
    # Surfaces how many "Closed Positions" are organic sells vs. resolution-driven
    # closes, since trade rows behind older closes may have been purged by
    # retention (app/services/maintenance/retention.py) even when the close itself
    # is fully legitimate.
    _source_expr = func.coalesce(WalletClosedPosition.closed_at_source, "unlabeled")
    provenance_rows = (await session.execute(
        select(_source_expr, func.count())
        .where(WalletClosedPosition.wallet == wallet_address)
        .group_by(_source_expr)
        .order_by(func.count().desc())
    )).all()
    closed_position_provenance = [(source, count) for source, count in provenance_rows]

    # ROI = realized_pnl / total_cost_basis × 100
    total_cost_basis_val = (await session.execute(
        select(func.sum(WalletClosedPosition.cost_basis))
        .where(WalletClosedPosition.wallet == wallet_address)
    )).scalar_one_or_none()
    total_cost_basis_f = float(total_cost_basis_val or 0.0)
    roi: float | None = None
    if total_cost_basis_f > 0 and wallet and wallet.realized_pnl is not None:
        roi = wallet.realized_pnl / total_cost_basis_f * 100

    has_missing_cost_basis = trades_missing or positions_missing
    has_estimated_entry = positions_estimated
    if wallet:
        await _attach_weighted_log_roi([wallet], session)
    badges = _wallet_badges(wallet) if wallet else []

    return templates.TemplateResponse(
        request, "wallet_detail.html",
        {
            "wallet": wallet,
            "wallet_address": wallet_address,
            "trades": enriched_trades,
            "wallet_positions": wallet_positions,
            "has_missing_cost_basis": has_missing_cost_basis,
            "has_estimated_entry": has_estimated_entry,
            "positions_missing": positions_missing,
            "alerts": alerts,
            "score_history": score_history,
            "distinct_markets": distinct_markets,
            "closed_position_provenance": closed_position_provenance,
            "roi": roi,
            "valid_tags": sorted(VALID_TAGS),
            "valid_watch_statuses": sorted(VALID_WATCH_STATUSES),
            "badges": badges,
            "trades_pagination": trades_pagination,
            "positions_pagination": positions_pagination,
            "score_history_pagination": score_history_pagination,
            "alerts_pagination": alerts_pagination,
        },
    )


# ── Fragment routes (AJAX pagination) ─────────────────────────────────────────

@router.get("/wallets/{wallet_address}/score-history", response_class=HTMLResponse)
async def wallet_score_history_fragment(
    wallet_address: str,
    request: Request,
    session: AsyncSession = Depends(db_session),
    page: int = Query(1, ge=1),
):
    result = await session.execute(
        select(WalletScoreHistory)
        .where(WalletScoreHistory.wallet == wallet_address)
        .order_by(WalletScoreHistory.snapshot_ts.desc())
        .limit(7)
    )
    score_history = result.scalars().all()
    total = len(score_history)
    pagination = _make_pagination(
        page, total, _WALLET_DETAIL_SCORE_HISTORY_PAGE_SIZE,
        lambda p: f"/wallets/{wallet_address}?score_history_page={p}",
        lambda p: f"/wallets/{wallet_address}/score-history?page={p}",
    )
    return templates.TemplateResponse(
        request, "frag_score_history.html",
        {"score_history": score_history, "pagination": pagination},
    )


@router.get("/wallets/{wallet_address}/positions", response_class=HTMLResponse)
async def wallet_positions_fragment(
    wallet_address: str,
    request: Request,
    session: AsyncSession = Depends(db_session),
    page: int = Query(1, ge=1),
):
    total = (await session.execute(
        select(func.count()).select_from(WalletPosition).where(WalletPosition.wallet == wallet_address)
    )).scalar_one()
    offset = (page - 1) * _WALLET_DETAIL_POSITIONS_PAGE_SIZE
    result = await session.execute(
        select(WalletPosition)
        .where(WalletPosition.wallet == wallet_address)
        .order_by(WalletPosition.status.desc(), WalletPosition.opened_at.desc())
        .limit(_WALLET_DETAIL_POSITIONS_PAGE_SIZE)
        .offset(offset)
    )
    wallet_positions = result.scalars().all()
    positions_missing = any(p.avg_entry_price == 0.0 for p in wallet_positions)
    positions_estimated = any(p.estimated_entry for p in wallet_positions)
    pagination = _make_pagination(
        page, total, _WALLET_DETAIL_POSITIONS_PAGE_SIZE,
        lambda p: f"/wallets/{wallet_address}?positions_page={p}",
        lambda p: f"/wallets/{wallet_address}/positions?page={p}",
    )
    return templates.TemplateResponse(
        request, "frag_positions.html",
        {
            "wallet_positions": wallet_positions,
            "positions_missing": positions_missing,
            "has_estimated_entry": positions_estimated,
            "pagination": pagination,
        },
    )


@router.get("/wallets/{wallet_address}/trades", response_class=HTMLResponse)
async def wallet_trades_fragment(
    wallet_address: str,
    request: Request,
    session: AsyncSession = Depends(db_session),
    page: int = Query(1, ge=1),
):
    total = (await session.execute(
        select(func.count()).select_from(Trade).where(Trade.wallet == wallet_address)
    )).scalar_one()
    offset = (page - 1) * _WALLET_DETAIL_TRADES_PAGE_SIZE
    result = await session.execute(
        select(Trade)
        .where(Trade.wallet == wallet_address)
        .order_by(Trade.ts.desc())
        .limit(_WALLET_DETAIL_TRADES_PAGE_SIZE)
        .offset(offset)
    )
    raw_trades = result.scalars().all()
    enriched_trades, has_missing_cost_basis = await _build_enriched_trades(
        session, wallet_address, raw_trades
    )
    pagination = _make_pagination(
        page, total, _WALLET_DETAIL_TRADES_PAGE_SIZE,
        lambda p: f"/wallets/{wallet_address}?trades_page={p}",
        lambda p: f"/wallets/{wallet_address}/trades?page={p}",
    )
    return templates.TemplateResponse(
        request, "frag_trades.html",
        {
            "trades": enriched_trades,
            "has_missing_cost_basis": has_missing_cost_basis,
            "pagination": pagination,
        },
    )


@router.get("/wallets/{wallet_address}/alerts", response_class=HTMLResponse)
async def wallet_alerts_fragment(
    wallet_address: str,
    request: Request,
    session: AsyncSession = Depends(db_session),
    page: int = Query(1, ge=1),
):
    total = (await session.execute(
        select(func.count()).select_from(Alert).where(Alert.wallet == wallet_address)
    )).scalar_one()
    offset = (page - 1) * _WALLET_DETAIL_ALERTS_PAGE_SIZE
    result = await session.execute(
        select(Alert)
        .where(Alert.wallet == wallet_address)
        .order_by(Alert.created_at.desc())
        .limit(_WALLET_DETAIL_ALERTS_PAGE_SIZE)
        .offset(offset)
    )
    alerts = result.scalars().all()
    pagination = _make_pagination(
        page, total, _WALLET_DETAIL_ALERTS_PAGE_SIZE,
        lambda p: f"/wallets/{wallet_address}?alerts_page={p}",
        lambda p: f"/wallets/{wallet_address}/alerts?page={p}",
    )
    return templates.TemplateResponse(
        request, "frag_alerts.html",
        {"alerts": alerts, "pagination": pagination},
    )


@router.post("/wallets/{wallet_address}/set-watch", response_class=HTMLResponse)
async def wallet_set_watch(
    wallet_address: str,
    watch_status: str = Form(...),
    session: AsyncSession = Depends(db_session),
):
    result = await session.execute(select(Wallet).where(Wallet.wallet == wallet_address))
    wallet = result.scalar_one_or_none()
    if wallet and watch_status in VALID_WATCH_STATUSES:
        wallet.watch_status = watch_status
        await session.commit()
        logger.info("wallet_watch_set_via_ui", wallet=wallet_address, status=watch_status)
    elif wallet and watch_status == "none":
        wallet.watch_status = None
        await session.commit()
    return RedirectResponse(f"/wallets/{wallet_address}", status_code=303)


@router.post("/wallets/{wallet_address}/set-tag", response_class=HTMLResponse)
async def wallet_set_tag(
    wallet_address: str,
    manual_tag: str = Form(""),
    notes: str = Form(""),
    session: AsyncSession = Depends(db_session),
):
    result = await session.execute(select(Wallet).where(Wallet.wallet == wallet_address))
    wallet = result.scalar_one_or_none()
    if wallet:
        wallet.manual_tag = manual_tag if manual_tag and manual_tag in VALID_TAGS else None
        wallet.notes = notes or None
        await session.commit()
        logger.info("wallet_tag_set_via_ui", wallet=wallet_address, tag=wallet.manual_tag)
    return RedirectResponse(f"/wallets/{wallet_address}", status_code=303)


@router.post("/wallets/{wallet_address}/set-badges", response_class=HTMLResponse)
async def wallet_set_badges(
    wallet_address: str,
    force_sybil: str = Form(""),
    force_informed: str = Form(""),
    force_farmer: str = Form(""),
    session: AsyncSession = Depends(db_session),
):
    result = await session.execute(select(Wallet).where(Wallet.wallet == wallet_address))
    wallet = result.scalar_one_or_none()
    if wallet:
        if force_sybil:
            wallet.suspected_sybil = force_sybil == "true"
        # "" → reset to system (None); "true"/"false" → override
        wallet.manual_informed = True if force_informed == "true" else (False if force_informed == "false" else None)
        wallet.manual_farmer = True if force_farmer == "true" else (False if force_farmer == "false" else None)
        await session.commit()
        logger.info(
            "wallet_badges_override_via_ui",
            wallet=wallet_address,
            sybil=force_sybil,
            informed=force_informed,
            farmer=force_farmer,
        )
    return RedirectResponse(f"/wallets/{wallet_address}", status_code=303)


# ──────────────────────────────────────────────────────────────────────────────
# Analytics — Favorite-Longshot Bias
# ──────────────────────────────────────────────────────────────────────────────

_PRICE_BUCKETS = [
    ("80-84%", 0.80, 0.8499),
    ("85-89%", 0.85, 0.8999),
    ("90-94%", 0.90, 0.9499),
    ("95%+",   0.95, 1.0001),
]
_MIN_SAMPLE_SIZE = 5


def _assign_price_bucket(price: float) -> str | None:
    for label, lo, hi in _PRICE_BUCKETS:
        if lo <= price <= hi:
            return label
    return None


@router.get("/analytics", response_class=HTMLResponse)
async def analytics_page(
    request: Request,
    session: AsyncSession = Depends(db_session),
    category: str | None = Query(None),
):
    from collections import defaultdict

    selected_category = category if category and category != "all" else None

    # Distinct categories that have resolved snapshots (for dropdown)
    cat_stmt = (
        select(Market.category)
        .join(MarketPriceSnapshot, MarketPriceSnapshot.market_id == Market.market_id)
        .where(
            MarketPriceSnapshot.resolved == True,  # noqa: E712
            Market.category.isnot(None),
        )
        .distinct()
        .order_by(Market.category)
    )
    all_categories = (await session.execute(cat_stmt)).scalars().all()

    # Load resolved snapshots, optionally filtered by category
    snap_stmt = (
        select(MarketPriceSnapshot, Market.category.label("market_category"))
        .join(Market, MarketPriceSnapshot.market_id == Market.market_id)
        .where(MarketPriceSnapshot.resolved == True)  # noqa: E712
        .order_by(MarketPriceSnapshot.snapshot_ts.asc())
    )
    if selected_category:
        snap_stmt = snap_stmt.where(Market.category == selected_category)

    snap_rows = (await session.execute(snap_stmt)).all()
    snapshots_with_cat = [(row[0], row[1]) for row in snap_rows]

    earliest_ts: datetime | None = snapshots_with_cat[0][0].snapshot_ts if snapshots_with_cat else None
    total_snapshots = len(snapshots_with_cat)

    # Group by (category, price_bucket, snapshot_bucket) when showing all;
    # by (price_bucket, snapshot_bucket) when filtered to one category.
    if selected_category:
        groups: dict = defaultdict(list)
        for snap, _ in snapshots_with_cat:
            bucket = _assign_price_bucket(snap.high_side_price)
            if bucket is None:
                continue
            groups[(bucket, snap.snapshot_bucket)].append(snap)

        # Always emit all 4 buckets × both windows so the table is never empty.
        rows = []
        for label, _, _ in _PRICE_BUCKETS:
            for win in ("T24", "T48"):
                items = groups.get((label, win), [])
                sample_size = len(items)
                if sample_size == 0:
                    rows.append({
                        "category": selected_category,
                        "price_bucket": label,
                        "snapshot_window": win,
                        "sample_size": 0,
                        "implied_win_rate": None,
                        "actual_win_rate": None,
                        "edge": None,
                        "avg_roi": None,
                        "net_roi": None,
                    })
                else:
                    implied_win_rate = sum(s.high_side_price for s in items) / sample_size
                    wins_list = [s for s in items if s.high_side_won is True]
                    actual_win_rate = len(wins_list) / sample_size
                    edge = actual_win_rate - implied_win_rate
                    avg_roi = (
                        sum((1.0 - s.high_side_price) / s.high_side_price * 100 for s in wins_list) / len(wins_list)
                        if wins_list else 0.0
                    )
                    rows.append({
                        "category": selected_category,
                        "price_bucket": label,
                        "snapshot_window": win,
                        "sample_size": sample_size,
                        "implied_win_rate": implied_win_rate,
                        "actual_win_rate": actual_win_rate,
                        "edge": edge,
                        "avg_roi": avg_roi,
                        "net_roi": avg_roi * 0.98,
                    })
        # Order matches _PRICE_BUCKETS enumeration (already sorted above)
    else:
        all_groups: dict = defaultdict(list)
        for snap, cat in snapshots_with_cat:
            bucket = _assign_price_bucket(snap.high_side_price)
            if bucket is None:
                continue
            all_groups[(cat or "Unknown", bucket, snap.snapshot_bucket)].append(snap)

        rows = []
        for (cat_label, price_bucket, snap_bucket), items in all_groups.items():
            if len(items) < _MIN_SAMPLE_SIZE:
                continue
            sample_size = len(items)
            implied_win_rate = sum(s.high_side_price for s in items) / sample_size
            wins_list = [s for s in items if s.high_side_won is True]
            actual_win_rate = len(wins_list) / sample_size
            edge = actual_win_rate - implied_win_rate
            avg_roi = (
                sum((1.0 - s.high_side_price) / s.high_side_price * 100 for s in wins_list) / len(wins_list)
                if wins_list else 0.0
            )
            rows.append({
                "category": cat_label,
                "price_bucket": price_bucket,
                "snapshot_window": snap_bucket,
                "sample_size": sample_size,
                "implied_win_rate": implied_win_rate,
                "actual_win_rate": actual_win_rate,
                "edge": edge,
                "avg_roi": avg_roi,
                "net_roi": avg_roi * 0.98,
            })

        bucket_order = {label: i for i, (label, _, _) in enumerate(_PRICE_BUCKETS)}
        rows.sort(key=lambda r: (r["category"], bucket_order.get(r["price_bucket"], 99), r["snapshot_window"]))

    return templates.TemplateResponse(
        request, "analytics.html",
        {
            "rows": rows,
            "earliest_ts": earliest_ts,
            "min_sample_size": _MIN_SAMPLE_SIZE,
            "total_snapshots": total_snapshots,
            "all_categories": list(all_categories),
            "selected_category": selected_category or "all",
        },
    )
