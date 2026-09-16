"""The ZERO two sources count from, or the OFFSET that brings one onto the other.

A water level and a bed elevation are only on one axis when both documents count
from the same datum. What a document counts from is stated on the thing itself -
on the layer a fetch produced, or on the rows of a survey that carries its own
project datum - so the check is over what the caller holds, and an unstated zero
refuses by name. Two DIFFERING zeros are bridged only by an offset somebody
measured, named here as the row it came from: a shift nobody measured would be
indistinguishable from a measurement to every reader downstream.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

__all__ = ["Alignment", "DatumError", "Offset", "align", "datum_of", "names_frame",
           "offset_row", "one_datum"]


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


@dataclass(frozen=True, slots=True)
class Offset:
    """How far one vertical frame's zero sits above another's, and who measured it.

    ``metres`` is ADDED to an elevation on ``from_frame`` to read that elevation
    on ``to_frame``. The frames are empty on a shift the caller stated bare, which
    nothing can check against the datums it is applied between."""

    metres: float
    from_frame: str = ""
    to_frame: str = ""
    source: str = ""
    uncertainty_m: float | None = None

    @property
    def names_frames(self) -> bool:
        return bool(self.from_frame and self.to_frame)

    def reversed(self) -> "Offset":
        """The same measurement read the other way.

        A negation, which is what a grid shift is; a conversion the service runs
        through the ellipsoid is not exactly reversible, and asking it in the
        direction it is wanted is a few centimetres closer than negating it."""
        return Offset(metres=-self.metres, from_frame=self.to_frame,
                      to_frame=self.from_frame, source=self.source,
                      uncertainty_m=self.uncertainty_m)

    @property
    def note(self) -> str:
        """What a run SAYS about the shift it applied."""
        bridge = (f" between {self.from_frame} and {self.to_frame}"
                  if self.names_frames else "")
        who = f", stated by {self.source}" if self.source else ", stated on the call"
        how_close = (f" (+/- {self.uncertainty_m:.3f} m)"
                     if self.uncertainty_m is not None else "")
        return f"{self.metres:+.3f} m{bridge}{how_close}{who}"


@dataclass(frozen=True, slots=True)
class Alignment:
    """What it takes to read one source on another's datum: the datum both end up
    on, the metres added to the first, and the PHRASE a caller says it in - which
    is empty where nothing was shifted."""

    datum: str
    shift_m: float
    note: str


def offset_row(value: Any) -> Offset | None:
    """THE ingestion: a fetched offset record, an :class:`Offset`, or a bare number.

    ``None`` only when nothing came. A number is metres the caller stated with no
    frames on it, which is a shift the datums cannot be checked against."""
    if value is None or isinstance(value, Offset):
        return value
    if isinstance(value, bool):
        raise DatumError("DATUM_OFFSET_INVALID",
                         "an offset was given as a boolean, which is not metres.")
    if isinstance(value, (int, float)):
        return Offset(metres=float(value))
    read = (value if isinstance(value, Mapping)
            else {field: getattr(value, field, None)
                  for field in ("offset_m", "from_frame", "to_frame", "source",
                                "uncertainty_m")})
    try:
        metres = float(read["offset_m"])
    except (KeyError, TypeError, ValueError):
        raise DatumError(
            "DATUM_OFFSET_INVALID",
            f"{value!r} is not an offset: an offset is metres, or the record a "
            "vertical-datum fetch returns, which states offset_m and the two "
            "frames it bridges.")
    uncertainty = read.get("uncertainty_m")
    return Offset(metres=metres,
                  from_frame=str(read.get("from_frame") or "").strip(),
                  to_frame=str(read.get("to_frame") or "").strip(),
                  source=str(read.get("source") or "").strip(),
                  uncertainty_m=(float(uncertainty) if uncertainty is not None
                                 else None))


def names_frame(datum: str, frame: str) -> bool:
    """Does this stated datum NAME that frame?

    A source states its datum in its own words - "NAVD88 (metres, positive up)",
    "SD (Columbia River Datum: CRD)" - so the frame is looked for as a run of its
    words. A frame whose name ENDS another's, as IGLD85 ends LWD_IGLD85, reads as
    present in the longer one."""
    words = re.findall(r"[a-z0-9]+", str(datum or "").lower())
    wanted = re.findall(r"[a-z0-9]+", str(frame or "").lower())
    if not wanted:
        return False
    return any(words[i:i + len(wanted)] == wanted
               for i in range(len(words) - len(wanted) + 1))


def align(source: Any, onto: Any, *, offset: Any = None,
          code_prefix: str = "") -> Alignment:
    """Read ``source``'s elevations on ``onto``'s datum, or refuse by name.

    Same datum, nothing is shifted and an unstated one refuses. Different datums,
    the ``offset`` row bridges them - checked against both frames where it names
    them - and an absent offset refuses rather than laying one over the other."""
    here, there = datum_of(source), datum_of(onto)
    if here == there or not (here and there):
        return Alignment(datum=one_datum(source, onto, code_prefix=code_prefix),
                         shift_m=0.0, note="")
    bridge = offset_row(offset)
    if bridge is None:
        one_datum(source, onto, code_prefix=code_prefix)
    if bridge.names_frames:
        if names_frame(there, bridge.from_frame) and names_frame(here, bridge.to_frame):
            bridge = bridge.reversed()
        elif not (names_frame(here, bridge.from_frame)
                  and names_frame(there, bridge.to_frame)):
            raise DatumError(
                f"{code_prefix}DATUM_OFFSET_MISMATCH",
                f"the offset bridges {bridge.from_frame} and {bridge.to_frame}, "
                f"and what it was applied to reads on {here!r} and {there!r}. An "
                "offset between two other frames is not a conversion between "
                "these; name the offset the two sources actually need.")
    return Alignment(datum=there, shift_m=bridge.metres,
                     note=f"read on {there} through a stated offset of {bridge.note}")
