"""NESTOR: the fields a maintenance dredge acts on, and the grade it digs to.

The module reads three files on every action - the polygons that NAME the
fields, the actions that say when and how deep, and the surface reference each
node's chainage and design grade are interpolated from - so a run naming two of
them is a run NESTOR cannot read. What is built here is their CONTENT; the three
keywords that name them are the GAIA wrapper's dredging composite.

A field is cut out of the reach's own mapped water rather than drawn: the
cross-channel box at the stated station, intersected with the water held back
from its banks by the declared setback, so the dig stops short of the toe it
would otherwise undercut and a stretch too narrow to dredge excludes ITSELF.
"""

from __future__ import annotations

import datetime
from typing import Any, Mapping

__all__ = ["DredgeFieldError", "NESTOR_TIME_ORIGIN", "dredge_field"]

#: The time origin the deck stamps so NESTOR's action dates map to sim seconds
#: through DateStringToSeconds (seconds since MARDAT/MARTIM).
NESTOR_TIME_ORIGIN = (2024, 1, 1, 0, 0, 0)
#: NESTOR matches a polygon NAME to an action's FieldDig/FieldDump on the first
#: three numerals, and its ThreeDigitsNumeral check rejects a leading zero.
_DIG_FIELD = "101_channel"
_DUMP_FIELD = "102_spoil"
#: How a comment opens in the files below. Every one of those readers keys on the
#: leading character alone, and it carries no space after it.
_COMMENT = "#"


class DredgeFieldError(RuntimeError):
    """No dredge field can be cut; carries an open-set ``error_code``."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


def _nestor_time(offset_s: float) -> str:
    """A sim-seconds offset as NESTOR's absolute ``yyyy.mm.dd-hh:mm:ss``."""
    base = datetime.datetime(*NESTOR_TIME_ORIGIN)
    return (base + datetime.timedelta(seconds=float(offset_s))
            ).strftime("%Y.%m.%d-%H:%M:%S")


def _channel_box(centerline: Any, station_frac: float, length_m: float,
                 width_m: float) -> Any:
    """A channel-crossing rectangle around one centerline station.

    The corners are laid on the local along-channel tangent, so the box brackets
    the wetted section rather than sitting square to the grid. It is deliberately
    wider than any channel: what decides the CROSS-channel extent of a dredge
    field is the water the reach was cut from, and the box only brackets the
    along-channel stretch.
    """
    import numpy as np
    from shapely.geometry import Polygon

    line = np.asarray(centerline, dtype=float)
    arc = np.concatenate([[0.0], np.cumsum(
        np.hypot(*np.diff(line, axis=0).T))])
    total = float(arc[-1]) if arc[-1] > 0 else 1.0
    index = int(np.argmin(np.abs(arc - max(0.0, min(1.0, float(station_frac)))
                                 * total)))
    centre = line[index]
    tangent = line[min(index + 1, len(line) - 1)] - line[max(index - 1, 0)]
    norm = float(np.hypot(tangent[0], tangent[1]))
    unit = np.array([1.0, 0.0]) if norm < 1e-9 else tangent / norm
    perp = np.array([-unit[1], unit[0]])
    half_l, half_w = length_m / 2.0, width_m / 2.0
    return Polygon([
        centre - half_l * unit - half_w * perp,
        centre + half_l * unit - half_w * perp,
        centre + half_l * unit + half_w * perp,
        centre - half_l * unit + half_w * perp])


def _reach_water(reach_polygon_utm: Any) -> Any:
    """The reach's mapped water as one shapely polygon, in the mesh's metres."""
    import numpy as np
    from shapely.geometry import Polygon, shape
    from shapely.ops import unary_union

    if reach_polygon_utm is None:
        raise DredgeFieldError(
            "TELEMAC_DREDGE_ZONE_UNMAPPED",
            "a dredge field is cut out of the reach's own mapped water and no "
            "reach polygon was handed to the author, so there is nothing to cut "
            "it from.")
    if hasattr(reach_polygon_utm, "geom_type"):
        geometry = reach_polygon_utm
    elif isinstance(reach_polygon_utm, dict):
        geometry = shape(reach_polygon_utm.get("geometry") or reach_polygon_utm)
    else:
        rings = np.asarray(reach_polygon_utm, dtype=float)
        geometry = (unary_union([Polygon(r) for r in rings]) if rings.ndim == 3
                    else Polygon(rings))
    return geometry.buffer(0)


