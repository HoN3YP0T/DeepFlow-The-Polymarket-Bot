"""Record what models predicted and how markets actually settled.

Revision ID: c3d4e5f6a7b8
Revises: b1c2d3e4f5a6

Calibration was not unstarted work, it was unstartable: ``signals`` stored a
model probability and nothing in the schema had ever recorded how a market
resolved, so only half of each (prediction, outcome) pair existed. These three
tables are the missing half and the fit that comes out of it.

``predictions`` follows ``market_snapshots``: composite primary key including the
time column, because TimescaleDB refuses to convert a table whose unique indexes
omit the partitioning column, and the conversion runs against a populated table.
It is registered as a hypertable here when the extension is present, on the same
conditional terms as revision b1c2d3e4f5a6.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c3d4e5f6a7b8"
down_revision = "b1c2d3e4f5a6"
branch_labels = None
depends_on = None

_PROB = sa.Numeric(10, 8)

#: Predictions arrive at roughly the rate estimates are sampled, which is far
#: slower than the snapshot stream -- so a wider chunk than snapshots' one day.
CHUNK_INTERVAL = "7 days"


def _timescale_present(connection: sa.Connection) -> bool:
    return bool(
        connection.execute(
            sa.text("select 1 from pg_extension where extname = 'timescaledb'")
        ).scalar()
    )


def upgrade() -> None:
    op.create_table(
        "predictions",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("engine", sa.String(length=32), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("condition_id", sa.String(length=80), nullable=False),
        sa.Column("token_id", sa.String(length=80), nullable=False),
        sa.Column("model_probability", _PROB, nullable=False),
        sa.Column("calibrated_probability", _PROB, nullable=False),
        sa.Column("uncertainty", _PROB, nullable=False),
        sa.Column("horizon_seconds", sa.Integer(), nullable=True),
        sa.Column("predicted_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", "predicted_at"),
    )
    op.create_index("ix_predictions_engine", "predictions", ["engine"])
    op.create_index("ix_predictions_category", "predictions", ["category"])
    op.create_index("ix_predictions_condition_id", "predictions", ["condition_id"])
    op.create_index("ix_predictions_token_id", "predictions", ["token_id"])
    op.create_index("ix_predictions_predicted_at", "predictions", ["predicted_at"])
    op.create_index("ix_prediction_condition_time", "predictions", ["condition_id", "predicted_at"])
    op.create_index("ix_prediction_engine_time", "predictions", ["engine", "predicted_at"])

    op.create_table(
        "market_resolutions",
        sa.Column("condition_id", sa.String(length=80), nullable=False),
        sa.Column("token_id", sa.String(length=80), nullable=False),
        sa.Column("payout", _PROB, nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("was_disputed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("source", sa.String(length=32), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("condition_id", "token_id"),
    )
    op.create_index("ix_market_resolutions_status", "market_resolutions", ["status"])
    op.create_index("ix_market_resolutions_resolved_at", "market_resolutions", ["resolved_at"])

    op.create_table(
        "calibration_fits",
        sa.Column("id", sa.Integer(), sa.Identity(), nullable=False),
        sa.Column("engine", sa.String(length=32), nullable=False),
        sa.Column("knots", sa.JSON(), nullable=False),
        sa.Column("samples", sa.Integer(), nullable=False),
        sa.Column("markets", sa.Integer(), nullable=False),
        sa.Column("brier_before", sa.Numeric(12, 8), nullable=False),
        sa.Column("brier_after", sa.Numeric(12, 8), nullable=False),
        sa.Column("ece_before", sa.Numeric(12, 8), nullable=False),
        sa.Column("ece_after", sa.Numeric(12, 8), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("fitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_calibration_fits_engine", "calibration_fits", ["engine"])
    op.create_index("ix_calibration_fits_active", "calibration_fits", ["active"])
    op.create_index("ix_calibration_fits_fitted_at", "calibration_fits", ["fitted_at"])

    connection = op.get_bind()
    if _timescale_present(connection):
        op.execute(
            sa.text(
                "select create_hypertable("
                " 'predictions', 'predicted_at',"
                " chunk_time_interval => cast(:interval as interval),"
                " migrate_data => true,"
                " if_not_exists => true)"
            ).bindparams(interval=CHUNK_INTERVAL)
        )
    else:
        # Matches revision b1c2d3e4f5a6: plain PostgreSQL is a supported target,
        # and the ordinary indexes above cover the same queries.
        print("timescaledb absent; leaving predictions unpartitioned")


def downgrade() -> None:
    op.drop_table("calibration_fits")
    op.drop_table("market_resolutions")
    op.drop_table("predictions")
