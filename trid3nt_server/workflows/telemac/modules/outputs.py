"""A module's outputs: the primitive set, and the read of each off a solved run.

A primitive is named from the module's variable vocabulary - ``field("T1", t)``,
``series("H")``, ``max_over_time("T1")``, ``profile("T1", along)``, ``extent()``,
``mesh()``, ``mass_balance()``, ``drogues()`` - and a template lists them with
how each is published. The result file is read INSIDE the TELEMAC image, where
``TelemacFile`` lives: one container round trip per file, and no parser here."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, replace
from functools import cached_property
from pathlib import Path
from typing import Any, Mapping

from trid3nt_server.workflows.mesh.meshers.drivers import drivers_dir
from trid3nt_server.workflows.publishing import (
    Field,
    Frames,
    Profile,
    Read,
    Series,
    Track,
)
from trid3nt_server.workflows.runtime import DeclarativeError

logger = logging.getLogger("trid3nt_server.workflows.telemac.modules.outputs")

__all__ = [
    "PRIMITIVES",
    "Measure",
    "OutputEmpty",
    "Primitive",
    "SelafinReadError",
    "Solved",
    "drogues",
    "extent",
    "field",
    "mass_balance",
    "max_over_time",
    "mesh",
    "profile",
    "read_selafin",
    "series",
]

_TELEMAC_IMAGE_DEFAULT = "trid3nt-local/telemac:latest"
_INCONTAINER_SCRIPT = "telemac_result_driver.py"
_CONTAINER_TIMEOUT_S = 1800
_FIELDS_NAME = "telemac_result_fields.npz"
_META_NAME = "telemac_result_meta.json"

#: A tracer's visible edge is a fraction of its own peak, above a small absolute
#: floor: a dilute plume still draws whole, a run that injected nothing refuses,
#: and the frames counted as active are the ones the ribbon is visible in.
TRACER_EDGE_FRACTION = 0.05
TRACER_FLOOR = 1e-3
#: The engine spells a variable's unit in capitals after the name; these are
#: the SI spellings a reader expects for the ones that are not plain lower-case.
_UNITS = {"MG/L": "mg/L", "G/L": "g/L", "MGO2/L": "mgO2/L"}


class SelafinReadError(RuntimeError):
    """The engine's reader could not open the result file."""

    error_code = "TELEMAC_RESULT_READ_FAILED"


class OutputEmpty(DeclarativeError):
    """The variable a primitive names is not in the result, or never rose above
    its floor: the run computed nothing the primitive can read."""

    error_code = "TELEMAC_OUTPUT_EMPTY"


