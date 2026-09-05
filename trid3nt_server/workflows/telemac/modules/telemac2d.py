"""The TELEMAC-2D wrapper: its catalog, its composites, and its outputs.

The wrapper asserts NO value of its own. The engine's default is its whole
position, and every opinion above it lives in a template.

What it does hold, beyond the catalog, is the keyword GROUPS that are repetitive
and failure-prone to write one keyword at a time. A point source is five
keywords plus a time series whose columns are sized to the tracer count; a wind
is five keywords plus the meteorological from-direction transform; a coupled
module is a steering file of its own plus the three keywords the carrier names it
by. Each of those is ONE value here, so the group cannot half-arrive.

The OUTPUTS are the other half of the wrapper: the module's results bound to the
readers that publish them. The bindings live here rather than in a template
because what a TELEMAC-2D result file holds is the module's fact, not the
question's.
"""

from __future__ import annotations

import math
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from ..products.products import publish_do_products, publish_dye_products
from ..products.rain_on_grid import publish_rain_on_grid_products
from .module import Module

__all__ = ["T2D", "Continuation", "Oil", "Release", "Wind", "SOURCES_FILENAME"]

#: The point-source time series the SOURCES FILE names.
SOURCES_FILENAME = "river_sources.txt"
#: The directory the engine compiles when a run patches its own Fortran. The
#: keyword names a DIRECTORY, so the manifest channel and the steering statement
#: are the same word rather than two derivations of it.
USER_FORTRAN_DIR = "user_fortran"
OIL_STEERING_FILENAME = "oil_spill.txt"
DROGUES_FILENAME = "drogues.txt"

#: How far past the last simulated instant a forcing series runs, so the engine's
#: time interpolation never reads off the end of it.
_SERIES_TAIL_S = 100.0
#: The gap a step change is written across. A series is read by linear
#: interpolation, so a release that stops has to state both sides of the step.
_STEP_GAP_S = 0.1


def Release(*, at: Any, q: Any, tracers: Any,  # noqa: N802 - a value constructor
            until_s: Any, window_s: Any = None) -> Mapping[str, Any]:
    """ONE point source: where it discharges, how much, of what, and for how long.

    ``at`` is the settled release point in the mesh's own metres. ``tracers`` is
    the value this source carries for EVERY tracer the run declares, in the
    order the names are declared, because the engine reads the array by position.
    ``window_s`` is a finite release - held, then stepped to nothing, so the slug
    advects and passes; ``None`` is a permitted discharge, held flat for the
    whole run. ``until_s`` is the horizon the series is written over.

    A MAPPING and not an object, because the sheet's one ref walk descends
    mappings: a late-bound read inside a release is bound by fill before the
    composite ever expands it.
    """
    return MappingProxyType({"at": at, "q": q, "tracers": tracers,
                             "window_s": window_s, "until_s": until_s})


def Wind(*, speed_mps: Any, from_deg: Any,  # noqa: N802 - a value constructor
         drag: Any = None) -> Mapping[str, Any]:
    """A steady wind, stated the way weather states one: where it blows FROM."""
    return MappingProxyType({"speed_mps": speed_mps, "from_deg": from_deg,
                             "drag": drag})


def Continuation(*, previous: Any) -> Mapping[str, Any]:  # noqa: N802
    """The run this one picks up from, by the staged name the engine reads."""
    return MappingProxyType({"previous": previous})


def Oil(*, steering: Any, fortran: Any, release_step: Any,  # noqa: N802
        drogues: Any, drogues_period_steps: Any) -> Mapping[str, Any]:
    """The oil module riding on top of the tracer solve.

    ``steering`` is the module's own preset file and ``fortran`` the per-run
    source the release coordinates are compiled into; both are content, written
    beside the deck that names them.
    """
    return MappingProxyType({"steering": steering, "fortran": fortran,
                             "release_step": release_step, "drogues": drogues,
                             "drogues_period_steps": drogues_period_steps})


