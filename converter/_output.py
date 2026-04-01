"""Centralized output management — verbosity control and warning aggregation."""

import sys
from enum import IntEnum


class Verbosity(IntEnum):
    QUIET = 0
    NORMAL = 1
    VERBOSE = 2


# Module-level state — set once by CLI at startup
_verbosity: Verbosity = Verbosity.NORMAL


def set_verbosity(level: Verbosity) -> None:
    global _verbosity
    _verbosity = level


def get_verbosity() -> Verbosity:
    return _verbosity


def info(msg: str) -> None:
    """Print informational message to stderr (suppressed in quiet mode)."""
    if _verbosity >= Verbosity.NORMAL:
        print(msg, file=sys.stderr)


def verbose(msg: str) -> None:
    """Print verbose diagnostic to stderr (only in verbose mode)."""
    if _verbosity >= Verbosity.VERBOSE:
        print(msg, file=sys.stderr)


def error(msg: str) -> None:
    """Print error message to stderr (always shown)."""
    print(msg, file=sys.stderr)


class WarningCollector:
    """Collects warnings during a conversion pass, prints them to stderr,
    and provides a count for the batch summary."""

    def __init__(self) -> None:
        self._count = 0

    @property
    def count(self) -> int:
        return self._count

    def warn(self, msg: str) -> None:
        self._count += 1
        if _verbosity >= Verbosity.NORMAL:
            print(f"  WARNING: {msg}", file=sys.stderr)

    def reset(self) -> None:
        self._count = 0


# Global collector — used by transform/writer during a conversion pass
warnings = WarningCollector()