def read_selafin(path: str | Path) -> dict[str, Any]:
    """A result file -> its mesh and per-variable time series.

    ``varnames`` carry no unit (``varunits`` does), ``ikle`` is 0-based, origins
    are not applied."""
    # {"varnames": [str], "varunits": [str], "npoin": int, "nelem": int,
    #  "x": ndarray(npoin2), "y": ndarray(npoin2), "ikle": ndarray(nelem, ndp),
    #  "nplan": int, "npoin2": int, "nelem2": int, "ikle2": ndarray(nelem2, 3),
    #  "x_origin": int, "y_origin": int, "times": ndarray(nframes),
    #  "data": {varname: ndarray(nframes, npoin)}}
    # ``x``/``y`` stay exactly as the file stores them: a reader that places a
    # local-coordinate mesh adds the origin it recovers from the domain bbox,
    # and applying it here would double the offset.
    import numpy as np

    slf = Path(path).resolve()
    scratch = Path(tempfile.mkdtemp(prefix="telemac-read-"))
    try:
        meta = _run_driver(slf, scratch)
        fields = np.load(scratch / _FIELDS_NAME)
        varnames = [str(name) for name in meta["varnames"]]
        return {
            "varnames": varnames,
            "varunits": [str(unit) for unit in meta.get("varunits") or ()],
            "npoin": int(meta["npoin"]),
            "nelem": int(meta["nelem"]),
            # The vertical shape a 3D result carries. A 3D field is flat over
            # NPOIN3 and is NPLAN planes stacked over the 2D mesh, bottom first;
            # a 2D file reports one plane and the same mesh twice.
            "nplan": int(meta.get("nplan", 1)),
            "npoin2": int(meta.get("npoin2", meta["npoin"])),
            "nelem2": int(meta.get("nelem2", meta["nelem"])),
            "x": fields["x"],
            "y": fields["y"],
            "ikle": fields["ikle"],
            "ikle2": fields["ikle2"] if "ikle2" in fields else fields["ikle"],
            "x_origin": int(meta["x_origin"]),
            "y_origin": int(meta["y_origin"]),
            "times": fields["times"],
            "data": {name: fields[f"v{index}"]
                     for index, name in enumerate(varnames)},
        }
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _run_driver(slf: Path, scratch: Path) -> dict[str, Any]:
    """One driver run in the TELEMAC box -> the header it reported."""
    image = os.environ.get("TRID3NT_TELEMAC_IMAGE") or _TELEMAC_IMAGE_DEFAULT
    config = scratch / "telemac_result_config.json"
    config.write_text(json.dumps({"slf": f"/in/{slf.name}"}))
    argv = [
        "docker", "run", "--rm", "--network", "none",
        "-v", f"{drivers_dir()}:/drivers:ro", "-v", f"{slf.parent}:/in:ro",
        "-v", f"{scratch}:/data", image, "python",
        f"/drivers/{_INCONTAINER_SCRIPT}", f"/data/{config.name}", "/data"]
    logger.info("telemac result read: %s", " ".join(argv))
    cp = subprocess.run(argv, capture_output=True, text=True,
                        timeout=_CONTAINER_TIMEOUT_S)
    if cp.returncode != 0:
        raise SelafinReadError(
            f"the engine's own reader could not open {slf.name} "
            f"(rc={cp.returncode}):\n{cp.stdout[-2000:]}\n{cp.stderr[-2000:]}")
    return json.loads((scratch / _META_NAME).read_text())


# -- the primitive set ------------------------------------------------------ #

@dataclass(frozen=True, eq=True)
class Primitive:
    """One read a template asks for, and how it is published.

    A value: the template lists primitives, the door reads and publishes them.
    Hashed by what it reads, so a resolved point or line of any shape keys it."""

    kind: str
    variable: str | None = None
    #: ``"every"`` for the whole time series, an int frame index, or a float
    #: instant in seconds; ``None`` where the primitive has no time axis.
    t: Any = None
    #: The Point a series is read at; ``None`` reads the domain maximum.
    at: Any = None
    #: The line a profile is read along, as a geometry source.
    along: Any = None
    #: The plane of a 3D variable, bottom first; ``None`` on a 2D module.
    plane: int | None = None
    #: The coupled module whose result this reads; ``None`` reads the run's own.
    module: str | None = None
    publish: str | None = None
    style: Any = None
    #: ``(read, reads, params) -> lines`` drawn on the chart beside the read.
    reference: Any = None

    def __hash__(self) -> int:
        return hash((self.kind, self.variable, repr(self.t), repr(self.at),
                     repr(self.along), self.plane, self.module))

    def layer(self, *, style: Mapping[str, Any] | None = None) -> "Primitive":
        """Publish this field as a layer on the map, styled by ``style``."""
        return replace(self, publish="layer", style=style)

    def chart(self, *, reference: Any = None) -> "Primitive":
        """Publish this series or profile as a chart, ``reference`` lines beside it."""
        return replace(self, publish="chart", reference=reference)

    def animate(self) -> "Primitive":
        """Publish this field over time as an animation of the result file."""
        return replace(self, publish="animate")

    def station(self) -> "Primitive":
        """Publish this series at its Point as a station layer carrying it."""
        return replace(self, publish="station")

    def measure(self, stat: str) -> "Measure":
        """One of this primitive's measures, as an answer a template names."""
        return Measure(self.key, stat)

    @property
    def key(self) -> "Primitive":
        """What one read answers: the primitive without its publishing."""
        return replace(self, publish=None, style=None, reference=None)


