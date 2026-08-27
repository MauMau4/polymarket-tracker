"""
Poll the CLOB Data API for recent trades by watched wallets and insert any
that were missed during live WebSocket ingestion.

Usage:
    python -m app.tasks.poll_watched_wallets
    python -m app.tasks.poll_watched_wallets --days 30
    python -m app.tasks.poll_watched_wallets --days 1 --wallet 0xabc...

Safe to run multiple times — deduplicates by (tx_hash, asset_id).
Triggers one full-universe wallet-score recompute at the end of the run if
any wallet had a new closed position (SELL), not per-wallet.

API endpoint: GET https://data-api.polymarket.com/trades?user={wallet}&limit=500
Pagination: offset+limit, newest-first, no cursor/next field (verified against
live responses — see decisions/2026-08-06.md). Offset pagination against a
live wallet can re-deliver rows already fetched earlier in the same run (new
trades landing between requests push older rows down by one page), so
duplicate delivery within a single run is expected, not just possible — the
drift direction only ever repeats rows, never skips one. Dedup key:
trades.tx_hash + trades.asset_id, checked against both a pre-loaded DB set
and an in-run seen-set updated as trades are inserted (the DB set alone can't
catch a duplicate that arrives twice before either copy is committed).

Operator-experience/robustness additions (punch list item 16, decisions/
2026-08-XX.md): a progress counter (`[N of M]`) covering every wallet
including skipped ones; `--wallet` to poll an explicit subset (bypasses
--max-trades-per-day — see `run()`'s docstring for the reasoning); 429
retry-with-backoff instead of silent truncation; a loud `TRUNCATED` marker
plus an end-of-run per-wallet summary table for any wallet whose pagination
ended early; and a full-output log file per run (`logs/poll_YYYYMMDD_HHMMSS.log`).
None of this changes what gets ingested or how it is deduped.
"""
import argparse
import asyncio
import selectors
import sys
import os
from collections import namedtuple
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.market import Market
from app.db.models.token import Token
from app.db.models.trade import Trade, AttributionStatus
from app.db.models.wallet import Wallet
from app.db.models.wallet_position import WalletPosition
from app.db.session import get_session_factory
from app.logging import get_logger
from app.services.wallets.exit_skill import (
    update_buy_position,
    write_sell_closed_position,
)
from app.services.wallets.positions import (
    update_position_on_buy,
    update_position_on_sell,
)
from app.services.wallets.profiler import update_wallet_activity
from app.services.wallets.scorer import run_all_wallet_scores

logger = get_logger(__name__)

# Row shape matching `select(Wallet.wallet, Wallet.watch_status)`'s result,
# used for --wallet's explicit list too (which may include addresses with no
# `wallets` row at all — watch_status "unwatched" in that case) so run()'s
# loop doesn't need to know which source `watched` came from.
_WalletRow = namedtuple("_WalletRow", ["wallet", "watch_status"])

_DATA_API_BASE = "https://data-api.polymarket.com"

# Commit every N trades rather than every one. Cost-basis/position mutations
# only need same-session autoflush (not a commit) to stay consistent across
# trades — see decisions/2026-08-06.md for why 100 was chosen: it bounds a
# mid-batch failure's rollback to <=100 trades (cheap and safe to redo — the
# next run's dedup skips whatever already committed) while cutting a busy
# 30-day wallet from thousands of commits to tens.
_BATCH_SIZE = 100

# Safety cap on pagination depth (100k trades/wallet) so a bug in the cutoff
# logic can't spin forever against a live API.
_MAX_PAGES = 200

# 429 retry/backoff (item 16, decisions/2026-08-XX.md): a wallet hit a 429
# mid-pagination on the 2026-08-11 backfill and the run kept whatever pages
# it had, silently — the wallet's history for the rest of that run was
# truncated with no signal it had happened. Rate limits are windowed, so a
# 429 is very likely to clear within tens of seconds rather than persist.
# 5 attempts with a doubling 2s/4s/8s/16s/32s backoff (~62s worst-case total
# wait for one page) gives a real window time to reset without stalling a
# wallet indefinitely on a long run. A genuine error (network failure, or
# any non-429 status) is not retried — it still breaks immediately, as
# before this change, and is reported as a truncation like any other early
# stop. Whether the API sends `Retry-After` on a 429 could not be confirmed
# either way this session — a 40-request concurrent burst against the live
# API never triggered one — so the header is honored when present and the
# computed backoff used otherwise, rather than assuming either way.
_RATE_LIMIT_MAX_RETRIES = 5
_RATE_LIMIT_BASE_DELAY_SECONDS = 2.0