def _dredge_field(water: Any, box: Any, offset_m: float, length_m: float, *,
                  what: str) -> Any:
    """One field: the station box, cut to the water held back from its banks.

    ONE mechanism, two behaviours. The inward offset is the BANK SETBACK, so the
    dig stops short of the toe it would otherwise undercut; and a stretch
    narrower than twice that setback has no inside left, so it excludes ITSELF
    rather than being excluded by a width rule nobody measured.
    """
    station = box.centroid
    field = _at_station(box.intersection(water.buffer(-float(offset_m))), station)
    if field is None:
        wetted = _at_station(box.intersection(water), station)
        measured = 0.0 if wetted is None else wetted.area / max(float(length_m), 1e-6)
        raise DredgeFieldError(
            "TELEMAC_DREDGE_ZONE_TOO_NARROW",
            f"the {what} field is empty: at this station the reach's mapped water "
            f"measures about {measured:.1f} m across, and a {float(offset_m):g} m "
            "bank setback leaves nothing between the two banks to dig. Lower "
            "dredge_bank_offset_m, move the station, or supply the polygon.")
    return field


def _at_station(cut: Any, station: Any) -> Any:
    """The one piece of ``cut`` at ``station``; ``None`` when the cut is empty.

    A box wide enough to cross any channel also crosses a MEANDER: on a bend it
    reaches the same reach's next loop, so the field is the water at THIS station
    rather than the largest piece the box happened to touch.
    """
    if cut.is_empty:
        return None
    if cut.geom_type == "Polygon":
        return cut
    pieces = [g for g in cut.geoms if g.geom_type == "Polygon" and not g.is_empty]
    return min(pieces, key=station.distance) if pieces else None


def _ring(geometry: Any) -> list[tuple[float, float]]:
    """A field polygon as the corner list NESTOR's polygon file writes."""
    return [(float(x), float(y)) for x, y in geometry.exterior.coords[:-1]]


def _supplied_field(corners: Any, water: Any, *, what: str
                    ) -> list[tuple[float, float]]:
    """A user-supplied field, validated CONTAINED in the reach's water."""
    from shapely.geometry import Polygon

    polygon = Polygon([(float(x), float(y)) for x, y in corners]).buffer(0)
    if not water.contains(polygon):
        outside = polygon.difference(water).area / max(polygon.area, 1e-9)
        raise DredgeFieldError(
            "TELEMAC_DREDGE_ZONE_OUTSIDE_WATER",
            f"the supplied {what} polygon lies {outside:.0%} outside the reach's "
            "mapped water; a dig or a dump on dry land is not a run this "
            "author can write.")
    return _ring(polygon)


def _dredge_zones(field: Mapping[str, Any], centerline: Any,
                  reach_polygon_utm: Any
                  ) -> tuple[list, list | None, dict[str, Any]]:
    """The dig field, the dump field when asked for, and what was measured.

    A SUPPLIED polygon wins and is validated inside the water. Otherwise the
    field AUTO-FILLS from geometry the run already measured: the cross-channel
    box at the stated station, cut to the reach polygon held back from its banks
    by the declared setback.
    """
    water = _reach_water(reach_polygon_utm)
    offset = float(field["bank_offset_m"])
    length = float(field["zone_len_m"])
    span = float(max(water.bounds[2] - water.bounds[0],
                     water.bounds[3] - water.bounds[1])) * 2.0
    supplied_dig = list(field.get("dig_utm") or ())
    supplied_dump = list(field.get("dump_utm") or ())
    want_dump = bool(field["disposal"]) or len(supplied_dump) >= 3

    if len(supplied_dig) >= 3:
        dig = _supplied_field(supplied_dig, water, what="dredge")
    else:
        dig = _ring(_dredge_field(
            water, _channel_box(centerline, field["station_frac"],
                                length, span), offset, length, what="dredge"))
    dump = None
    if want_dump:
        if len(supplied_dump) >= 3:
            dump = _supplied_field(supplied_dump, water, what="disposal")
        else:
            dump = _ring(_dredge_field(
                water, _channel_box(
                    centerline, field["disposal_station_frac"],
                    length, span), offset, length, what="disposal"))
    note = {
        "dredge_bank_offset_m": offset,
        "dredge_zone_len_m": length,
        "dredge_zone_source": "supplied" if len(supplied_dig) >= 3 else "auto",
        "disposal_zone_source": (None if dump is None else
                                 "supplied" if len(supplied_dump) >= 3 else "auto"),
    }
    return dig, dump, note


