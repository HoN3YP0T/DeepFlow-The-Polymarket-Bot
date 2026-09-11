"""ORM schema.

Design notes:

* Time-series tables (``market_snapshots``, ``smart_money_events``) are the
  TimescaleDB hypertable candidates -- see ``migrations/`` for the conversion.
* ``orders.client_key`` is UNIQUE. Idempotency is enforced by the database, not
  by application-level checking, because the check-then-act version has a race
  that produces exactly the duplicate order it was meant to prevent.
* Money columns are ``Numeric``, never float.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

_MONEY = Numeric(20, 8)
_PROB = Numeric(10, 8)


class Base(DeclarativeBase):
    pass


class MarketRow(Base):
    __tablename__ = "markets"

    condition_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    event_id: Mapped[str | None] = mapped_column(String(80), index=True)
    question: Mapped[str] = mapped_column(Text)
    slug: Mapped[str | None] = mapped_column(String(255))
    category: Mapped[str] = mapped_column(String(32), index=True)
    classification_confidence: Mapped[Decimal | None] = mapped_column(_PROB)
    resolution_validity: Mapped[str] = mapped_column(String(32), index=True)
    resolution_criteria: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    lifecycle_state: Mapped[str] = mapped_column(String(32), index=True)
    outcomes: Mapped[list[Any] | None] = mapped_column(JSON)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    closed: Mapped[bool] = mapped_column(Boolean, default=False)
    end_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MarketSnapshotRow(Base):
    """Time series. Hypertable on ``captured_at``."""

    __tablename__ = "market_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    condition_id: Mapped[str] = mapped_column(String(80), index=True)
    token_id: Mapped[str] = mapped_column(String(80), index=True)
    best_bid: Mapped[Decimal | None] = mapped_column(_MONEY)
    best_ask: Mapped[Decimal | None] = mapped_column(_MONEY)
    mid: Mapped[Decimal | None] = mapped_column(_MONEY)
    spread: Mapped[Decimal | None] = mapped_column(_MONEY)
    liquidity: Mapped[Decimal | None] = mapped_column(_MONEY)
    volume_24h: Mapped[Decimal | None] = mapped_column(_MONEY)
    book_imbalance: Mapped[Decimal | None] = mapped_column(Numeric(12, 8))
    flow_imbalance: Mapped[Decimal | None] = mapped_column(Numeric(12, 8))
    data_quality: Mapped[str] = mapped_column(String(16))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    __table_args__ = (Index("ix_snapshot_token_time", "token_id", "captured_at"),)


class SignalRow(Base):
    __tablename__ = "signals"

    signal_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    condition_id: Mapped[str] = mapped_column(String(80), index=True)
    token_id: Mapped[str] = mapped_column(String(80), index=True)
    action: Mapped[str] = mapped_column(String(16), index=True)
    category: Mapped[str] = mapped_column(String(32), index=True)
    market_probability: Mapped[Decimal] = mapped_column(_PROB)
    model_probability: Mapped[Decimal] = mapped_column(_PROB)
    edge: Mapped[Decimal] = mapped_column(Numeric(12, 8))
    net_ev: Mapped[Decimal] = mapped_column(Numeric(12, 8))
    confidence: Mapped[int] = mapped_column(Integer)
    smart_money_score: Mapped[int | None] = mapped_column(Integer)
    rationale: Mapped[str] = mapped_column(Text)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class OrderRow(Base):
    __tablename__ = "orders"

    client_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    order_id: Mapped[str | None] = mapped_column(String(120), index=True)
    signal_id: Mapped[str | None] = mapped_column(ForeignKey("signals.signal_id"))
    condition_id: Mapped[str] = mapped_column(String(80), index=True)
    token_id: Mapped[str] = mapped_column(String(80), index=True)
    side: Mapped[str] = mapped_column(String(8))
    order_type: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(24), index=True)
    size_shares: Mapped[Decimal] = mapped_column(_MONEY)
    limit_price: Mapped[Decimal] = mapped_column(_MONEY)
    filled_shares: Mapped[Decimal] = mapped_column(_MONEY, default=Decimal(0))
    average_fill_price: Mapped[Decimal | None] = mapped_column(_MONEY)
    run_mode: Mapped[str] = mapped_column(String(16), index=True)
    error: Mapped[str | None] = mapped_column(Text)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    # Database-level idempotency. The application also checks, but this is the
    # guarantee that survives a concurrent retry from two coroutines.
    __table_args__ = (UniqueConstraint("client_key", name="uq_orders_client_key"),)


class PositionRow(Base):
    __tablename__ = "positions"

    position_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    condition_id: Mapped[str] = mapped_column(String(80), index=True)
    token_id: Mapped[str] = mapped_column(String(80), index=True)
    shares: Mapped[Decimal] = mapped_column(_MONEY)
    average_entry_price: Mapped[Decimal] = mapped_column(_MONEY)
    entry_probability: Mapped[Decimal] = mapped_column(_PROB)
    realized_pnl: Mapped[Decimal] = mapped_column(_MONEY, default=Decimal(0))
    run_mode: Mapped[str] = mapped_column(String(16), index=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class SmartWalletRow(Base):
    """Scored wallet. Score is earned from performance, not from size."""

    __tablename__ = "smart_wallets"

    wallet: Mapped[str] = mapped_column(String(64), primary_key=True)
    smart_score: Mapped[int] = mapped_column(Integer, index=True)
    realized_pnl: Mapped[Decimal | None] = mapped_column(_MONEY)
    roi: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    accuracy: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    trade_count: Mapped[int] = mapped_column(Integer, default=0)
    category_specialization: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    first_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_scored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SmartMoneyEventRow(Base):
    """Time series of notable wallet actions."""

    __tablename__ = "smart_money_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    wallet: Mapped[str] = mapped_column(String(64), index=True)
    condition_id: Mapped[str] = mapped_column(String(80), index=True)
    token_id: Mapped[str] = mapped_column(String(80))
    side: Mapped[str] = mapped_column(String(8))
    notional_usdc: Mapped[Decimal] = mapped_column(_MONEY)
    entry_price: Mapped[Decimal] = mapped_column(_MONEY)
    market_probability_at_entry: Mapped[Decimal] = mapped_column(_PROB)
    is_exit: Mapped[bool] = mapped_column(Boolean, default=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class JournalRow(Base):
    """Append-only decision log. Section 23.

    ``kind`` is one of ENTERED / REJECTED / HELD / EXITED. Rejections are
    first-class rows: the rejected set is how you tell a well-calibrated gate
    from one that simply never fires.
    """

    __tablename__ = "journal"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(16), index=True)
    condition_id: Mapped[str | None] = mapped_column(String(80), index=True)
    signal_id: Mapped[str | None] = mapped_column(String(64), index=True)
    position_id: Mapped[str | None] = mapped_column(String(64), index=True)
    reason: Mapped[str] = mapped_column(Text)
    context: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    run_mode: Mapped[str] = mapped_column(String(16), index=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class ReconciliationRow(Base):
    __tablename__ = "reconciliations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trigger: Mapped[str] = mapped_column(String(32))
    succeeded: Mapped[bool] = mapped_column(Boolean, index=True)
    discrepancies: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    ran_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class AuditRow(Base):
    """Sensitive-action audit trail. Section 29."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(String(120), index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
