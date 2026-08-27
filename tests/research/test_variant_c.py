from datetime import date, datetime, timedelta, timezone

import pytest

from pathfinder.research.variant_c import (
    C2_EXPLORE_BASKET_PCTS,
    C2_EXPLORE_SL_PCTS,
    C2_EXPLORE_TP_PCTS,
    C2_REGULAR_SEASON_END,
    C4_HALF_SPREAD,
    C4_SLIPPAGE_ALLOWANCE,
    C2ExploreCell,
    C2TournamentObservation,
    C4ExitLeg,
    ExitLeg,
    Tournament,
    TeamSeries,
    apply_bracket_exit,
    apply_bracket_exit_c4,
    apply_exit_ladder,
    equal_share_return,
    find_plateaus,
    first_real_price_date,
    hurdle_return,
    net_return_for_legs,
    price_at_or_before,
    price_at_time_exit,
    resolution_price,
    select_c2_candidates,
    _c2_eligible_tournaments,
    _c2_tournament_observation,
    _stitch_windows,
    _tournament_entry_snapshot,
)

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _series(*day_price_pairs):
    return [(T0 + timedelta(days=d), p) for d, p in day_price_pairs]


def test_first_real_price_date_flat_series_returns_none():
    series = _series((0, 0.20), (1, 0.20), (2, 0.20))
    assert first_real_price_date(series) is None


def test_first_real_price_date_finds_first_sustained_move():
    series = _series((0, 0.20), (1, 0.20), (2, 0.35), (3, 0.40))
    assert first_real_price_date(series) == T0 + timedelta(days=2)


def test_first_real_price_date_single_blip_not_enough():
    # day 2 moves but reverts on day 3 — needs two consecutive moved points
    series = _series((0, 0.20), (1, 0.20), (2, 0.35), (3, 0.201), (4, 0.36), (5, 0.37))
    assert first_real_price_date(series) == T0 + timedelta(days=4)


def test_price_at_or_before_exact_match():
    series = _series((0, 0.20), (2, 0.25), (5, 0.30))
    assert price_at_or_before(series, T0 + timedelta(days=2)) == 0.25


def test_price_at_or_before_interpolates_to_last_prior():
    series = _series((0, 0.20), (2, 0.25), (5, 0.30))
    assert price_at_or_before(series, T0 + timedelta(days=4)) == 0.25


def test_price_at_or_before_too_stale_returns_none():
    series = _series((0, 0.20), (2, 0.25))
    assert price_at_or_before(series, T0 + timedelta(days=10), max_staleness_days=3) is None


def test_price_at_or_before_no_prior_point_returns_none():
    series = _series((5, 0.30))
    assert price_at_or_before(series, T0) is None


def test_resolution_price_is_last_point():
    series = _series((0, 0.20), (10, 0.95), (20, 0.995))
    assert resolution_price(series) == 0.995


def test_resolution_price_empty_series():
    assert resolution_price([]) is None


def test_apply_exit_ladder_baseline_hold_uses_resolution():
    legs = apply_exit_ladder(
        "baseline_hold", entry_price=0.30, entry_time=T0,
        prices_after_entry=_series((1, 0.5)), resolution_time=T0 + timedelta(days=30),
        resolution_price_val=0.99,
    )
    assert legs == [ExitLeg(1.0, 0.99, T0 + timedelta(days=30))]


def test_apply_exit_ladder_s1_stage_both_thresholds_hit():
    after = _series((1, 0.55), (2, 0.62), (3, 0.70), (4, 0.90))
    legs = apply_exit_ladder(
        "S1_stage", entry_price=0.30, entry_time=T0,
        prices_after_entry=after, resolution_time=T0 + timedelta(days=30),
        resolution_price_val=0.99,
    )
    assert len(legs) == 2
    assert legs[0].fraction == 0.5 and legs[0].exit_price == 0.62
    assert legs[1].fraction == 0.5 and legs[1].exit_price == 0.90


