"""The ZERO two sources count from, or the refusal that names the two they state.

A water level and a bed elevation are only on one axis when both documents count
from the same datum. What a document counts from is stated on the thing itself -
on the layer a fetch produced, or on the rows of a survey that carries its own
project datum - so the check is over what the caller holds, and an unstated or a
differing zero refuses by name rather than letting a free surface be placed over
a bed it has no arithmetic with.
"""

from __future__ import annotations

from typing import Any, Mapping

__all__ = ["DatumError", "datum_of", "one_datum"]


class DatumError(RuntimeError):
    """A typed refusal: ``DATUM_UNSTATED`` or ``DATUMS_DIFFER``."""

    error_code: str
    retryable: bool = False

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


def datum_of(source: Any) -> str:
    """What ONE source states its elevations are counted from, or "".

    A layer states its own - the survey's project datum, the spec row's zero
    carried onto what it produced - and a bare source NAME is looked up on the
    row that declares it."""
    stated = (source.get("vertical_datum") if isinstance(source, Mapping)
              else getattr(source, "vertical_datum", None))
    if stated:
        return str(stated).strip()
    return _stated(source) if isinstance(source, str) else ""


def one_datum(*sources: Any, code_prefix: str = "") -> str:
    """The vertical datum EVERY source states, or a typed refusal.

    ``code_prefix`` stamps the caller's own error family onto the refusal."""
    stated = {_label(source): datum_of(source) for source in sources}
    missing = sorted(name for name, datum in stated.items() if not datum)
    if missing:
        raise DatumError(
            f"{code_prefix}DATUM_UNSTATED",
            f"{', '.join(missing)} states no vertical datum, so a water level "
            "and a bed elevation cannot be placed on one axis. State the datum "
            "on the source row from the dataset's own documentation.")
    distinct = sorted(set(stated.values()))
    if len(distinct) > 1:
        spelled = "; ".join(f"{name} reads on {datum!r}"
                            for name, datum in sorted(stated.items()))
        raise DatumError(
            f"{code_prefix}DATUMS_DIFFER",
            f"{spelled}, and no offset between them is stated anywhere, so one "
            "cannot be placed over the other. Name sources on one datum, or "
            "state the offset on the rows.")
    return distinct[0]


def _stated(name: str) -> str:
    from trid3nt_server.tools.fetchers._router.registration import get_spec

    return (getattr(get_spec(name), "vertical_datum", None) or "").strip()


def _label(source: Any) -> str:
    """How a refusal NAMES this source: the tool name, or what the layer calls
    itself."""
    if isinstance(source, str):
        return source
    for field in ("name", "layer_id"):
        found = (source.get(field) if isinstance(source, Mapping)
                 else getattr(source, field, None))
        if found:
            return str(found)
    return repr(source)