@dataclass(frozen=True)
class Measure:
    """A measure of a primitive's read, named by a template as an answer."""

    primitive: Primitive
    stat: str


def field(name: str, t: Any = -1, *, plane: int | None = None,
          module: str | None = None) -> Primitive:
    """A variable over the domain at an instant, or over every instant."""
    return Primitive("field", variable=name, t=t, plane=plane, module=module)


def series(name: str, at: Any = None, *, plane: int | None = None,
           module: str | None = None) -> Primitive:
    """A variable over time: at a Point, or the domain maximum at each instant."""
    return Primitive("series", variable=name, at=at, plane=plane, module=module)


def max_over_time(name: str, *, plane: int | None = None,
                  module: str | None = None) -> Primitive:
    """A variable's envelope: the maximum every node reached, and when."""
    return Primitive("max_over_time", variable=name, plane=plane, module=module)


def profile(name: str, along: Any, t: Any = -1, *, plane: int | None = None,
            module: str | None = None) -> Primitive:
    """A variable along a line at an instant: the cross-section mean per station."""
    return Primitive("profile", variable=name, along=along, t=t, plane=plane,
                     module=module)


def extent(*, module: str | None = None) -> Primitive:
    """The solved domain, and how much of it held water at the end."""
    return Primitive("extent", module=module)


def mesh(*, module: str | None = None) -> Primitive:
    """The mesh the run solved on: its counts and its measured edge."""
    return Primitive("mesh", module=module)


def mass_balance(*, module: str | None = None) -> Primitive:
    """The engine's own closure, off the listing it printed."""
    return Primitive("mass_balance", module=module)


def drogues() -> Primitive:
    """The particle track the module wrote, as the positions at written instants."""
    return Primitive("drogues")


# -- the read of each, off a solved run ------------------------------------- #