def test_apply_exit_ladder_s1_stage_only_first_threshold_hit_rest_to_resolution():
    after = _series((1, 0.55), (2, 0.62), (3, 0.70))
    legs = apply_exit_ladder(
        "S1_stage", entry_price=0.30, entry_time=T0,
        prices_after_entry=after, resolution_time=T0 + timedelta(days=30),
        resolution_price_val=0.0,  # team lost
    )
    assert len(legs) == 2
    assert legs[0].exit_price == 0.62
    assert legs[1].fraction == 0.5 and legs[1].exit_price == 0.0


def test_apply_exit_ladder_s1_stage_never_hit_all_to_resolution():
    after = _series((1, 0.32), (2, 0.35))
    legs = apply_exit_ladder(
        "S1_stage", entry_price=0.30, entry_time=T0,
        prices_after_entry=after, resolution_time=T0 + timedelta(days=30),
        resolution_price_val=0.0,
    )
    assert legs == [ExitLeg(1.0, 0.0, T0 + timedelta(days=30))]


def test_apply_exit_ladder_s2_percent_thresholds_relative_to_entry():
    entry = 0.20
    after = _series((1, 0.24), (2, 0.25), (3, 0.30), (4, 0.32))  # +25%=0.25, +60%=0.32
    legs = apply_exit_ladder(
        "S2_percent", entry_price=entry, entry_time=T0,
        prices_after_entry=after, resolution_time=T0 + timedelta(days=30),
        resolution_price_val=0.99,
    )
    assert len(legs) == 2
    assert legs[0].exit_price == 0.25
    assert legs[1].exit_price == 0.32


def test_apply_exit_ladder_unknown_rule_raises():
    with pytest.raises(ValueError):
        apply_exit_ladder("nonsense", 0.3, T0, [], T0, 0.5)


def test_net_return_for_legs_winning_hold_to_resolution():
    legs = [ExitLeg(1.0, 1.0, T0 + timedelta(days=30))]
    ret, days = net_return_for_legs(entry_price=0.30, legs=legs, entry_time=T0, cost_cents=1.0)
    # net_entry = 0.31, net_exit = 0.99 -> return = (0.99-0.31)/0.31
    assert abs(ret - (0.99 - 0.31) / 0.31) < 1e-9
    assert days == 30


def test_net_return_for_legs_losing_hold_to_resolution():
    legs = [ExitLeg(1.0, 0.0, T0 + timedelta(days=30))]
    ret, _ = net_return_for_legs(entry_price=0.30, legs=legs, entry_time=T0, cost_cents=1.0)
    # net_exit = -0.01 (cost still paid on a worthless exit) -> total loss exceeds 100%
    assert ret < -1.0


def test_net_return_for_legs_staged_weighted_average():
    legs = [ExitLeg(0.5, 0.5, T0 + timedelta(days=5)), ExitLeg(0.5, 1.0, T0 + timedelta(days=30))]
    ret, days = net_return_for_legs(entry_price=0.30, legs=legs, entry_time=T0, cost_cents=0.0)
    leg1 = (0.5 - 0.30) / 0.30
    leg2 = (1.0 - 0.30) / 0.30
    assert abs(ret - (0.5 * leg1 + 0.5 * leg2)) < 1e-9
    assert days == 0.5 * 5 + 0.5 * 30


def test_hurdle_return_zero_days_is_zero():
    assert hurdle_return(0.0) == 0.0


def test_hurdle_return_one_year_matches_annual_rate():
    assert abs(hurdle_return(365.0, annual_rate=0.05) - 0.05) < 1e-9


def test_hurdle_return_positive_for_any_positive_holding():
    assert hurdle_return(10.0) > 0.0


