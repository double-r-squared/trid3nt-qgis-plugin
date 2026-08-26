"""The USER-INPUT species: things the user hands us - clicks, sketches, values.

One normalizer per SHAPE, and both routes to a param go through it. A value can
arrive DRAWN (the draw gate's reply) or TYPED (a wire coercion), and if each route
carried its own coercion the gate vocabulary and the wire vocabulary would drift:
a polygon the canvas returns closed and a polygon the model types open would
become two different params with one name. So the gate machinery reads these and
so do the templates' coercions - the no-double-middleware law, applied to our own
front door.

A malformed value REFUSES, typed. Degrading a bad point to a derived location is
the silent-swallow class: the run models somewhere else and says nothing.
``code`` is the caller's own error code, because a refusal reads to the model as
that engine's refusal.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping, Sequence

from .errors import DeclarativeError

__all__ = [
    "UserInputError",
    "bearing",
    "bearing_deg",
    "bbox",
    "lonlat_bbox",
    "point",
    "polygon_ring",
    "polyline_coords",
    "polyline_set",
    "lonlat_point",
]

_DEFAULT_CODE = "USER_INPUT_INVALID"


class UserInputError(DeclarativeError):
    """A value the user supplied is not the shape the param declares."""

    error_code = _DEFAULT_CODE

    def __init__(self, message: str, *, code: str = _DEFAULT_CODE) -> None:
        super().__init__(message)
        self.error_code = code


def _refuse(message: str, code: str) -> UserInputError:
    return UserInputError(message, code=code)


def _pair(value: Any) -> tuple[float, float] | None:
    try:
        lon, lat = (float(v) for v in tuple(value))  # type: ignore[misc]
    except (TypeError, ValueError):
        return None
    return (lon, lat)


def _on_earth(lon: float, lat: float) -> bool:
    return -180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0


def lonlat_point(value: Any, *, label: str = "point",
                 code: str = _DEFAULT_CODE) -> tuple[float, float] | None:
    """``(lon, lat)`` from a click or a typed pair; ``None`` only when nothing came."""
    if value is None:
        return None
    pair = _pair(value)
    if pair is None:
        raise _refuse(
            f"{label} {value!r} is not a (lon, lat) pair. Supply it as two numbers "
            "in EPSG:4326, or omit it.", code)
    if not _on_earth(*pair):
        raise _refuse(
            f"{label} ({pair[0]}, {pair[1]}) is off the earth; it is (lon, lat) in "
            "EPSG:4326, longitude first.", code)
    return pair


def _vertices(value: Any, label: str, code: str) -> list[list[float]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise _refuse(f"{label} {value!r} is not a list of (lon, lat) vertices.", code)
    out: list[list[float]] = []
    for vertex in value:
        pair = _pair(vertex)
        if pair is None:
            raise _refuse(
                f"{label} carries the vertex {vertex!r}, which is not a (lon, lat) "
                "pair.", code)
        if not _on_earth(*pair):
            raise _refuse(
                f"{label} carries the vertex ({pair[0]}, {pair[1]}), which is off the "
                "earth; vertices are (lon, lat) in EPSG:4326, longitude first.", code)
        out.append([pair[0], pair[1]])
    return out


def polyline_coords(value: Any, *, label: str = "line",
                    code: str = _DEFAULT_CODE) -> list[list[float]] | None:
    """A drawn or typed OPEN line as ``[[lon, lat], ...]``; two vertices minimum."""
    if value is None:
        return None
    coords = _vertices(value, label, code)
    if len(coords) < 2:
        raise _refuse(f"{label} needs at least two vertices to be a line.", code)
    return coords


def polyline_set(value: Any, *, label: str = "lines",
                 code: str = _DEFAULT_CODE) -> list[list[list[float]]] | None:
    """One or MANY drawn/typed lines as ``[[[lon, lat], ...], ...]``.

    The plural of :func:`polyline_coords`, and the shape a context slot that
    accepts ``geometry="polyline"`` normalizes to: a breakwater field is several
    ways, a single sketched barrier is one, and the consumer must not have to
    care which arrived. A GeoJSON-ish mapping (the draw gate's reply, a Feature,
    a FeatureCollection) unwraps here rather than in each consumer, which is the
    whole point of the species living in one place.
    """
    if value is None:
        return None
    if isinstance(value, Mapping):
        if value.get("type") == "FeatureCollection":
            features = value.get("features") or []
            out: list[list[list[float]]] = []
            for feature in features:
                one = polyline_set(feature, label=label, code=code)
                if one:
                    out.extend(one)
            if not out:
                raise _refuse(f"{label} carries no line geometry.", code)
            return out
        geometry = value.get("geometry") or value
        coords = geometry.get("coordinates") if isinstance(geometry, Mapping) else None
        if coords is None:
            raise _refuse(f"{label} carries no line coordinates.", code)
        value = coords if geometry.get("type") == "MultiLineString" else [coords]
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
        raise _refuse(f"{label} {value!r} is not a line or a list of lines.", code)
    # One line is a list of PAIRS; many lines is a list of LISTS of pairs. The
    # first element decides, which is why an empty value refuses above rather
    # than reading as "many lines, none of them".
    first = value[0]
    if isinstance(first, Sequence) and not isinstance(first, (str, bytes)) \
            and first and isinstance(first[0], Sequence) \
            and not isinstance(first[0], (str, bytes)):
        lines = [polyline_coords(line, label=label, code=code) for line in value]
        return [line for line in lines if line]
    one = polyline_coords(value, label=label, code=code)
    return [one] if one else None


def polygon_ring(value: Any, *, label: str = "polygon",
                 code: str = _DEFAULT_CODE) -> list[list[float]] | None:
    """A drawn or typed polygon as an OPEN outer ring - no repeated last vertex.

    Open is the one representation, chosen because the two producers disagree: the
    canvas closes its ring and a typed list usually does not. Normalizing here is
    what keeps "how many vertices does this polygon have" from having two answers.
    """
    if value is None:
        return None
    if isinstance(value, Mapping):
        value = ((value.get("geometry") or value).get("coordinates") or [None])[0]
        if value is None:
            raise _refuse(f"{label} carries no polygon coordinates.", code)
    coords = _vertices(value, label, code)
    if len(coords) >= 2 and coords[0] == coords[-1]:
        coords = coords[:-1]
    if len(coords) < 3:
        raise _refuse(f"{label} needs at least three vertices to be a polygon.", code)
    return coords


def lonlat_bbox(value: Any, *, label: str = "extent",
                code: str = _DEFAULT_CODE) -> tuple[float, float, float, float] | None:
    """``(min_lon, min_lat, max_lon, max_lat)``, ORDERED - a dragged box or a typed one.

    A box dragged right-to-left arrives with its corners the other way round; a
    consumer that subtracts them would get a negative extent and clip to nothing.
    """
    if value is None:
        return None
    parts: Iterable[Any]
    if isinstance(value, str):
        parts = [p for p in value.replace(" ", "").split(",") if p]
    elif isinstance(value, Sequence):
        parts = value
    else:
        raise _refuse(f"{label} {value!r} is not a bounding box.", code)
    try:
        nums = [float(v) for v in parts]
    except (TypeError, ValueError):
        raise _refuse(
            f"{label} {value!r} is not four numbers "
            "(min_lon, min_lat, max_lon, max_lat) in EPSG:4326.", code) from None
    if len(nums) != 4:
        raise _refuse(
            f"{label} has {len(nums)} numbers; a bounding box is exactly four "
            "(min_lon, min_lat, max_lon, max_lat) in EPSG:4326.", code)
    west, south, east, north = nums
    if not (_on_earth(west, south) and _on_earth(east, north)):
        raise _refuse(f"{label} {tuple(nums)} is off the earth.", code)
    return (min(west, east), min(south, north), max(west, east), max(south, north))


def bearing_deg(value: Any, *, label: str = "bearing",
                code: str = _DEFAULT_CODE) -> float | None:
    """A compass bearing, WRAPPED to [0, 360).

    A bearing is cyclic, so 370 is 10 and -90 is 270 - clamping one to a declared
    bound would turn a legal direction into a different legal direction. The wrap
    happens HERE, before the door, which is why the param can still declare
    ``bounds=(0, 360)`` and have them mean something.
    """
    if value is None:
        return None
    try:
        return float(value) % 360.0
    except (TypeError, ValueError):
        raise _refuse(f"{label} {value!r} is not a number of degrees.", code) from None


# -- coercion factories: the WIRE route into the same normalizers ---------- #

def _coercion(param: str, normalize: Callable[..., Any], label: str | None,
              code: str) -> Callable[[Mapping[str, Any]], dict[str, Any]]:
    def _coerce(args: Mapping[str, Any]) -> dict[str, Any]:
        return {param: normalize(args.get(param), label=label or param, code=code)}

    _coerce.__name__ = f"{normalize.__name__}:{param}"
    return _coerce


def point(param: str, *, label: str | None = None,
          code: str = _DEFAULT_CODE) -> Callable[[Mapping[str, Any]], dict[str, Any]]:
    """A coercion reading one wire field into a clean ``(lon, lat)``."""
    return _coercion(param, lonlat_point, label, code)


def bbox(param: str, *, label: str | None = None,
         code: str = _DEFAULT_CODE) -> Callable[[Mapping[str, Any]], dict[str, Any]]:
    """A coercion reading one wire field into an ordered lon/lat bounding box."""
    return _coercion(param, lonlat_bbox, label, code)


def bearing(param: str, *, label: str | None = None,
            code: str = _DEFAULT_CODE) -> Callable[[Mapping[str, Any]], dict[str, Any]]:
    """A coercion wrapping one wire field to a compass bearing in [0, 360)."""
    return _coercion(param, bearing_deg, label, code)
