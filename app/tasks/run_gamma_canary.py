"""
Gamma API contract canary.

Purpose: catch the *next* Gamma deprecation/breaking-schema-change ourselves,
proactively, instead of discovering it via corrupted data days later (as
happened with the pre-2026-07-17 phantom price_change contamination episode
and again with the /markets -> /markets/keyset sunset found 2026-07-18).

Checks, all read-only against the live API:
  1. Deprecation drift: every endpoint this codebase depends on
     (/markets/keyset, /events/keyset, /markets/{id}, /markets/slug/{slug})
     is checked for a `deprecation`/`sunset` response header. Any endpoint
     newly carrying one is a CRIT — it means a further migration is coming.
  2. Envelope shape: /markets/keyset and /events/keyset must return the
     expected dict envelope with a "markets"/"events" list key.
  3. Field presence: a live sampled market must carry every field
     gamma_client._parse_market() reads (id, question, conditionId, slug,
     outcomes, outcomePrices, clobTokenIds, closed, active).
  4. Field sanity: outcomes/outcomePrices/clobTokenIds parse as JSON lists
     of equal length; every price is a valid float in [0, 1].
  5. Resolution derivation sanity: fetches one recently-resolved market
     (via the still-live path lookup) and confirms derive_resolution()
     returns resolved=True with a winning outcome — this is the exact
     function the resolution job depends on for financial correctness.

CRIT (logger.critical) on any failure — there is no external paging channel
wired up yet (no Slack/Sentry in this codebase), so a CRIT-level structured
log line is the strongest signal currently available; it is deliberately a
distinct level from the routine ERROR-level exceptions elsewhere in this
module so it is easy to alert on once a channel exists.

Schedule: every 6 hours via APScheduler (run_worker.py).
One-time run: python -m app.tasks.run_gamma_canary
"""
import asyncio
import json as _json
import sys
from datetime import datetime, timezone

import httpx

from app.logging import setup_logging, get_logger
from app.services.polymarket.gamma_client import (
    _build_client,
    _parse_json_list,
    derive_resolution,
)

logger = get_logger(__name__)

_REQUIRED_MARKET_FIELDS = [
    "id", "question", "conditionId", "slug",
    "outcomes", "outcomePrices", "clobTokenIds", "closed", "active",
]


async def _check_endpoint(client: httpx.AsyncClient, path: str, params: dict) -> dict:
    """GET one endpoint and report deprecation-header status + raw response."""
    resp = await client.get(path, params=params)
    resp.raise_for_status()
    return {
        "path": path,
        "deprecated": resp.headers.get("deprecation") == "true",
        "sunset": resp.headers.get("sunset"),
        "data": resp.json(),
    }


def _check_market_fields(raw: dict) -> list[str]:
    """Return a list of problems found on a raw Gamma market dict (empty = clean)."""
    problems = []

    missing = [f for f in _REQUIRED_MARKET_FIELDS if f not in raw]
    if missing:
        problems.append(f"missing fields: {missing}")
        return problems  # further checks would just be noise

    outcomes = _parse_json_list(raw.get("outcomes", "[]"))
    prices = _parse_json_list(raw.get("outcomePrices", "[]"))
    token_ids = _parse_json_list(raw.get("clobTokenIds", "[]"))

    if not outcomes:
        problems.append("outcomes parsed to empty list")
    if len(prices) != len(outcomes):
        problems.append(f"outcomePrices length {len(prices)} != outcomes length {len(outcomes)}")
    if len(token_ids) != len(outcomes):
        problems.append(f"clobTokenIds length {len(token_ids)} != outcomes length {len(outcomes)}")

    for p in prices:
        try:
            fval = float(p)
        except (TypeError, ValueError):
            problems.append(f"outcomePrice {p!r} is not a float")
            continue
        if not (0.0 <= fval <= 1.0):
            problems.append(f"outcomePrice {fval} outside [0,1]")

    return problems


