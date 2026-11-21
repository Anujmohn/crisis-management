from __future__ import annotations

import logging
import re
import sys

_SENSITIVE_PATTERNS = [
    re.compile(r"(password\s*[:=]\s*)([^\s,]+)", re.IGNORECASE),
    re.compile(r"(secret\s*[:=]\s*)([^\s,]+)", re.IGNORECASE),
    re.compile(r"(token\s*[:=]\s*)([^\s,]+)", re.IGNORECASE),
    re.compile(r"(authorization\s*[:=]\s*)([^\s,]+)", re.IGNORECASE),
]


class SecurityRedactingFormatter(logging.Formatter):
    """
    Log formatter that redacts sensitive credentials and ensures
    passwords and secret tokens are never leaked to logs or monitoring streams.
    """

    def format(self, record: logging.LogRecord) -> str:
        formatted = super().format(record)
        for pattern in _SENSITIVE_PATTERNS:
            formatted = pattern.sub(r"\1[REDACTED]", formatted)
        return formatted


def configure_logging(log_level: str = "INFO") -> None:
    """Configures structured, security-aware logging across the application."""
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    formatter = SecurityRedactingFormatter(
        fmt="%(asctime)s [%(levelname)s] [%(name)s]: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)
    root_logger.handlers = [handler]

    # Silence overly verbose third-party loggers in production
    for noisy in ("urllib3", "asyncio", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