@dataclass
class FetchResult:
    """Result of paginating one wallet's trade history.

    `truncated=True` means pagination ended for a reason other than
    "history exhausted" (short page) or "reached the lookback cutoff" — the
    fetched trades are real but incomplete. `truncation_reason` is one of
    "rate_limited" (429 retries exhausted), "http_error" (non-429 failure),
    or "max_pages" (hit the _MAX_PAGES safety cap).
    """
    trades: list[dict]
    truncated: bool = False
    truncation_reason: str | None = None


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    """Parse a Retry-After header (seconds, or an HTTP-date) if present."""
    raw = resp.headers.get("Retry-After")
    if raw is None:
        return None
    raw = raw.strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        when = parsedate_to_datetime(raw)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0.0, (when - datetime.now(tz=timezone.utc)).total_seconds())
    except Exception:
        return None


async def _get_page_with_retry(
    client: httpx.AsyncClient, url: str, params: dict, wallet: str, offset: int
) -> tuple[httpx.Response | None, str | None]:
    """
    Fetch one page, retrying on 429 with exponential backoff (Retry-After
    honored when present). A non-429 error is not retried — it reports
    'http_error' immediately, same behavior as before this change. Returns
    (response, truncation_reason); reason is None on success.
    """
    delay = _RATE_LIMIT_BASE_DELAY_SECONDS
    for attempt in range(_RATE_LIMIT_MAX_RETRIES + 1):
        try:
            resp = await client.get(url, params=params)
        except Exception as exc:
            logger.error(
                "poll_fetch_error", wallet=wallet[:12], offset=offset, error=str(exc)
            )
            print(f"  ERROR fetching trades from Data API (offset={offset}): {exc}")
            return None, "http_error"

        if resp.status_code == 429:
            if attempt >= _RATE_LIMIT_MAX_RETRIES:
                print(
                    f"  RATE LIMITED (429) at offset={offset}, giving up after "
                    f"{_RATE_LIMIT_MAX_RETRIES} retries"
                )
                logger.warning(
                    "poll_rate_limit_exhausted", wallet=wallet[:12], offset=offset
                )
                return None, "rate_limited"
            wait = _retry_after_seconds(resp)
            if wait is None:
                wait = delay
            print(
                f"  Rate limited (429) at offset={offset}, retrying in {wait:.0f}s "
                f"(attempt {attempt + 1}/{_RATE_LIMIT_MAX_RETRIES})"
            )
            logger.warning(
                "poll_rate_limited_retry", wallet=wallet[:12], offset=offset,
                attempt=attempt + 1, wait_seconds=wait,
            )
            await asyncio.sleep(wait)
            delay *= 2
            continue

        try:
            resp.raise_for_status()
        except Exception as exc:
            logger.error(
                "poll_fetch_error", wallet=wallet[:12], offset=offset, error=str(exc)
            )
            print(f"  ERROR fetching trades from Data API (offset={offset}): {exc}")
            return None, "http_error"

        return resp, None

    return None, "rate_limited"  # unreachable safety net


# ---------------------------------------------------------------------------
# Data API fetch
# ---------------------------------------------------------------------------

async def _fetch_wallet_trades(
    wallet: str, cutoff_ts: datetime, limit: int = 500
) -> FetchResult:
    """
    Fetch all trades for a wallet back to cutoff_ts, paginating via `offset`.

    The Data API returns trades newest-first with no cursor/next field —
    offset+limit is the only pagination mechanism (verified against live
    responses). Stops when a page is short (history exhausted) or the oldest
    row on the page already predates cutoff_ts. A page straddling the cutoff
    is still fetched in full; trimming to the exact window happens in
    _filter_and_sort_window, so no in-window row on that page is lost.

    Any other stop (429 retries exhausted, a genuine HTTP error, or hitting
    the _MAX_PAGES safety cap) is reported via FetchResult.truncated — the
    trades already fetched are kept (nothing is discarded), but the wallet's
    history for this run is incomplete and the caller must say so loudly.
    """
    url = f"{_DATA_API_BASE}/trades"
    all_trades: list[dict] = []
    offset = 0
    async with httpx.AsyncClient(timeout=20.0, verify=False) as client:
        for _ in range(_MAX_PAGES):
            params = {"user": wallet, "limit": limit, "offset": offset}
            resp, reason = await _get_page_with_retry(client, url, params, wallet, offset)
            if resp is None:
                return FetchResult(all_trades, truncated=True, truncation_reason=reason)

            data = resp.json()
            page = data if isinstance(data, list) else data.get("data", [])

            if not page:
                return FetchResult(all_trades, truncated=False)

            all_trades.extend(page)

            if len(page) < limit:
                return FetchResult(all_trades, truncated=False)
            if _parse_ts(page[-1].get("timestamp")) < cutoff_ts:
                return FetchResult(all_trades, truncated=False)

            offset += limit
        else:
            logger.warning("poll_fetch_max_pages_hit", wallet=wallet[:12], max_pages=_MAX_PAGES)
            print(f"  TRUNCATED: hit the {_MAX_PAGES}-page safety cap before exhausting history")
            return FetchResult(all_trades, truncated=True, truncation_reason="max_pages")

    return FetchResult(all_trades, truncated=False)