def test_tournament_entry_snapshot_counts_missing_price_data_as_skipped():
    # Regression: a team whose market didn't exist yet at entry_date (no
    # price data at all before entry_date) must count toward n_skipped,
    # not silently vanish — otherwise wide-offset cells (T-30) look
    # artificially cleaner than narrow-offset cells just because more teams
    # aren't listed yet, rather than because liquidity is actually better.
    from datetime import date
    tournament = Tournament("T", "X", "30+", "X-2026", "slug", date(2026, 3, 1))
    # T0 = 2026-01-01; season_start = T0+59; entry_offset=30 -> entry_date = T0+29
    no_data_yet = TeamSeries("A", "tok-a", _series((32, 0.20), (35, 0.25)))  # first point after entry_date
    illiquid_at_entry = TeamSeries("B", "tok-b", _series((0, 0.20), (29, 0.20)))  # flat through entry_date
    liquid_at_entry = TeamSeries("C", "tok-c", _series((0, 0.20), (5, 0.35), (10, 0.40), (29, 0.45)))

    valid, n_skipped = _tournament_entry_snapshot(tournament, [no_data_yet, illiquid_at_entry, liquid_at_entry], entry_offset_days=30)

    assert n_skipped == 2  # both A (no data) and B (flat/illiquid) must be counted
    assert len(valid) == 1
    assert valid[0][0].team == "C"


def test_tournament_entry_snapshot_volume_tiebreak_on_price_tie():
    tournament = Tournament("T", "X", "30+", "X-2026", "slug", date(2026, 3, 1))
    low_vol = TeamSeries("A", "tok-a", _series((0, 0.20), (5, 0.40), (10, 0.41), (57, 0.30)), volume=100.0)
    high_vol = TeamSeries("B", "tok-b", _series((0, 0.20), (5, 0.40), (10, 0.41), (57, 0.30)), volume=500.0)
    valid, _ = _tournament_entry_snapshot(tournament, [low_vol, high_vol], entry_offset_days=1)
    assert [ts.team for ts, _ in valid] == ["B", "A"]  # same price, higher volume ranks first


# ── price_at_time_exit (decided-price fallback) ────────────────────────────


def test_price_at_time_exit_uses_ordinary_lookup_when_fresh():
    series = _series((0, 0.20), (2, 0.25))
    assert price_at_time_exit(series, T0 + timedelta(days=2)) == 0.25


def test_price_at_time_exit_falls_back_to_stale_near_zero_price():
    # last point is 20 days stale (past the default 7-day window) but pinned near 0
    series = _series((0, 0.20), (5, 0.30), (6, 0.0005))
    assert price_at_time_exit(series, T0 + timedelta(days=26)) == 0.0005


def test_price_at_time_exit_falls_back_to_stale_near_one_price():
    series = _series((0, 0.20), (5, 0.70), (6, 0.995))
    assert price_at_time_exit(series, T0 + timedelta(days=26)) == 0.995


def test_price_at_time_exit_does_not_fall_back_for_stale_midrange_price():
    # stale AND ambiguous (not near 0 or 1) -- must stay None, not guessed
    series = _series((0, 0.20), (5, 0.30), (6, 0.45))
    assert price_at_time_exit(series, T0 + timedelta(days=26)) is None


def test_price_at_time_exit_no_prior_point_returns_none():
    series = _series((5, 0.30))
    assert price_at_time_exit(series, T0) is None


# ── C2 bracket exit ───────────────────────────────────────────────────────


def test_apply_bracket_exit_take_profit_triggers_full_close():
    after = _series((1, 0.22), (2, 0.31), (3, 0.10))  # entry 0.20 -> TP=0.30 hit day 2
    leg = apply_bracket_exit(
        entry_price=0.20, entry_time=T0, prices_after_entry=after,
        time_exit_time=T0 + timedelta(days=10), time_exit_price=0.50,
    )
    assert leg == ExitLeg(1.0, 0.31, T0 + timedelta(days=2))


def test_apply_bracket_exit_stop_loss_triggers_full_close():
    after = _series((1, 0.19), (2, 0.09))  # entry 0.20 -> SL=0.10 hit day 2
    leg = apply_bracket_exit(
        entry_price=0.20, entry_time=T0, prices_after_entry=after,
        time_exit_time=T0 + timedelta(days=10), time_exit_price=0.50,
    )
    assert leg == ExitLeg(1.0, 0.09, T0 + timedelta(days=2))


