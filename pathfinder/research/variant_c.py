"""
Variant C — structural favorite-longshot harvesting in large-field
mutually-exclusive outright books (decisions/2026-07-18.md gate spec).

No wallet-following component (orthogonal to Variant A/B) and no DB
dependency at all — the entire input is Gamma event metadata plus CLOB
`prices-history` for historical, already-resolved tournaments, both fetched
live and read-only. This module is the Variant C gate implementation:
`run_variant_c_backtest()` executes the full declared grid from the gate
spec and returns per-tier results; nothing here reads or writes the tracker
database.

Judgment calls (declared in the gate-spec decision entry, restated here at
the point they're implemented):
  1. Liquidity gate: an entry at offset day T is only taken once the token's
     price has moved off its flat initial-listing default — see
     `first_real_price_date()`. Skipped cells are counted, never filled
     with a stale price.
  2. Exit ladders are price-triggered only (S1: absolute price thresholds,
     S2: percent-gain thresholds), because bracket-round boundaries don't
     generalize to season-long conference/division books.
  3. Resolution/settlement price comes from Gamma's own `outcomePrices`
     field (the Yes-outcome price on the closed market), NOT the last point
     of the CLOB `prices-history` series. Verified this matters: CLOB
     trading can stop before a market is administratively resolved once the
     real-world outcome is obvious (e.g. Super Bowl LX's winning team's own
     price series peaked at 0.69 and never printed again, while Gamma's
     `outcomePrices` correctly shows `["1","0"]` for that same market) — the
     CLOB series' last point is "last trade," not "settlement," and using it
     as settlement silently zeroed out every winner's payoff in an early
     version of this module (caught via a 0% calibration win rate across
     every single grid cell, which is what a total resolution-detection
     failure looks like, not a real finding). Only prices within 0.02 of 0
     or 1 are trusted as resolved; anything else is treated as
     "not reliably resolved" and that team-market is dropped from analysis
     (same conservative-direction convention as `gamma_client.derive_resolution`).
  4. Cluster = season (`Tournament.season_id`), not the individual
     field-size book — a season's 30+/~15/4-6 books are correlated draws
     from the same underlying outcome.
  5. Stake is normalized to $1 per favorite pick; returns are reported as a
     fraction of stake, consistent with the ROI convention used elsewhere
     in this package.
"""
from __future__ import annotations

import asyncio
import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from statistics import mean

import httpx

from pathfinder.research.stats import BootstrapResult, cluster_bootstrap_ci

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

# ── Declared grid (decisions/2026-07-18.md — do not add/drop cells after seeing results) ──
ENTRY_OFFSETS_DAYS: list[int] = [30, 14, 7, 1]
FAVORITE_COUNTS: list[int] = [1, 2, 3]
COST_TIERS_CENTS: list[float] = [1.0, 2.0]
EXIT_RULES: list[str] = ["S1_stage", "S2_percent", "baseline_hold"]
HURDLE_ANNUAL_RATE = 0.05
LIQUIDITY_MIN_PRICE_MOVE = 0.005
FETCH_CONCURRENCY = 15


@dataclass(frozen=True)
class Tournament:
    name: str
    sport: str
    tier: str  # "30+", "~15", "4-6"
    season_id: str  # bootstrap cluster key
    event_slug: str
    season_start: date