def _filter_and_sort_window(raw_trades: list[dict], cutoff_ts: datetime) -> list[dict]:
    """Trim to the lookback window and sort oldest-first so BUYs are always
    processed before their matching SELLs."""
    return sorted(
        (t for t in raw_trades if _parse_ts(t.get("timestamp")) >= cutoff_ts),
        key=lambda t: _parse_ts(t.get("timestamp")),
    )


async def _estimate_trades_per_day(wallet: str, limit: int = 500) -> float | None:
    """
    Cheap one-page density probe, used only to decide whether
    --max-trades-per-day should skip this wallet before paying for full
    pagination + per-trade ingestion. Returns None when there isn't enough
    data in a single page to estimate a rate (fewer than 2 trades, or a page
    that spans zero time — e.g. every trade at the same timestamp).
    """
    url = f"{_DATA_API_BASE}/trades"
    params = {"user": wallet, "limit": limit}
    try:
        async with httpx.AsyncClient(timeout=20.0, verify=False) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
            page = data if isinstance(data, list) else data.get("data", [])
    except Exception as exc:
        logger.warning("poll_rate_probe_error", wallet=wallet[:12], error=str(exc))
        return None

    if len(page) < 2:
        return None

    newest = _parse_ts(page[0].get("timestamp"))
    oldest = _parse_ts(page[-1].get("timestamp"))
    span_days = (newest - oldest).total_seconds() / 86400.0
    if span_days <= 0:
        return None
    return len(page) / span_days


# ---------------------------------------------------------------------------
# Market / token resolution
# ---------------------------------------------------------------------------

async def _resolve_market_and_outcome(
    session: AsyncSession,
    asset_id: str,
    condition_id: str,
    api_outcome: str | None,
) -> tuple[str | None, str | None]:
    """
    Return (market_id, outcome) for the given asset.

    Lookup order:
      1. tokens table by asset_id  (primary — fastest)
      2. markets table by condition_id  (fallback — market exists, token missing)
      3. Gamma API fetch via condition_id lookup  (last resort)

    Returns (None, None) if the market cannot be resolved.
    """
    # 1. Token lookup
    tok_result = await session.execute(
        select(Token.market_id, Token.outcome).where(Token.asset_id == asset_id)
    )
    tok = tok_result.one_or_none()
    if tok:
        return tok.market_id, tok.outcome or api_outcome

    # 2. Market lookup by condition_id
    if condition_id:
        mkt_result = await session.execute(
            select(Market.market_id).where(Market.condition_id == condition_id)
        )
        mkt = mkt_result.scalar_one_or_none()
        if mkt:
            # Market exists but token row is missing — insert token and return
            session.add(Token(
                asset_id=asset_id,
                market_id=mkt,
                outcome=api_outcome,
                token_id=asset_id,
            ))
            return mkt, api_outcome

    # 3. CLOB API by condition_id → market_slug → Gamma by slug.
    #    The old Gamma query-param filters (?condition_id=, ?clob_token_ids=)
    #    are silently ignored by Gamma and return unrelated default listings —
    #    the previous fallback here was upserting random junk markets.
    if condition_id:
        try:
            from app.config import get_settings
            from app.services.polymarket.gamma_client import fetch_market_by_slug
            from app.services.discovery.refresh import _upsert_market, _upsert_token

            settings = get_settings()
            slug: str | None = None
            clob_url = f"{settings.polymarket_clob_base_url}/markets/{condition_id}"
            async with httpx.AsyncClient(timeout=15.0, verify=settings.gamma_ssl_verify) as client:
                resp = await client.get(clob_url)
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, dict):
                        slug = data.get("market_slug")

            if slug:
                info = await fetch_market_by_slug(slug)
                if info:
                    await _upsert_market(session, info)
                    for tok_info in (info.tokens or []):
                        await _upsert_token(session, info.market_id, tok_info.token_id, tok_info.outcome)
                    # Only return if the asset is confirmed in this market
                    tok_result2 = await session.execute(
                        select(Token.market_id, Token.outcome).where(Token.asset_id == asset_id)
                    )
                    tok2 = tok_result2.one_or_none()
                    if tok2:
                        return tok2.market_id, tok2.outcome or api_outcome
                    logger.warning(
                        "poll_asset_not_in_market_tokens",
                        asset_id=asset_id[:20],
                        market_id=info.market_id,
                        slug=slug,
                    )
        except Exception as exc:
            logger.warning("poll_clob_slug_fallback_error", condition_id=condition_id[:20], error=str(exc))

    return None, None


