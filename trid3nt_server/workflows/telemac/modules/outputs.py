"""A module's outputs: the primitive set, the read of each off a solved run, and
the format that read is delivered in.

A primitive is named from the module's variable vocabulary - ``field("T1", t)``,
``series("H")``, ``max_over_time("T1")``, ``profile("T1", along)``, ``extent()``,
``mesh()``, ``mass_balance()``, ``drogues()``, ``column("T1", at)`` - and a
template lists them with how each is published. The result file is read INSIDE
the TELEMAC image, where ``TelemacFile`` lives: one container round trip per
file, and no parser here."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, replace
from dataclasses import field as dataclass_field
from functools import cached_property
from pathlib import Path
from typing import Any, Mapping

from trid3nt_server.render.formats import Chart, Deliverable, Mesh, Vector
from trid3nt_server.workflows.mesh.meshers.drivers import drivers_dir
from trid3nt_server.workflows.runtime import DeclarativeError

logger = logging.getLogger("trid3nt_server.workflows.telemac.modules.outputs")

__all__ = [
    "NOT_ASKED",
    "NOT_READ",
    "PRIMITIVES",
    "Field",
    "Frames",
    "Line",
    "Measure",
    "Profile",
    "Read",
    "Series",
    "Track",
    "deliver",
    "OutputEmpty",
    "Primitive",
    "SelafinReadError",
    "Solved",
    "mesh_area_m2",
    "wetted_fraction",
    "column",
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

#: A declared EDGE is a fraction of the variable's OWN range - the magnitude the
#: row reads over the record - and never an absolute concentration: a trace
#: substance at half a microgram per litre has a shape, and an absolute floor
#: would refuse it as an empty field. The frames counted as active are the ones
#: it is visible in. WHICH rows have an edge is the module's table, never a
#: spelling.
EDGE_FRACTION = 0.05
#: The engine spells a variable's unit in capitals after the name; these are
#: the SI spellings a reader expects for the ones that are not plain lower-case.
_UNITS = {"MG/L": "mg/L", "G/L": "g/L", "MGO2/L": "mgO2/L", "DEGC": "degC"}


# -- what a primitive READ, as a value ------------------------------------- #

@dataclass(frozen=True, kw_only=True)
class Read:
    """What one primitive measured, by name. A bare read publishes nothing."""

    measures: Mapping[str, Any] = dataclass_field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class Field(Read):
    """One variable over the 2D nodes at one instant, or its envelope over time.

    ``t`` is the instant, ``None`` the envelope; ``plane`` names which plane of a
    3D result this is, ``None`` on a 2D one; a node below ``floor`` is nothing."""

    name: str
    units: str
    values: Any
    t: float | None = None
    plane: str | None = None
    floor: float | None = None
    #: Which of the values the measures and the legend were read over - the
    #: nodes this run held water on. ``None`` reads the field whole.
    wet: Any = None


@dataclass(frozen=True, kw_only=True)
class Line:
    """One more line on a chart, over the chart's own x axis: a reference the
    caller computed beside the read, drawn under its own label."""

    label: str
    x: Any
    values: Any


@dataclass(frozen=True, kw_only=True)
class Series(Read):
    """One variable over time: the domain maximum at each instant, or a point's."""

    name: str
    units: str
    times: Any
    values: Any
    #: Where the series was read - ``"the domain maximum"`` or a point's name.
    at: str
    #: The station the series was read at, in lon/lat; ``None`` for a maximum.
    lon: float | None = None
    lat: float | None = None
    lines: tuple[Line, ...] = ()


@dataclass(frozen=True, kw_only=True)
class Profile(Read):
    """One variable along a line at one instant: a value per station."""

    name: str
    units: str
    distance_m: Any
    values: Any
    #: What the x axis IS - ``"downstream distance"``.
    along: str
    lines: tuple[Line, ...] = ()


@dataclass(frozen=True, kw_only=True)
class Track(Read):
    """Positions at written instants, as a GeoJSON FeatureCollection in lon/lat."""

    features: Mapping[str, Any]


@dataclass(frozen=True, kw_only=True)
class Frames(Read):
    """One variable over time, as the result file an animation plays from."""

    name: str
    units: str
    #: Every frame over the 2D nodes. A temporal layer's legend is measured over
    #: the WHOLE record: ranged on the last frame alone, a variable that peaks
    #: and flushes paints its animation against an empty field.
    values: Any
    #: The result file's basename under the run prefix.
    file: str
    #: The dataset group the mesh reader binds the variable by.
    group: str
    epsg: int
    reference_time: str | None
    frames: int
    #: Where the variable stops being drawn. A group the RESULT FILE carries
    #: cannot be rewritten with nothing below it, so the floor travels on the
    #: style row and the renderer masks there.
    floor: float | None = None


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
    #: The line a profile is read along, as a geometry source, and how far off
    #: it a node still belongs to the profile; ``None`` reads the whole domain.
    along: Any = None
    within: Any = None
    #: The AREA a field's measures are taken within, as a geometry source;
    #: ``None`` measures the whole domain. What is PUBLISHED is the whole field
    #: either way - an area narrows the answer, never the picture.
    over: Any = None
    #: The THRESHOLD a series is asked when it first exceeds, as a number or a
    #: declared param. A crossing is a function of the whole series rather than
    #: of any one of its statistics, so it is read here and answered as
    #: ``t_above``; ``None`` asks no crossing and emits no measure.
    above: Any = None
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
                     repr(self.along), repr(self.within), repr(self.over),
                     repr(self.above), self.plane, self.module))

    def layer(self, *, style: Mapping[str, Any] | None = None) -> "Primitive":
        """Publish this field as a layer on the map, styled by ``style``."""
        return replace(self, publish="layer", style=style)

    def chart(self, *, reference: Any = None) -> "Primitive":
        """Publish this series or profile as a chart, ``reference`` lines beside it."""
        return replace(self, publish="chart", reference=reference)

    def animate(self, *, style: Mapping[str, Any] | None = None) -> "Primitive":
        """Publish this field over time as ONE temporal layer, styled by ``style``.

        A time-varying variable has no still layer beside it: the layer carries
        every frame, and a picture of one instant is a render of that layer."""
        return replace(self, publish="animate", style=style)

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


