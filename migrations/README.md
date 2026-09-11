# Migrations

```bash
alembic revision --autogenerate -m "describe change"
alembic upgrade head
```

The DSN comes from `DEEPFLOW_DATABASE__DSN` via application settings, not from
`alembic.ini`.

## TimescaleDB

`market_snapshots` and `smart_money_events` are time-series tables. After the
initial schema migration, convert them in a follow-up revision:

```python
op.execute("SELECT create_hypertable('market_snapshots', 'captured_at')")
op.execute("SELECT create_hypertable('smart_money_events', 'observed_at')")
```

Autogenerate will not produce these — hypertable conversion is not part of the
SQLAlchemy model, so it has to be written by hand.