# ---------------------------------------------------------------------------
# Dedup check
# ---------------------------------------------------------------------------

async def _load_captured_keys(session: AsyncSession, wallet: str) -> set[tuple[str, str]]:
    """
    Batch-load every (tx_hash, asset_id) pair already in `trades` for this
    wallet, replacing a per-trade dedup SELECT with one query.

    Deliberately NOT scoped by the poll's lookback window: the original
    per-trade check (`_trade_already_captured`) matched on existence
    anywhere in the table, and a stored trade's `ts` isn't guaranteed to
    agree with the cutoff to the second (it may have been captured by a
    different path — e.g. the live WS ingestor — with its own timestamp
    source). Scoping by ts risked silently missing a genuinely-captured row
    whose stored ts falls just outside the window, causing a duplicate
    insert attempt. Wallet-only costs one extra index scan on
    ix_trades_wallet_ts and is bounded by one wallet's total history.
    """
    result = await session.execute(
        select(Trade.tx_hash, Trade.asset_id).where(Trade.wallet == wallet)
    )
    return {(row.tx_hash, row.asset_id) for row in result.all()}


# ---------------------------------------------------------------------------
# Single-trade ingestion
# ---------------------------------------------------------------------------

async def _ingest_trade(session: AsyncSession, raw: dict) -> dict | None:
    """
    Ingest one trade from the Data API.

    Returns a summary dict on success, None if skipped.
    """
    tx_hash = raw.get("transactionHash", "")
    asset_id = raw.get("asset", "")
    condition_id = raw.get("conditionId", "")
    side = (raw.get("side") or "").upper()
    price_raw = raw.get("price")
    size_raw = raw.get("size")
    timestamp_raw = raw.get("timestamp")
    api_outcome = raw.get("outcome")
    wallet = raw.get("proxyWallet", "")

    if not tx_hash or not asset_id or not wallet:
        return None

    try:
        price = float(price_raw) if price_raw is not None else None
        size = float(size_raw) if size_raw is not None else None
    except (ValueError, TypeError):
        return None

    if price is None or size is None or side not in ("BUY", "SELL"):
        return None

    # Parse timestamp
    try:
        ts_val = float(timestamp_raw)
        if ts_val > 1e10:
            ts_val /= 1000.0
        ts = datetime.fromtimestamp(ts_val, tz=timezone.utc)
    except (ValueError, TypeError, OSError):
        ts = datetime.now(tz=timezone.utc)

    # Resolve market
    market_id, outcome = await _resolve_market_and_outcome(
        session, asset_id, condition_id, api_outcome
    )
    if not market_id:
        logger.warning(
            "poll_market_not_found",
            asset_id=asset_id[:20],
            condition_id=condition_id[:20] if condition_id else "",
        )
        return None

    notional_usd = round(price * size, 2)

    # Insert Trade row
    trade = Trade(
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
        attribution_status=AttributionStatus.attributed,
        source="clob_poll",
        external_trade_id=f"clob_poll-{tx_hash}",
        raw_payload=raw,
    )
    session.add(trade)

    # Cost basis and position tracking
    if side == "BUY":
        await update_buy_position(session, wallet, asset_id, market_id, price, size)
        await update_position_on_buy(session, wallet, asset_id, market_id, price, size, ts, outcome)
    elif side == "SELL":
        sell_pnl = await write_sell_closed_position(
            session, wallet, asset_id, market_id, price, size, ts
        )
        await update_position_on_sell(
            session, wallet, asset_id, size, sell_pnl, ts,
            market_id=market_id, price=price,
        )

    return {
        "side": side,
        "outcome": outcome or asset_id[:16],
        "price": price,
        "notional_usd": notional_usd,
        "ts": ts,
    }


# ---------------------------------------------------------------------------
# Per-wallet poll
# ---------------------------------------------------------------------------