#: What a measure answers when the value it is held to was never supplied. A
#: question nobody asked is not a gap in the delivery, so the sheet states it and
#: the answer PASSES; the sentence rides the answer itself, which is the seam a
#: measure already has for saying something a number cannot.
NOT_ASKED = "not asked: "
#: What a measure answers when the read it names came back empty: the reason,
#: prefixed so a delivery refuses the sentence rather than reading it as an answer.
NOT_READ = "not read: "


@dataclass(frozen=True)
class Measure:
    """A measure of a primitive's read, named by a template as an answer.

    ``over`` answers with the measure's ratio to a sheet value, ``below`` with
    whether the measure lies under one: the two arithmetics a verdict needs."""

    primitive: Primitive
    stat: str
    op: str | None = None
    against: Any = None
    held_to: str | None = None
    #: What the sheet states INSTEAD when the value above was never supplied and
    #: the read came back empty: the question was not asked, not missed.
    unasked: str | None = None
    #: What the run states INSTEAD when the read came back WHOLE and the stat
    #: this measure names has no value in it - an instant nothing reached. The
    #: run measured the question and the answer is the absence.
    absent: str | None = None

    def over(self, value: Any) -> "Measure":
        """This measure divided by ``value``: a number, a declared param, or
        ANOTHER MEASURE, which makes a ratio between two reads expressible.

        A measure held against another reads its own primitive UNANCHORED, so
        the place a ratio's denominator is read at is not a slot the user fills."""
        placed = isinstance(value, Measure) and any(
            getattr(value.primitive, slot) is not None
            for slot in ("at", "along", "over"))
        if placed:
            raise DeclarativeError(
                f"{value.primitive.kind}({value.primitive.variable}) is held to a "
                "place the run resolves, and a measure used as a DENOMINATOR is "
                "read without one; hold this measure to an unplaced read.")
        return replace(self, op="over", against=value, held_to=_named(value))

    def below(self, value: Any) -> "Measure":
        """Whether this measure lies below ``value``, a number or a declared param."""
        return replace(self, op="below", against=value, held_to=_named(value))

    def otherwise(self, sentence: str) -> "Measure":
        """What this measure reads when the stat it names carries no value: the
        series was read whole and the instant is not in it.

        An instant nothing reached is not a gap in the delivery, so the sentence
        rides the answer and the delivery PASSES on it."""
        return replace(self, absent=str(sentence))

    def needs(self, value: Any, *, without: str) -> "Measure":
        """Asked only where ``value`` was supplied; ``without`` says what the run
        is instead, and a delivery PASSES on that sentence rather than refusing.

        The lever is the user's: a run nobody asked the question of has no gap."""
        return replace(self, against=value, held_to=_named(value),
                       unasked=str(without))

    def answer(self, measured: Any, against: Any) -> Any:
        """The answer this measure names, off what was read and what it is held to."""
        if measured is None and self.absent:
            return f"{NOT_ASKED}{self.absent}"
        if measured is None or self.op is None:
            return measured
        if against is None:
            return (f"{NOT_ASKED}{self.held_to or 'the value it is held to'} "
                    "was not supplied")
        if self.op == "over":
            return None if not float(against) else float(measured) / float(against)
        return bool(float(measured) < float(against))


def _named(value: Any) -> str | None:
    """What a measure is held to: a declared param's name, another read's own
    words, or ``None`` for a literal."""
    if isinstance(value, Measure):
        return f"{value.stat} of {value.primitive.variable}"
    return getattr(value, "name", None)


def field(name: str, t: Any = -1, *, plane: int | None = None,
          module: str | None = None, over: Any = None) -> Primitive:
    """A variable over the domain at an instant, or over every instant.

    ``over`` narrows what the MEASURES are taken within to one area."""
    return Primitive("field", variable=name, t=t, plane=plane, module=module,
                     over=over)


def series(name: str, at: Any = None, *, plane: int | None = None,
           module: str | None = None, above: Any = None) -> Primitive:
    """A variable over time: at a Point, or the domain maximum at each instant.

    ``above`` is a threshold the series is asked when it first exceeds, which
    the read answers as ``t_above``."""
    return Primitive("series", variable=name, at=at, plane=plane, module=module,
                     above=above)


def max_over_time(name: str, *, plane: int | None = None,
                  module: str | None = None) -> Primitive:
    """A variable's envelope: the maximum every node reached, and when."""
    return Primitive("max_over_time", variable=name, plane=plane, module=module)


def profile(name: str, along: Any, t: Any = -1, *, within_m: Any = None,
            plane: int | None = None, module: str | None = None) -> Primitive:
    """A variable along a line at an instant: the mean per station of the nodes
    within ``within_m`` of the line, or of the whole domain when none is stated."""
    return Primitive("profile", variable=name, along=along, within=within_m, t=t,
                     plane=plane, module=module)


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