def test_apply_bracket_exit_neither_triggered_falls_to_time_exit():
    after = _series((1, 0.21), (2, 0.22))  # stays inside (0.10, 0.30) the whole time
    leg = apply_bracket_exit(
        entry_price=0.20, entry_time=T0, prices_after_entry=after,
        time_exit_time=T0 + timedelta(days=5), time_exit_price=0.25,
    )
    assert leg == ExitLeg(1.0, 0.25, T0 + timedelta(days=5))


def test_apply_bracket_exit_ignores_prices_after_time_exit():
    # day 6 would trigger TP but is past the time-exit boundary (day 5) -- must be ignored
    after = _series((1, 0.21), (6, 0.35))
    leg = apply_bracket_exit(
        entry_price=0.20, entry_time=T0, prices_after_entry=after,
        time_exit_time=T0 + timedelta(days=5), time_exit_price=0.23,
    )
    assert leg == ExitLeg(1.0, 0.23, T0 + timedelta(days=5))


def test_apply_bracket_exit_custom_tp_sl_pct():
    after = _series((1, 0.26))  # entry 0.20, tp_pct=0.30 -> TP=0.26
    leg = apply_bracket_exit(
        entry_price=0.20, entry_time=T0, prices_after_entry=after,
        time_exit_time=T0 + timedelta(days=10), time_exit_price=0.50,
        tp_pct=0.30, sl_pct=0.20,
    )
    assert leg == ExitLeg(1.0, 0.26, T0 + timedelta(days=1))


def test_apply_bracket_exit_triggers_before_needing_time_exit_price():
    # SL triggers day 2; time_exit_price=None must never be consulted
    after = _series((1, 0.19), (2, 0.09))
    leg = apply_bracket_exit(
        entry_price=0.20, entry_time=T0, prices_after_entry=after,
        time_exit_time=T0 + timedelta(days=10), time_exit_price=None,
    )
    assert leg == ExitLeg(1.0, 0.09, T0 + timedelta(days=2))


def test_apply_bracket_exit_neither_triggered_and_no_time_exit_price_returns_none():
    after = _series((1, 0.21), (2, 0.22))  # stays inside the bracket band
    leg = apply_bracket_exit(
        entry_price=0.20, entry_time=T0, prices_after_entry=after,
        time_exit_time=T0 + timedelta(days=5), time_exit_price=None,
    )
    assert leg is None


# ── equal-share weighting ─────────────────────────────────────────────────


def test_equal_share_return_weights_by_entry_price():
    # weights = entry_prices (cost=0): (0.2*1.0 + 0.8*-0.5) / 1.0 = -0.2
    ret = equal_share_return(entry_prices=[0.2, 0.8], leg_returns=[1.0, -0.5], cost_cents=0.0)
    assert abs(ret - (-0.2)) < 1e-9


def test_equal_share_return_empty_is_zero():
    assert equal_share_return([], [], cost_cents=1.0) == 0.0


# ── _c2_tournament_observation ────────────────────────────────────────────


def test_c2_tournament_observation_field_too_small_returns_none():
    tournament = Tournament("T", "X", "4-6", "TEST-SEASON", "slug", date(2026, 3, 1))
    series = [TeamSeries(f"team{i}", f"tok{i}", _series((0, 0.20), (20, 0.30), (21, 0.30))) for i in range(3)]
    assert _c2_tournament_observation(tournament, series, basket_size=6) is None


def test_c2_tournament_observation_insufficient_liquid_candidates_returns_none():
    tournament = Tournament("T", "X", "30+", "TEST-SEASON", "slug", date(2026, 3, 1))
    liquid = [
        TeamSeries(f"liquid{i}", f"tok{i}", _series((0, 0.05), (20, 0.20), (21, 0.20), (58, 0.20)))
        for i in range(5)
    ]
    illiquid = TeamSeries("flat", "tok-flat", _series((0, 0.20), (58, 0.20)))  # never clears liquidity gate
    assert _c2_tournament_observation(tournament, liquid + [illiquid], basket_size=6) is None


