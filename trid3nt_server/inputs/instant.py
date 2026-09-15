"""An INSTANT: the moment a run is about, from whatever the wire sent.

A date, a datetime, a trailing-Z timestamp and nothing at all all enter through
``instant``; ``day`` narrows one to the calendar day a daily record is asked
over. Which cycle, sample or forecast a run reads is physically consequential,
so a value that does not parse REFUSES by name rather than reading the latest.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Mapping

from trid3nt_server.workflows.runtime import WireArgsError

__all__ = ["day", "event_time", "instant"]


def instant(value: Any, *, label: str = "event_time",
            code: str = "EVENT_TIME_INVALID") -> str | None:
    """THE ingestion: a wire date or datetime -> a UTC ISO-8601 timestamp.

    ``None`` means no instant was asked for, which a producer reads as latest;
    a bare date is midnight UTC; anything unparseable refuses typed."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    iso = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        moment = dt.datetime.fromisoformat(iso)
    except ValueError:
        raise WireArgsError(
            f"{label}={value!r} is not a parseable ISO-8601 date or datetime "
            "(e.g. '2026-08-20' or '2026-08-20T06:00:00Z'). Omit it to read the "
            "most recent published value.", error_code=code) from None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(dt.timezone.utc).isoformat()


def day(value: Any, *, label: str = "event_time",
        code: str = "EVENT_TIME_INVALID") -> str:
    """The calendar DAY a daily record is read over, as ``YYYY-MM-DD``.

    Nothing asked for is TODAY in UTC; anything unparseable refuses typed."""
    stated = instant(value, label=label, code=code)
    if stated is None:
        return dt.datetime.now(dt.timezone.utc).date().isoformat()
    return stated[:10]


def event_time(*, label: str = "event_time",
               code: str = "EVENT_TIME_INVALID") -> Any:
    """A coercion reading the wire's ``event_time`` into a pinned UTC instant."""

    def _coerce(args: Mapping[str, Any]) -> dict[str, Any]:
        return {"event_time": instant(args.get("event_time"), label=label, code=code)}

    _coerce.__name__ = "event_time"
    return _coerce