def _polygon_file(dig: Any, dump: Any) -> str:
    """NESTOR's polygon file: a named block per field, then a bare terminator."""
    lines = [f"{_COMMENT}NESTOR polygon file - dredge/dump zones (UTM m)",
             f"NAME {_DIG_FIELD}"]
    lines += [f"{x:.3f} {y:.3f}" for x, y in dig]
    if dump:
        lines.append(f"NAME {_DUMP_FIELD}")
        lines += [f"{x:.3f} {y:.3f}" for x, y in dump]
    return "\n".join(lines + ["ENDFILE"]) + "\n"


def _action_file(rule: Mapping[str, Any], *, duration_s: float,
                 has_dump: bool) -> str:
    """NESTOR's action file - one action, in one of two modes.

    SCHEDULED digs a target volume over a window. BY CRITERION triggers wherever
    the silted bed rises within a tolerance of the design grade, digs down at a
    stated rate, and re-arms across the run so re-siltation is dredged again.
    """
    mode = str(rule["mode"]).lower()
    start = _nestor_time(max(0.0, float(rule["start_frac"])) * duration_s)
    end = _nestor_time(min(1.0, float(rule["end_frac"])) * duration_s)
    # RESTART is read as a Fortran LOGICAL, so it takes a Fortran literal and not
    # the DAMOCLES YES/NO the steering files use.
    lines = ["/ NESTOR action file - channel maintenance dredging",
             f"/ mode={mode}", "RESTART = F", "ACTION"]
    if mode == "criterion":
        rate = max(float(rule["rate_m_per_s"]), 1.0e-9)
        lines += [
            "  ActionType      = Dig_by_criterion",
            f"  FieldDig        = {_DIG_FIELD}",
            f"  TimeStart       = {start}",
            f"  TimeEnd         = {end}",
            f"  TimeRepeat      = {max(duration_s / 4.0, 1.0):g}",
            f"  DigRate         = {rate:g}",
            f"  CritDepth       = {float(rule['crit_depth_m']):g}",
            f"  DigDepth        = {float(rule['dig_depth_m']):g}",
            "  MinVolume       = 0.",
            "  MinVolumeRadius = 0.",
            # SECTIONS interpolates the grade from the surface-reference profiles;
            # GRID would demand a gridded field that does not exist here.
            "  ReferenceLevel  = SECTIONS"]
        if has_dump:
            lines += [f"  FieldDump       = {_DUMP_FIELD}",
                      f"  DumpRate        = {rate:g}"]
    else:
        lines += [
            "  ActionType      = Dig_by_time",
            f"  FieldDig        = {_DIG_FIELD}",
            f"  TimeStart       = {start}",
            f"  TimeEnd         = {end}",
            f"  DigVolume       = {max(float(rule['volume_m3']), 1.0):g}"]
        if has_dump:
            # A dump field with no rate places the dug spoil over the same window.
            lines.append(f"  FieldDump       = {_DUMP_FIELD}")
    return "\n".join(lines + ["ENDACTION", "ENDFILE"]) + "\n"


