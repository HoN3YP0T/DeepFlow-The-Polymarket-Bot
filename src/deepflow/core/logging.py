"""Structured logging setup.

JSON in production so logs are queryable; console renderer for local work.
Secrets are scrubbed by a processor rather than by discipline at call sites.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

#: Keys whose values are replaced with a placeholder wherever they appear.
REDACTED_KEYS = frozenset(
    {"private_key", "api_secret", "api_passphrase", "jwt_secret", "password", "authorization"}
)
_PLACEHOLDER = "***redacted***"


def _redact(
    _logger: object, _name: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    """Blank out anything that looks like a credential, at any nesting depth."""

    def scrub(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                k: (_PLACEHOLDER if k.lower() in REDACTED_KEYS else scrub(v))
                for k, v in value.items()
            }
        if isinstance(value, (list, tuple)):
            return type(value)(scrub(v) for v in value)
        return value

    return scrub(event_dict)  # type: ignore[no-any-return]


def configure_logging(*, level: str = "INFO", fmt: str = "json") -> None:
    """Install the processor chain. Call once, at startup."""
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level.upper())

    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer()
        if fmt == "json"
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            _redact,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[level.upper()]
        ),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Module-level logger."""
    return structlog.get_logger(name)  # type: ignore[no-any-return]