def _releases(value: Any) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """N releases -> the source keywords they mean, and the series they name.

    Every array is written in ONE order - the order the releases were declared -
    so the abscissa, the ordinate, the discharge and the tracer block of source
    number i are the same source. The tracer block is flattened by position
    because that is how the engine reads it.
    """
    releases = list(value)
    if not releases:
        raise ValueError("a releases composite with no release in it states "
                         "nothing; omit it instead.")
    tracers: list[Any] = []
    for release in releases:
        tracers += list(release["tracers"])
    # The allocation bound is stated only when the run needs MORE sources than
    # the dictionary already allows for. Restating a number the engine already
    # holds would put an opinion in the deck; exceeding it silently would drop
    # every source past the bound.
    allowed = int(T2D.slot("MAXIMUM_NUMBER_OF_SOURCES").engine_default)
    return ({**({} if len(releases) <= allowed
                else {"MAXIMUM_NUMBER_OF_SOURCES": len(releases)}),
             "ABSCISSAE_OF_SOURCES": [float(r["at"][0]) for r in releases],
             "ORDINATES_OF_SOURCES": [float(r["at"][1]) for r in releases],
             "WATER_DISCHARGE_OF_SOURCES": [float(r["q"]) for r in releases],
             "VALUES_OF_THE_TRACERS_AT_THE_SOURCES": tracers,
             "SOURCES_FILE": SOURCES_FILENAME},
            {SOURCES_FILENAME: _series(releases)})


def _series(releases: Sequence[Mapping[str, Any]]) -> str:
    """The sources time series, on the scenario's own absolute clock.

    The declared scenario is written over whatever stretch of that clock this run
    covers: a release opens at zero because that is when it was declared, and the
    last row runs past where the run stops. A continued run therefore carries the
    SAME scenario forward - a pulse whose window has already elapsed continues as
    zero, which is what a finite release means, rather than being re-released
    into a second experiment.
    """
    horizon = max(float(r["until_s"]) for r in releases) + _SERIES_TAIL_S
    breaks = {0.0, horizon}
    for release in releases:
        window = release["window_s"]
        if window is not None:
            breaks |= {float(window), float(window) + _STEP_GAP_S}
    times = sorted(t for t in breaks if t <= horizon)
    columns = ["T"] + [f"Q({i})" for i in range(1, len(releases) + 1)] + [
        f"TR({i},{j})" for i, release in enumerate(releases, start=1)
        for j in range(1, len(release["tracers"]) + 1)]
    units = ["s"] + ["m3/s"] * len(releases) + [
        "mg/l" for release in releases for _ in release["tracers"]]
    rows = ["#", " ".join(columns), " ".join(units)]
    for t in times:
        discharges = [_on(r, t) * float(r["q"]) for r in releases]
        concentrations = [_on(r, t) * float(v)
                          for r in releases for v in r["tracers"]]
        rows.append(" ".join([f"{t:.3f}"]
                             + [f"{v:.6g}" for v in discharges + concentrations]))
    return "\n".join(rows) + "\n"


def _on(release: Mapping[str, Any], t: float) -> float:
    """Is this release discharging at ``t``? 1 while its window is open, else 0."""
    window = release["window_s"]
    return 1.0 if window is None or t <= float(window) else 0.0