def _surface_ref_file(centerline: Any, *, grade_m: float,
                      half_width_m: float) -> str:
    """NESTOR's surface reference file - a fence of channel-crossing profiles.

    Every field node has to lie BETWEEN two profiles for its grade and chainage
    to interpolate, and consecutive profiles have to stay near-parallel, so the
    fence spans the whole reach at a spacing set by its own width and the end
    profiles are nudged past the ends to enclose the extreme nodes.
    """
    import numpy as np

    line = np.asarray(centerline, dtype=float)
    arc = np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(line, axis=0).T))])
    total = float(arc[-1]) or 1.0
    step = max(int(len(line) // max(int(total / max(half_width_m, 1.0)) + 2, 3)), 1)
    indices = list(range(0, len(line), step))
    if indices[-1] != len(line) - 1:
        indices.append(len(line) - 1)
    lines = [f"{_COMMENT}NESTOR surface reference - design grade profiles"]
    for index in indices:
        centre = line[index]
        tangent = line[min(index + 1, len(line) - 1)] - line[max(index - 1, 0)]
        unit = tangent / (float(np.hypot(tangent[0], tangent[1])) or 1.0)
        perp = np.array([-unit[1], unit[0]])
        push = -5.0 if index == 0 else (5.0 if index == len(line) - 1 else 0.0)
        left = centre + push * unit - half_width_m * perp
        right = centre + push * unit + half_width_m * perp
        lines.append(f"{left[0]:.3f} {left[1]:.3f} {grade_m:.3f} "
                     f"{right[0]:.3f} {right[1]:.3f} {grade_m:.3f} "
                     f"{float(arc[index] / total):.5f}")
    return "\n".join(lines + ["END"]) + "\n"


def _field_half_width_m(field: Any) -> float:
    """Half the cut field's own largest extent - the fence's reach, measured."""
    import numpy as np

    corners = np.asarray(field, dtype=float)
    return float(np.max(np.ptp(corners, axis=0))) / 2.0


def _mean_bed_over(polygon: Any, node_xy: Any, node_bed: Any) -> float:
    """The mean bed inside ``polygon``, or over the whole mesh when none is in it."""
    import numpy as np
    from shapely.geometry import MultiPoint, Polygon
    from shapely.prepared import prep

    if node_xy is None or node_bed is None:
        return 0.0
    points = np.asarray(node_xy, dtype=float)
    bed = np.asarray(node_bed, dtype=float)
    field = prep(Polygon(np.asarray(polygon, dtype=float)))
    inside = np.array([field.covers(p) for p in MultiPoint(points).geoms])
    return float(np.mean(bed[inside])) if inside.any() else float(np.mean(bed))


def dredge_field(*, field: Mapping[str, Any], rule: Mapping[str, Any],
                 centerline_utm: Any, reach_polygon_utm: Any,
                 node_xy: Any, node_bed: Any, duration_s: float,
                 design_grade_m: float | None = None) -> dict[str, Any]:
    """The three NESTOR files' CONTENT, and what was measured to cut them.

    The design grade is the mean bed over the dig field when none was stated -
    the grade a maintenance dredge digs back TO is the channel that is there,
    not a number invented for the steering file.
    """
    from trid3nt_server.workflows.runtime import journal_note

    dig, dump, measured = _dredge_zones(field, centerline_utm, reach_polygon_utm)
    grade = (_mean_bed_over(dig, node_xy, node_bed) if design_grade_m is None
             else float(design_grade_m))
    # The profile fence has to enclose every field node, so it is sized off the
    # field that was actually cut rather than off a width nobody surveyed.
    half_width = _field_half_width_m(dig)
    journal_note(
        f"dredging: the {measured['dredge_zone_source']} dig field spans "
        f"{2.0 * half_width:.0f} m across the channel over a "
        f"{measured['dredge_zone_len_m']:g} m station, held "
        f"{measured['dredge_bank_offset_m']:g} m back from the mapped banks; the "
        f"design grade is {float(grade):.2f} m.")
    return {"action": _action_file(rule, duration_s=duration_s,
                                   has_dump=dump is not None),
            "polygon": _polygon_file(dig, dump),
            "surface_ref": _surface_ref_file(centerline_utm, grade_m=float(grade),
                                             half_width_m=half_width),
            "has_dump": dump is not None, "design_grade_m": float(grade),
            **measured}