def test_c2_tournament_observation_missing_time_exit_price_returns_none(monkeypatch):
    monkeypatch.setitem(C2_REGULAR_SEASON_END, "TEST-SEASON", date(2026, 3, 10))
    tournament = Tournament("T", "X", "30+", "TEST-SEASON", "slug", date(2026, 3, 1))
    # price series ends day 58 (entry date itself) -- nothing at/near time-exit (day 68)
    series = [TeamSeries("A", "tok-a", _series((0, 0.05), (20, 0.20), (21, 0.20), (58, 0.20)))]
    assert _c2_tournament_observation(tournament, series, basket_size=1) is None


def test_c2_tournament_observation_no_time_exit_analog_returns_none():
    tournament = Tournament("T", "X", "30+", "NCAA-2025", "slug", date(2026, 3, 1))
    series = [TeamSeries("A", "tok-a", _series((0, 0.05), (20, 0.20), (21, 0.20), (58, 0.20), (68, 0.25)))]
    assert _c2_tournament_observation(tournament, series, basket_size=1) is None


def test_c2_tournament_observation_recovers_eliminated_leg_via_decided_price(monkeypatch):
    monkeypatch.setitem(C2_REGULAR_SEASON_END, "TEST-SEASON", date(2026, 3, 10))
    tournament = Tournament("T", "X", "30+", "TEST-SEASON", "slug", date(2026, 3, 1))
    # entry=0.20 (day58); eliminated shortly after entry (day60, price crashes to 0.0005)
    # and never trades again -- 12+ days stale by the time-exit (day68), but decided.
    series = [TeamSeries(
        "Eliminated", "tok-a",
        _series((0, 0.05), (10, 0.05), (20, 0.20), (21, 0.20), (58, 0.20), (60, 0.0005)),
    )]
    obs = _c2_tournament_observation(tournament, series, basket_size=1)
    assert obs is not None  # would have been None without the decided-price fallback
    # SL triggers day 60 itself (0.0005 <= entry*0.5=0.10) before ever reaching the time-exit fallback
    assert obs.equal_dollar_return_base < -0.9


def test_c2_tournament_observation_computes_returns_and_flags_cost_sign_flip(monkeypatch):
    monkeypatch.setitem(C2_REGULAR_SEASON_END, "TEST-SEASON", date(2026, 3, 10))
    tournament = Tournament("T", "X", "30+", "TEST-SEASON", "slug", date(2026, 3, 1))
    # entry=0.20 (day58); stays inside the (0.10, 0.30) bracket; time-exit (day68) price=0.23
    series = [TeamSeries(
        "A", "tok-a",
        _series((0, 0.05), (10, 0.05), (20, 0.20), (21, 0.20), (58, 0.20), (60, 0.22), (65, 0.21), (68, 0.23)),
    )]
    obs = _c2_tournament_observation(tournament, series, basket_size=1)
    assert obs is not None
    assert obs.n_favorites_used == 1
    assert obs.field_size == 1
    # net_entry_base=0.21, net_exit_base=0.22 -> ret=+0.0476...
    assert abs(obs.equal_dollar_return_base - (0.22 - 0.21) / 0.21) < 1e-9
    # net_entry_stress=0.22, net_exit_stress=0.21 -> ret=-0.0455...
    assert abs(obs.equal_dollar_return_stress - (0.21 - 0.22) / 0.22) < 1e-9
    assert obs.sign_flip_legs == ["A"]


# ── C2 confirmation vs. discovery split ───────────────────────────────────