class Solved:
    """One solved run's files, each opened once: the module's result, the listing.

    ``run`` is the handle the solve step returned; ``body`` names the module."""

    def __init__(self, run: Mapping[str, Any], body: Any) -> None:
        self.run = run
        self.body = body
        self.run_id = str(run["run_id"])
        self.utm_epsg = int(run["utm_epsg"])
        self.result_file = str(getattr(body, "RESULT_FILE", "") or run["result_basename"])

    @cached_property
    def result(self) -> dict[str, Any]:
        from ..solving.solve import download_result

        local = download_result(self.run_id, self.result_file)
        try:
            return read_selafin(local)
        finally:
            Path(local).unlink(missing_ok=True)

    @cached_property
    def listing(self) -> str:
        from ..solving.solve import download_result

        local = download_result(self.run_id, "full_listing.log")
        try:
            return Path(local).read_text(errors="replace")
        finally:
            Path(local).unlink(missing_ok=True)

    @cached_property
    def lonlat(self) -> tuple[Any, Any]:
        import numpy as np
        from pyproj import Transformer

        back = Transformer.from_crs(self.utm_epsg, 4326, always_xy=True)
        lon, lat = back.transform(np.asarray(self.result["x"]),
                                  np.asarray(self.result["y"]))
        return np.asarray(lon), np.asarray(lat)

    def variable(self, token: str) -> tuple[str, str]:
        """``(result variable, units)`` for a token of the module's vocabulary.

        ``T<n>`` is the n-th declared tracer name; the rest read off the table."""
        upper = str(token).strip().upper()
        if upper.startswith("T") and upper[1:].isdigit():
            names = list(self.run.get("tracer_names") or ())
            index = int(upper[1:]) - 1
            if index >= len(names):
                return self._appended_tracer(token, names, index)
            padded = str(names[index]).ljust(32)
            wanted, unit = padded[:16].strip(), padded[16:].strip()
        else:
            table = getattr(self.body, "VARIABLES", {})
            if upper not in table:
                raise OutputEmpty(
                    f"{token!r} is not in the {self.body.MODULE} variable "
                    f"vocabulary ({', '.join(table)}).")
            wanted, unit = table[upper]
        for name in self.result["varnames"]:
            if name.strip().upper() == wanted.upper():
                return name, _UNITS.get(unit.upper(), unit.lower())
        raise OutputEmpty(
            f"{token} ({wanted}) is not among the variables the result carries "
            f"({self.result['varnames']}).")

    def _appended_tracer(self, token: str, names: list[Any], index: int
                         ) -> tuple[str, str]:
        """A tracer a coupled module appended behind the carrier's declared ones.

        The result lists tracers in declared order, so the n-th sits n-1 past the
        first declared name; its unit is the one the record stores."""
        varnames = list(self.result["varnames"])
        units = list(self.result.get("varunits") or [""] * len(varnames))
        first = str(names[0]).ljust(32)[:16].strip().upper() if names else None
        start = next((i for i, name in enumerate(varnames)
                      if name.strip().upper() == first), None)
        position = None if start is None else start + index
        if position is None or position >= len(varnames):
            raise OutputEmpty(
                f"{token} names tracer {index + 1}; this run declares {len(names)} "
                f"and the result carries {varnames}.")
        unit = units[position] if position < len(units) else ""
        return varnames[position], _UNITS.get(unit.upper(), unit.lower())

    def frames(self, token: str, plane: int | None) -> tuple[str, str, Any]:
        """``(variable, units, values(nframes, npoin2))`` for a token, one plane."""
        import numpy as np

        name, units = self.variable(token)
        values = np.asarray(self.result["data"][name], dtype="float64")
        nplan = int(self.result.get("nplan", 1))
        if nplan > 1:
            npoin2 = int(self.result["npoin2"])
            values = values.reshape(values.shape[0], nplan, npoin2)[
                :, nplan - 1 if plane is None else int(plane), :]
        if values.size == 0:
            raise OutputEmpty(f"{token} carries no time steps in {self.result_file}.")
        return name, units, values


def _is_tracer(token: str) -> bool:
    upper = str(token).strip().upper()
    return upper.startswith("T") and upper[1:].isdigit()


def _floor(token: str, peak: float) -> float | None:
    """Where a variable stops being drawn: a tracer's visible edge, else nowhere."""
    return (max(TRACER_FLOOR, TRACER_EDGE_FRACTION * peak) if _is_tracer(token)
            else None)


def _envelope(token: str, times: Any, values: Any) -> dict[str, Any]:
    """The measures a variable over time carries: its peak, when, how long, how far."""
    import numpy as np

    per_frame = values.max(axis=1)
    peak_i = int(np.argmax(per_frame))
    peak = float(per_frame[peak_i])
    # A peak on the last instant is where the window closed, not where the
    # variable crested: the run was still rising, so the peak is a floor.
    measures: dict[str, Any] = {"max": peak, "t_max": float(times[peak_i]),
                                "frames": int(values.shape[0]),
                                "truncated": bool(times.size > 1
                                                  and peak_i == times.size - 1)}
    floor = _floor(token, peak)
    if floor is not None and peak < floor:
        raise OutputEmpty(
            f"{token} never exceeded its floor {floor:.4g} anywhere (peak {peak:.4g}).")
    if floor is not None:
        measures["active_frames"] = int((per_frame > floor).sum())
    return measures


def _travel_m(x: Any, y: Any, values: Any, floor: float | None) -> float | None:
    """How far the field's centroid moved from where it first appeared, in metres."""
    import numpy as np

    if floor is None:
        return None
    track = []
    for frame in values:
        above = frame > floor
        if above.any() and frame[above].sum() > 0:
            weight = frame[above]
            track.append(((x[above] * weight).sum() / weight.sum(),
                          (y[above] * weight).sum() / weight.sum()))
    if len(track) < 2:
        return None
    x0, y0 = track[0]
    return round(max(float(np.hypot(cx - x0, cy - y0)) for cx, cy in track), 1)