async def poll_wallet(wallet_addr: str, watch_status: str, cutoff_ts: datetime) -> dict:
    """
    Poll one watched wallet. Returns a summary dict.

    One session for the whole wallet, committed every _BATCH_SIZE trades.
    A failure (ingest error or commit error) rolls back the current
    uncommitted batch and aborts the REST of this wallet's trades for this
    run — it does not retry within the run and does not touch other
    wallets, each of which gets its own fresh session. Trades from
    already-committed batches stay; whatever didn't commit is picked up
    (idempotently, via the dedup check) on the next run. This mirrors the
    decisions/2026-07-19.md genre_discovery incident, where a caught
    exception without a rollback left the session in pending-rollback and
    silently failed every subsequent item — here every failure path rolls
    back before doing anything else.
    """
    fetch_result = await _fetch_wallet_trades(wallet_addr, cutoff_ts)
    raw_trades = fetch_result.trades
    print(f"  Found {len(raw_trades)} trades from Data API")
    if fetch_result.truncated:
        print(
            f"  TRUNCATED: pagination stopped early ({fetch_result.truncation_reason}) — "
            f"this wallet's history for this run is incomplete. Safe to re-run: "
            f"dedup skips whatever already landed and continues past it."
        )
        logger.warning(
            "poll_wallet_truncated", wallet=wallet_addr[:12],
            reason=fetch_result.truncation_reason, trades_fetched=len(raw_trades),
        )

    recent = _filter_and_sort_window(raw_trades, cutoff_ts)
    print(f"  Within lookback window: {len(recent)}")

    empty_summary = {
        "found": len(raw_trades), "in_window": len(recent), "skipped": 0,
        "inserted": 0, "new_sells": 0, "details": [], "aborted": False,
        "truncated": fetch_result.truncated,
        "truncation_reason": fetch_result.truncation_reason,
        "earliest_ts": None, "latest_ts": None,
    }
    if not recent:
        return empty_summary

    factory = get_session_factory()
    skipped = 0
    inserted = 0
    new_sells = 0
    insert_details: list[dict] = []

    # Two-part dedup: a pre-loaded DB set (one query, not one per trade) plus
    # an in-run set updated as trades are inserted. Offset pagination against
    # a live wallet can re-deliver the same fill twice within one fetch (new
    # trades landing between page requests shift older rows into view again)
    # — the DB set alone can't catch that because neither copy is committed
    # yet when the set is built.
    seen_this_run: set[tuple[str, str]] = set()
    batch_details: list[dict] = []
    batch_new_sells = 0
    aborted = False

    async with factory() as session:
        already_captured = await _load_captured_keys(session, wallet_addr)

        for raw in recent:
            tx_hash = raw.get("transactionHash", "")
            asset_id = raw.get("asset", "")
            key = (tx_hash, asset_id)

            if key in already_captured or key in seen_this_run:
                skipped += 1
                continue

            try:
                result = await _ingest_trade(session, raw)
            except Exception as exc:
                logger.error(
                    "poll_ingest_trade_error",
                    wallet=wallet_addr[:12], tx_hash=tx_hash[:16], error=str(exc),
                )
                print(f"  ERROR ingesting trade {tx_hash[:16]}...: {exc}")
                await session.rollback()
                batch_details, batch_new_sells = [], 0
                aborted = True
                break

            if result is None:
                skipped += 1
                continue

            seen_this_run.add(key)
            batch_details.append(result)
            if result["side"] == "SELL":
                batch_new_sells += 1

            if len(batch_details) >= _BATCH_SIZE:
                try:
                    await session.commit()
                except Exception as exc:
                    logger.error(
                        "poll_batch_commit_error", wallet=wallet_addr[:12], error=str(exc)
                    )
                    print(f"  ERROR committing batch: {exc}")
                    await session.rollback()
                    batch_details, batch_new_sells = [], 0
                    aborted = True
                    break
                inserted += len(batch_details)
                new_sells += batch_new_sells
                insert_details.extend(batch_details)
                batch_details, batch_new_sells = [], 0

        if not aborted and batch_details:
            try:
                await session.commit()
            except Exception as exc:
                logger.error(
                    "poll_final_commit_error", wallet=wallet_addr[:12], error=str(exc)
                )
                print(f"  ERROR committing final batch: {exc}")
                await session.rollback()
                aborted = True
            else:
                inserted += len(batch_details)
                new_sells += batch_new_sells
                insert_details.extend(batch_details)

    if aborted:
        print(
            f"  ABORTED remaining trades for this wallet after a batch failure "
            f"({inserted} trade(s) committed before it). The rest will be "
            f"retried on the next run — dedup makes that safe."
        )

    print(f"  Already captured: {skipped}")
    print(f"  Newly inserted:   {inserted}")
    for d in insert_details:
        ts_str = d["ts"].strftime("%Y-%m-%d %H:%M:%S")
        print(
            f"    + {d['side']} {d['outcome']} @ {d['price']:.2f}, "
            f"${d['notional_usd']:.2f} ({ts_str})"
        )

    earliest_ts = min((d["ts"] for d in insert_details), default=None)
    latest_ts = max((d["ts"] for d in insert_details), default=None)

    return {
        "found": len(raw_trades),
        "in_window": len(recent),
        "skipped": skipped,
        "inserted": inserted,
        "new_sells": new_sells,
        "details": insert_details,
        "aborted": aborted,
        "truncated": fetch_result.truncated,
        "truncation_reason": fetch_result.truncation_reason,
        "earliest_ts": earliest_ts,
        "latest_ts": latest_ts,
    }


