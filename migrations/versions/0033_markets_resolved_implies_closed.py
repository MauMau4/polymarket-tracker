"""markets_resolved_implies_closed

Root cause (decisions/2026-07-23.md, issue 2): several `run_force_resolve_*`
one-off scripts (app/tasks/run_force_resolve_4_markets_20260630.py and
siblings) force-close a wallet's stale open position by writing directly to
`Market.resolved` / `Market.resolution` / `Market.active` via raw ORM
attribute assignment — bypassing `_upsert_market()`
(app/services/discovery/refresh.py), the single chokepoint that has always
correctly enforced `resolved ⇒ closed` (insert branch ~line 84, update branch
~line 100-101). Those scripts never set `closed` or `resolution_checked_at`
at all, so every market they touch lands in exactly the impossible state
that surfaced 4 World Cup Winner markets (Colombia/Germany/Netherlands/
Portugal) resolved 'Yes' in a book that already has a real winner (Spain).
Live count at migration time: 34 existing `markets` rows violate this
invariant, all traceable to the same script family (individual-match O/U
and Team-to-Advance markets from the same force-resolve batches, plus the 4
World Cup Winner rows this session corrects).

Fix: a DB-level CHECK constraint is the right chokepoint precisely because
the bug's root cause was code bypassing the ORM-level chokepoint — a
constraint can't be bypassed by any future direct-write script, raw SQL, or
another as-yet-unwritten code path the way `_upsert_market`'s own internal
logic can be. Added NOT VALID: enforces the invariant on every INSERT/UPDATE
from this point forward without requiring the 34 pre-existing violating rows
to be cleaned up first (that's a separate, much larger cleanup — same script
family, unrelated wallets/markets — explicitly out of scope for this fix,
which only corrects the 4 World Cup Winner rows). Once that larger cleanup
happens, `ALTER TABLE markets VALIDATE CONSTRAINT
ck_markets_resolved_implies_closed;` can retroactively validate the rest
without re-taking a table lock (NOT VALID + VALIDATE CONSTRAINT is the
standard low-downtime pattern for adding a constraint to a table with
existing violations).

Revision ID: 0033
Revises: 0032
Create Date: 2026-07-23 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0033"
down_revision: Union[str, None] = "0032"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE markets
        ADD CONSTRAINT ck_markets_resolved_implies_closed
        CHECK (NOT resolved OR closed)
        NOT VALID;
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE markets DROP CONSTRAINT IF EXISTS ck_markets_resolved_implies_closed;")