def _wind(value: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """A from-direction -> the velocity components the engine reads.

    The meteorological direction names where the wind comes FROM; the engine
    reads where it blows TOWARD, in the mesh's own frame. Wind from the north
    drives water southward, wind from the west drives it eastward.
    """
    speed = float(value["speed_mps"])
    theta = math.radians(float(value["from_deg"]))
    drag = value.get("drag")
    return ({"WIND": True, "OPTION_FOR_WIND": 1,
             "WIND_VELOCITY_ALONG_X": -speed * math.sin(theta),
             "WIND_VELOCITY_ALONG_Y": -speed * math.cos(theta),
             **({} if drag is None else
                {"COEFFICIENT_OF_WIND_INFLUENCE": float(drag)})}, {})


def _continue_from(value: Mapping[str, Any]
                   ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """The previous computation, and the precision it has to be read at.

    Naming a previous computation file IS the continuation from release 9.0 - the
    boolean that used to arm it left the dictionary - and the engine then reads
    that file's last record as the initial state, so the file's own
    initial-condition statements go unread. The file named is a RESTART FILE,
    which the engine writes in DOUBLE precision; the format keyword defaults to
    single, and reading a double file as a single one is not a restart that means
    anything.
    """
    return ({"PREVIOUS_COMPUTATION_FILE": str(value["previous"]),
             "PREVIOUS_COMPUTATION_FILE_FORMAT": "SERAFIND"}, {})


def _oil(value: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """The oil module: its steering file, its per-run Fortran, and the drogues."""
    return ({"FORTRAN_FILE": USER_FORTRAN_DIR,
             "OIL_SPILL_STEERING_FILE": OIL_STEERING_FILENAME,
             "MAXIMUM_NUMBER_OF_DROGUES": int(value["drogues"]),
             "PRINTOUT_PERIOD_FOR_DROGUES": int(value["drogues_period_steps"]),
             "ASCII_DROGUES_FILE": DROGUES_FILENAME},
            {OIL_STEERING_FILENAME: value["steering"],
             f"{USER_FORTRAN_DIR}/oil_flot.f": value["fortran"]})


def _rain(value: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """Distributed rain or evaporation, sized to the run's own tracer count.

    Signed: positive rains, negative evaporates. With tracers present DAMOCLES
    REQUIRES a rainwater concentration per tracer, and rainwater carries none of
    them - which is exactly the array a hand-written deck gets wrong when a
    coupling adds a tracer behind it.
    """
    tracers = int(value["tracers"])
    return ({"RAIN_OR_EVAPORATION": True,
             "RAIN_OR_EVAPORATION_IN_MM_PER_DAY": float(value["mm_per_day"]),
             # A run with no tracers states no rainwater concentrations. An
             # EMPTY list is not that statement - it is a keyword with nothing
             # after it, which DAMOCLES reads as the next line's business.
             **({} if not tracers else
                {"VALUES_OF_TRACERS_IN_THE_RAIN": [0.0] * tracers}),
             **({} if value.get("hours") is None else
                {"DURATION_OF_RAIN_OR_EVAPORATION_IN_HOURS": float(value["hours"])})},
            {})


def Rain(*, mm_per_day: Any, tracers: Any, hours: Any = None  # noqa: N802
         ) -> Mapping[str, Any]:
    """Rain or evaporation at every wet node, independent of any hydrograph."""
    return MappingProxyType({"mm_per_day": mm_per_day, "tracers": tracers,
                             "hours": hours})


def _coupling(value: Any) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """The coupled bodies a template named -> what the CARRIER states about them.

    The body's own slots are not the carrier's; they go to the coupled module's
    own steering file, which the serializer writes against that module's own
    dictionary. What lands here is the three things the carrier says: which
    modules it couples with, what each one's steering file is called, and - for
    WAQTEL - which process it runs.
    """
    bodies = list(value)
    slots: dict[str, Any] = {
        "COUPLING_WITH": ";".join(body["module"].upper() for body in bodies)}
    files: dict[str, Any] = {}
    for body in bodies:
        slots[f"{body['module'].upper()}_STEERING_FILE"] = body["steering"]
        if "process" in body:
            slots["WATER_QUALITY_PROCESS"] = body["process"]
        files[body["steering"]] = body
    return slots, files


T2D = Module("telemac2d")
T2D.composites(releases=_releases, wind=_wind, continue_from=_continue_from,
               oil=_oil, rain=_rain, coupling=_coupling)
T2D.outputs(dye=publish_dye_products, dissolved_oxygen=publish_do_products,
            flood_depth=publish_rain_on_grid_products)