# ── Universe (decisions/2026-07-18.md) ──
TOURNAMENT_UNIVERSE: list[Tournament] = [
    # 30+ tier
    Tournament("NFL 2025 season — Big Game Champion 2026", "NFL", "30+", "NFL-2025", "super-bowl-champion-2026-731", date(2025, 9, 4)),
    Tournament("NBA 2024-25 — NBA Champion", "NBA", "30+", "NBA-2024-25", "nba-champion-2024-2025", date(2024, 10, 22)),
    Tournament("NBA 2025-26 — 2026 NBA Champion", "NBA", "30+", "NBA-2025-26", "2026-nba-champion", date(2025, 10, 21)),
    Tournament("MLB 2025 — World Series Champion 2025", "MLB", "30+", "MLB-2025", "world-series-champion-2025", date(2025, 3, 27)),
    Tournament("MLB 2024 — World Series Champion 2024", "MLB", "30+", "MLB-2024", "world-series-champion-2024", date(2024, 3, 28)),
    Tournament("NHL 2024-25 — Stanley Cup Champion 2025", "NHL", "30+", "NHL-2024-25", "stanley-cup-winner", date(2024, 10, 8)),
    Tournament("NHL 2025-26 — 2026 NHL Stanley Cup Champion", "NHL", "30+", "NHL-2025-26", "2026-nhl-stanley-cup-champion", date(2025, 10, 7)),
    Tournament("NCAA March Madness 2025", "NCAA", "30+", "NCAA-2025", "2025-ncaa-tournament-winner", date(2025, 3, 18)),
    Tournament("NCAA March Madness 2026", "NCAA", "30+", "NCAA-2026", "2026-ncaa-tournament-winner", date(2026, 3, 17)),
    # 2026-07-18 expansion: international club soccer (universe-expansion follow-up)
    Tournament("Champions League 2024-25 — Winner", "Champions League", "30+", "UCL-2024-25", "champions-league-winner-2025", date(2024, 9, 17)),
    # 2026-07-23: Variant C revisit-trigger registration (decisions/2026-07-18.md, "World Cup 2026
    # included, add it the day it resolves cleanly") — resolution verified before adding, see
    # decisions/2026-07-23.md. season_start = real-world tournament kickoff (Gamma's own startDate
    # on this event is just its 2025-07-02 listing date, not usable); field size 51 country markets.
    Tournament("World Cup 2026 — Winner", "Soccer", "30+", "WC-2026", "world-cup-winner", date(2026, 6, 11)),
    # ~15 tier
    Tournament("NFL 2024 — AFC Champion", "NFL", "~15", "NFL-2024", "afc-champion", date(2024, 9, 5)),
    Tournament("NFL 2024 — NFC Champion", "NFL", "~15", "NFL-2024", "nfc-champion", date(2024, 9, 5)),
    Tournament("NFL 2025 — AFC Champion", "NFL", "~15", "NFL-2025", "afc-champion-1", date(2025, 9, 4)),
    Tournament("NFL 2025 — NFC Champion", "NFL", "~15", "NFL-2025", "nfc-champion-1", date(2025, 9, 4)),
    Tournament("NBA 2024-25 — Eastern Conf Champion", "NBA", "~15", "NBA-2024-25", "nba-eastern-conference-champion", date(2024, 10, 22)),
    Tournament("NBA 2024-25 — Western Conf Champion", "NBA", "~15", "NBA-2024-25", "nba-western-conference-champion", date(2024, 10, 22)),
    Tournament("NBA 2025-26 — Eastern Conf Champion", "NBA", "~15", "NBA-2025-26", "nba-playoffs-eastern-conference-champion", date(2025, 10, 21)),
    Tournament("NBA 2025-26 — Western Conf Champion", "NBA", "~15", "NBA-2025-26", "nba-playoffs-western-conference-champion", date(2025, 10, 21)),
    Tournament("NHL 2024-25 — Eastern Conf Champion", "NHL", "~15", "NHL-2024-25", "nhl-eastern-conference-champion", date(2024, 10, 8)),
    Tournament("NHL 2024-25 — Western Conf Champion", "NHL", "~15", "NHL-2024-25", "nhl-western-conference-champion", date(2024, 10, 8)),
    Tournament("NHL 2025-26 — Eastern Conf Champion", "NHL", "~15", "NHL-2025-26", "nhl-eastern-conference-champion-198", date(2025, 10, 7)),
    Tournament("NHL 2025-26 — Western Conf Champion", "NHL", "~15", "NHL-2025-26", "nhl-western-conference-champion-865", date(2025, 10, 7)),
    # 2026-07-18 expansion: international soccer/cricket (universe-expansion follow-up)
    Tournament("Europa League 2024-25 — Winner", "Europa League", "~15", "UEL-2024-25", "europa-league-winner-24-25", date(2024, 9, 25)),
    Tournament("ICC T20 World Cup 2026 — Winner", "Cricket", "~15", "T20WC-2026", "2026-icc-t20-mens-world-cup-winner", date(2026, 2, 7)),
    # 4-6 tier
    Tournament("NFL 2024 — AFC East", "NFL", "4-6", "NFL-2024", "afc-east-champion", date(2024, 9, 5)),
    Tournament("NFL 2024 — AFC North", "NFL", "4-6", "NFL-2024", "afc-north-winner", date(2024, 9, 5)),
    Tournament("NFL 2024 — AFC South", "NFL", "4-6", "NFL-2024", "afc-south", date(2024, 9, 5)),
    Tournament("NFL 2024 — AFC West", "NFL", "4-6", "NFL-2024", "afc-west-winner", date(2024, 9, 5)),
    Tournament("NFL 2024 — NFC East", "NFL", "4-6", "NFL-2024", "nfc-east-winner", date(2024, 9, 5)),
    Tournament("NFL 2024 — NFC North", "NFL", "4-6", "NFL-2024", "nfc-north-winner-1", date(2024, 9, 5)),
    Tournament("NFL 2024 — NFC South", "NFL", "4-6", "NFL-2024", "nfc-south-winner-1", date(2024, 9, 5)),
    Tournament("NFL 2024 — NFC West", "NFL", "4-6", "NFL-2024", "nfc-west-winner", date(2024, 9, 5)),
    Tournament("NFL 2025 — AFC East", "NFL", "4-6", "NFL-2025", "afc-east-winner-11", date(2025, 9, 4)),
    Tournament("NFL 2025 — AFC North", "NFL", "4-6", "NFL-2025", "afc-north-winner-1", date(2025, 9, 4)),
    Tournament("NFL 2025 — AFC South", "NFL", "4-6", "NFL-2025", "afc-south-winner-1", date(2025, 9, 4)),
    Tournament("NFL 2025 — AFC West", "NFL", "4-6", "NFL-2025", "afc-west-winner-1", date(2025, 9, 4)),
    Tournament("NFL 2025 — NFC East", "NFL", "4-6", "NFL-2025", "nfc-east-winner-1", date(2025, 9, 4)),
    Tournament("NFL 2025 — NFC North", "NFL", "4-6", "NFL-2025", "nfc-north-winner-11", date(2025, 9, 4)),
    Tournament("NFL 2025 — NFC South", "NFL", "4-6", "NFL-2025", "nfc-south-winner-11", date(2025, 9, 4)),
    Tournament("NFL 2025 — NFC West", "NFL", "4-6", "NFL-2025", "nfc-west-winner-1", date(2025, 9, 4)),
    Tournament("MLB 2025 — AL East", "MLB", "4-6", "MLB-2025", "al-east-division-winner", date(2025, 3, 27)),
    Tournament("MLB 2025 — AL Central", "MLB", "4-6", "MLB-2025", "al-central-division-winner", date(2025, 3, 27)),
    Tournament("MLB 2025 — AL West", "MLB", "4-6", "MLB-2025", "al-west-division-winner", date(2025, 3, 27)),
    Tournament("MLB 2025 — NL East", "MLB", "4-6", "MLB-2025", "nl-east-division-winner", date(2025, 3, 27)),
    Tournament("MLB 2025 — NL Central", "MLB", "4-6", "MLB-2025", "nl-central-division-winner", date(2025, 3, 27)),
    Tournament("MLB 2025 — NL West", "MLB", "4-6", "MLB-2025", "nl-west-division-winner", date(2025, 3, 27)),
    Tournament("NHL 2025-26 — Pacific Division", "NHL", "4-6", "NHL-2025-26", "nhl-pacific-division-winner-228", date(2025, 10, 7)),
    Tournament("NHL 2025-26 — Central Division", "NHL", "4-6", "NHL-2025-26", "nhl-central-division-winner-948", date(2025, 10, 7)),
    Tournament("NHL 2025-26 — Metropolitan Division", "NHL", "4-6", "NHL-2025-26", "nhl-metropolitan-division-winner-831", date(2025, 10, 7)),
    Tournament("NHL 2025-26 — Atlantic Division", "NHL", "4-6", "NHL-2025-26", "nhl-atlantic-division-winner-747", date(2025, 10, 7)),
]


@dataclass(frozen=True)
class TeamSeries:
    team: str
    token_id: str
    prices: list[tuple[datetime, float]]  # sorted ascending by time
    settlement_price: float | None = None  # Gamma outcomePrices[yes_idx] — ground truth, see judgment call 3
    settlement_time: datetime | None = None  # Gamma closedTime
    volume: float = 0.0  # Gamma volumeNum/volume — C2 basket tie-break only, unused by the original grid


# ── Pure functions (unit-testable without network) ──────────────────────


def first_real_price_date(prices: list[tuple[datetime, float]]) -> datetime | None:
    """First timestamp at which two consecutive observations both differ
    from the series' first observation by more than LIQUIDITY_MIN_PRICE_MOVE
    — the point price discovery has visibly begun. None if it never happens
    (a token that's flat for its entire observed history)."""
    if len(prices) < 2:
        return None
    p0 = prices[0][1]
    for i in range(len(prices) - 1):
        if abs(prices[i][1] - p0) > LIQUIDITY_MIN_PRICE_MOVE and abs(prices[i + 1][1] - p0) > LIQUIDITY_MIN_PRICE_MOVE:
            return prices[i][0]
    return None


def price_at_or_before(prices: list[tuple[datetime, float]], as_of: datetime, max_staleness_days: int = 3) -> float | None:
    """Last observed price at or before `as_of`, or None if the most recent
    prior observation is older than max_staleness_days (no stale fill)."""
    candidate = None
    for ts, p in prices:
        if ts <= as_of:
            candidate = (ts, p)
        else:
            break
    if candidate is None:
        return None
    ts, p = candidate
    if (as_of - ts) > timedelta(days=max_staleness_days):
        return None
    return p


def resolution_price(prices: list[tuple[datetime, float]]) -> float | None:
    """Last recorded price in the series — the settlement price for an
    already-resolved historical market (see module docstring, judgment call 3)."""
    if not prices:
        return None
    return prices[-1][1]


@dataclass(frozen=True)
class ExitLeg:
    fraction: float
    exit_price: float
    exit_time: datetime