def column(name: str, at: Any = None, t: Any = -1) -> Primitive:
    """A 3D variable down the planes at a Point, or at the deepest node, at an instant."""
    return Primitive("column", variable=name, at=at, t=t)


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
        # WHICH of the run's files MDAL opens as the mesh a derived group is
        # drawn over. A 3D result is no mesh format: the module writes the 2D
        # result beside it, and that is the mesh its planes are read onto.
        self.display_file = str(run.get("display_basename") or self.result_file)

    @cached_property
    def result(self) -> dict[str, Any]:
        from trid3nt_server.workflows.solver.solver import download_result

        local = download_result(self.run_id, self.result_file)
        try:
            return read_selafin(local)
        finally:
            Path(local).unlink(missing_ok=True)

    @cached_property
    def listing(self) -> str:
        from trid3nt_server.workflows.solver.solver import download_result

        local = download_result(self.run_id, "full_listing.log")
        try:
            return Path(local).read_text(errors="replace")
        finally:
            Path(local).unlink(missing_ok=True)

    @cached_property
    def xy(self) -> tuple[Any, Any]:
        """The node coordinates of the 2D mesh every read is taken over.

        A 3D result stores NPLAN copies of the 2D mesh and its coordinate arrays
        span all of them, while every value a read holds has been reduced to one
        plane - so the first NPOIN2 of them are the nodes those values stand on."""
        import numpy as np

        nodes = int(self.result["npoin2"])
        return (np.asarray(self.result["x"])[:nodes],
                np.asarray(self.result["y"])[:nodes])

    @cached_property
    def lonlat(self) -> tuple[Any, Any]:
        import numpy as np
        from pyproj import Transformer

        x, y = self.xy
        back = Transformer.from_crs(self.utm_epsg, 4326, always_xy=True)
        lon, lat = back.transform(x, y)
        return np.asarray(lon), np.asarray(lat)

    @property
    def display_mesh(self) -> tuple[int, int]:
        """``(nodes, cells)`` of the 2D mesh a derived dataset group is bound to."""
        return int(self.result["npoin2"]), int(self.result["nelem2"])

    @cached_property
    def bbox(self) -> tuple[float, float, float, float]:
        """The lon/lat box the solved mesh spans - where the camera flies."""
        lon, lat = self.lonlat
        return (float(lon.min()), float(lat.min()),
                float(lon.max()), float(lat.max()))

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
            # WHAT THIS RUN WROTE is the run's own published table: a row a
            # module allocates under a switch - a dynamic ice cover, a salinity,
            # a frazil class - is written only where the deck opened it, and the
            # static vocabulary alone cannot say which.
            published = self.output_row(upper)
            table = getattr(self.body, "MODULE_OUTPUT", {})
            if published.get("name"):
                wanted, unit = str(published["name"]), str(published.get("unit") or "")
            elif upper in table:
                wanted, unit = table[upper].name, table[upper].unit
            else:
                raise OutputEmpty(
                    f"{token!r} is not a variable {self.body.MODULE} writes "
                    f"({', '.join(table)}).")
        for name in self.result["varnames"]:
            if name.strip().upper() == wanted.upper():
                return name, _UNITS.get(unit.upper(), unit.lower())
        raise OutputEmpty(
            f"{token} ({wanted}) is not among the variables the result carries "
            f"({self.result['varnames']}).")

    def output_row(self, token: str) -> Mapping[str, Any]:
        """THIS run's own table row for a token - its style and its edge.

        A read is deduplicated by a primitive stripped of its publishing, so what
        a variable draws by is the run's own table row and never the publishing
        primitive's - and legend and mask read the one declaration."""
        upper = str(token).strip().upper()
        module = str(self.body.MODULE).upper()
        for row in self.run.get("module_output") or ():
            if (str(row.get("token")).strip().upper() == upper
                    and str(row.get("module")).upper() == module):
                return row
        return {}

    def style(self, token: str) -> Any:
        """The style row this run draws a token under."""
        return self.output_row(token).get("style")

    def has_edge(self, token: str) -> bool:
        """Does this token's row declare a visible EDGE? -> nothing more.

        Declared on the module's own table: a quantity concentrated somewhere in
        the domain has one, a variable the water already carries does not."""
        return bool(self.output_row(token).get("has_edge"))

    def injected(self, token: str) -> bool:
        """Did the DECK put this token's quantity into the domain? -> nothing more.

        What the deck put in and the run lost is a broken run; a variable the
        engine grows from the physics is honestly zero where nothing happened."""
        return bool(self.output_row(token).get("injected"))

    @cached_property
    def host_result(self) -> dict[str, Any]:
        """The run's OWN result - where the water depth is.

        A coupled module writes its own file and no depth of its own, so the
        mask its variables are read under is the host's, by node."""
        from trid3nt_server.workflows.solver.solver import download_result

        basename = str(self.run.get("result_basename") or self.result_file)
        if basename == self.result_file:
            return self.result
        local = download_result(self.run_id, basename)
        try:
            return read_selafin(local)
        finally:
            Path(local).unlink(missing_ok=True)

    @cached_property
    def wet(self) -> Any:
        """Which nodes held water at each written instant, or ``None``.

        The same WATER DEPTH above the wet tolerance the renderer draws, so a
        measure and the picture of it cannot disagree. A result with no depth row
        carries no mask, and every node is read - which is the honest answer, not
        a mask invented from something else."""
        import numpy as np

        host = self.host_result
        picked = next((v for v in host["varnames"]
                       if v.strip().upper().startswith("WATER DEPTH")), None)
        if picked is None:
            return None
        depth = np.asarray(host["data"][picked], dtype="float64")
        if depth.size == 0:
            return None
        npoin2, nplan = int(host["npoin2"]), int(host.get("nplan", 1))
        if nplan > 1:
            depth = depth.reshape(depth.shape[0], nplan, npoin2)[:, -1, :]
        return depth > _WET_TOL_M

    def varies(self, token: str) -> bool:
        """Does this token's row vary in time? -> nothing more.

        A row that does not is a property of the DOMAIN - the bed it was cut
        from - defined where there is no water at all, so no wet mask applies."""
        row = self.output_row(token)
        return bool(row.get("varies", True)) if row else True

    def mask_for(self, token: str, values: Any) -> Any:
        """The mask a token's measures and legend are read under, or ``None``.

        The nodes the run held water on, less every node the engine wrote a
        NON-FINITE value on: a module that initialises a row at infinity has not
        measured anything there, and neither the number nor the picture is about
        it. ``None`` where nothing is masked at all - the run carries no depth
        to mask by, or the row is the domain's own rather than a quantity the
        water carries, and every value is finite."""
        import numpy as np

        finite = np.isfinite(values)
        wet = None if not self.varies(token) else self.wet_like(values)
        if wet is None:
            return None if bool(finite.all()) else finite
        return wet & finite

    def wet_like(self, values: Any) -> Any:
        """The wet mask shaped to ``values`` - one flag per value - or ``None``.

        A coupled module writing on its own cadence borrows the mask BY NODE:
        a node wet at any instant of the host run is a node its own record is
        read on."""
        import numpy as np

        mask = self.wet
        if mask is None:
            return None
        frames, nodes = mask.shape
        want_frames, want_nodes = int(values.shape[0]), int(values.shape[1])
        if want_nodes > nodes:
            return None
        if want_nodes < nodes:
            mask = mask[:, :want_nodes]
        if frames == want_frames:
            return mask
        return np.broadcast_to(mask.any(axis=0), (want_frames, want_nodes))

    def _appended_tracer(self, token: str, names: list[Any], index: int
                         ) -> tuple[str, str]:
        """A tracer a coupled module appended behind the carrier's declared ones.

        The result lists tracers in declared order, so the n-th sits n-1 past the
        first declared name; where the carrier declares NONE, the appended ones
        begin where the host's own table ends. The unit is the record's."""
        varnames = list(self.result["varnames"])
        units = list(self.result.get("varunits") or [""] * len(varnames))
        if names:
            first = str(names[0]).ljust(32)[:16].strip().upper()
            start = next((i for i, name in enumerate(varnames)
                          if name.strip().upper() == first), None)
        else:
            own = {row.name.strip().upper()
                   for row in self.body.MODULE_OUTPUT.values()}
            start = next((i for i, name in enumerate(varnames)
                          if name.strip().upper() not in own), None)
        position = None if start is None else start + index
        if position is None or position >= len(varnames):
            raise OutputEmpty(
                f"{token} names tracer {index + 1}; this run declares {len(names)} "
                f"and the result carries {varnames}.")
        unit = units[position] if position < len(units) else ""
        return varnames[position], _UNITS.get(unit.upper(), unit.lower())

    def frames(self, token: str, plane: int | None) -> tuple[str, str, Any]:
        """``(variable, units, values(nframes, npoin2))`` for a token, one plane.

        A token the module DEFINES over the result's variables is read through
        its own definition; the rest are the result's own arrays."""
        import numpy as np

        derived = self.body.DERIVED.get(str(token).strip().upper())
        if derived is not None:
            return derived(self)
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

    def plane_label(self, plane: int | None) -> str | None:
        """What a plane of this result is called, bottom first; ``None`` on 2D."""
        nplan = int(self.result.get("nplan", 1))
        if nplan < 2:
            return None
        index = nplan - 1 if plane is None else int(plane)
        return ("bottom plane" if index == 0 else "surface plane"
                if index == nplan - 1 else f"plane {index + 1} of {nplan}")

    def node_at(self, at: Any) -> int:
        """The 2D node nearest ``at``, a Point in any of the shapes it arrives in."""
        import numpy as np

        from trid3nt_server.inputs.point import as_utm

        px, py = as_utm(_point(at), self.utm_epsg)
        x, y = self.xy
        return int(np.argmin(np.hypot(x - px, y - py)))