def _parse_ts(raw) -> datetime:
    try:
        val = float(raw)
        if val > 1e10:
            val /= 1000.0
        return datetime.fromtimestamp(val, tz=timezone.utc)
    except (ValueError, TypeError, OSError):
        return datetime.min.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# End-of-run summary (item 16) — every one of the M wallets, so the operator
# never has to reconstruct what happened from scrollback.
# ---------------------------------------------------------------------------

@dataclass
class WalletRunRecord:
    wallet: str
    outcome: str  # polled | skipped-excluded | skipped-rate | TRUNCATED | error (+combinable)
    found: int = 0
    inserted: int = 0
    earliest_ts: datetime | None = None
    latest_ts: datetime | None = None


def _print_run_summary_table(records: list["WalletRunRecord"]) -> None:
    """Per-wallet table covering every wallet in the run, printed last (after
    the score refresh) so it survives being the final thing on screen."""
    header = (
        f"{'Wallet':<44} {'Outcome':<20} {'Found':>7} {'Inserted':>9}  "
        f"{'Earliest':<20} {'Latest':<20}"
    )
    rule = "-" * len(header)
    print("\n" + "=" * len(header))
    print("RUN SUMMARY")
    print("=" * len(header))
    print(header)
    print(rule)
    for r in records:
        earliest = r.earliest_ts.strftime("%Y-%m-%d %H:%M:%S") if r.earliest_ts else "-"
        latest = r.latest_ts.strftime("%Y-%m-%d %H:%M:%S") if r.latest_ts else "-"
        print(
            f"{r.wallet:<44} {r.outcome:<20} {r.found:>7} {r.inserted:>9}  "
            f"{earliest:<20} {latest:<20}"
        )
    print(rule)
    truncated_n = sum(1 for r in records if "TRUNCATED" in r.outcome)
    error_n = sum(1 for r in records if "error" in r.outcome)
    print(
        f"{len(records)} wallet(s) total | {truncated_n} truncated | {error_n} error(s)"
    )
    print("=" * len(header))


# ---------------------------------------------------------------------------
# Console-to-file logging (item 16) — scrollback is not a record.
# ---------------------------------------------------------------------------

class _TeeStream:
    """Forwards every write to both the real stream and a log file, so the
    full run (summary table included) survives after the console scrolls."""

    def __init__(self, stream, log_file):
        self._stream = stream
        self._log_file = log_file

    def write(self, data):
        self._stream.write(data)
        self._log_file.write(data)
        return len(data)

    def flush(self):
        self._stream.flush()
        self._log_file.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