def apply_exit_ladder(
    rule: str,
    entry_price: float,
    entry_time: datetime,
    prices_after_entry: list[tuple[datetime, float]],
    resolution_time: datetime,
    resolution_price_val: float,
) -> list[ExitLeg]:
    """Applies one of the declared exit rules to a single team's post-entry
    price path. `prices_after_entry` must be sorted ascending and contain
    only points strictly after entry_time. Returns 1-2 legs summing to
    fraction=1.0."""
    if rule == "baseline_hold":
        return [ExitLeg(1.0, resolution_price_val, resolution_time)]

    if rule == "S1_stage":
        thresholds = [(0.5, 0.60), (0.5, 0.85)]
    elif rule == "S2_percent":
        thresholds = [(0.5, entry_price * 1.25), (0.5, entry_price * 1.60)]
    else:
        raise ValueError(f"unknown exit rule: {rule}")

    legs: list[ExitLeg] = []
    remaining = 1.0
    for frac, trigger_price in thresholds:
        hit = next((pt for pt in prices_after_entry if pt[1] >= trigger_price - 1e-9), None)
        if hit is not None:
            legs.append(ExitLeg(frac, hit[1], hit[0]))
            remaining -= frac
    if remaining > 1e-9:
        legs.append(ExitLeg(remaining, resolution_price_val, resolution_time))
    return legs


def net_return_for_legs(entry_price: float, legs: list[ExitLeg], entry_time: datetime, cost_cents: float) -> tuple[float, float]:
    """Returns (net_return_fraction, holding_days). Each leg's dollars are
    invested at `entry_price + cost` (per-side cost) and sold at
    `exit_price - cost`; the leg's return is (net_exit - net_entry) /
    net_entry, i.e. a % return on the dollars actually committed to that
    leg — not a $1-notional/share-count model, which sidesteps having to
    track fractional share counts across staged exits at different prices.
    The total is the fraction-weighted average of each leg's % return, and
    holding_days is the fraction-weighted average holding period."""
    cost = cost_cents / 100.0
    net_entry = entry_price + cost
    total_return = 0.0
    weighted_days = 0.0
    for leg in legs:
        net_exit = leg.exit_price - cost
        leg_return = (net_exit - net_entry) / net_entry if net_entry > 0 else 0.0
        total_return += leg.fraction * leg_return
        days = max((leg.exit_time - entry_time).total_seconds() / 86400.0, 0.0)
        weighted_days += leg.fraction * days
    return total_return, weighted_days


def hurdle_return(holding_days: float, annual_rate: float = HURDLE_ANNUAL_RATE) -> float:
    """Capital-lockup opportunity-cost hurdle, compounded over the actual
    holding period."""
    return (1 + annual_rate) ** (holding_days / 365.0) - 1


# ── Fetch layer (live, read-only) ────────────────────────────────────────


_SETTLEMENT_TOLERANCE = 0.02


def _parse_gamma_datetime(value: str | None) -> datetime | None:
    """Parse a Gamma timestamp (ISO 'T...Z' or Postgres-style space/'+00')."""
    if not value:
        return None
    normalized = value.replace("Z", "+00:00").replace(" ", "T", 1)
    if normalized.endswith("+00"):
        normalized += ":00"
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


async def fetch_event_teams(client: httpx.AsyncClient, event_slug: str) -> list[dict]:
    resp = await client.get(f"{GAMMA_BASE}/events/slug/{event_slug}")
    resp.raise_for_status()
    data = resp.json()
    out = []
    for m in data.get("markets", []):
        token_ids = json.loads(m.get("clobTokenIds") or "[]")
        outcomes = json.loads(m.get("outcomes") or "[]")
        out_prices = json.loads(m.get("outcomePrices") or "[]")
        if not token_ids or not outcomes:
            continue
        # "Yes" token is whichever outcome label says Yes; groupItemTitle is the team name.
        yes_idx = next((i for i, o in enumerate(outcomes) if o.strip().lower() == "yes"), 0)

        settlement_price = None
        if m.get("closed") and yes_idx < len(out_prices):
            try:
                raw_price = float(out_prices[yes_idx])
            except (TypeError, ValueError):
                raw_price = None
            if raw_price is not None and (raw_price <= _SETTLEMENT_TOLERANCE or raw_price >= 1 - _SETTLEMENT_TOLERANCE):
                settlement_price = raw_price

        raw_vol = m.get("volumeNum")
        if raw_vol is None:
            raw_vol = m.get("volume")
        try:
            volume = float(raw_vol) if raw_vol is not None else 0.0
        except (TypeError, ValueError):
            volume = 0.0

        out.append({
            "team": m.get("groupItemTitle") or m.get("question"),
            "token_id": token_ids[yes_idx],
            "settlement_price": settlement_price,
            "settlement_time": _parse_gamma_datetime(m.get("closedTime")),
            "volume": volume,
        })
    return out


async def fetch_price_series(client: httpx.AsyncClient, token_id: str) -> list[tuple[datetime, float]]:
    resp = await client.get(
        f"{CLOB_BASE}/prices-history",
        params={"market": token_id, "interval": "max", "fidelity": 1440},
    )
    resp.raise_for_status()
    data = resp.json()
    points = data.get("history", [])
    series = [
        (datetime.fromtimestamp(pt["t"], tz=timezone.utc), float(pt["p"]))
        for pt in points
        if "t" in pt and "p" in pt
    ]
    series.sort(key=lambda x: x[0])
    return series


async def fetch_tournament_series(client: httpx.AsyncClient, tournament: Tournament, sem: asyncio.Semaphore) -> list[TeamSeries]:
    teams = await fetch_event_teams(client, tournament.event_slug)

    async def _one(t: dict) -> TeamSeries | None:
        async with sem:
            try:
                prices = await fetch_price_series(client, t["token_id"])
            except Exception:
                return None
            if not prices:
                return None
            return TeamSeries(
                team=t["team"], token_id=t["token_id"], prices=prices,
                settlement_price=t.get("settlement_price"),
                settlement_time=t.get("settlement_time"),
                volume=t.get("volume", 0.0),
            )

    results = await asyncio.gather(*(_one(t) for t in teams))
    return [r for r in results if r is not None]


async def fetch_universe(
    universe: list[Tournament] = TOURNAMENT_UNIVERSE, concurrency: int = FETCH_CONCURRENCY
) -> dict[str, list[TeamSeries]]:
    """Returns {event_slug: [TeamSeries, ...]} for every tournament in the universe."""
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(timeout=30.0, verify=False, headers={"Accept": "application/json"}) as client:
        results = await asyncio.gather(
            *(fetch_tournament_series(client, t, sem) for t in universe)
        )
    return {t.event_slug: series for t, series in zip(universe, results)}


# ── Grid evaluation (pure — takes fetched series, no network) ───────────


@dataclass
class TournamentCellObservation:
    tournament: Tournament
    basket_return: float
    weighted_holding_days: float
    n_favorites_used: int
    hurdle_cleared: bool
    win_rate: float  # fraction of the selected favorites that actually won (0 or 1/N)
    mean_entry_price: float


