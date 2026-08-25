"""The one bespoke coercion ``tomawac_wave_field`` needs, beside the template.

Four question classes, and people name them in words - "will the swell break at
the beach", "does the ebb current build the sea", "how much does the shelf damp
it" - none of which is the literal value the param takes. Reading those words is
this template's own concern, not a shared verb, so by the readability principle
it sits in a sibling file rather than in the template's declarations.
"""

from __future__ import annotations

from typing import Any

__all__ = ["WAVE_MODES", "wave_mode"]

#: The four question classes, in the order the docstring introduces them.
WAVE_MODES = ("fetch_growth", "shoaling", "bottom_friction", "wave_current")

#: Words that name a class other than the fetch-growth default. Ordered: a prompt
#: about breaking waves at the beach is a SHOALING question even if it also says
#: "current", because the first match is the one the asker led with.
_MODE_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("shoaling", ("shoal", "breaking", "nearshore", "beach", "surf")),
    ("wave_current", ("current", "opposing", "following", "tidal jet", "ebb")),
    ("bottom_friction", ("bottom friction", "friction", "dissipat", "shelf")),
)


def wave_mode() -> Any:
    """A coercion resolving ``wave_mode`` to one of the four question classes.

    An explicit legal value stands. Anything else - an unknown word, or nothing at
    all - is read off the ``location`` phrasing, which is where a caller putting
    the question in words puts it.

    Neither field carrying a signal leaves NO row: a coercion's output merges into
    the door-1 supplied sheet, so emitting the fall-through class here would
    resolve it through the USER door and report the template's own default as
    "supplied on this invocation". Abstaining lets the declared default -
    ``fetch_growth``, the class a bare "how big do the waves get" is asking about -
    seat through its own door with its own basis.
    """

    def _coerce(args: Any) -> dict[str, Any]:
        explicit = str(args.get("wave_mode") or "").strip().lower()
        if explicit in WAVE_MODES:
            return {"wave_mode": explicit}
        phrase = str(args.get("location") or "").lower()
        for mode, words in _MODE_WORDS:
            if any(word in phrase for word in words):
                return {"wave_mode": mode}
        return {}

    _coerce.__name__ = "wave_mode"
    return _coerce
