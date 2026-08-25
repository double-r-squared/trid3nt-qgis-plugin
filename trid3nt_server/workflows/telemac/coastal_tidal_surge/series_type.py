"""The one bespoke coercion ``coastal_tidal_surge`` needs, beside the template.

Which water-level record the question is about is answered in words far more
often than by argument - "the calm tide", "the astronomical prediction", "what
the harmonic tables say" all mean the same thing and none of them is the literal
value the param takes. Reading those words is this template's own concern, not a
shared verb, so by the readability principle it sits in a sibling file rather
than in the template's declarations.
"""

from __future__ import annotations

from typing import Any

__all__ = ["SERIES_TYPES", "series_type"]

#: The two question classes: the OBSERVED record (tide + surge) and the
#: astronomical PREDICTION (calm tide, the control that isolates the surge).
SERIES_TYPES = ("observed", "prediction")

#: Words that name the astronomical prediction. Everything else is the observed
#: record, which is what somebody asking about flooding almost always means.
_PREDICTION_WORDS = ("predict", "astronomical", "calm tide", "tide table",
                     "harmonic")


def series_type() -> Any:
    """A coercion resolving ``series_type`` to one of the two question classes.

    An explicit legal value stands. Anything else - an unknown word, or nothing at
    all - is read off the ``location`` phrasing, which is where a caller putting
    the question in words puts it. The resolved value is always one of the two, so
    the deck never carries a series class the worker cannot write a boundary for.

    Prediction wording is the only positive SIGNAL there is: ``observed`` is the
    else-branch, so no wording leaves NO row. A coercion's output merges into the
    door-1 supplied sheet, and emitting the else-branch here would resolve it
    through the USER door and report the template's own default as "supplied on
    this invocation". Abstaining lets the declared ``observed`` default seat
    through its own door with its own basis.
    """

    def _coerce(args: Any) -> dict[str, Any]:
        explicit = str(args.get("series_type") or "").strip().lower()
        if explicit in SERIES_TYPES:
            return {"series_type": explicit}
        phrase = str(args.get("location") or "").lower()
        if any(word in phrase for word in _PREDICTION_WORDS):
            return {"series_type": "prediction"}
        return {}

    _coerce.__name__ = "series_type"
    return _coerce
