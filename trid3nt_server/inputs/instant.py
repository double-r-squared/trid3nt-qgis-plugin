"""An INSTANT: the moment a run is about, from whatever the wire sent.

A date, a datetime, a trailing-Z timestamp, "now" and nothing at all all enter through
``instant``; "now" is the clock at ingestion, to the minute, so the run is pinned to it. Which cycle, sample or forecast a run reads is physically consequential,
so a value that does not parse REFUSES by name rather than reading the latest.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Mapping

from .user_input import WireArgsError

__all__ = ["NO_MOMENT_WORD", "event_time", "instant"]

#: What a wire event_time says when the ask has no moment; the coercion passes it through and the fill reads it as a stated absence.
NO_MOMENT_WORD = "none"


def instant(value: Any, *, label: str = "event_time",
            code: str = "EVENT_TIME_INVALID") -> str | None:
    """THE ingestion: a wire date or datetime -> a UTC ISO-8601 timestamp.

    ``None`` means no instant was asked for, which a producer reads as latest;
    a bare date is midnight UTC; "now" is the current minute; anything unparseable refuses typed."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.lower() == "now":
        return dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0).isoformat()
    iso = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        moment = dt.datetime.fromisoformat(iso)
    except ValueError:
        raise WireArgsError(
            f"{label}={value!r} is not a parseable ISO-8601 date or datetime "
            "(e.g. '2026-08-20' or '2026-08-20T06:00:00Z') or 'now'. Omit it to read the "
            "most recent published value.", error_code=code) from None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(dt.timezone.utc).isoformat()


def event_time(*, label: str = "event_time",
               code: str = "EVENT_TIME_INVALID") -> Any:
    """A coercion reading the wire's ``event_time`` into a pinned UTC instant, or 'none' where the ask has no moment."""

    def _coerce(args: Mapping[str, Any]) -> dict[str, Any]:
        if str(args.get("event_time") or "").strip().lower() == NO_MOMENT_WORD:
            return {"event_time": NO_MOMENT_WORD}
        return {"event_time": instant(args.get("event_time"), label=label, code=code)}

    _coerce.__name__ = "event_time"
    return _coerce