def _edge(has_edge: bool, peak: float) -> float | None:
    """The visible edge of a variable whose ROW declares it has one: a fraction
    of the magnitude that row reads. Nothing for a variable that has none."""
    return EDGE_FRACTION * peak if has_edge else None


def _drawn(values: Any, wet: Any = None) -> Any:
    """``values`` with every node the run held no water on read as nothing.

    The measures and the legend are both taken off this, so the number and the
    picture answer over the same nodes by construction."""
    import numpy as np

    if wet is None:
        return values
    return np.where(wet, values, np.nan)


def _floor(has_edge: bool, values: Any, row: Any = None,
           wet: Any = None) -> float | None:
    """Where a variable stops being drawn: its declared edge, where it HAS one.
    A variable that is everywhere above its edge - a temperature, a salinity - is
    a field with no absent region, and is drawn and ranged whole."""
    import numpy as np

    from trid3nt_server.render import presets

    drawn = _drawn(values, wet)
    edge = _edge(has_edge, presets.declared_peak(drawn, row))
    finite = np.asarray(drawn)[np.isfinite(drawn)]
    return (edge if edge is not None and finite.size
            and float(finite.min()) < edge else None)


def _per_frame(values: Any, wet: Any) -> tuple[Any, Any, Any]:
    """Each frame's extremes over the nodes the run held water on, and which
    frames held any at all.

    A frame with no wet node answers nothing rather than answering zero."""
    import numpy as np

    if wet is None:
        return (values.max(axis=1), values.min(axis=1),
                np.ones(values.shape[0], dtype=bool))
    live = np.asarray(wet).any(axis=1)
    highs = np.full(values.shape[0], np.nan)
    lows = np.full(values.shape[0], np.nan)
    if live.any():
        highs[live] = np.where(wet[live], values[live], -np.inf).max(axis=1)
        lows[live] = np.where(wet[live], values[live], np.inf).min(axis=1)
    return highs, lows, live


def _envelope(token: str, times: Any, values: Any, row: Any = None,
              has_edge: bool = False, wet: Any = None,
              above: Any = None, injected: bool = False) -> dict[str, Any]:
    """The measures a series over time carries: its peak and its trough with the
    instants they fall on, where it stands at the end, how far it swings between
    the two, how many frames it was read over, and - where a threshold was asked
    - the first instant it stands above one, all over the WET nodes."""
    import numpy as np

    from trid3nt_server.render import presets

    highs, lows, live = _per_frame(values, wet)
    if not live.any():
        raise OutputEmpty(
            f"{token} was never read on a node this run held water at: every "
            "frame is dry, so there is nothing for a measure to be about.")
    index = np.flatnonzero(live)
    peak_i = int(index[int(np.argmax(highs[live]))])
    peak = float(highs[peak_i])
    # A peak on the last instant is where the window closed, not where the
    # variable crested: the run was still rising, so the peak is a floor.
    low_i = int(index[int(np.argmin(lows[live]))])
    last_i = int(index[-1])
    measures: dict[str, Any] = {"max": peak, "t_max": float(times[peak_i]),
                                "min": float(lows[low_i]),
                                "t_min": float(times[low_i]),
                                "last": float(highs[last_i]),
                                "range": peak - float(lows[low_i]),
                                "frames": int(live.sum()),
                                "truncated": bool(times.size > 1
                                                  and peak_i == times.size - 1)}
    # The edge is a fraction of the magnitude the ROW declares, the same
    # statistic the legend's top reads: taking it off a record maximum a drying
    # node carries would mask the whole field the run produced.
    edge = _edge(has_edge, presets.declared_peak(_drawn(values, wet), row))
    # THE HONESTY FLOOR is about SHAPE, not magnitude, and it is about what the
    # DECK PUT IN: a quantity the deck injected and the run lost has no region
    # to draw and no reach to measure, and the run is broken. A variable the
    # engine grows where the physics makes it - an ice cover, a bed evolution -
    # is zero because nothing happened, which is the answer rather than a gap.
    if injected and not peak > 0.0:
        raise OutputEmpty(
            f"{token} is zero at every node this run held water on, so the run "
            "injected nothing there is a shape of.")
    if edge is not None:
        measures["active_frames"] = int((highs[live] > edge).sum())
    if above is not None:
        # The FIRST frame the series stands above the threshold, on the run's
        # own clock. A threshold nothing ever crossed has no instant, and the
        # answer is the absence rather than a number nobody measured.
        crossed = index[highs[live] > float(above)]
        measures["t_above"] = (float(times[int(crossed[0])]) if crossed.size
                               else None)
    return measures


