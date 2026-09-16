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
from typing import Any, Mapping, Sequence

__all__ = ["Alignment", "DatumError", "OFFSET_FETCH", "Offset", "align",
           "datum_of", "names_frame", "offset_ask", "offset_row", "one_datum",
           "one_frame", "onto_frame", "published_offset"]

#: The fetch that measures one frame's zero against another's at a point. The
#: RUNTIME declares this row where a source reaches the run on another frame and
#: publishes no shift of its own; no question writes it, and nothing here calls
#: it - a coercion reads the row's value.
OFFSET_FETCH = "fetch_vertical_datum_offset"

#: The VDatum grid the row is asked under. The service serves no lookup from a
#: point to the region that covers it, so inland CONUS is what a run states and
#: a point another region owns refuses by name rather than answering from the
#: wrong grid.
OFFSET_REGION = "contiguous"


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
    common = one_frame(distinct)
    if common is None:
        spelled = "; ".join(f"{name} reads on {datum!r}"
                            for name, datum in sorted(stated.items()))
        raise DatumError(
            f"{code_prefix}DATUMS_DIFFER",
            f"{spelled}, and no offset between them is stated anywhere, so one "
            "cannot be placed over the other. Name sources on one datum, or "
            "state the offset on the rows.")
    return common


def one_frame(datums: Sequence[str]) -> str | None:
    """The frame every one of these spellings NAMES, or ``None`` for two frames.

    A source states its datum in its own words - "NAVD88 (metres, positive up)"
    beside a bare "NAVD88" - and two spellings of one zero are one zero. The
    fullest spelling is what comes back, because it is what a reader learns from."""
    stated = [d for d in datums if d]
    if not stated:
        return None
    for candidate in sorted(stated, key=len):
        if all(names_frame(other, candidate) for other in stated):
            return max(stated, key=len)
    return None


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
    if one_frame([here, there]) or not (here and there):
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


def published_offset(source: Any) -> Offset | None:
    """The shift a source publishes about ITSELF, or ``None``.

    A survey measured on a district's project datum states in its own metadata
    how far that zero sits above a national frame; nothing else knows it, and a
    service will not serve a datum nobody but its owner uses."""
    metres = getattr(source, "datum_offset_m", None)
    if isinstance(source, Mapping):
        metres = source.get("datum_offset_m", metres)
    onto = (source.get("datum_offset_frame") if isinstance(source, Mapping)
            else getattr(source, "datum_offset_frame", None))
    if metres is None or not onto:
        return None
    return Offset(metres=float(metres), from_frame=datum_of(source),
                  to_frame=str(onto).strip(),
                  source=f"{_label(source)}'s own metadata")


def onto_frame(source: Any, frame: Any, *, offset: Any = None,
               code_prefix: str = "") -> Alignment:
    """Read ``source``'s elevations on the RUN's vertical frame, or refuse by name.

    One frame per run, so every elevation a run ingests is brought onto it here:
    a source already on it is not shifted, one that publishes its own shift is
    moved by that measurement, and one that publishes none is moved by ``offset``
    - the row the runtime declared for this pair. A pair nothing measures refuses
    naming both frames rather than laying one over the other.

    A run that names no frame asks nothing, and an unstated zero refuses the way
    it does anywhere else - what a slot does with a source that states none is
    that slot's own statement."""
    wanted = str(frame or "").strip()
    here = datum_of(source)
    if not wanted:
        return Alignment(datum=here, shift_m=0.0, note="")
    onto = {"vertical_datum": wanted, "name": "the run's vertical frame"}
    if not here or one_frame([here, wanted]):
        return align(source, onto, code_prefix=code_prefix)
    bridge = published_offset(source) or offset_row(offset)
    return align(source, onto, offset=bridge, code_prefix=code_prefix)


def offset_ask(source: Any, frame: Any, *, at: Any = None
               ) -> dict[str, Any] | None:
    """What a DATA row on :data:`OFFSET_FETCH` ASKS for this source, or ``None``.

    ``None`` says no row is owed: the run names no frame, the source states none
    or already stands on it, the source publishes its own shift, there is no
    point to ask at, or the service transforms neither frame - and that last one
    leaves the alignment to refuse naming both, which is the honest answer for a
    district's project datum no service knows."""
    wanted = str(frame or "").strip()
    here = datum_of(source)
    if not wanted or not here or one_frame([here, wanted]):
        return None
    if published_offset(source) is not None:
        return None
    point = _point_of(at)
    served = _served_frames()
    from_frame, to_frame = _as_served(here, served), _as_served(wanted, served)
    if point is None or not (from_frame and to_frame):
        return None
    return {"point": list(point), "from_frame": from_frame,
            "to_frame": to_frame, "region": OFFSET_REGION}


def _served_frames() -> tuple[str, ...]:
    """The frames the offset fetch transforms between, as it names them."""
    from trid3nt_server.tools.fetchers._router.registration import get_spec

    try:
        return tuple((get_spec(OFFSET_FETCH).params["from_frame"]).values or ())
    except (KeyError, AttributeError):
        return ()


def _as_served(datum: str, served: Sequence[str]) -> str:
    """This stated datum as the frame name the fetch knows, or "".

    A source states its zero in its own words, and a service takes one word."""
    return next((name for name in served if names_frame(datum, name)), "")


def _point_of(at: Any) -> tuple[float, float] | None:
    """The lon/lat the offset is asked at: what the caller named, else the centre
    of the domain the run is standing on."""
    if at is not None:
        lon = getattr(at, "lon", None)
        lat = getattr(at, "lat", None)
        if lon is not None and lat is not None:
            return float(lon), float(lat)
        pair = list(at)
        if len(pair) == 2:
            return float(pair[0]), float(pair[1])
    from trid3nt_server.workflows.runtime.domain import current_domain

    domain = current_domain()
    box = getattr(domain, "bbox", None) if domain is not None else None
    if not box:
        return None
    west, south, east, north = (float(v) for v in box)
    return (west + east) / 2.0, (south + north) / 2.0