@dataclass
class GridCellResult:
    tier: str
    entry_offset_days: int
    n_favorites: int
    exit_rule: str
    cost_cents: float
    observations: list[TournamentCellObservation]
    n_skipped_liquidity: int
    n_tournaments_total: int
    bootstrap: BootstrapResult | None
    calibration_bootstrap: BootstrapResult | None
    hurdle_clear_rate: float | None


def _tournament_entry_snapshot(
    tournament: Tournament, series_list: list[TeamSeries], entry_offset_days: int
) -> tuple[list[tuple[TeamSeries, float]], int]:
    """Returns (valid teams sorted by entry price desc, n_skipped_for_liquidity)
    at the given entry offset for one tournament. A team is skipped whether
    its market doesn't exist yet at entry_date (no price data at all) or it
    exists but hasn't cleared the liquidity gate — both are "could not take
    this entry," and both must count toward the skip total or wide-offset
    cells would silently look cleaner than they are."""
    entry_date = datetime.combine(tournament.season_start, datetime.min.time(), tzinfo=timezone.utc) - timedelta(days=entry_offset_days)
    valid: list[tuple[TeamSeries, float]] = []
    n_skipped = 0
    for ts in series_list:
        price = price_at_or_before(ts.prices, entry_date)
        if price is None:
            n_skipped += 1
            continue
        frpd = first_real_price_date(ts.prices)
        if frpd is None or entry_date < frpd:
            n_skipped += 1
            continue
        valid.append((ts, price))
    # Price desc primary; volume desc tie-break (C2 spec, decisions/2026-07-18.md) —
    # harmless for the original grid, which never has price ties in practice.
    valid.sort(key=lambda pair: (-pair[1], -pair[0].volume))
    return valid, n_skipped


def evaluate_grid(fetched: dict[str, list[TeamSeries]]) -> list[GridCellResult]:
    """Runs the complete declared grid (decisions/2026-07-18.md) over
    already-fetched price series. Pure/offline — no network calls."""
    by_tier: dict[str, list[Tournament]] = {}
    for t in TOURNAMENT_UNIVERSE:
        by_tier.setdefault(t.tier, []).append(t)

    results: list[GridCellResult] = []

    for tier, tournaments in by_tier.items():
        for entry_offset in ENTRY_OFFSETS_DAYS:
            # Compute the ranked snapshot once per tournament per offset (shared across n_favorites/exit_rule/cost)
            snapshots: dict[str, tuple[list[tuple[TeamSeries, float]], int]] = {}
            for t in tournaments:
                series_list = fetched.get(t.event_slug, [])
                snapshots[t.event_slug] = _tournament_entry_snapshot(t, series_list, entry_offset)

            for n_fav in FAVORITE_COUNTS:
                for exit_rule in EXIT_RULES:
                    for cost_cents in COST_TIERS_CENTS:
                        observations: list[TournamentCellObservation] = []
                        n_skipped_total = 0
                        for t in tournaments:
                            valid, n_skipped = snapshots[t.event_slug]
                            n_skipped_total += n_skipped
                            if not valid:
                                continue
                            picks = valid[:n_fav]
                            entry_date = datetime.combine(t.season_start, datetime.min.time(), tzinfo=timezone.utc) - timedelta(days=entry_offset)

                            leg_returns: list[float] = []
                            leg_days: list[float] = []
                            wins = 0
                            entry_prices: list[float] = []
                            for ts, entry_price in picks:
                                entry_prices.append(entry_price)
                                after = [(dt, p) for dt, p in ts.prices if dt > entry_date]
                                # Ground truth is Gamma's own settlement price, NOT the CLOB
                                # series' last point (see module docstring, judgment call 3) —
                                # a team with no reliably-resolved settlement is dropped.
                                res_price = ts.settlement_price
                                if res_price is None:
                                    continue
                                res_time = ts.settlement_time or ts.prices[-1][0]
                                if res_price >= 1 - _SETTLEMENT_TOLERANCE:
                                    wins += 1
                                legs = apply_exit_ladder(exit_rule, entry_price, entry_date, after, res_time, res_price)
                                ret, days = net_return_for_legs(entry_price, legs, entry_date, cost_cents)
                                leg_returns.append(ret)
                                leg_days.append(days)

                            if not leg_returns:
                                continue
                            basket_return = mean(leg_returns)
                            weighted_days = mean(leg_days)
                            hurdle = hurdle_return(weighted_days)
                            observations.append(TournamentCellObservation(
                                tournament=t,
                                basket_return=basket_return,
                                weighted_holding_days=weighted_days,
                                n_favorites_used=len(picks),
                                hurdle_cleared=basket_return > hurdle,
                                win_rate=wins / len(picks),
                                mean_entry_price=mean(entry_prices),
                            ))

                        bootstrap = None
                        calib_bootstrap = None
                        hurdle_rate = None
                        if observations:
                            values = [o.basket_return for o in observations]
                            clusters = [o.tournament.season_id for o in observations]
                            bootstrap = cluster_bootstrap_ci(values, clusters)
                            win_values = [o.win_rate for o in observations]
                            calib_bootstrap = cluster_bootstrap_ci(win_values, clusters)
                            hurdle_rate = sum(1 for o in observations if o.hurdle_cleared) / len(observations)

                        results.append(GridCellResult(
                            tier=tier,
                            entry_offset_days=entry_offset,
                            n_favorites=n_fav,
                            exit_rule=exit_rule,
                            cost_cents=cost_cents,
                            observations=observations,
                            n_skipped_liquidity=n_skipped_total,
                            n_tournaments_total=len(tournaments),
                            bootstrap=bootstrap,
                            calibration_bootstrap=calib_bootstrap,
                            hurdle_clear_rate=hurdle_rate,
                        ))
    return results


async def run_variant_c_backtest(concurrency: int = FETCH_CONCURRENCY) -> list[GridCellResult]:
    """Top-level entry point: fetches the full universe live, then runs the
    complete declared grid. See decisions/2026-07-18.md for the gate spec."""
    fetched = await fetch_universe(TOURNAMENT_UNIVERSE, concurrency=concurrency)
    return evaluate_grid(fetched)


# ── C2 confirmation test + C2-EXPLORE discovery grid ─────────────────────
# New strategy-space region, registered decisions/2026-07-18.md ("C2 pre-
# registration" entry, logged BEFORE any of this ran) — NOT part of the
# original 216-cell grid above and NOT a re-test of S1_stage/S2_percent/
# baseline_hold. Key differences: full (100%) close on a symmetric TP/SL
# bracket (the original rules never have a stop-loss), a fixed-calendar
# time-exit fallback at regular-season-end instead of resolution, and a
# fixed 6-favorite basket (vs. {1,2,3}).
#
# NFL 2025-26 is the discovery sample that generated this hypothesis (the
# operator's own manual analysis) and is excluded from every confirmation
# statistic below — evaluated only as a separately-labeled, never-pooled
# line via `C2Result.discovery_observations`.

DISCOVERY_SEASON_ID = "NFL-2025"

C2_BASKET_SIZE = 6
C2_TP_PCT = 0.5
C2_SL_PCT = 0.5
C2_ENTRY_OFFSET_DAYS = 1  # T-1, reuses TOURNAMENT_UNIVERSE.season_start