def test_c2_eligible_tournaments_excludes_ncaa_and_splits_discovery():
    confirmation, discovery = _c2_eligible_tournaments()
    confirmation_season_ids = {t.season_id for t in confirmation}
    discovery_season_ids = {t.season_id for t in discovery}
    assert "NCAA-2025" not in confirmation_season_ids
    assert "NCAA-2026" not in confirmation_season_ids
    assert discovery_season_ids == {"NFL-2025"}
    assert confirmation_season_ids.isdisjoint(discovery_season_ids)
    # every confirmation-eligible tournament must resolve to a real time-exit date
    assert all(t.season_id in C2_REGULAR_SEASON_END for t in confirmation)


# ── plateau detection ──────────────────────────────────────────────────────


def _mk_obs(season_id: str, ret: float) -> C2TournamentObservation:
    tournament = Tournament("T", "X", "30+", season_id, "slug", date(2026, 3, 1))
    return C2TournamentObservation(
        tournament=tournament, n_favorites_used=6, field_size=6,
        equal_dollar_return_base=ret, equal_dollar_return_stress=ret,
        equal_share_return_base=ret, equal_share_return_stress=ret,
        weighted_holding_days=30.0, sign_flip_legs=[],
    )


def _mk_cell(basket_pct: float, tp_pct: float, sl_pct: float, ret: float | None, season_id: str = "S") -> C2ExploreCell:
    observations = [] if ret is None else [_mk_obs(season_id, ret)]
    return C2ExploreCell(
        basket_pct=basket_pct, tp_pct=tp_pct, sl_pct=sl_pct,
        observations=observations, n_skipped=0, bootstrap_equal_dollar=None, per_tier={},
    )


def test_find_plateaus_connects_adjacent_positive_cells_and_isolates_lone_positive():
    cell_a = _mk_cell(0.10, 0.40, 0.20, ret=0.05, season_id="A")
    cell_b = _mk_cell(0.10, 0.50, 0.20, ret=0.08, season_id="B")  # adjacent to A on the tp axis
    cell_c = _mk_cell(0.40, 0.60, 0.50, ret=0.20, season_id="C")  # far away, isolated
    cell_d = _mk_cell(0.20, 0.40, 0.30, ret=-0.05, season_id="D")  # negative, excluded

    components = find_plateaus([cell_a, cell_b, cell_c, cell_d])

    sizes = sorted(len(c) for c in components)
    assert sizes == [1, 2]
    all_cells_in_components = {(c.basket_pct, c.tp_pct, c.sl_pct) for comp in components for c in comp}
    assert (0.20, 0.40, 0.30) not in all_cells_in_components  # negative cell never appears


def test_select_c2_candidates_picks_largest_plateau_then_isolated_positive():
    cell_a = _mk_cell(0.10, 0.40, 0.20, ret=0.05, season_id="A")
    cell_b = _mk_cell(0.10, 0.50, 0.20, ret=0.08, season_id="B")
    cell_c = _mk_cell(0.40, 0.60, 0.50, ret=0.20, season_id="C")

    candidates = select_c2_candidates([cell_a, cell_b, cell_c], max_candidates=2)

    assert len(candidates) == 2
    coords = {(c.basket_pct, c.tp_pct, c.sl_pct) for c in candidates}
    assert (0.40, 0.60, 0.50) in coords  # the isolated positive cell must be one of the 2 centers
    assert coords & {(0.10, 0.40, 0.20), (0.10, 0.50, 0.20)}  # one of the connected pair is the other


def test_select_c2_candidates_empty_grid_returns_nothing():
    assert select_c2_candidates([], max_candidates=2) == []


# ── C4: window stitching ────────────────────────────────────────────────


def test_stitch_windows_short_span_single_window():
    start = T0
    end = T0 + timedelta(days=5)
    assert _stitch_windows(start, end, window_days=14) == [(start, end)]


def test_stitch_windows_exact_multiple():
    start = T0
    end = T0 + timedelta(days=28)
    windows = _stitch_windows(start, end, window_days=14)
    assert windows == [(start, start + timedelta(days=14)), (start + timedelta(days=14), end)]