def _new_log_path() -> str:
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    logs_dir = os.path.join(repo_root, "logs")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return os.path.join(logs_dir, f"poll_{ts}.log")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def run(
    lookback_days: int = 7,
    max_trades_per_day: float | None = None,
    exclude_wallets: set[str] | None = None,
    include_wallets: set[str] | None = None,
) -> None:
    """
    Poll every watched wallet, then run activity-stat and score refreshes
    once each, after the wallet loop — not per-wallet.

    max_trades_per_day / exclude_wallets are polling-only operational
    filters — they decide which wallets THIS SCRIPT spends time on, and
    never touch wallets.watch_status or anything the live WS ingestor reads.
    A wallet skipped here is still watched, still bypasses the ingestor's
    notional filter, and stays eligible for polling on a future run with a
    higher/no threshold. Rate is estimated via one cheap page-0 probe
    (_estimate_trades_per_day) so an excluded wallet costs one API call, not
    a full paginated fetch plus per-trade ingestion.

    include_wallets (--wallet, repeatable) replaces the watched-wallet
    universe entirely when given: only the named addresses are polled, and
    they need not be watch_status='watch' — an address with no `wallets` row
    at all is still polled, labeled "unwatched" for display. DECISION (item
    16): an explicitly named wallet always bypasses --max-trades-per-day —
    naming a wallet is read as "I want this one regardless of the rate
    heuristic," not as a request to also apply it. --exclude-wallet still
    applies on top of --wallet (it's a stronger, more specific override in
    the other direction — dropping one wallet from an otherwise-explicit
    list is a reasonable thing to want and costs nothing to keep honoring).

    Score refresh: run_all_wallet_scores() once, not refresh_wallet_score()
    per wallet. refresh_wallet_score() is a thin wrapper that itself calls
    run_all_wallet_scores() internally (app/services/wallets/scorer.py:298)
    — it re-derives every wallet's normalized score from scratch and then
    reads back just the one requested wallet's result. Calling it once per
    wallet-with-new-sells (which, per item 3's own framing, is "nearly
    every wallet" on a wide-window poll) would re-run the identical
    full-universe aggregation up to len(watched) times for a result that's
    byte-identical every time, since all commits are already done before
    this phase starts. run_all_wallet_scores() once is what the existing
    daily score-refresh cron already does (app/tasks/run_score_refresh.py)
    for the same reason.
    """
    cutoff_ts = datetime.now(tz=timezone.utc) - timedelta(days=lookback_days)
    exclude_wallets = {w.lower() for w in (exclude_wallets or set())}
    include_wallets = {w.lower() for w in (include_wallets or set())}
    print(f"Polling watched wallets for missed trades...")
    print(f"Lookback: {lookback_days} days (since {cutoff_ts.strftime('%Y-%m-%d %H:%M UTC')})")
    if include_wallets:
        print(
            f"Explicit wallet list: {len(include_wallets)} wallet(s) via --wallet "
            f"(bypasses --max-trades-per-day; --exclude-wallet still applies)"
        )
    if max_trades_per_day is not None:
        print(f"Rate filter: skipping wallets estimated above {max_trades_per_day:.0f} trades/day")
    if exclude_wallets:
        print(f"Excluding {len(exclude_wallets)} wallet(s) via --exclude-wallet")

    factory = get_session_factory()
    if include_wallets:
        async with factory() as session:
            result = await session.execute(
                select(Wallet.wallet, Wallet.watch_status).where(
                    Wallet.wallet.in_(include_wallets)
                )
            )
            found = {row.wallet.lower(): row.watch_status for row in result.all()}
        watched = [
            _WalletRow(addr, found.get(addr, "unwatched"))
            for addr in sorted(include_wallets)
        ]
    else:
        async with factory() as session:
            result = await session.execute(
                select(Wallet.wallet, Wallet.watch_status).where(
                    Wallet.watch_status.in_(["watch", "high-interest"])
                )
            )
            watched = result.all()

    if not watched:
        print("No watched wallets found.")
        return

    print(f"Found {len(watched)} watched wallet(s).")

    total_inserted = 0
    wallets_with_activity: list[str] = []
    wallets_with_new_sells: list[str] = []
    skipped: list[tuple[str, str]] = []
    records: list[WalletRunRecord] = []
    total = len(watched)

    for idx, row in enumerate(watched, start=1):
        wallet_addr = row.wallet
        label = f"{wallet_addr[:8]}...{wallet_addr[-6:]}"
        is_explicit = wallet_addr.lower() in include_wallets
        print(f"\n[{idx} of {total}] Wallet: {label} ({row.watch_status})")

        if wallet_addr.lower() in exclude_wallets:
            print(f"  SKIPPED: excluded via --exclude-wallet")
            skipped.append((wallet_addr, "excluded"))
            print(f"  (skipped so far: {len(skipped)})")
            records.append(WalletRunRecord(wallet=wallet_addr, outcome="skipped-excluded"))
            continue

        if not is_explicit and max_trades_per_day is not None:
            rate = await _estimate_trades_per_day(wallet_addr)
            if rate is not None and rate > max_trades_per_day:
                print(
                    f"  SKIPPED: estimated {rate:.0f} trades/day exceeds "
                    f"--max-trades-per-day {max_trades_per_day:.0f}"
                )
                logger.info(
                    "poll_wallet_skipped_rate_limit",
                    wallet=wallet_addr[:12], estimated_rate=rate, threshold=max_trades_per_day,
                )
                skipped.append((wallet_addr, f"rate {rate:.0f}/day > {max_trades_per_day:.0f}"))
                print(f"  (skipped so far: {len(skipped)})")
                records.append(WalletRunRecord(wallet=wallet_addr, outcome="skipped-rate"))
                continue

        summary = await poll_wallet(wallet_addr, row.watch_status, cutoff_ts)
        total_inserted += summary["inserted"]
        if summary["inserted"] > 0:
            wallets_with_activity.append(wallet_addr)
        if summary["new_sells"] > 0:
            wallets_with_new_sells.append(wallet_addr)

        outcome_flags = []
        if summary.get("truncated"):
            outcome_flags.append("TRUNCATED")
        if summary.get("aborted"):
            outcome_flags.append("error")
        records.append(WalletRunRecord(
            wallet=wallet_addr,
            outcome="+".join(outcome_flags) if outcome_flags else "polled",
            found=summary["found"],
            inserted=summary["inserted"],
            earliest_ts=summary.get("earliest_ts"),
            latest_ts=summary.get("latest_ts"),
        ))

    polled_count = len(watched) - len(skipped)
    truncated_wallets = [r.wallet for r in records if "TRUNCATED" in r.outcome]
    print(
        f"\nDone. {total_inserted} new trade(s) inserted across {polled_count} polled "
        f"wallet(s) ({len(skipped)} skipped)."
    )
    if truncated_wallets:
        shown = ", ".join(f"{w[:8]}...{w[-6:]}" for w in truncated_wallets)
        print(
            f"WARNING: {len(truncated_wallets)} wallet(s) truncated this run — "
            f"incomplete history, safe to re-run: {shown}"
        )

    if wallets_with_activity:
        print(f"\nUpdating activity stats for {len(wallets_with_activity)} wallet(s)...")
        async with factory() as session:
            for wallet_addr in wallets_with_activity:
                await update_wallet_activity(session, wallet_addr)
            await session.commit()

    if wallets_with_new_sells:
        print(
            f"\nRefreshing wallet scores (full-universe recompute; "
            f"{len(wallets_with_new_sells)} wallet(s) had new closed positions)..."
        )
        async with factory() as session:
            score_result = await run_all_wallet_scores(session)
            await session.commit()
        print(f"  Scores recomputed for {score_result.get('wallets_scored', 0)} qualifying wallet(s).")

    # Printed last, deliberately — after the score refresh — so it's the
    # final thing on screen and in the log file.
    _print_run_summary_table(records)


