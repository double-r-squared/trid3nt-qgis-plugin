"""A module's outputs: the primitive set, and the read of each off a solved run.

A primitive is named from the module's variable vocabulary - ``field("T1", t)``,
``series("H")``, ``max_over_time("T1")``, ``extent()``, ``mesh()``,
``mass_balance()`` - and a template lists them with how each is published. The
result file is read INSIDE the TELEMAC image, where ``TelemacFile`` lives: one
container round trip per file, no network, and no parser of the format here."""

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
from trid3nt_server.workflows.publishing import Field, Frames, Read, Series
from trid3nt_server.workflows.runtime import DeclarativeError

logger = logging.getLogger("trid3nt_server.workflows.telemac.modules.outputs")

__all__ = [
    "PRIMITIVES",
    "Measure",
    "OutputEmpty",
    "Primitive",
    "SelafinReadError",
    "Solved",
    "extent",
    "field",
    "mass_balance",
    "max_over_time",
    "mesh",
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
_UNITS = {"MG/L": "mg/L", "G/L": "g/L"}


class SelafinReadError(RuntimeError):
    """The engine's reader could not open the result file."""

    error_code = "TELEMAC_RESULT_READ_FAILED"


class OutputEmpty(DeclarativeError):
    """The variable a primitive names is not in the result, or never rose above
    its floor: the run computed nothing the primitive can read."""

    error_code = "TELEMAC_OUTPUT_EMPTY"


def read_selafin(path: str | Path) -> dict[str, Any]:
    """A result file -> its mesh and per-variable time series.

    ``varnames`` carry no unit, ``ikle`` is 0-based, origins are not applied."""
    # {"varnames": [str], "npoin": int, "nelem": int,
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

@dataclass(frozen=True)
class Primitive:
    """One read a template asks for, and how it is published.

    A value: the template lists primitives, the door reads and publishes them."""

    kind: str
    variable: str | None = None
    #: ``"every"`` for the whole time series, an int frame index, or a float
    #: instant in seconds; ``None`` where the primitive has no time axis.
    t: Any = None
    #: The Point a series is read at; ``None`` reads the domain maximum.
    at: Any = None
    #: The plane of a 3D variable, bottom first; ``None`` on a 2D module.
    plane: int | None = None
    publish: str | None = None
    style: Any = None

    def layer(self, *, style: Mapping[str, Any] | None = None) -> "Primitive":
        """Publish this field as a layer on the map, styled by ``style``."""
        return replace(self, publish="layer", style=style)

    def chart(self) -> "Primitive":
        """Publish this series as a chart."""
        return replace(self, publish="chart")

    def animate(self) -> "Primitive":
        """Publish this field over time as an animation of the result file."""
        return replace(self, publish="animate")

    def measure(self, stat: str) -> "Measure":
        """One of this primitive's measures, as an answer a template names."""
        return Measure(self.key, stat)

    @property
    def key(self) -> "Primitive":
        """What one read answers: the primitive without its publishing."""
        return replace(self, publish=None, style=None)


@dataclass(frozen=True)
class Measure:
    """A measure of a primitive's read, named by a template as an answer."""

    primitive: Primitive
    stat: str


def field(name: str, t: Any = -1, *, plane: int | None = None) -> Primitive:
    """A variable over the domain at an instant, or over every instant."""
    return Primitive("field", variable=name, t=t, plane=plane)


def series(name: str, at: Any = None, *, plane: int | None = None) -> Primitive:
    """A variable over time: at a Point, or the domain maximum at each instant."""
    return Primitive("series", variable=name, at=at, plane=plane)


def max_over_time(name: str, *, plane: int | None = None) -> Primitive:
    """A variable's envelope: the maximum every node reached, and when."""
    return Primitive("max_over_time", variable=name, plane=plane)


def extent() -> Primitive:
    """The solved domain, and how much of it held water at the end."""
    return Primitive("extent")


def mesh() -> Primitive:
    """The mesh the run solved on: its counts and its measured edge."""
    return Primitive("mesh")


def mass_balance() -> Primitive:
    """The engine's own closure, off the listing it printed."""
    return Primitive("mass_balance")


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
                raise OutputEmpty(
                    f"{token} names tracer {index + 1}, and this run declares "
                    f"{len(names)}.")
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
    measures: dict[str, Any] = {"max": peak, "t_max": float(times[peak_i]),
                                "frames": int(values.shape[0])}
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
    # is an instant in seconds, read at the nearest frame the engine wrote.
    index = (int(primitive.t) if isinstance(primitive.t, int)
             else int(np.argmin(np.abs(times - float(primitive.t)))))
    lon, lat = solved.lonlat
    return Field(name=name, units=units, lon=lon, lat=lat,
                 ikle=solved.result["ikle2"], values=values[index],
                 t=float(times[index]), floor=_floor(primitive.variable,
                                                     measures["max"]),
                 measures=measures)


def read_series(primitive: Primitive, solved: Solved) -> Series:
    """``series(name, at)``: the domain maximum per instant, or a Point's value."""
    import numpy as np

    name, units, values = solved.frames(primitive.variable, primitive.plane)
    times = np.asarray(solved.result["times"], dtype="float64")
    measures = _envelope(primitive.variable, times, values)
    if primitive.at is None:
        return Series(name=name, units=units, times=times, values=values.max(axis=1),
                      at="the domain maximum", measures=measures)
    from trid3nt_server.workflows.inputs import Point
    from trid3nt_server.workflows.inputs.point import as_utm

    point = primitive.at if isinstance(primitive.at, Point) else Point(*primitive.at)
    px, py = as_utm(point, solved.utm_epsg)
    node = int(np.argmin(np.hypot(np.asarray(solved.result["x"]) - px,
                                  np.asarray(solved.result["y"]) - py)))
    return Series(name=name, units=units, times=times, values=values[:, node],
                  at=f"at {point.name or 'the point'}", measures=measures)


def read_max_over_time(primitive: Primitive, solved: Solved) -> Field:
    """``max_over_time(name)``: the maximum every node reached, and when."""
    import numpy as np

    name, units, values = solved.frames(primitive.variable, primitive.plane)
    times = np.asarray(solved.result["times"], dtype="float64")
    measures = _envelope(primitive.variable, times, values)
    lon, lat = solved.lonlat
    return Field(name=name, units=units, lon=lon, lat=lat,
                 ikle=solved.result["ikle2"], values=values.max(axis=0),
                 floor=_floor(primitive.variable, measures["max"]),
                 measures=measures)


def read_extent(primitive: Primitive, solved: Solved) -> Read:
    """``extent()``: the domain's lon/lat bounds, and its wetted fraction."""
    from ..products.run_reads import wetted_fraction

    lon, lat = solved.lonlat
    return Read(measures={
        "bbox": [float(lon.min()), float(lat.min()), float(lon.max()), float(lat.max())],
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
    """``mass_balance()``: the closure the engine printed in its own listing."""
    from ..products.run_reads import continuity_rel_error, gaia_mass_balance

    if solved.body.MODULE == "gaia":
        return Read(measures=gaia_mass_balance(solved.listing))
    return Read(measures={"continuity_rel_error": continuity_rel_error(solved.listing)})


#: The primitive set, as every wrapper binds it: the reader of each, by kind.
PRIMITIVES: Mapping[str, Any] = {
    "field": read_field, "series": read_series, "max_over_time": read_max_over_time,
    "extent": read_extent, "mesh": read_mesh, "mass_balance": read_mass_balance,
}