# Regular-season-end / group-stage-end analog, one date per season cluster
# (decisions/2026-07-18.md, "C2_REGULAR_SEASON_END table" — real-world
# public sports-calendar dates, flagged for operator verification, same
# convention as the original season-start table). A season_id absent from
# this dict has no analog and is structurally out of C2 scope (NCAA — pure
# single-elimination from Day 1, no regular season or group stage).
C2_REGULAR_SEASON_END: dict[str, date] = {
    "NFL-2024": date(2025, 1, 5),
    "NFL-2025": date(2026, 1, 4),  # discovery sample only
    "NBA-2024-25": date(2025, 4, 13),
    "NBA-2025-26": date(2026, 4, 12),
    "MLB-2024": date(2024, 9, 29),
    "MLB-2025": date(2025, 9, 28),
    "NHL-2024-25": date(2025, 4, 17),
    "NHL-2025-26": date(2026, 4, 16),
    "UCL-2024-25": date(2025, 1, 29),   # league-phase final matchday, live-corroborated vs. book endDate
    "UEL-2024-25": date(2025, 1, 30),   # league-phase final matchday, live-corroborated vs. book endDate
    "T20WC-2026": date(2026, 2, 19),    # group-stage-end ESTIMATE — LOW CONFIDENCE, see decisions/2026-07-18.md
    "WC-2026": date(2026, 6, 27),        # group-stage-end ESTIMATE (48-team/12-group format, 3 matchdays) —
                                          # LOW CONFIDENCE, same caveat as T20WC-2026; tournament boundary
                                          # itself (final 2026-07-19) IS live-corroborated (Gamma outcomePrices,
                                          # see decisions/2026-07-23.md), only this internal cutover is not.
}

C2_EXPLORE_BASKET_PCTS: list[float] = [0.10, 0.20, 0.30, 0.40]
C2_EXPLORE_TP_PCTS: list[float] = [0.40, 0.50, 0.60]
C2_EXPLORE_SL_PCTS: list[float] = [0.20, 0.30, 0.40, 0.50]


def _c2_eligible_tournaments(discovery_season_id: str = DISCOVERY_SEASON_ID) -> tuple[list[Tournament], list[Tournament]]:
    """Splits TOURNAMENT_UNIVERSE into (confirmation-eligible, discovery-
    sample) for C2/C2-EXPLORE. Tournaments whose season_id has no entry in
    C2_REGULAR_SEASON_END (NCAA — no regular-season/group-stage analog) are
    dropped from both lists entirely, per the gate spec's own exclusion
    clause."""
    confirmation: list[Tournament] = []
    discovery: list[Tournament] = []
    for t in TOURNAMENT_UNIVERSE:
        if t.season_id == discovery_season_id:
            discovery.append(t)
        elif t.season_id in C2_REGULAR_SEASON_END:
            confirmation.append(t)
    return confirmation, discovery


def price_at_time_exit(
    prices: list[tuple[datetime, float]], as_of: datetime, max_staleness_days: int = 7,
    decided_tolerance: float = _SETTLEMENT_TOLERANCE,
) -> float | None:
    """Time-exit price for one leg: `price_at_or_before` first, then a
    fallback to the last point at-or-before `as_of` regardless of
    staleness IF that point is within `decided_tolerance` of 0 or 1.

    Found necessary during the first live C2 run (decisions/2026-07-18.md,
    "C2 results" entry): a favorite that's been mathematically eliminated
    well before regular-season-end typically stops trading — its price sits
    pinned near 0 for weeks, past any reasonable staleness window, then no
    new points ever arrive. That's the exact "trading stops once the
    outcome is obvious" pattern already documented and handled for
    settlement pricing (module docstring, judgment call 3) — a genuinely
    decided price, not missing data, so it gets the same tolerance-band
    treatment here rather than silently dropping the whole basket. A stale
    point NOT near 0/1 is still treated as missing — this fallback only
    fires for prices that are unambiguous either way."""
    p = price_at_or_before(prices, as_of, max_staleness_days=max_staleness_days)
    if p is not None:
        return p
    candidate = None
    for ts, pr in prices:
        if ts <= as_of:
            candidate = pr
        else:
            break
    if candidate is not None and (candidate <= decided_tolerance or candidate >= 1 - decided_tolerance):
        return candidate
    return None


def apply_bracket_exit(
    entry_price: float,
    entry_time: datetime,
    prices_after_entry: list[tuple[datetime, float]],
    time_exit_time: datetime,
    time_exit_price: float | None,
    tp_pct: float = C2_TP_PCT,
    sl_pct: float = C2_SL_PCT,
) -> ExitLeg | None:
    """Full-close TP/SL bracket with a fixed-calendar time-exit fallback —
    the C2 exit family (decisions/2026-07-18.md), distinct from
    `apply_exit_ladder`'s staged/resolution-fallback rules. Scans forward
    through daily closes up to (and including) time_exit_time; the first
    close at or beyond +tp_pct or at/below -sl_pct triggers a full close —
    checked BEFORE `time_exit_price` is ever consulted, so a leg that's
    already resolved (stopped out or took profit) earlier in its holding
    period never needs a price at the time-exit boundary at all. Only if
    neither triggers by time_exit_time does `time_exit_price` get used;
    if that's None (no reliable price at the boundary), returns None —
    the caller drops the whole basket (conservative), not the resolved
    legs too. Fixed during the first live C2 run (decisions/2026-07-18.md,
    "C2 results" entry) after eagerly requiring a time-exit price for
    every leg, even already-resolved ones, cratered the observation count.

    Same-day-both-touchable SL-first tie-break (spec): with this module's
    daily-close series a single price point can never be simultaneously
    `>=TP` and `<=SL` (TP price > SL price for any positive entry price),
    so the tie-break is specified for intraday data this backtest doesn't
    have and cannot actually fire here — see decisions/2026-07-18.md."""
    tp_price = entry_price * (1 + tp_pct)
    sl_price = entry_price * (1 - sl_pct)
    for ts, p in prices_after_entry:
        if ts > time_exit_time:
            break
        if p <= sl_price + 1e-9 or p >= tp_price - 1e-9:
            return ExitLeg(1.0, p, ts)
    if time_exit_price is None:
        return None
    return ExitLeg(1.0, time_exit_price, time_exit_time)


def equal_share_return(entry_prices: list[float], leg_returns: list[float], cost_cents: float) -> float:
    """Portfolio return under equal-SHARE weighting (C2's secondary-
    reported weighting, vs. equal-dollar primary = simple mean of
    leg_returns). Equal share counts mean dollar allocation per leg scales
    with entry price, so the portfolio return is the entry-cost-weighted
    average of the leg returns, not their simple mean."""
    if not entry_prices:
        return 0.0
    cost = cost_cents / 100.0
    weights = [ep + cost for ep in entry_prices]
    total = sum(weights)
    if total <= 0:
        return mean(leg_returns)
    return sum(w * r for w, r in zip(weights, leg_returns)) / total