def _travel_m(x: Any, y: Any, values: Any, floor: float | None) -> float | None:
    """How far the field's centroid moved from where it first appeared, in metres.

    Over the nodes the field is VISIBLE at: above its declared edge where it has
    one, and everywhere the run held water where it has none."""
    import numpy as np

    track = []
    for frame in values:
        above = np.isfinite(frame) if floor is None else frame > floor
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
    row, edge = solved.style(primitive.variable), solved.has_edge(primitive.variable)
    wet = solved.mask_for(primitive.variable, values)
    measures = _envelope(primitive.variable, times, values, row, edge, wet,
                         injected=solved.injected(primitive.variable))
    if primitive.t == "every":
        floor = _floor(edge, values, row, wet)
        x, y = solved.xy
        measures["travel_m"] = _travel_m(x, y, _drawn(values, wet), floor)
        # The values a temporal layer carries HERE are what its legend is
        # measured over, and the layer paints the result file's own group: a
        # node the run never wet moves neither the range nor the picture.
        return Frames(name=name, units=units, values=_drawn(values, wet),
                      file=solved.result_file,
                      group=name.strip(), epsg=solved.utm_epsg,
                      reference_time=solved.run.get("started_at"),
                      frames=int(times.size), floor=floor, measures=measures)
    # An int is a frame index, counted from the file's own first frame; a float
    # is an instant in seconds, read at the nearest frame the engine wrote. The
    # measures are the frame's own; the envelope only sets the visible edge.
    index = (int(primitive.t) if isinstance(primitive.t, int)
             else int(np.argmin(np.abs(times - float(primitive.t)))))
    frame = values[index]
    held = (np.ones(frame.size, dtype=bool) if wet is None
            else np.asarray(wet)[index])
    if primitive.over is not None:
        held = held & _within(primitive.over, solved, frame.size)
    inside = frame[held]
    if not inside.size:
        raise OutputEmpty(
            f"{primitive.variable} has no node this run held water at within "
            f"what the measure was asked over, at t = {times[index]:g} s.")
    return Field(name=name, units=units, values=frame, wet=held,
                 t=float(times[index]), plane=solved.plane_label(primitive.plane),
                 floor=_floor(edge, values, row, wet),
                 measures={"max": float(inside.max()), "min": float(inside.min()),
                           "mean": float(inside.mean()),
                           "spread": float(inside.max() - inside.min()),
                           "nodes": int(inside.size),
                           "t": float(times[index]), "frames": int(times.size)})


def _within(over: Any, solved: Solved, nodes: int) -> Any:
    """The nodes inside ``over``, as a mask over the first ``nodes`` of the result.

    An area the mesh has no node in is a refusal: a measure over nothing would
    read as a bed that did not move."""
    import numpy as np
    from shapely import contains_xy
    from shapely.geometry import shape as _shape
    from shapely.ops import unary_union

    from trid3nt_server.inputs.geometry import (
        flatten_geometries, read_geometry_doc,
    )

    lon, lat = solved.lonlat
    area = unary_union([_shape(g)
                        for g in flatten_geometries(read_geometry_doc(over))])
    mask = np.asarray(contains_xy(area, lon[:nodes], lat[:nodes]))
    if not mask.any():
        raise OutputEmpty(
            "the area a measure was asked over holds no node of the mesh the run "
            "solved on, so there is nothing in it to read.")
    return mask


def _point(at: Any) -> Any:
    """The Point a primitive was anchored at, whatever shape the anchor took."""
    from trid3nt_server.inputs import Point

    if isinstance(at, Point):
        return at
    if isinstance(at, Mapping):
        return Point(float(at["lon"]), float(at["lat"]), at.get("name"))
    return Point(*at)


def read_series(primitive: Primitive, solved: Solved) -> Series:
    """``series(name, at)``: the domain maximum per instant, or a Point's value.

    The measures are the returned series' own, whichever it is. A token the
    module prints rather than writes is read off the listing, at the liquid
    boundary the Point lies on."""
    import numpy as np

    if primitive.variable in solved.body.LISTING:
        return _boundary_series(primitive, solved)
    name, units, values = solved.frames(primitive.variable, primitive.plane)
    times = np.asarray(solved.result["times"], dtype="float64")
    row, edge = solved.style(primitive.variable), solved.has_edge(primitive.variable)
    wet = solved.mask_for(primitive.variable, values)
    if primitive.at is None:
        highs, _lows, _live = _per_frame(values, wet)
        return Series(name=name, units=units, times=times, values=highs,
                      at="the domain maximum",
                      measures=_envelope(
                          primitive.variable, times, values, row, edge, wet,
                          primitive.above,
                          injected=solved.injected(primitive.variable)))
    point = _point(primitive.at)
    node = solved.node_at(point)
    lon, lat = solved.lonlat
    # The measures are the SERIES' own: a question asked at a point is answered
    # at that point, and the domain's extremes answer a different question. The
    # line and the numbers are read under the same mask, so an instant the node
    # carries no reading at is a gap in both.
    column = values[:, node:node + 1]
    held = None if wet is None else np.asarray(wet)[:, node:node + 1]
    return Series(name=name, units=units, times=times,
                  values=_drawn(column, held)[:, 0],
                  at=f"at {point.name or 'the point'}",
                  lon=float(lon[node]), lat=float(lat[node]),
                  measures=_envelope(primitive.variable, times, column, row,
                                     edge, held, primitive.above,
                                     injected=solved.injected(primitive.variable)))


