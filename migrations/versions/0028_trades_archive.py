"""trades_archive

Requirement: retention (app/services/maintenance/retention.py) currently
DELETEs trades older than 30 days once their market resolves. That's about
to start engaging at scale now that the resolver is fixed (07-11), and the
84 days of trade history currently sitting in `trades` is irreplaceable for
Pathfinder's event study. This adds an archive-on-purge target instead of a
delete-only purge.

trades_archive mirrors trades' columns, partitioned by month on `ts` so old
partitions can later be detached/exported (disk pressure, laptop move).
Minimal indexes only — (wallet, ts) and (market_id, ts) — this table is
write-mostly (purge job, one-time seed) and read by Pathfinder research
code via the trades_full view, never by a tracker hot path.

No primary key / uniqueness constraint by design (matches the "minimal
indexes only" requirement); writers are responsible for not double-inserting
(NOT EXISTS guard in the seed script and the updated purge job).

Revision ID: 0028
Revises: 0027
Create Date: 2026-07-17 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0028"
down_revision: Union[str, None] = "0027"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Covers the current trade history (min ts 2026-04-25) plus a few months of
# headroom. New partitions beyond this are created on demand by
# retention.ensure_archive_partition() — see app/services/maintenance/retention.py.
_PARTITIONS = [
    ("2026_04", "2026-04-01", "2026-05-01"),
    ("2026_05", "2026-05-01", "2026-06-01"),
    ("2026_06", "2026-06-01", "2026-07-01"),
    ("2026_07", "2026-07-01", "2026-08-01"),
    ("2026_08", "2026-08-01", "2026-09-01"),
    ("2026_09", "2026-09-01", "2026-10-01"),
]


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE trades_archive (
            id                  VARCHAR NOT NULL,
            external_trade_id   VARCHAR,
            ts                  TIMESTAMPTZ NOT NULL,
            market_id           VARCHAR NOT NULL,
            asset_id            VARCHAR NOT NULL,
            outcome             VARCHAR,
            price               NUMERIC(18,6),
            size                NUMERIC(18,6),
            notional_usd        NUMERIC(18,2),
            side                VARCHAR,
            wallet              VARCHAR,
            tx_hash             VARCHAR,
            attribution_status  attribution_status_enum NOT NULL DEFAULT 'unresolved',
            source              VARCHAR NOT NULL,
            raw_payload         JSONB NOT NULL DEFAULT '{}'::jsonb,
            trade_type          VARCHAR,
            outcome_result      SMALLINT,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            archived_at         TIMESTAMPTZ NOT NULL DEFAULT now()
        ) PARTITION BY RANGE (ts);
        """
    )

    for suffix, start, end in _PARTITIONS:
        op.execute(
            f"CREATE TABLE trades_archive_{suffix} PARTITION OF trades_archive "
            f"FOR VALUES FROM ('{start}') TO ('{end}');"
        )

    # CREATE INDEX on the partitioned parent auto-propagates to every partition.
    op.execute("CREATE INDEX ix_trades_archive_wallet_ts ON trades_archive (wallet, ts);")
    op.execute("CREATE INDEX ix_trades_archive_market_id_ts ON trades_archive (market_id, ts);")

    op.execute(
        """
        CREATE VIEW trades_full AS
        SELECT id, external_trade_id, ts, market_id, asset_id, outcome, price, size,
               notional_usd, side, wallet, tx_hash, attribution_status, source,
               raw_payload, trade_type, outcome_result, created_at
        FROM trades
        UNION ALL
        SELECT id, external_trade_id, ts, market_id, asset_id, outcome, price, size,
               notional_usd, side, wallet, tx_hash, attribution_status, source,
               raw_payload, trade_type, outcome_result, created_at
        FROM trades_archive;
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS trades_full;")
    op.execute("DROP TABLE IF EXISTS trades_archive;")