@dataclass
class C2TournamentObservation:
    tournament: Tournament
    n_favorites_used: int
    field_size: int
    equal_dollar_return_base: float
    equal_dollar_return_stress: float
    equal_share_return_base: float
    equal_share_return_stress: float
    weighted_holding_days: float
    sign_flip_legs: list[str]  # teams whose leg return flips sign between 1c and 2c cost


def _c2_tournament_observation(
    tournament: Tournament,
    series_list: list[TeamSeries],
    basket_size: int,
    tp_pct: float = C2_TP_PCT,
    sl_pct: float = C2_SL_PCT,
) -> C2TournamentObservation | None:
    """One tournament's C2 basket result, or None if it can't be computed
    (field too small for the basket, fewer than `basket_size` candidates
    clear the liquidity gate at T-1, or any picked leg's bracket never
    triggers AND has no reliable price at the time-exit date) — always a
    whole-basket skip, never a partially-filled basket, so the strategy
    definition doesn't silently change leg-by-leg."""
    field_size = len(series_list)
    if field_size < basket_size:
        return None
    valid, _n_skipped_liquidity = _tournament_entry_snapshot(tournament, series_list, entry_offset_days=C2_ENTRY_OFFSET_DAYS)
    if len(valid) < basket_size:
        return None
    picks = valid[:basket_size]

    time_exit_date = C2_REGULAR_SEASON_END.get(tournament.season_id)
    if time_exit_date is None:
        return None
    time_exit_dt = datetime.combine(time_exit_date, datetime.min.time(), tzinfo=timezone.utc)
    entry_date = datetime.combine(tournament.season_start, datetime.min.time(), tzinfo=timezone.utc) - timedelta(days=C2_ENTRY_OFFSET_DAYS)

    entry_prices: list[float] = []
    leg_returns_base: list[float] = []
    leg_returns_stress: list[float] = []
    leg_days: list[float] = []
    sign_flip_legs: list[str] = []
    for ts, entry_price in picks:
        # Only consulted if the bracket doesn't trigger before time_exit_dt
        # (apply_bracket_exit checks SL/TP first) — price_at_time_exit does
        # the ordinary staleness lookup, then a decided-price fallback for
        # favorites that were eliminated (price pinned near 0) and stopped
        # trading well before the time-exit date. See both docstrings.
        time_exit_price = price_at_time_exit(ts.prices, time_exit_dt)
        after = [(dt, p) for dt, p in ts.prices if dt > entry_date]
        leg = apply_bracket_exit(entry_price, entry_date, after, time_exit_dt, time_exit_price, tp_pct=tp_pct, sl_pct=sl_pct)
        if leg is None:
            return None
        ret_base, days = net_return_for_legs(entry_price, [leg], entry_date, cost_cents=1.0)
        ret_stress, _ = net_return_for_legs(entry_price, [leg], entry_date, cost_cents=2.0)
        entry_prices.append(entry_price)
        leg_returns_base.append(ret_base)
        leg_returns_stress.append(ret_stress)
        leg_days.append(days)
        if (ret_base > 0) != (ret_stress > 0):
            sign_flip_legs.append(ts.team)

    return C2TournamentObservation(
        tournament=tournament,
        n_favorites_used=len(picks),
        field_size=field_size,
        equal_dollar_return_base=mean(leg_returns_base),
        equal_dollar_return_stress=mean(leg_returns_stress),
        equal_share_return_base=equal_share_return(entry_prices, leg_returns_base, cost_cents=1.0),
        equal_share_return_stress=equal_share_return(entry_prices, leg_returns_stress, cost_cents=2.0),
        weighted_holding_days=mean(leg_days),
        sign_flip_legs=sign_flip_legs,
    )


def _c2_bootstrap(observations: list[C2TournamentObservation], value_fn) -> BootstrapResult | None:
    if not observations:
        return None
    values = [value_fn(o) for o in observations]
    clusters = [o.tournament.season_id for o in observations]
    return cluster_bootstrap_ci(values, clusters)


@dataclass
class C2Result:
    label: str
    tp_pct: float
    sl_pct: float
    basket_size: int
    observations: list[C2TournamentObservation]
    n_eligible_tournaments: int
    n_skipped: int
    discovery_observations: list[C2TournamentObservation]
    bootstrap_equal_dollar: BootstrapResult | None
    bootstrap_equal_share: BootstrapResult | None

    @property
    def n_sports(self) -> int:
        return len({o.tournament.sport for o in self.observations})


def evaluate_c2(
    fetched: dict[str, list[TeamSeries]],
    basket_size: int = C2_BASKET_SIZE,
    tp_pct: float = C2_TP_PCT,
    sl_pct: float = C2_SL_PCT,
) -> C2Result:
    """THE confirmation test — one cell, the only source of a C2 verdict
    (decisions/2026-07-18.md). Runs on the confirmation universe (NFL
    2025-26 excluded); the discovery sample is evaluated too but reported
    separately, never pooled into the bootstrap."""
    confirmation_tournaments, discovery_tournaments = _c2_eligible_tournaments()

    observations: list[C2TournamentObservation] = []
    n_skipped = 0
    for t in confirmation_tournaments:
        obs = _c2_tournament_observation(t, fetched.get(t.event_slug, []), basket_size, tp_pct, sl_pct)
        if obs is None:
            n_skipped += 1
        else:
            observations.append(obs)

    discovery_observations = [
        obs for t in discovery_tournaments
        if (obs := _c2_tournament_observation(t, fetched.get(t.event_slug, []), basket_size, tp_pct, sl_pct)) is not None
    ]

    return C2Result(
        label=f"top-{basket_size}",
        tp_pct=tp_pct,
        sl_pct=sl_pct,
        basket_size=basket_size,
        observations=observations,
        n_eligible_tournaments=len(confirmation_tournaments),
        n_skipped=n_skipped,
        discovery_observations=discovery_observations,
        bootstrap_equal_dollar=_c2_bootstrap(observations, lambda o: o.equal_dollar_return_base),
        bootstrap_equal_share=_c2_bootstrap(observations, lambda o: o.equal_share_return_base),
    )


@dataclass
class C2ExploreCell:
    basket_pct: float
    tp_pct: float
    sl_pct: float
    observations: list[C2TournamentObservation]
    n_skipped: int
    bootstrap_equal_dollar: BootstrapResult | None
    per_tier: dict[str, tuple[int, int]]  # tier -> (n_observations, n_clusters)