def read_field(primitive: Primitive, solved: Solved) -> Read:
    """``field(name, t)``: one instant as a Field, or every instant as Frames."""
    import numpy as np

    name, units, values = solved.frames(primitive.variable, primitive.plane)
    times = np.asarray(solved.result["times"], dtype="float64")
    measures = _envelope(primitive.variable, times, values)
    if primitive.t == "every":
        floor = _floor(primitive.variable, measures["max"])
        measures["travel_m"] = _travel_m(np.asarray(solved.result["x"]),
                                         np.asarray(solved.result["y"]),
                                         values, floor)
        return Frames(name=name, units=units, file=solved.result_file,
                      group=name.strip(), epsg=solved.utm_epsg,
                      reference_time=solved.run.get("started_at"),
                      frames=int(times.size), measures=measures)
    # An int is a frame index, counted from the file's own first frame; a float
    # is an instant in seconds, read at the nearest frame the engine wrote. The
    # measures are the frame's own; the envelope only sets the visible edge.
    index = (int(primitive.t) if isinstance(primitive.t, int)
             else int(np.argmin(np.abs(times - float(primitive.t)))))
    lon, lat = solved.lonlat
    frame = values[index]
    return Field(name=name, units=units, lon=lon, lat=lat,
                 ikle=solved.result["ikle2"], values=frame,
                 t=float(times[index]), floor=_floor(primitive.variable,
                                                     measures["max"]),
                 measures={"max": float(frame.max()), "min": float(frame.min()),
                           "t": float(times[index]), "frames": int(times.size)})


def _point(at: Any) -> Any:
    """The Point a primitive was anchored at, whatever shape the anchor took."""
    from trid3nt_server.workflows.inputs import Point

    if isinstance(at, Point):
        return at
    if isinstance(at, Mapping):
        return Point(float(at["lon"]), float(at["lat"]), at.get("name"))
    return Point(*at)


def read_series(primitive: Primitive, solved: Solved) -> Series:
    """``series(name, at)``: the domain maximum per instant, or a Point's value.

    A token the module prints rather than writes is read off the listing, at
    the liquid boundary the Point lies on."""
    import numpy as np

    if primitive.variable in solved.body.LISTING:
        return _boundary_series(primitive, solved)
    name, units, values = solved.frames(primitive.variable, primitive.plane)
    times = np.asarray(solved.result["times"], dtype="float64")
    measures = _envelope(primitive.variable, times, values)
    if primitive.at is None:
        return Series(name=name, units=units, times=times, values=values.max(axis=1),
                      at="the domain maximum", measures=measures)
    from trid3nt_server.workflows.inputs.point import as_utm

    point = _point(primitive.at)
    px, py = as_utm(point, solved.utm_epsg)
    node = int(np.argmin(np.hypot(np.asarray(solved.result["x"]) - px,
                                  np.asarray(solved.result["y"]) - py)))
    lon, lat = solved.lonlat
    return Series(name=name, units=units, times=times, values=values[:, node],
                  at=f"at {point.name or 'the point'}",
                  lon=float(lon[node]), lat=float(lat[node]), measures=measures)


