# Tests

```bash
pytest                  # all
pytest -m "not integration"
pytest tests/unit -q
```

## Current coverage

Unit tests cover the components that are **implemented**, not stubbed:

| File | Covers |
|---|---|
| `test_state_machine.py` | Legal/illegal transitions, no-mutation on refusal, terminal states, exposure guard |
| `test_sizing.py` | Kelly closed form, uncertainty haircut, every cap, which limit binds |
| `test_safety_gate.py` | Mandatory failure, unregistered check, raising check, no short-circuit |
| `test_idempotency.py` | Retry collision, genuine re-entry, salt escape hatch |
| `test_settings.py` | Live-mode interlocks, auth requirement, secret redaction |
| `test_domain.py` | Crossed/locked book rejection, `None` vs `0`, immutability |

## Still to write

Per `docs/ROADMAP.md`, each phase lands with its tests. Before live, the
failure-injection suite must cover: duplicate orders, partial fills, API
timeout, WebSocket disconnect with a gap, stale data, database outage, process
restart mid-order, incorrect market state, reconciliation failure, extreme
volatility.

## Conventions

- **No wall-clock time.** Use the `clock` fixture (`ManualClock`). Freshness
  logic is time-dependent, and a test that uses `datetime.now()` is a test that
  fails at midnight.
- **No network.** Adapters are mocked with `respx`; engines take domain
  objects.
- **Test the invariant, not the implementation.** `test_sizing.py` asserts that
  nothing approaches full Kelly, not that a particular multiplication happened.