def evaluate_c2_explore(fetched: dict[str, list[TeamSeries]]) -> list[C2ExploreCell]:
    """C2-EXPLORE — the declared 48-cell discovery grid (decisions/
    2026-07-18.md). Runs on the same confirmation universe as C2 (NFL
    2025-26 excluded — including the discovery-sample season here would let
    the same data that produced the C2 hypothesis also drive C3 candidate
    selection, which is circular). Never produces a verdict."""
    confirmation_tournaments, _discovery = _c2_eligible_tournaments()
    cells: list[C2ExploreCell] = []
    for basket_pct in C2_EXPLORE_BASKET_PCTS:
        for tp_pct in C2_EXPLORE_TP_PCTS:
            for sl_pct in C2_EXPLORE_SL_PCTS:
                observations: list[C2TournamentObservation] = []
                n_skipped = 0
                for t in confirmation_tournaments:
                    series_list = fetched.get(t.event_slug, [])
                    field_size = len(series_list)
                    basket_size = max(2, math.ceil(basket_pct * field_size)) if field_size else 2
                    obs = _c2_tournament_observation(t, series_list, basket_size, tp_pct, sl_pct)
                    if obs is None:
                        n_skipped += 1
                    else:
                        observations.append(obs)

                per_tier: dict[str, tuple[int, int]] = {}
                for tier in ("30+", "~15", "4-6"):
                    tier_obs = [o for o in observations if o.tournament.tier == tier]
                    per_tier[tier] = (len(tier_obs), len({o.tournament.season_id for o in tier_obs}))

                cells.append(C2ExploreCell(
                    basket_pct=basket_pct, tp_pct=tp_pct, sl_pct=sl_pct,
                    observations=observations, n_skipped=n_skipped,
                    bootstrap_equal_dollar=_c2_bootstrap(observations, lambda o: o.equal_dollar_return_base),
                    per_tier=per_tier,
                ))
    return cells


def _c2_explore_coord(cell: C2ExploreCell) -> tuple[int, int, int]:
    return (
        C2_EXPLORE_BASKET_PCTS.index(cell.basket_pct),
        C2_EXPLORE_TP_PCTS.index(cell.tp_pct),
        C2_EXPLORE_SL_PCTS.index(cell.sl_pct),
    )


def find_plateaus(cells: list[C2ExploreCell]) -> list[list[C2ExploreCell]]:
    """Connected components of "positive" cells — pooled equal-dollar mean
    net return > 0 with at least one observation — under single-step
    adjacency on exactly one of the three axes (basket_pct/tp_pct/sl_pct
    index +/-1, other two held fixed). Plateau definition fixed in
    decisions/2026-07-18.md before this grid was run, since the operator's
    instruction ("identify PLATEAUS, not peak cells") didn't specify one."""
    def is_positive(c: C2ExploreCell) -> bool:
        return bool(c.observations) and mean(o.equal_dollar_return_base for o in c.observations) > 0

    by_coord = {_c2_explore_coord(c): c for c in cells}
    positive_coords = {coord for coord, c in by_coord.items() if is_positive(c)}

    visited: set[tuple[int, int, int]] = set()
    components: list[list[C2ExploreCell]] = []
    for start in positive_coords:
        if start in visited:
            continue
        stack = [start]
        visited.add(start)
        comp_coords = []
        while stack:
            b, t, s = cur = stack.pop()
            comp_coords.append(cur)
            for n in ((b + 1, t, s), (b - 1, t, s), (b, t + 1, s), (b, t - 1, s), (b, t, s + 1), (b, t, s - 1)):
                if n in positive_coords and n not in visited:
                    visited.add(n)
                    stack.append(n)
        components.append([by_coord[c] for c in comp_coords])
    return components


def select_c2_candidates(cells: list[C2ExploreCell], max_candidates: int = 2) -> list[C2ExploreCell]:
    """At most `max_candidates` plateau centers (decisions/2026-07-18.md):
    the largest connected positive components first (tie-break: highest
    pooled mean return), and within each chosen component the cell with the
    most same-component positive neighbors (tie-break: n_clusters, then
    mean return) — never the single best-performing cell in isolation."""
    components = find_plateaus(cells)
    if not components:
        return []

    def comp_mean(comp: list[C2ExploreCell]) -> float:
        all_obs = [o for c in comp for o in c.observations]
        return mean(o.equal_dollar_return_base for o in all_obs) if all_obs else 0.0

    components.sort(key=lambda comp: (len(comp), comp_mean(comp)), reverse=True)

    candidates: list[C2ExploreCell] = []
    for comp in components[:max_candidates]:
        coord_set = {_c2_explore_coord(c) for c in comp}

        def n_pos_neighbors(c: C2ExploreCell, coord_set=coord_set) -> int:
            b, t, s = _c2_explore_coord(c)
            neighbors = ((b + 1, t, s), (b - 1, t, s), (b, t + 1, s), (b, t - 1, s), (b, t, s + 1), (b, t, s - 1))
            return sum(1 for n in neighbors if n in coord_set)

        def n_clusters(c: C2ExploreCell) -> int:
            return len({o.tournament.season_id for o in c.observations})

        center = max(
            comp,
            key=lambda c: (
                n_pos_neighbors(c),
                n_clusters(c),
                mean(o.equal_dollar_return_base for o in c.observations) if c.observations else 0.0,
            ),
        )
        candidates.append(center)
    return candidates


async def run_c2(concurrency: int = FETCH_CONCURRENCY) -> tuple[C2Result, list[C2ExploreCell]]:
    """Top-level entry point for C2 + C2-EXPLORE: fetches the full universe
    live once, then runs both the confirmation cell and the 48-cell
    discovery grid against it. See decisions/2026-07-18.md."""
    fetched = await fetch_universe(TOURNAMENT_UNIVERSE, concurrency=concurrency)
    c2 = evaluate_c2(fetched)
    explore = evaluate_c2_explore(fetched)
    return c2, explore


# ── C4 — corrected-instrument re-test of C2 ──────────────────────────────
# decisions/2026-07-18.md, "C2 audit follow-up" entries (logged before this
# code ran). Same universe, same rules, same basket composition as C2 —
# only the fill price on a triggered bracket exit and the cost-per-side
# value change. Verdict is final for this parameter set (operator
# instruction: no C5 on the same TP/SL/basket/time-exit rules).

C4_STITCH_WINDOW_DAYS = 14  # safe margin under the empirical 15d-works/18d-fails cutoff (see decisions/)
C4_FIDELITY_MINUTES = 1
C4_P75_SPREAD = 0.01               # measured, book_snapshots on the World Cup 2026 Winner book
C4_MAX_SPREAD = 0.02               # measured, same source
C4_HALF_SPREAD = C4_P75_SPREAD / 2         # 0.005 — TP/SL fill correction + cost-per-side share this base value
C4_SLIPPAGE_ALLOWANCE = C4_MAX_SPREAD - C4_P75_SPREAD  # 0.01 — extra SL-only haircut, see decisions/
C4_COST_PER_SIDE = C4_HALF_SPREAD  # 0.005, replaces C2's flat 0.01 (1c)


def _stitch_windows(start: datetime, end: datetime, window_days: int = C4_STITCH_WINDOW_DAYS) -> list[tuple[datetime, datetime]]:
    """Splits [start, end) into consecutive <=window_days chunks. Pure/
    testable — the network fetch (fetch_fine_series) just calls this and
    issues one request per chunk."""
    windows: list[tuple[datetime, datetime]] = []
    cur = start
    while cur < end:
        nxt = min(cur + timedelta(days=window_days), end)
        windows.append((cur, nxt))
        cur = nxt
    return windows


