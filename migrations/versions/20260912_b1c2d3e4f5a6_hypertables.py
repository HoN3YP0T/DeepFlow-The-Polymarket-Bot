"""Convert the time-series tables to TimescaleDB hypertables, when available.

Revision ID: b1c2d3e4f5a6
Revises: a7a9d2e99c12

Conditional on purpose. Production runs ``timescale/timescaledb:latest-pg16``, but
development, CI and this container run plain PostgreSQL, where the extension does
not exist. A migration that hard-required it would make the schema unrunnable
everywhere except production -- which is the one place you least want the first
attempt to happen.

So: create the extension if it is installable, convert if it is present, and log
and continue if not. The tables are correct either way; only the partitioning is
absent, and ordinary indexes cover the same queries at development volumes.

The composite primary keys this depends on -- ``(id, captured_at)`` and
``(id, observed_at)`` -- are set in the initial migration rather than here.
TimescaleDB refuses to convert a table whose unique indexes omit the partitioning
column, and by the time a conversion runs the table has data in it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b1c2d3e4f5a6"
down_revision = "a7a9d2e99c12"
branch_labels = None
depends_on = None

#: (table, time column, chunk interval). Chunk sizing follows Timescale's own
#: guidance: aim for chunks that fit comfortably in memory. Snapshots arrive far
#: faster than wallet events, so they get the shorter interval.
HYPERTABLES = (
    ("market_snapshots", "captured_at", "1 day"),
    ("smart_money_events", "observed_at", "7 days"),
)


def _timescale_available(connection: sa.Connection) -> bool:
    return bool(
        connection.execute(
            sa.text("select 1 from pg_available_extensions where name = 'timescaledb'")
        ).scalar()
    )


def upgrade() -> None:
    connection = op.get_bind()

    if not _timescale_available(connection):
        # Not an error. Plain PostgreSQL is a supported target for development
        # and CI; the tables and their indexes already exist from the previous
        # revision.
        print("timescaledb unavailable; leaving time-series tables unpartitioned")
        return

    op.execute("create extension if not exists timescaledb cascade")

    for table, time_column, interval in HYPERTABLES:
        # migrate_data moves the rows that already exist into chunks. Without it,
        # converting a populated table fails rather than partitioning silently.
        op.execute(
            sa.text(
                "select create_hypertable("
                " :table, :time_column,"
                " chunk_time_interval => cast(:interval as interval),"
                " migrate_data => true,"
                " if_not_exists => true)"
            ).bindparams(table=table, time_column=time_column, interval=interval)
        )


def downgrade() -> None:
    # A hypertable cannot be turned back into a plain table in place. Undoing
    # this means recreating the table and copying rows, which is a data-migration
    # decision rather than something a downgrade should do unattended -- doing it
    # silently would risk dropping chunks the operator still needs.
    print("hypertable conversion is not reversible in place; no action taken")
