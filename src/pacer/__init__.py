"""pacer — pipe stdin to stdout at a bandwidth capped by time of day."""

from .core import (
    TokenBucket,
    Window,
    active_window,
    burst_for,
    main,
    parse_rate,
    parse_size,
    parse_window,
    run,
)

__all__ = [
    "TokenBucket",
    "Window",
    "active_window",
    "burst_for",
    "main",
    "parse_rate",
    "parse_size",
    "parse_window",
    "run",
]

__version__ = "0.1.0"
