"""The scalar check an input's bounds and a keyword's plausibility row share.

Coercion and the range test in one home: bool is not a number, a refusal names
the value and never the range, and the range is read where the schema is read -
the docstring line and ``describe_keywords``. The error type is the caller's,
so an input refuses on the wire and a keyword in the dictionary's own voice.
"""

from __future__ import annotations

__all__ = ["scalar"]


def scalar(name: str, value: object, bounds: tuple[float, float], *,
           units: str = "", position: int | None = None,
           beyond: str = "") -> float:
    """``value`` as a float inside ``bounds``, or the ValueError naming it and not the range.

    ``units`` decorate the value as they were declared, ``position`` names the
    element of a list the refusal is about, and ``beyond`` is the caller's own
    next step appended to an out-of-range refusal."""
    # bool is an int: True would coerce to 1.0 and slip past a bounded refusal.
    try:
        number = None if isinstance(value, bool) else float(value)
    except (TypeError, ValueError):
        number = None
    if number is None:
        raise ValueError(
            f"{name}={value!r} is not a number"
            + (f" in {units}" if units else "")
            + ". It is not silently defaulted.")
    lo, hi = float(bounds[0]), float(bounds[1])
    if not lo <= number <= hi:
        raise ValueError(
            f"{name}={number:g}" + (f" {units}" if units else "")
            + " is outside the declared range"
            + (f" at position {position + 1}" if position is not None else "")
            + f". {beyond or 'State a value inside it.'}")
    return number