async def fetch_fine_series(
    client: httpx.AsyncClient, token_id: str, start: datetime, end: datetime, sem: asyncio.Semaphore,
    window_days: int = C4_STITCH_WINDOW_DAYS, fidelity: int = C4_FIDELITY_MINUTES,
) -> list[tuple[datetime, float]]:
    """Stitches consecutive <=window_days startTs/endTs requests at
    `fidelity` minutes to recover genuine fine-grained history for a
    resolved market's full [start, end) span — a single explicit-range
    request hard-fails (empty, any fidelity) beyond ~15-17 days, discovered
    this session (decisions/2026-07-18.md, C2 audit item 1(b)). Concatenates
    and dedupes by timestamp; a chunk that errors contributes nothing rather
    than aborting the whole fetch (same conservative-on-failure convention
    as fetch_tournament_series)."""
    windows = _stitch_windows(start, end, window_days)

    async def _one(w_start: datetime, w_end: datetime) -> list[tuple[datetime, float]]:
        async with sem:
            try:
                resp = await client.get(
                    f"{CLOB_BASE}/prices-history",
                    params={
                        "market": token_id,
                        "startTs": int(w_start.timestamp()),
                        "endTs": int(w_end.timestamp()),
                        "fidelity": fidelity,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception:
                return []
            return [
                (datetime.fromtimestamp(pt["t"], tz=timezone.utc), float(pt["p"]))
                for pt in data.get("history", []) if "t" in pt and "p" in pt
            ]

    chunks = await asyncio.gather(*(_one(a, b) for a, b in windows))
    merged: dict[datetime, float] = {}
    for chunk in chunks:
        for ts, p in chunk:
            merged[ts] = p
    return sorted(merged.items())


@dataclass(frozen=True)
class C4ExitLeg:
    exit_price: float
    exit_time: datetime
    reason: str  # "TP", "SL", "time_exit"


def apply_bracket_exit_c4(
    entry_price: float,
    fine_prices_after_entry: list[tuple[datetime, float]],
    time_exit_time: datetime,
    time_exit_price: float | None,
    tp_pct: float = C2_TP_PCT,
    sl_pct: float = C2_SL_PCT,
    half_spread: float = C4_HALF_SPREAD,
    slippage_allowance: float = C4_SLIPPAGE_ALLOWANCE,
) -> C4ExitLeg | None:
    """C4's corrected-instrument bracket exit (decisions/2026-07-18.md, C4
    pre-registration): scans a FINE-fidelity series (not C2's daily one) for
    the first true crossing, which may fall earlier than a daily series
    could ever detect, and records a realistic fill price instead of
    whatever raw print the coarse series happened to catch:
      TP: fills exactly at the TP level (limit-order convention — a resting
          sell limit fills at its own price when touched, never the later,
          possibly-much-better print C2 recorded).
      SL: fills at `SL_level - half_spread - slippage_allowance` (stop/
          market-order convention — crosses the spread, plus a slippage
          buffer for reacting after the level is breached), rather than
          C2's often-much-worse next-print price.
    Falls back to time_exit_price (unchanged from C2 — out of scope for
    this correction, see decisions/) if neither triggers by time_exit_time."""
    tp_level = entry_price * (1 + tp_pct)
    sl_level = entry_price * (1 - sl_pct)
    for ts, p in fine_prices_after_entry:
        if ts > time_exit_time:
            break
        if p <= sl_level + 1e-9:
            return C4ExitLeg(sl_level - half_spread - slippage_allowance, ts, "SL")
        if p >= tp_level - 1e-9:
            return C4ExitLeg(tp_level, ts, "TP")
    if time_exit_price is None:
        return None
    return C4ExitLeg(time_exit_price, time_exit_time, "time_exit")


@dataclass
class C4LegResult:
    team: str
    token_id: str
    entry_price: float
    entry_date: str
    exit_price: float
    exit_date: str
    reason: str
    ret: float
    days: float


@dataclass
class C4TournamentObservation:
    tournament: Tournament
    legs: list[C4LegResult]
    basket_return: float  # equal-dollar mean of leg returns, C4_COST_PER_SIDE


async def _c4_tournament_observation(
    client: httpx.AsyncClient, t: Tournament, series_list: list[TeamSeries], sem: asyncio.Semaphore,
) -> C4TournamentObservation | None:
    """Same basket (top-6, same liquidity gate, same entry price) as C2's
    _c2_tournament_observation — only the exit price and cost model differ.
    Returns None on the identical conditions C2 would (field/liquidity too
    small for a top-6 basket, or no regular-season-end analog)."""
    valid, _n_skipped = _tournament_entry_snapshot(t, series_list, entry_offset_days=C2_ENTRY_OFFSET_DAYS)
    if len(valid) < C2_BASKET_SIZE:
        return None
    picks = valid[:C2_BASKET_SIZE]

    time_exit_date = C2_REGULAR_SEASON_END.get(t.season_id)
    if time_exit_date is None:
        return None
    time_exit_dt = datetime.combine(time_exit_date, datetime.min.time(), tzinfo=timezone.utc)
    entry_date = datetime.combine(t.season_start, datetime.min.time(), tzinfo=timezone.utc) - timedelta(days=C2_ENTRY_OFFSET_DAYS)

    legs: list[C4LegResult] = []
    for ts, entry_price in picks:
        fine = await fetch_fine_series(client, ts.token_id, entry_date, time_exit_dt, sem)
        after = [(dt, p) for dt, p in fine if dt > entry_date]
        time_exit_price = price_at_time_exit(ts.prices, time_exit_dt)  # daily series — unchanged from C2, out of scope
        c4leg = apply_bracket_exit_c4(entry_price, after, time_exit_dt, time_exit_price)
        if c4leg is None:
            return None  # whole-basket skip, same conservative convention as C2
        ret, days = net_return_for_legs(
            entry_price, [ExitLeg(1.0, c4leg.exit_price, c4leg.exit_time)], entry_date,
            cost_cents=C4_COST_PER_SIDE * 100,
        )
        legs.append(C4LegResult(
            team=ts.team, token_id=ts.token_id, entry_price=entry_price,
            entry_date=entry_date.date().isoformat(), exit_price=c4leg.exit_price,
            exit_date=c4leg.exit_time.date().isoformat(), reason=c4leg.reason,
            ret=ret, days=days,
        ))

    return C4TournamentObservation(tournament=t, legs=legs, basket_return=mean(l.ret for l in legs))


async def run_c4(concurrency: int = FETCH_CONCURRENCY) -> tuple[list[C4TournamentObservation], list[C4TournamentObservation]]:
    """Top-level C4 entry point (decisions/2026-07-18.md pre-registration):
    fetches the daily universe once (for basket ranking, identical to C2),
    then re-derives every confirmation- and discovery-sample leg's exit via
    fine-fidelity fill correction + measured cost. Returns
    (confirmation_observations, discovery_observations)."""
    fetched = await fetch_universe(TOURNAMENT_UNIVERSE, concurrency=concurrency)
    confirmation, discovery = _c2_eligible_tournaments()

    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(timeout=30.0, verify=False, headers={"Accept": "application/json"}) as client:
        conf_results = await asyncio.gather(
            *(_c4_tournament_observation(client, t, fetched.get(t.event_slug, []), sem) for t in confirmation)
        )
        disc_results = await asyncio.gather(
            *(_c4_tournament_observation(client, t, fetched.get(t.event_slug, []), sem) for t in discovery)
        )

    return [r for r in conf_results if r is not None], [r for r in disc_results if r is not None]