def _boundary_series(primitive: Primitive, solved: Solved) -> Series:
    """A printed token at a Point: the series the listing carries for the liquid
    boundary nearest it, as the engine measured the flux across that boundary.
    The measures carry the volume that crossed it over the sampled instants."""
    import numpy as np
    from pyproj import Transformer

    from ..products.run_reads import boundary_flux
    from trid3nt_server.workflows.inputs.point import as_utm

    if primitive.at is None:
        raise OutputEmpty(f"{primitive.variable} is read at a liquid boundary; "
                          "series() needs the Point it is read at.")
    boundaries = list(solved.run.get("liquid_boundaries") or ())
    if not boundaries:
        raise OutputEmpty("the run records no liquid boundary, so there is none "
                          f"to read {primitive.variable} across.")
    point = _point(primitive.at)
    px, py = as_utm(point, solved.utm_epsg)
    nearest = min(boundaries,
                  key=lambda b: float(np.hypot(float(b["x"]) - px,
                                               float(b["y"]) - py)))
    times, flows = boundary_flux(solved.listing, boundary=int(nearest["number"]))
    if not times:
        raise OutputEmpty(f"the listing prints no flux for liquid boundary "
                          f"{nearest['number']}.")
    times_arr = np.asarray(times, dtype="float64")
    flows_arr = np.asarray(flows, dtype="float64")
    measures = _envelope(primitive.variable, times_arr, flows_arr[:, None])
    measures["integral"] = round(float(np.trapezoid(flows_arr, times_arr)), 3)
    name, unit = solved.body.VARIABLES[primitive.variable]
    lon, lat = Transformer.from_crs(solved.utm_epsg, 4326, always_xy=True).transform(
        float(nearest["x"]), float(nearest["y"]))
    return Series(name=name, units=_UNITS.get(unit.upper(), unit.lower()),
                  times=times_arr, values=flows_arr,
                  at=f"at {point.name or 'liquid boundary ' + str(nearest['number'])}",
                  lon=float(lon), lat=float(lat), measures=measures)


def read_max_over_time(primitive: Primitive, solved: Solved) -> Field:
    """``max_over_time(name)``: the maximum every node reached, and when."""
    import numpy as np

    name, units, values = solved.frames(primitive.variable, primitive.plane)
    times = np.asarray(solved.result["times"], dtype="float64")
    measures = _envelope(primitive.variable, times, values)
    lon, lat = solved.lonlat
    envelope = values.max(axis=0)
    # The extreme and the field: one pit can set the maximum while the field the
    # run produced sits orders of magnitude below it, so the 99th percentile of
    # the envelope rides beside the maximum.
    measures["p99"] = float(np.percentile(envelope, 99))
    return Field(name=name, units=units, lon=lon, lat=lat,
                 ikle=solved.result["ikle2"], values=envelope,
                 floor=_floor(primitive.variable, measures["max"]),
                 measures=measures)


def read_extent(primitive: Primitive, solved: Solved) -> Read:
    """``extent()``: the domain's lon/lat bounds, its area, its wetted fraction."""
    from ..products.run_reads import mesh_area_m2, wetted_fraction

    lon, lat = solved.lonlat
    return Read(measures={
        "bbox": [float(lon.min()), float(lat.min()), float(lon.max()), float(lat.max())],
        "area_km2": round(mesh_area_m2(solved.result) / 1.0e6, 4),
        **wetted_fraction(solved.result)})


def read_mesh(primitive: Primitive, solved: Solved) -> Read:
    """``mesh()``: the mesh the run solved on - its counts and its measured edge."""
    return Read(measures={
        "nodes": int(solved.result["npoin2"]),
        "elements": int(solved.result["nelem2"]),
        "planes": int(solved.result.get("nplan", 1)),
        "epsg": solved.utm_epsg,
        "size_m": solved.run.get("mesh_size_m"),
        "resolution_label": solved.run.get("mesh_resolution_label")})


def read_mass_balance(primitive: Primitive, solved: Solved) -> Read:
    """``mass_balance()``: the closure the engine printed in its own listing.

    The water balance carries the final block's volumes, outflow-positive
    across the liquid boundaries like the flux series, and where the runoff
    routine printed the rainfall it accumulated, the volume that fell on the
    meshed domain and the fraction of it that left."""
    from ..products.run_reads import (
        continuity_rel_error,
        final_balance,
        gaia_mass_balance,
        mesh_area_m2,
    )

    if solved.body.MODULE == "gaia":
        return Read(measures=gaia_mass_balance(solved.listing))
    measures: dict[str, Any] = {
        "continuity_rel_error": continuity_rel_error(solved.listing)}
    final = final_balance(solved.listing)
    if "boundary_volume_m3" in final:
        final["outflow_volume_m3"] = -final.pop("boundary_volume_m3") + 0.0
    if "rain_depth_m" in final:
        final["rain_volume_m3"] = round(
            final["rain_depth_m"] * mesh_area_m2(solved.result), 3)
        if final.get("outflow_volume_m3") is not None and final["rain_volume_m3"] > 0.0:
            final["runoff_coefficient"] = round(
                final["outflow_volume_m3"] / final["rain_volume_m3"], 6)
    return Read(measures={**measures, **final})