def _boundary_series(primitive: Primitive, solved: Solved) -> Series:
    """A printed token at a Point: the series the listing carries for the liquid
    boundary nearest it, as the engine measured the flux across that boundary.
    The measures carry the volume that crossed it over the sampled instants."""
    import numpy as np
    from pyproj import Transformer

    from .listing import boundary_flux
    from trid3nt_server.inputs.point import as_utm

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
    # A printed FLUX is measured across a boundary, not at a node, so no node
    # mask applies to it: what the engine printed is the whole of the reading.
    measures = _envelope(primitive.variable, times_arr, flows_arr[:, None],
                         solved.style(primitive.variable),
                         solved.has_edge(primitive.variable))
    measures["integral"] = round(float(np.trapezoid(flows_arr, times_arr)), 3)
    row = solved.body.MODULE_OUTPUT[primitive.variable]
    name, unit = row.name, row.unit
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
    row, edge = solved.style(primitive.variable), solved.has_edge(primitive.variable)
    wet = solved.mask_for(primitive.variable, values)
    measures = _envelope(primitive.variable, times, values, row, edge, wet,
                         injected=solved.injected(primitive.variable))
    # The envelope is over the instants each node HELD WATER: a node's peak taken
    # from the frames it was dry in is a reading of nothing.
    drawn = _drawn(values, wet)
    envelope = np.where(np.isfinite(drawn).any(axis=0),
                        np.nanmax(np.where(np.isfinite(drawn), drawn, -np.inf),
                                  axis=0), np.nan)
    ever = np.isfinite(envelope)
    # The extreme and the field: one pit can set the maximum while the field the
    # run produced sits orders of magnitude below it, so the 99th percentile of
    # the envelope rides beside the maximum.
    measures["p99"] = float(np.percentile(envelope[ever], 99))
    return Field(name=name, units=units,
                 values=np.where(ever, envelope, 0.0), wet=ever,
                 plane=solved.plane_label(primitive.plane),
                 floor=_floor(edge, values, row, wet),
                 measures=measures)


#: The depth an element has to hold to count as wet. TELEMAC's own tidal-flat
#: treatment leaves films thinner than this on a drying bar, and counting them as
#: conveyance is what would make the heuristic agree with the domain by
#: construction.
_WET_TOL_M = 0.02


def mesh_area_m2(mesh: Mapping[str, Any]) -> float:
    """The area the elements of a read result cover, in the mesh's own metres."""
    import numpy as np

    ikle = np.asarray(mesh["ikle"], dtype=int)
    x, y = np.asarray(mesh["x"]), np.asarray(mesh["y"])
    if ikle.size == 0:
        return 0.0
    a, b, c = ikle[:, 0], ikle[:, 1], ikle[:, 2]
    return float((0.5 * np.abs((x[b] - x[a]) * (y[c] - y[a])
                               - (x[c] - x[a]) * (y[b] - y[a]))).sum())


def wetted_fraction(mesh: Mapping[str, Any], *, wet_tol_m: float = _WET_TOL_M
                    ) -> dict[str, Any]:
    """How much of the solved domain still held water at the final frame.

    By element, a HEURISTIC; ``mesh`` is the record the postprocess ALREADY read."""
    # The reach domain is the mapped ACTIVE CHANNEL, which at bankfull includes
    # the gravel bars a low flow leaves dry: TELEMAC wets and dries them natively,
    # so a low-flow run is correct and its conveyance width is still narrower than
    # the domain it solved on. Nothing about the result says so, and a reader
    # looking at a ribbon inside a wider mesh has no number to read it against.
    import numpy as np

    # SELAFIN pads a variable name to 32 chars with its unit trailing ('WATER
    # DEPTH     M'), so an exact-key lookup never matches a real result.
    picked = next((v for v in mesh["varnames"]
                   if v.strip().upper().startswith("WATER DEPTH")), None)
    depth = mesh["data"].get(picked) if picked is not None else None
    ikle = np.asarray(mesh["ikle"], dtype=int)
    if depth is None or np.asarray(depth).size == 0 or ikle.size == 0:
        return {}
    x, y = np.asarray(mesh["x"]), np.asarray(mesh["y"])
    a, b, c = ikle[:, 0], ikle[:, 1], ikle[:, 2]
    area = 0.5 * np.abs((x[b] - x[a]) * (y[c] - y[a])
                        - (x[c] - x[a]) * (y[b] - y[a]))
    final = np.asarray(depth)[-1]
    wet = area[final[ikle].mean(axis=1) > float(wet_tol_m)]
    total = mesh_area_m2(mesh)
    if total <= 0.0:
        return {}
    return {"mesh_area_m2": total, "wet_area_m2": float(wet.sum()),
            "wetted_fraction": round(float(wet.sum()) / total, 4),
            "wet_tol_m": float(wet_tol_m)}