def main() -> None:
    # Market/outcome names are arbitrary Unicode (esports team names, etc.)
    # and Windows' console defaults to cp1252, which can't encode most of
    # it — a name containing e.g. U+014D crashes plain print() mid-run and
    # kills the whole poll, discarding every wallet after the one that hit
    # it (though trades already committed before the print stay committed).
    # errors="replace" makes output encoding a display concern, never a
    # reason to abort real work.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Poll the CLOB Data API for missed trades by watched wallets."
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Lookback window in days (default: 7)",
    )
    parser.add_argument(
        "--max-trades-per-day",
        type=float,
        default=None,
        help=(
            "Skip wallets whose current trading rate (estimated via a cheap "
            "one-page probe) exceeds this. Polling-only filter — does not "
            "change watch_status or the live ingestor."
        ),
    )
    parser.add_argument(
        "--exclude-wallet",
        action="append",
        default=[],
        help="Wallet address to skip entirely this run (repeatable).",
    )
    parser.add_argument(
        "--wallet",
        action="append",
        default=[],
        help=(
            "Poll ONLY this wallet address (repeatable), instead of every "
            "watch_status='watch'/'high-interest' wallet. Works for any "
            "address, including ones not currently watched. DECISION: "
            "bypasses --max-trades-per-day (naming a wallet means 'I want "
            "this one regardless of the rate heuristic') — --exclude-wallet "
            "still applies on top of it."
        ),
    )
    args = parser.parse_args()

    log_path = _new_log_path()
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    log_file = open(log_path, "w", encoding="utf-8", errors="replace")
    original_stdout = sys.stdout
    sys.stdout = _TeeStream(original_stdout, log_file)
    print(f"Logging full run output to: {log_path}")

    loop = asyncio.SelectorEventLoop(selectors.SelectSelector())
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(run(
            lookback_days=args.days,
            max_trades_per_day=args.max_trades_per_day,
            exclude_wallets=set(args.exclude_wallet),
            include_wallets=set(args.wallet),
        ))
    finally:
        print(f"Log file: {log_path}")
        loop.close()
        sys.stdout = original_stdout
        log_file.close()


if __name__ == "__main__":
    main()
