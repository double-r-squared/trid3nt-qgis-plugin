"""The one bespoke coercion ``artemis_harbor_agitation`` needs, beside the template.

Three question classes, asked in words: "does the breakwater shelter the berths",
"does the basin ring at the swell period", "does the reef focus the waves". None
of those is the literal value the param takes, and reading them is this template's
own concern rather than a shared verb - so by the readability principle it sits in
a sibling file rather than in the template's declarations.
"""

from __future__ import annotations

from typing import Any

__all__ = ["AGITATION_MODES", "agitation_mode"]

#: The three question classes, in the order the docstring introduces them.
AGITATION_MODES = ("diffraction", "resonance", "shoal")

#: Words that name a class other than the diffraction default. Ordered: a prompt
#: about a ringing basin is a RESONANCE question even where a breakwater is also
#: mentioned, because the first match is the one the asker led with.
_MODE_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    # "reson", not "resonan": the ask says "resonate" as often as "resonance".
    ("resonance", ("reson", "seiche", "ringing", "amplif", "natural period")),
    ("shoal", ("shoal", "reef", "focus", "refract", "bar ")),
)


def agitation_mode() -> Any:
    """A coercion resolving ``wave_mode`` to one of the three question classes.

    An explicit legal value stands. Anything else is read off the ``location``
    phrasing.

    Neither field carrying a signal leaves NO row: a coercion's output merges into
    the door-1 supplied sheet, so emitting the fall-through class here would
    resolve it through the USER door and report the template's own default as
    "supplied on this invocation". Abstaining lets the declared default -
    ``diffraction``, what "does this breakwater shelter the harbour" means - seat
    through its own door with its own basis.
    """

    def _coerce(args: Any) -> dict[str, Any]:
        explicit = str(args.get("wave_mode") or "").strip().lower()
        if explicit in AGITATION_MODES:
            return {"wave_mode": explicit}
        phrase = str(args.get("location") or "").lower()
        for mode, words in _MODE_WORDS:
            if any(word in phrase for word in words):
                return {"wave_mode": mode}
        return {}

    _coerce.__name__ = "agitation_mode"
    return _coerce