#: How many stations a profile is binned into along its line.
_PROFILE_STATIONS = 60
#: A node shallower than this at the instant is dry and off the profile.
_PROFILE_WET_M = 0.01


def _chainage(x: Any, y: Any, line: Any) -> tuple[Any, Any]:
    """Each node's arc length along ``line`` at its nearest segment, and that
    segment's unit direction: the along-line coordinate and its local axis."""
    import numpy as np

    line = np.asarray(line, dtype="float64")
    ax, ay, bx, by = line[:-1, 0], line[:-1, 1], line[1:, 0], line[1:, 1]
    dx, dy = bx - ax, by - ay
    length = np.hypot(dx, dy)
    cum = np.concatenate([[0.0], np.cumsum(length)])
    t = np.clip(((x[:, None] - ax) * dx + (y[:, None] - ay) * dy)
                / np.maximum(length * length, 1e-9), 0.0, 1.0)
    d2 = (ax + t * dx - x[:, None]) ** 2 + (ay + t * dy - y[:, None]) ** 2
    k = np.argmin(d2, axis=1)
    s = cum[k] + t[np.arange(x.size), k] * length[k]
    unit = np.stack([dx[k], dy[k]], axis=1) / np.maximum(length[k], 1e-9)[:, None]
    return s, unit


def read_profile(primitive: Primitive, solved: Solved) -> Profile:
    """``profile(name, along, t)``: the depth-weighted cross-section mean of a
    variable per station down a line, at one instant. The line runs the way the
    solved flow goes when the module carries velocities; the reach's own order
    otherwise. The measures carry the minimum and maximum with their stations,
    and the depth-weighted mean along-line speed when velocities are carried."""
    import numpy as np

    from trid3nt_server.workflows.mesh.shared.nodes import read_centerline_utm

    name, units, values = solved.frames(primitive.variable, primitive.plane)
    times = np.asarray(solved.result["times"], dtype="float64")
    index = (int(primitive.t) if isinstance(primitive.t, int)
             else int(np.argmin(np.abs(times - float(primitive.t)))))
    x = np.asarray(solved.result["x"], dtype="float64")
    y = np.asarray(solved.result["y"], dtype="float64")
    line = read_centerline_utm(primitive.along, solved.utm_epsg)
    s, axis = _chainage(x, y, line)
    weight, along = np.ones(x.size), None
    if all(token in solved.body.VARIABLES for token in ("H", "U", "V")):
        depth = solved.frames("H", primitive.plane)[2][index]
        weight = np.where(depth > _PROFILE_WET_M, depth, 0.0)
        u = solved.frames("U", primitive.plane)[2][index]
        v = solved.frames("V", primitive.plane)[2][index]
        along = u * axis[:, 0] + v * axis[:, 1]
        if float((along * weight).sum()) < 0.0:
            s, along = s.max() - s, -along
    if not (weight > 0.0).any():
        raise OutputEmpty(f"no wet node at t = {times[index]:g} s to read "
                          f"{primitive.variable} along the line.")
    edges = np.linspace(0.0, float(s.max()) or 1.0, _PROFILE_STATIONS + 1)
    station = np.clip(np.digitize(s, edges) - 1, 0, _PROFILE_STATIONS - 1)
    frame = values[index]
    distance, means = [], []
    for b in range(_PROFILE_STATIONS):
        w = weight[station == b]
        if w.sum() <= 0.0:
            continue
        distance.append(float(0.5 * (edges[b] + edges[b + 1])))
        means.append(float((frame[station == b] * w).sum() / w.sum()))
    if len(distance) < 3:
        raise OutputEmpty(f"{primitive.variable} reaches {len(distance)} wet "
                          "stations along the line; a profile needs three.")
    means_arr = np.asarray(means)
    lo, hi = int(means_arr.argmin()), int(means_arr.argmax())
    measures: dict[str, Any] = {
        "min": float(means_arr[lo]), "x_min_m": distance[lo],
        "max": float(means_arr[hi]), "x_max_m": distance[hi],
        "t": float(times[index]), "stations": len(distance),
        "velocity_mps": (None if along is None else
                         float((along * weight).sum() / weight.sum()))}
    return Profile(name=name, units=units, distance_m=np.asarray(distance),
                   values=means_arr, along="downstream distance", measures=measures)