def read_extent(primitive: Primitive, solved: Solved) -> Read:
    """``extent()``: the domain's lon/lat bounds, its area, its wetted fraction."""
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
    meshed domain and the fraction of it that left. The sediment balance carries
    the per-class closure and, where a dredge ran, the volumes it moved."""
    from .listing import (
        continuity_rel_error, final_balance, gaia_mass_balance, nestor_volumes,
    )

    if solved.body.MODULE == "gaia":
        # The dredge's own figures ride here because they close the same bed: a
        # run that armed no dredge printed none and carries none.
        return Read(measures={**gaia_mass_balance(solved.listing),
                              **nestor_volumes(solved.listing)})
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


def _chainage(x: Any, y: Any, line: Any) -> tuple[Any, Any, Any]:
    """Each node's arc length along ``line`` at its nearest segment, that
    segment's unit direction, and the node's distance off the line: the
    along-line coordinate, its local axis, and how far the node is from it."""
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
    return s, unit, np.sqrt(d2[np.arange(x.size), k])


def read_profile(primitive: Primitive, solved: Solved) -> Profile:
    """``profile(name, along, t, within_m)``: the depth-weighted mean of a
    variable per station down a line, at one instant, over the nodes within the
    stated distance of it or the whole domain. The line runs the way the solved
    flow goes when the module carries velocities; its own order otherwise. The
    measures carry the minimum and maximum with their stations, and the
    depth-weighted mean along-line speed when velocities are carried, and the
    swing between the two ends of the range."""
    import numpy as np

    from trid3nt_server.workflows.mesh.shared.nodes import read_centerline_utm

    name, units, values = solved.frames(primitive.variable, primitive.plane)
    times = np.asarray(solved.result["times"], dtype="float64")
    index = (int(primitive.t) if isinstance(primitive.t, int)
             else int(np.argmin(np.abs(times - float(primitive.t)))))
    x, y = (np.asarray(v, dtype="float64") for v in solved.xy)
    line = read_centerline_utm(primitive.along, solved.utm_epsg)
    s, axis, off = _chainage(x, y, line)
    weight, along = np.ones(x.size), None
    if primitive.within is not None:
        weight = np.where(off <= float(primitive.within), weight, 0.0)
    # The SAME wet mask every other measure is read under, off the host's own
    # depth: a film on a drying bar is not the water the profile is about, and a
    # coupled module with no depth of its own borrows the host's by node.
    wet = solved.mask_for(primitive.variable, values)
    if wet is not None and np.asarray(wet).shape[1] == x.size:
        weight = np.where(np.asarray(wet)[index], weight, 0.0)
    if all(token in solved.body.MODULE_OUTPUT for token in ("H", "U", "V")):
        depth = solved.frames("H", primitive.plane)[2][index]
        weight = weight * np.where(depth > _WET_TOL_M, depth, 0.0)
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
        "range": float(means_arr[hi] - means_arr[lo]),
        "t": float(times[index]), "stations": len(distance),
        "velocity_mps": (None if along is None else
                         float((along * weight).sum() / weight.sum()))}
    return Profile(name=name, units=units, distance_m=np.asarray(distance),
                   values=means_arr,
                   along="downstream distance" if along is not None
                   else "distance along the line",
                   measures=measures)


def read_column(primitive: Primitive, solved: Solved) -> Profile:
    """``column(name, at, t)``: a 3D variable down the planes at one node, at one
    instant, as a profile of depth below the free surface. ``at=None`` reads the
    deepest column the mesh carries, where vertical structure can exist at all.
    The measures carry the top and bottom values, their difference, the
    depth-weighted mean and the column's depth."""
    import numpy as np

    times = np.asarray(solved.result["times"], dtype="float64")
    index = (int(primitive.t) if isinstance(primitive.t, int)
             else int(np.argmin(np.abs(times - float(primitive.t)))))
    nplan, npoin2 = int(solved.result.get("nplan", 1)), int(solved.result["npoin2"])
    if nplan < 2:
        raise OutputEmpty(f"{solved.result_file} carries one plane; a column reads "
                          "the planes of a 3D result.")

    def _planes(token: str) -> Any:
        name = solved.variable(token)[0]
        frame = np.asarray(solved.result["data"][name], dtype="float64")[index]
        return frame.reshape(nplan, npoin2)

    z = _planes("Z")
    node = (int(np.argmin(z[0])) if primitive.at is None
            else solved.node_at(primitive.at))
    name, units = solved.variable(primitive.variable)
    values = _planes(primitive.variable)[:, node]
    depth = z[-1, node] - z[:, node]
    span = float(depth[0])
    mean = (float(np.trapezoid(values, z[:, node]) / span) if span > 1e-9
            else float(values.mean()))
    return Profile(name=name, units=units, distance_m=depth[::-1],
                   values=values[::-1], along="depth below the surface",
                   measures={"top": float(values[-1]), "bottom": float(values[0]),
                             "top_minus_bottom": float(values[-1] - values[0]),
                             "mean": mean, "depth_m": span,
                             "t": float(times[index]), "planes": nplan})


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

    from trid3nt_server.workflows.solver.solver import download_result
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


# -- the format each read is delivered in ----------------------------------- #

#: What a derived dataset group's file is called under the run prefix. The stem
#: keys the layer too, so one output's group never overwrites another's.
_DATASET_SUFFIX = ".dat"


def tracer_position(token: Any) -> int | None:
    """``T<n>`` -> ``n``, the tracer's place in the deck's own order; else ``None``.

    The token is what a module's table publishes a tracer under, and the position
    is what survives a run renaming the tracer after the release it was given."""
    upper = str(token or "").strip().upper()
    return (int(upper[1:]) if upper.startswith("T") and upper[1:].isdigit()
            else None)


def deliver(primitive: Primitive, read: Read, solved: Solved, *, caption: str,
            name: str, where: str) -> Deliverable:
    """One read, in the format QGIS opens it in.

    A field is the mesh the run solved on with one dataset group selected - the
    group the result file carries when the whole time series is played, and a
    group written beside it when the read is one instant or an envelope. A
    series or a profile is a chart payload; a track and a station are GeoJSON."""
    from trid3nt_server.render import presets
    from trid3nt_server.render.formats import quantity_of

    quantity = quantity_of(caption)
    tracer = tracer_position(primitive.variable)
    if isinstance(read, Frames):
        return Deliverable(
            product=Mesh(file=read.file, group=read.group, epsg=read.epsg,
                         reference_time=read.reference_time, frames=read.frames,
                         units=read.units, bbox=solved.bbox, floor=read.floor,
                         value_range=presets.measured_range(
                             read.values, primitive.style, floor=read.floor)),
            caption=caption, style=primitive.style, tracer=tracer)
    if isinstance(read, Field):
        return Deliverable(product=_derived_group(read, solved, caption=caption,
                                                  quantity=quantity,
                                                  style=primitive.style),
                           caption=caption, style=primitive.style, tracer=tracer)
    if isinstance(read, Track):
        return Deliverable(product=Vector(features=read.features),
                           caption=caption, style=primitive.style)
    if isinstance(read, Series) and primitive.publish == "station":
        return Deliverable(
            product=_station(read, caption=caption,
                             reference_time=solved.run.get("started_at")),
            caption=caption,
            # A station is a point a reader locates the series by, not a
            # quantity painted over the domain.
            style={"kind": "reference", "geometry": "point"})
    return Deliverable(product=Chart(payload=_chart(read, caption=caption,
                                                    where=where)),
                       caption=caption, style=primitive.style)


