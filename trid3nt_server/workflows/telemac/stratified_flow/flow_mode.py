"""The one bespoke coercion ``telemac3d_stratified_flow`` needs, beside the template.

Three question classes, asked in words: "does the lake turn over", "where does the
water go when the wind blows", "how far up the estuary does the salt reach". None
of those is the literal value the param takes, and reading them is this template's
own concern rather than a shared verb - so by the readability principle it sits in
a sibling file rather than in the template's declarations.
"""

from __future__ import annotations

from typing import Any

__all__ = ["FLOW_MODES", "flow_mode"]

#: The three question classes, in the order the docstring introduces them.
FLOW_MODES = ("stratification", "wind_circulation", "salt_wedge")

#: Words that name a class other than the stratification default. Ordered: the
#: first match is the one the asker led with.
_MODE_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("salt_wedge", ("salt wedge", "saline", "salinity", "estuar", "brackish")),
    ("wind_circulation", ("circulation", "return flow", "gyre", "wind-driven",
                          "wind driven", "upwelling")),
)


def flow_mode() -> Any:
    """A coercion resolving ``flow_mode`` to one of the three question classes.

    An explicit legal value stands. Anything else is read off the ``location``
    phrasing and falls through to stratification, which is what "does this lake
    stratify" - the question this tool is most often asked - means.
    """

    def _coerce(args: Any) -> dict[str, Any]:
        explicit = str(args.get("flow_mode") or "").strip().lower()
        if explicit in FLOW_MODES:
            return {"flow_mode": explicit}
        phrase = str(args.get("location") or "").lower()
        for mode, words in _MODE_WORDS:
            if any(word in phrase for word in words):
                return {"flow_mode": mode}
        return {"flow_mode": "stratification"}

    _coerce.__name__ = "flow_mode"
    return _coerce
