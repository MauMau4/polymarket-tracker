"""CLI for Pathfinder event-study research (architecture §3.3, FR-3).

Invocation: `python -m pathfinder.research.cli <command> ...` — argparse,
matching pathfinder/clusters/cli.py's precedent (no click/typer dependency).
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from app.db.session import get_session_factory
from app.logging import setup_logging
from pathfinder.config import get_config
from pathfinder.research.gate1 import run_gate1
from pathfinder.research.report import render_event_study_report
from pathfinder.research.signals import enumerate_signals, materialize_signals

_REPORTS_DIR = Path(__file__).resolve().parent.parent.parent / "reports"


async def enumerate_cmd(materialize: bool) -> None:
    setup_logging()
    config = get_config()
    factory = get_session_factory()

    async with factory() as session:
        result = await enumerate_signals(session, factory, config)
        if materialize:
            await materialize_signals(session, result.signals)
            await session.commit()

    f = result.funnel
    print("=== Signal enumeration funnel (config_version={}) ===".format(config.config_version))
    print(f"  raw crossings (net notional >= ${config.signal.signal_min_notional_usd:,.0f} "
          f"in {config.signal.accumulation_window_minutes}min, rising edge only): {f.raw_crossings:,}")
    print(f"  after price band [{config.signal.price_floor}, {config.signal.price_ceiling}]: {f.after_price_band:,}")
    print(f"  after >= {config.signal.min_hours_to_resolution}h to scheduled resolution: {f.after_time_to_resolution:,}")
    print(f"  after trailing {config.signal.volume_window_hours}h market volume "
          f">= ${config.signal.volume_floor_usd:,.0f}: {f.after_volume_floor:,}")
    print(f"  after point-in-time wallet qualification (as_of=detected_time): {f.after_qualification:,}")
    print(f"  distinct wallets (final): {f.distinct_wallets_final:,}")
    print(f"  distinct markets (final): {f.distinct_markets_final:,}")
    print("  NOTE: no UMA-active-dispute filter applied — that data isn't ingested "
          "anywhere in this schema (docs/m1-schema-audit.md Q4); every count above "
          "is 'passes every §5.2 filter we can currently check'.")
    if materialize:
        print(f"  materialized {len(result.signals)} rows into signals (config_version={config.config_version})")


async def gate1_cmd(n_boot: int) -> None:
    setup_logging()
    config = get_config()
    factory = get_session_factory()

    async with factory() as session:
        result = await run_gate1(session, factory, config, n_boot=n_boot)

    report_md = render_event_study_report(result, config)
    _REPORTS_DIR.mkdir(exist_ok=True)
    out_path = _REPORTS_DIR / f"event_study_{config.config_version}.md"
    out_path.write_text(report_md, encoding="utf-8")

    print(f"Gate 1 verdict: {'PASS' if result.verdict.passed else 'FAIL'}")
    for r in result.verdict.reasons:
        print(f"  - {r}")
    print(f"Report written to {out_path}")


async def _dispatch(args: argparse.Namespace) -> None:
    if args.command == "enumerate":
        await enumerate_cmd(materialize=not args.dry_run)
    elif args.command == "gate1":
        await gate1_cmd(n_boot=args.n_boot)


def main() -> None:
    parser = argparse.ArgumentParser(prog="pathfinder-research")
    sub = parser.add_subparsers(dest="command", required=True)

    p_enum = sub.add_parser("enumerate", help="Run signal enumeration (FR-3) and report funnel counts")
    p_enum.add_argument("--dry-run", action="store_true", help="Report counts without writing to the signals table")

    p_gate1 = sub.add_parser("gate1", help="Run Gate 1 (markouts, matched controls, cluster bootstrap) and render the report")
    p_gate1.add_argument("--n-boot", type=int, default=2000, help="Bootstrap resample count (default 2000)")

    args = parser.parse_args()

    if sys.platform == "win32":
        asyncio.run(_dispatch(args), loop_factory=asyncio.SelectorEventLoop)
    else:
        asyncio.run(_dispatch(args))


if __name__ == "__main__":
    main()