def parse_drogues(path: str | Path) -> list[tuple[float, list[tuple[float, float]]]]:
    """The TecPlot ASCII drogues track -> ``[(t_s, [(x, y), ...]), ...]``.

    One ZONE per written instant; an unparsable row is skipped, not fatal."""
    import re

    zones: list[tuple[float, list[tuple[float, float]]]] = []
    time_s: float | None = None
    points: list[tuple[float, float]] = []
    for line in Path(path).read_text(errors="replace").splitlines():
        if line.startswith("ZONE"):
            if time_s is not None:
                zones.append((time_s, points))
            stamp = re.search(r"SOLUTIONTIME=\s*([\d.]+)", line)
            time_s, points = (float(stamp.group(1)) if stamp else 0.0), []
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 3:
            try:
                points.append((float(parts[1]), float(parts[2])))
            except ValueError:
                continue
    if time_s is not None:
        zones.append((time_s, points))
    return zones


#: How many written instants the track keeps: the release, mid-run and the end.
_TRACK_SNAPSHOTS = 3


def read_drogues(primitive: Primitive, solved: Solved) -> Track:
    """``drogues()``: the particle positions the module wrote, at three instants."""
    import numpy as np
    from pyproj import Transformer

    from ..solving.solve import download_result
    from .telemac2d import DROGUES_FILENAME

    local = download_result(solved.run_id, DROGUES_FILENAME)
    try:
        zones = parse_drogues(local)
    finally:
        Path(local).unlink(missing_ok=True)
    written = [(t, pts) for t, pts in zones if pts]
    if not written:
        raise OutputEmpty("the drogues track holds no float at any written instant.")
    to_lonlat = Transformer.from_crs(solved.utm_epsg, 4326, always_xy=True).transform
    keep = ([written[0], written[len(written) // 2], written[-1]]
            if len(written) >= _TRACK_SNAPSHOTS else written)
    features = []
    for t, pts in keep:
        lon, lat = to_lonlat(*zip(*pts))
        features.append({"type": "Feature",
                         "geometry": {"type": "MultiPoint",
                                      "coordinates": [[round(a, 6), round(b, 6)]
                                                      for a, b in zip(lon, lat)]},
                         "properties": {"t_s": t, "n": len(pts)}})
    first, last = np.mean(written[0][1], axis=0), np.mean(written[-1][1], axis=0)
    released, remaining = len(written[0][1]), len(written[-1][1])
    return Track(features={"type": "FeatureCollection", "features": features},
                 measures={"released": released, "remaining": remaining,
                           "exited": max(0, released - remaining),
                           "instants": len(written),
                           "drift_m": round(float(np.hypot(*(last - first))), 1)})


#: The primitive set, as every wrapper binds it: the reader of each, by kind.
PRIMITIVES: Mapping[str, Any] = {
    "field": read_field, "series": read_series, "max_over_time": read_max_over_time,
    "profile": read_profile, "extent": read_extent, "mesh": read_mesh,
    "mass_balance": read_mass_balance,
}