def test_stitch_windows_covers_full_span_with_no_gaps_or_overlaps():
    start = T0
    end = T0 + timedelta(days=47)
    windows = _stitch_windows(start, end, window_days=14)
    assert windows[0][0] == start
    assert windows[-1][1] == end
    for (a_start, a_end), (b_start, b_end) in zip(windows, windows[1:]):
        assert a_end == b_start  # contiguous, no gap or overlap
    assert all((w_end - w_start) <= timedelta(days=14) for w_start, w_end in windows)


def test_stitch_windows_empty_span():
    assert _stitch_windows(T0, T0, window_days=14) == []


# ── C4: bracket exit fill correction ──────────────────────────────────────


def test_apply_bracket_exit_c4_tp_fills_exactly_at_level_not_the_overshoot_print():
    # entry 0.20 -> TP level 0.30; the actual print that crosses it is 0.42 (like the
    # MLB Mets case) -- C4 must record the fill AT 0.30, not the overshoot print.
    after = _series((1, 0.28), (2, 0.42))
    leg = apply_bracket_exit_c4(entry_price=0.20, fine_prices_after_entry=after,
                                 time_exit_time=T0 + timedelta(days=10), time_exit_price=0.50)
    assert abs(leg.exit_price - 0.30) < 1e-9 and leg.exit_time == T0 + timedelta(days=2) and leg.reason == "TP"


def test_apply_bracket_exit_c4_sl_fills_below_level_by_half_spread_plus_slippage():
    # entry 0.20 -> SL level 0.10; actual print crossing it is 0.03 (like Baltimore) --
    # C4 fills at SL_level - half_spread - slippage_allowance, not the crashed print.
    after = _series((1, 0.15), (2, 0.03))
    leg = apply_bracket_exit_c4(entry_price=0.20, fine_prices_after_entry=after,
                                 time_exit_time=T0 + timedelta(days=10), time_exit_price=0.50)
    expected_fill = 0.10 - C4_HALF_SPREAD - C4_SLIPPAGE_ALLOWANCE
    assert leg == C4ExitLeg(expected_fill, T0 + timedelta(days=2), "SL")


def test_apply_bracket_exit_c4_neither_triggered_falls_to_time_exit_price_unchanged():
    after = _series((1, 0.21), (2, 0.22))
    leg = apply_bracket_exit_c4(entry_price=0.20, fine_prices_after_entry=after,
                                 time_exit_time=T0 + timedelta(days=5), time_exit_price=0.25)
    assert leg == C4ExitLeg(0.25, T0 + timedelta(days=5), "time_exit")


def test_apply_bracket_exit_c4_no_trigger_and_no_time_exit_price_returns_none():
    after = _series((1, 0.21), (2, 0.22))
    leg = apply_bracket_exit_c4(entry_price=0.20, fine_prices_after_entry=after,
                                 time_exit_time=T0 + timedelta(days=5), time_exit_price=None)
    assert leg is None


def test_apply_bracket_exit_c4_detects_earlier_crossing_than_coarse_data_could():
    # a fine series can show a crossing on day 1 that a daily series sampled
    # only every 2 days would have missed entirely until day 2 or later.
    after = _series((0.5, 0.29), (1, 0.31), (2, 0.35))  # crosses TP=0.30 at t=1 (fractional day ok)
    leg = apply_bracket_exit_c4(entry_price=0.20, fine_prices_after_entry=after,
                                 time_exit_time=T0 + timedelta(days=10), time_exit_price=0.50)
    assert leg.exit_time == T0 + timedelta(days=1)
    assert leg.reason == "TP"


def test_apply_bracket_exit_c4_ignores_prices_after_time_exit():
    after = _series((1, 0.21), (6, 0.35))  # day 6 would trigger TP but is past the boundary
    leg = apply_bracket_exit_c4(entry_price=0.20, fine_prices_after_entry=after,
                                 time_exit_time=T0 + timedelta(days=5), time_exit_price=0.23)
    assert leg == C4ExitLeg(0.23, T0 + timedelta(days=5), "time_exit")