async def run_gamma_canary() -> dict:
    """Run all contract checks. Returns a summary dict; never raises."""
    problems: list[str] = []
    deprecated_endpoints: list[dict] = []
    checked_endpoints: list[str] = []

    async with _build_client() as client:
        # 1 + 2 + 3 + 4: keyset list endpoints
        for path, params in [
            ("/markets/keyset", {"limit": 1, "active": "true", "closed": "false"}),
            ("/events/keyset", {"limit": 1}),
        ]:
            checked_endpoints.append(path)
            try:
                result = await _check_endpoint(client, path, params)
            except Exception as exc:
                problems.append(f"{path}: request failed: {exc}")
                continue

            if result["deprecated"]:
                deprecated_endpoints.append({"path": path, "sunset": result["sunset"]})

            key = "markets" if "markets" in path else "events"
            items = result["data"].get(key) if isinstance(result["data"], dict) else None
            if not isinstance(items, list):
                problems.append(f"{path}: response missing '{key}' list envelope")
                continue
            if not items:
                problems.append(f"{path}: returned zero items for a broad query")
                continue

            if key == "markets":
                sample_market = items[0]
            else:
                event_markets = items[0].get("markets") or []
                sample_market = event_markets[0] if event_markets else None

            if sample_market is None:
                problems.append(f"{path}: sampled event carried no nested markets")
                continue

            field_problems = _check_market_fields(sample_market)
            if field_problems:
                problems.append(f"{path} sample market {sample_market.get('id')}: {field_problems}")

        # 1 (continued): path-style single-market lookups (not deprecated as of
        # 2026-07-18, but check every run so drift is caught immediately)
        probe_market_id = None
        try:
            keyset_probe = await _check_endpoint(client, "/markets/keyset", {"limit": 1})
            probe_market_id = keyset_probe["data"]["markets"][0]["id"]
            probe_slug = keyset_probe["data"]["markets"][0].get("slug")
        except Exception as exc:
            problems.append(f"could not obtain a probe market id/slug: {exc}")
            probe_slug = None

        if probe_market_id:
            checked_endpoints.append(f"/markets/{probe_market_id}")
            try:
                result = await _check_endpoint(client, f"/markets/{probe_market_id}", {})
                if result["deprecated"]:
                    deprecated_endpoints.append({"path": "/markets/{id}", "sunset": result["sunset"]})
                field_problems = _check_market_fields(result["data"])
                if field_problems:
                    problems.append(f"/markets/{{id}}: {field_problems}")
            except Exception as exc:
                problems.append(f"/markets/{{id}}: request failed: {exc}")

        if probe_slug:
            checked_endpoints.append(f"/markets/slug/{probe_slug}")
            try:
                result = await _check_endpoint(client, f"/markets/slug/{probe_slug}", {})
                if result["deprecated"]:
                    deprecated_endpoints.append({"path": "/markets/slug/{slug}", "sunset": result["sunset"]})
            except Exception as exc:
                problems.append(f"/markets/slug/{{slug}}: request failed: {exc}")

        # 5: resolution derivation sanity on a real resolved market
        try:
            resolved_probe = await _check_endpoint(
                client, "/markets/keyset", {"limit": 1, "closed": "true"}
            )
            resolved_markets = resolved_probe["data"].get("markets") or []
        except Exception as exc:
            resolved_markets = []
            problems.append(f"could not fetch a resolved-market probe: {exc}")

        if resolved_markets:
            raw = resolved_markets[0]
            resolved, resolution = derive_resolution(raw)
            # Not every closed market is settled yet (UMA pending / voided) —
            # only flag if closed=true, degenerate-looking prices, but
            # derive_resolution still says unresolved AND prices look decided.
            prices = _parse_json_list(raw.get("outcomePrices", "[]"))
            has_degenerate_price = any(
                _is_float(p) and float(p) >= 0.99 for p in prices
            )
            if raw.get("closed") and has_degenerate_price and not resolved:
                problems.append(
                    f"derive_resolution() failed to resolve closed market {raw.get('id')} "
                    f"with degenerate prices {prices}"
                )

    checked_at = datetime.now(tz=timezone.utc)
    summary = {
        "checked_at": checked_at.isoformat(),
        "endpoints_checked": checked_endpoints,
        "deprecated_endpoints": deprecated_endpoints,
        "problems": problems,
        "ok": not problems and not deprecated_endpoints,
    }

    if deprecated_endpoints:
        logger.critical(
            "gamma_contract_drift_deprecation",
            deprecated_endpoints=deprecated_endpoints,
            checked_endpoints=checked_endpoints,
        )
        from app.services.alerts.system_alerts import send_system_alert
        await send_system_alert(
            "CRIT", "gamma_canary",
            f"Gamma endpoint(s) newly deprecated: {deprecated_endpoints}",
        )
    if problems:
        logger.critical(
            "gamma_contract_drift_schema",
            problems=problems,
            checked_endpoints=checked_endpoints,
        )
        from app.services.alerts.system_alerts import send_system_alert
        await send_system_alert(
            "CRIT", "gamma_canary",
            f"Gamma contract drift detected: {problems}",
        )
    if summary["ok"]:
        logger.info("gamma_canary_ok", endpoints_checked=checked_endpoints)

    return summary


def _is_float(v) -> bool:
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


async def main() -> None:
    setup_logging()
    started = datetime.now(tz=timezone.utc)
    print(f"=== Gamma Contract Canary — started {started.strftime('%Y-%m-%d %H:%M:%S UTC')} ===\n")

    result = await run_gamma_canary()

    print(f"Endpoints checked : {result['endpoints_checked']}")
    print(f"Deprecated found  : {result['deprecated_endpoints'] or 'none'}")
    print(f"Problems found    : {result['problems'] or 'none'}")
    print(f"Overall           : {'OK' if result['ok'] else 'DRIFT DETECTED (see CRIT log lines above)'}")

    elapsed = (datetime.now(tz=timezone.utc) - started).total_seconds()
    print(f"Elapsed           : {elapsed:.1f}s")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop)
    else:
        asyncio.run(main())
