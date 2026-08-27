"""resolution_checked_at

Fixes a resolver blind spot (decisions/2026-07-17.md): _sync_resolved_markets
only ever considered candidates with end_date < now, so any market that
resolves before its nominal end_date — "will X win the tournament/election"
outright-winner markets chief among them, where end_date is the event's
overall conclusion, not the date a given entrant's fate is decided — was
never checked against Gamma at all, regardless of age. Sample of 40
unresolved markets with a future end_date: 2/40 (5%) already closed on
Gamma. Scaled to the ~3,378 markets in that state, this is a real,
ongoing gap, not a one-off.

resolution_checked_at lets the resolver round-robin-sweep the
not-yet-due population (ORDER BY resolution_checked_at ASC NULLS FIRST)
without re-scanning the same markets every 30-minute run.

Revision ID: 0029
Revises: 0028
Create Date: 2026-07-17 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0029"
down_revision: Union[str, None] = "0028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "markets",
        sa.Column("resolution_checked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        """
        CREATE INDEX ix_markets_unresolved_future_end_date_checked
        ON markets (resolution_checked_at)
        WHERE resolved = false AND active = true
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_markets_unresolved_future_end_date_checked;")
    op.drop_column("markets", "resolution_checked_at")
