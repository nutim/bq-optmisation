"""Structured JSON logging for Cloud Run operations."""

import logging
import sys
from typing import cast

import structlog
from structlog.typing import FilteringBoundLogger


def configure_logging(level: str = "INFO") -> None:
    """Configure stdlib + structlog to emit one JSON object per line."""
    resolved = getattr(logging, str(level).upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=resolved)
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(resolved),
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> FilteringBoundLogger:
    return cast(FilteringBoundLogger, structlog.get_logger(name))
