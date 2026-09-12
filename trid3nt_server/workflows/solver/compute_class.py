"""The compute-class vocabulary: what a caller may say, and what it means.

The ladder is stated ONCE. A second copy of the set is how the rungs the
dispatcher serves and the rungs a caller is offered drift apart, and the drift
only shows as a wrong-sized solve nobody was told about.
"""

from __future__ import annotations

from typing import Any

__all__ = ["COMPUTE_CLASS_ALIAS", "ComputeClassUnknown", "compute_class"]

#: What a caller may say, mapped onto the ``ExecutionHandle.ComputeClass``
#: contract. ``medium`` is a retained SYNONYM of the contract's ``standard`` - it
#: is still the spelling most template Params declare, and renaming it is a
#: fleet-wide model-facing change rather than a dispatch one.
COMPUTE_CLASS_ALIAS: dict[str, str] = {
    "small": "small",
    "medium": "standard",
    "standard": "standard",
    "large": "large",
    "xlarge": "xlarge",
    "gpu": "gpu",
}


class ComputeClassUnknown(ValueError):
    """A caller named a compute class outside the ladder the dispatcher serves."""

    error_code: str = "COMPUTE_CLASS_UNKNOWN"


def compute_class() -> Any:
    """A coercion pinning a SUPPLIED ``compute_class`` to a rung the dispatcher serves.

    An ABSENT rung leaves no row: it would resolve as user-supplied."""

    def _coerce(args: Any) -> dict[str, Any]:
        raw = args.get("compute_class")
        value = str(raw or "").strip().lower()
        if not value:
            return {}
        if value not in COMPUTE_CLASS_ALIAS:
            # REFUSED, not substituted. Silently seating 'medium' gives a caller
            # who asked for 'xlarge' a medium solve, no provenance row saying so,
            # and a warning only the log ever sees.
            raise ComputeClassUnknown(
                f"compute_class {raw!r} is not a rung this dispatcher serves; the "
                f"ladder is {sorted(COMPUTE_CLASS_ALIAS)}. Omit it to take the "
                "template's declared default.")
        return {"compute_class": value}

    _coerce.__name__ = "compute_class"
    return _coerce
