"""outcome_result_stamped_resolution

Root cause (decisions/2026-08-06.md, punch list item 0): trades.outcome_result
and wallet_closed_positions rows are stamped once, at first resolution, from
whatever markets.resolution holds at that instant. Nothing ever revisits them
if markets.resolution is later corrected — by the canonical resolver, by a
second write path (run_market_resolver.py / run_market_status_refresh.py, both
of which set resolved/resolution/closed without going through
resolution.py::_sync_resolved_markets), or by a one-off fix script. The
07-23 World Cup false-Yes fix hit exactly this: it corrected markets.resolution
but the already-stamped trades never got revisited, freezing 15,189 trades
across 9 markets inverted (V4 in scripts/verify_worldcup_outcome_result.sql).

Fix (app/services/markets/resolution.py, this migration's paired code change):
_process_market_resolution now re-reads markets.resolution fresh at write time
instead of trusting a resolution string captured earlier in the same job run
(closes a TOCTOU race — the trades bulk UPDATE and the WCP exit_price
computation were both driven by the same stale parameter), AND the job's
candidate query now triggers on markets.resolution changing for an
already-resolved market, not just on trades.outcome_result being NULL.

outcome_result_stamped_resolution tracks the resolution value last used to
stamp this market's trades. A market is a re-stamp candidate when
`resolution IS DISTINCT FROM outcome_result_stamped_resolution` — true once
for every first-time resolution, and true again only if resolution later
changes. This keeps detection an O(markets table) indexed lookup every
30-minute cycle, not an O(trades table) scan: in steady state zero markets
match, and a correction event bounds the resulting trades UPDATE to that one
market's rows, never the full table.

Backfilled to the CURRENT resolution for every already-resolved, already-
closed market (same NOT VALID-style "invariant from now on" precedent as
migration 0033) so existing violations — the 9 known markets and anything
else — are NOT auto-corrected by this migration or by the standing job on
next deploy.

Excludes the 34 `resolved=True, closed=False` rows migration 0033 already
identified as a known, out-of-scope violation (item 13,
docs/open-items-punchlist.md P3): Postgres re-checks a NOT VALID constraint
on ANY UPDATE to an already-violating row, not just ones touching the
constrained columns, so a blanket backfill UPDATE fails against them
(confirmed directly — IntegrityError on France vs. Morocco: Team to Advance,
market_id 2805541). The paired code change mirrors this exclusion in
_find_markets_needing_restamp, so the standing job never attempts to write to
these rows either. They remain exactly as flagged in item 13 — "fix when
next touched" — untouched by this session. Per
this session's binding constraint ("every write must be preceded by a dry-run
count the operator approves"), those are remediated explicitly by the
operator-approved one-off script
(app/tasks/run_fix_outcome_result_stamp_20260806.py), which also updates this
column for the markets it fixes. Only resolution changes happening AFTER this
migration trigger the standing job's new correction path.

Revision ID: 0034
Revises: 0033
Create Date: 2026-08-06 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0034"
down_revision: Union[str, None] = "0033"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "markets",
        sa.Column("outcome_result_stamped_resolution", sa.String(), nullable=True),
    )
    op.execute(
        """
        UPDATE markets
        SET outcome_result_stamped_resolution = resolution
        WHERE resolved = true AND resolution IS NOT NULL AND closed = true
        """
    )
    op.execute(
        """
        CREATE INDEX ix_markets_restamp_pending
        ON markets (market_id)
        WHERE resolved = true
          AND closed = true
          AND resolution IS NOT NULL
          AND (resolution IS DISTINCT FROM outcome_result_stamped_resolution)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_markets_restamp_pending;")
    op.drop_column("markets", "outcome_result_stamped_resolution")