def _derived_group(read: Field, solved: Solved, *, caption: str, quantity: str,
                   style: Any) -> Mesh:
    """A read the result file carries no group for -> one written beside it.

    The values are written as the SMS ASCII dataset MDAL loads onto the mesh
    they were measured over; a node below the read's floor is written as nothing
    so the field draws where it is visible and the basemap shows through where
    it is not. The floor rides on the product too, so the animation of the same
    quantity - whose group the result file carries - masks where this one does."""
    import numpy as np

    from trid3nt_server import storage
    from trid3nt_server.render import presets
    from trid3nt_server.render.mesh_display import write_ascii_dataset

    values = np.asarray(read.values, dtype="float64").copy()
    # A node the engine wrote as non-finite is not a measurement: it is written
    # as nothing so the basemap shows through rather than a node at infinity
    # owning the ramp.
    values[~np.isfinite(values)] = np.nan
    if read.floor is not None:
        values[values < float(read.floor)] = np.nan
    label = f"{caption[:1].upper()}{caption[1:]}"
    group = (label if read.t is None
             else f"{label} at t = {float(read.t):g} s")
    if read.plane is not None:
        group = f"{group}, {read.plane}"
    stem = quantity if read.plane is None else f"{quantity}_{_token(read.plane)}"
    instant = "" if read.t is None else f"-t{int(read.t)}"
    basename = f"{stem}{instant}{_DATASET_SUFFIX}"
    nodes, cells = solved.display_mesh
    storage.client().put_object(
        Bucket=storage.runs_bucket(), Key=f"{solved.run_id}/{basename}",
        Body=write_ascii_dataset(values, name=group, nodes=nodes, cells=cells,
                                 t=0.0 if read.t is None else float(read.t)
                                 ).encode("utf-8"),
        ContentType="text/plain")
    return Mesh(file=solved.display_file, group=group, epsg=solved.utm_epsg,
                datasets=(basename,), bbox=solved.bbox, t=read.t, plane=read.plane,
                units=read.units, floor=read.floor,
                # RANGED over the nodes the measures were read over, so the
                # legend describes the water and not the film on a drying bar.
                value_range=presets.measured_range(_drawn(values, read.wet),
                                                   style, floor=read.floor))


def _token(text: str) -> str:
    return "_".join(str(text).strip().lower().split())


def _station(read: Series, *, caption: str, reference_time: str | None
             ) -> Vector:
    """A series at a station -> ONE point feature carrying the series inline.

    ``time_series_csv`` rows are ``iso,value`` counted from ``reference_time``,
    the same instant the run's frames are counted from; with no instant to count
    from the rows carry the run's own seconds."""
    from datetime import datetime, timedelta

    if read.lon is None or read.lat is None:
        raise OutputEmpty(f"the series {read.name!r} was read {read.at}, which is "
                          "no station to publish it at.")
    origin = (datetime.fromisoformat(reference_time.replace("Z", "+00:00"))
              if reference_time else None)
    rows = []
    for t, v in zip(read.times, read.values):
        stamp = ((origin + timedelta(seconds=float(t))).isoformat()
                 if origin is not None else f"{float(t):.3f}")
        rows.append(f"{stamp},{float(v):.6f}")
    label = f"{caption[:1].upper()}{caption[1:]}"
    feature = {
        "type": "Feature",
        "geometry": {"type": "Point",
                     "coordinates": [round(float(read.lon), 6),
                                     round(float(read.lat), 6)]},
        "properties": {"name": f"{label} {read.at}", "quantity": _token(caption),
                       "units": read.units, "variable": read.name,
                       "reference_time": reference_time,
                       "n_timesteps": len(rows),
                       "time_series_csv": "\n".join(rows) + "\n"},
    }
    return Vector(features={"type": "FeatureCollection", "features": [feature]},
                  units=read.units)


def _chart(read: Series | Profile, *, caption: str, where: str) -> dict[str, Any]:
    """A series or a profile -> the chart payload the dock renders, titled by the
    caption; every reference line rides as its own named series."""
    from trid3nt_server.render.charts import build_chart_payload

    title = f"{caption[:1].upper()}{caption[1:]}"
    if isinstance(read, Profile):
        x, at = [float(d) for d in read.distance_m], read.along
        xfield, axis = "x_m", f"{at[:1].upper()}{at[1:]} (m)"
    else:
        x, at = [float(t) for t in read.times], read.at
        xfield, axis = "t_s", "Time (s)"
    values = [float(v) for v in read.values]
    rows = [{xfield: a, "value": v, "series": title} for a, v in zip(x, values)]
    for line in read.lines:
        rows += [{xfield: float(a), "value": float(v), "series": line.label}
                 for a, v in zip(line.x, line.values)]
    encoding = {"x": {"field": xfield, "type": "quantitative", "title": axis},
                "y": {"field": "value", "type": "quantitative",
                      "title": f"{title} ({read.units})"}}
    if read.lines:
        encoding["color"] = {"field": "series", "type": "nominal", "title": None}
    peak = max(range(len(values)), key=values.__getitem__) if values else 0
    low = min(range(len(values)), key=values.__getitem__) if values else 0
    what = (f"; lowest {values[low]:.3g} {read.units} at {x[low]:.0f} m"
            if isinstance(read, Profile) else
            f"; peaks at {values[peak]:.3g} {read.units} at t = {x[peak]:.0f} s")
    return build_chart_payload(
        vega_lite_spec={
            "mark": {"type": "line", "point": not read.lines},
            "data": {"values": rows},
            "encoding": encoding,
        },
        title=f"{title}, {at} - {where}",
        caption=(f"The {caption}, {at}, at each of {len(x)} "
                 + ("stations" if isinstance(read, Profile) else "output times")
                 + (what if values else "") + "."
                 + "".join(f" {line.label} is drawn beside it." for line in read.lines)),
    )
