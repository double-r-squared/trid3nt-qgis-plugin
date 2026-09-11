"""The TELEMAC-2D wrapper: its dictionary, its composites, and its outputs.

The wrapper asserts NO value of its own. Each composite is ONE value standing for
a keyword group, so the group cannot half-arrive; the outputs are the primitive
set over the module's own variables, because what a result file holds is the
module's fact."""

from __future__ import annotations

import math
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from .module import Module
from .outputs import PRIMITIVES, read_drogues

__all__ = ["T2D", "Boundaries", "Continuation", "Friction", "Hyetograph",
           "Infiltration", "Oil", "Rain", "Rating", "Release", "Runoff",
           "TimeOrigin", "TracerNames", "Wind", "SOURCES_FILENAME", "VARIABLES"]

#: The module's variable vocabulary, by the mnemonic VARIABLES FOR GRAPHIC
#: PRINTOUTS spells: the name the result file carries it under, and its unit.
#: ``T<n>`` is the n-th NAMES OF TRACERS entry and is resolved off the run.
#: ``FLUX`` is the one the module PRINTS rather than writes: the discharge
#: across each liquid boundary, in its own water-volume balance.
VARIABLES: Mapping[str, tuple[str, str]] = MappingProxyType({
    "U": ("VELOCITY U", "M/S"), "V": ("VELOCITY V", "M/S"),
    "H": ("WATER DEPTH", "M"), "S": ("FREE SURFACE", "M"),
    "B": ("BOTTOM", "M"), "F": ("FROUDE NUMBER", ""),
    "Q": ("SCALAR FLOWRATE", "M2/S"), "M": ("SCALAR VELOCITY", "M/S"),
    "FLUX": ("FLUX BOUNDARY", "M3/S"),
})
LISTING: frozenset[str] = frozenset({"FLUX"})

#: The point-source time series the SOURCES FILE names.
SOURCES_FILENAME = "river_sources.txt"
#: The distributed fields a rain-fed catchment names: the per-node curve number
#: the engine interpolates, the friction zones and the laws they index, the
#: block hyetograph the time-varying branch reads, and the outlet's own Z(Q).
CN_MAP_FILENAME = "rog_cn_map.dat"
FRICTION_LAWS_FILENAME = "rog_friction.tbl"
ZONES_FILENAME = "rog_zones.dat"
HYETOGRAPH_FILENAME = "rog_hyeto.txt"
RATING_FILENAME = "rog_rating.txt"
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

    A MAPPING, not an object: the sheet's one ref walk descends mappings."""
    # ``at`` is the settled release point in the mesh's own metres. ``tracers`` is
    # the value this source carries for EVERY tracer the run declares, in the
    # order the names are declared, because the engine reads the array by
    # position. ``window_s`` is a finite release - held, then stepped to nothing,
    # so the slug advects and passes - while ``None`` is a permitted discharge
    # held flat for the whole run; ``until_s`` is the horizon it is written over.
    return MappingProxyType({"at": at, "q": q, "tracers": tracers,
                             "window_s": window_s, "until_s": until_s})


def TracerNames(*, names: Any, named_by: Any = None  # noqa: N802
                ) -> Mapping[str, Any]:
    """The tracer names, the first of them called what ``named_by`` is called.

    A name the user gave a point replaces the template's word; none keeps it."""
    return MappingProxyType({"names": names, "named_by": named_by})


def _tracer_names(value: Mapping[str, Any]
                  ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """The NAMES OF TRACERS list, its first entry renamed when a name came.

    A tracer name is 32 characters: the name in the first 16, the unit after."""
    names = [str(name) for name in value["names"]]
    given = value.get("named_by")
    if given and names:
        first = names[0].ljust(32)
        names[0] = f"{str(given).strip()[:16]:<16}{first[16:]}".rstrip()
    return ({"NAMES_OF_TRACERS": names}, {})


def Wind(*, speed_mps: Any, from_deg: Any,  # noqa: N802 - a value constructor
         drag: Any = None) -> Mapping[str, Any]:
    """A steady wind, stated the way weather states one: where it blows FROM."""
    return MappingProxyType({"speed_mps": speed_mps, "from_deg": from_deg,
                             "drag": drag})


def Continuation(*, previous: Any) -> Mapping[str, Any]:  # noqa: N802
    """The run this one picks up from, by the staged name the engine reads."""
    return MappingProxyType({"previous": previous})


def Oil(*, presets: Any, named: Any, at: Any, release_step: Any,  # noqa: N802
        drogues: Any, drogues_period_s: Any, time_step_s: Any) -> Mapping[str, Any]:
    """The oil module riding on top of the tracer solve: which preset, released
    where and at which step, and how many floats are written how often.

    ``presets`` is the table the deck carries; ``named`` picks one row of it."""
    return MappingProxyType({"presets": presets, "named": named, "at": at,
                             "release_step": release_step, "drogues": drogues,
                             "drogues_period_s": drogues_period_s,
                             "time_step_s": time_step_s})


def _releases(value: Any) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """N releases -> the source keywords they mean, and the series they name.

    Every array is written in the declared order and flattened by position."""
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

    A continued run carries the SAME scenario forward, never a re-release."""
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

    Meteorological FROM, engine TOWARD, in the mesh's own frame."""
    if not value["speed_mps"]:
        # A wind of no speed is not a wind. Stating one would put the whole wind
        # block in the deck for a run nobody asked a wind about.
        return ({}, {})
    speed = float(value["speed_mps"])
    theta = math.radians(float(value["from_deg"]))
    drag = value.get("drag")
    return ({"WIND": True,
             "WIND_VELOCITY_ALONG_X": -speed * math.sin(theta),
             "WIND_VELOCITY_ALONG_Y": -speed * math.cos(theta),
             **({} if drag is None else
                {"COEFFICIENT_OF_WIND_INFLUENCE": float(drag)})}, {})


def _continue_from(value: Mapping[str, Any]
                   ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """The previous computation this run starts from.

    Naming the file IS the continuation; its last record is the initial state."""
    if not value["previous"]:
        # This run continues nothing, so it states its own initial conditions.
        return ({}, {})
    # The engine reads that file's last record as the initial state, so the deck's
    # own initial-condition statements go unread. The FORMAT it is read at is a
    # choice among three the dictionary offers, so a template wanting a
    # non-default one states it beside the file it writes.
    return ({"PREVIOUS_COMPUTATION_FILE": str(value["previous"])}, {})


def _oil(value: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """The oil module: its steering file, its per-run Fortran, and the drogues.

    The write cadence is asked in seconds; the run's own step turns it into steps."""
    from ..authoring.oil import release_routine, steering_text

    presets = value["presets"]
    name = str(value["named"]).strip().lower().replace(" ", "_")
    if name not in presets:
        raise ValueError(f"{value['named']!r} is not an oil preset this deck "
                         f"carries; the presets are {sorted(presets)}.")
    x, y = value["at"]
    period = max(int(float(value["drogues_period_s"])
                     / max(float(value["time_step_s"]), 1e-6)), 1)
    return ({"FORTRAN_FILE": USER_FORTRAN_DIR,
             "OIL_SPILL_STEERING_FILE": OIL_STEERING_FILENAME,
             "MAXIMUM_NUMBER_OF_DROGUES": int(value["drogues"]),
             "PRINTOUT_PERIOD_FOR_DROGUES": period,
             "ASCII_DROGUES_FILE": DROGUES_FILENAME},
            {OIL_STEERING_FILENAME: steering_text(name, presets[name]),
             f"{USER_FORTRAN_DIR}/oil_flot.f": release_routine(
                 int(value["release_step"]), float(x), float(y))})


def _rain(value: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """Distributed rain or evaporation, sized to the run's own tracer count.

    Signed; with tracers present DAMOCLES demands one rainwater value each."""
    if value["mm_per_day"] is None:
        # No rate was resolved, so this run states no rain at all.
        return ({}, {})
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


def TimeOrigin(*, at: Any) -> Mapping[str, Any]:  # noqa: N802
    """The absolute instant this run's clock starts at, or nothing at all.

    A module reading DATES rather than sim seconds maps them through this."""
    return MappingProxyType({"at": at})


def _time_origin(value: Mapping[str, Any]) -> tuple[Mapping[str, Any],
                                                    Mapping[str, Any]]:
    """The origin -> the date and hour keywords, or no keyword at all."""
    if not value["at"]:
        return ({}, {})
    year, month, day, hour, minute, second = value["at"]
    return ({"ORIGINAL_DATE_OF_TIME": [int(year), int(month), int(day)],
             "ORIGINAL_HOUR_OF_TIME": [int(hour), int(minute), int(second)]}, {})


def Rain(*, mm_per_day: Any, tracers: Any, hours: Any = None  # noqa: N802
         ) -> Mapping[str, Any]:
    """Rain or evaporation at every wet node, independent of any hydrograph."""
    return MappingProxyType({"mm_per_day": mm_per_day, "tracers": tracers,
                             "hours": hours})


def _coupling(value: Any) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """The coupled bodies a template named -> what the CARRIER states about them.

    A coupled body's own slots go to that module's steering file, never here. A
    body whose ``given`` values all resolved to nothing was asked for nothing,
    and states nothing."""
    bodies = [body for body in value
              if any(v is not None for v in body.get("given", (True,)))]
    if not bodies:
        return ({}, {})
    slots: dict[str, Any] = {
        "COUPLING_WITH": ";".join(body["module"].upper() for body in bodies)}
    files: dict[str, Any] = {}
    for body in bodies:
        slots[f"{body['module'].upper()}_STEERING_FILE"] = body["steering"]
        if "process" in body:
            slots["WATER_QUALITY_PROCESS"] = body["process"]
        files[body["steering"]] = body
    return slots, files


def Boundaries(*, measured: Any, tracers: Any) -> Mapping[str, Any]:  # noqa: N802
    """The three PRESCRIBED lists, in the order the engine numbers its boundaries.

    Written from ``measured``, the mesh's own walk; ``tracers`` is one each."""
    return MappingProxyType({"measured": measured, "tracers": tracers})


def _boundaries(value: Mapping[str, Any]) -> tuple[Mapping[str, Any],
                                                   Mapping[str, Any]]:
    """The measured boundary walk -> the three lists the engine reads down.

    WHICH list carries a value comes from the ``.cli`` quad, not the role."""
    from trid3nt_server.workflows.mesh.topology import FREE_EXIT_ROLE

    measured = value["measured"]
    order = list(measured["liquid_boundary_order"])
    prescribes = list(measured["liquid_boundary_prescribes"])
    per_tracer = [float(v) for v in value["tracers"]]
    flowrates: list[float] = []
    elevations: list[float] = []
    tracers: list[float] = []
    for number, (role, what) in enumerate(zip(order, prescribes), start=1):
        if what not in ("flowrate", "elevation") and not (
                what == "nothing" and role == FREE_EXIT_ROLE):
            raise ValueError(
                f"liquid boundary {number} ({role!r}) carries a .cli code quad "
                f"that prescribes {what!r}, so nothing this deck writes at that "
                "number would be read; the boundary file and the steering file "
                "would describe different boundaries. A face meant to state no "
                f"condition carries the {FREE_EXIT_ROLE!r} role, which says so.")
        # A free exit reads NEITHER list, so both carry a placeholder that keeps
        # the lists in the measured order rather than shifting past it.
        flowrates.append(float(measured["inflow_q_m3s"])
                         if what == "flowrate" else 0.0)
        elevations.append(float(measured["outflow_stage_m"])
                          if what == "elevation" else 0.0)
        tracers += per_tracer
    return ({"PRESCRIBED_FLOWRATES": flowrates,
             "PRESCRIBED_ELEVATIONS": elevations,
             "PRESCRIBED_TRACERS_VALUES": tracers}, {})


def Runoff(*, node_xy: Any, cn2: Any, antecedent_moisture: Any,  # noqa: N802
           initial_abstraction: Any) -> Mapping[str, Any]:
    """The engine's own SCS curve-number infiltration, per mesh node."""
    return MappingProxyType({"node_xy": node_xy, "cn2": cn2,
                             "antecedent_moisture": antecedent_moisture,
                             "initial_abstraction": initial_abstraction})


def _runoff(value: Mapping[str, Any]) -> tuple[Mapping[str, Any],
                                               Mapping[str, Any]]:
    """The curve-number field -> the runoff keywords and the scatter they name.

    The scatter points ARE the mesh nodes, so the interpolation is identity."""
    import numpy as np

    xy = np.asarray(value["node_xy"], dtype=float)
    cn2 = np.clip(np.asarray(value["cn2"], dtype=float), 1.0, 100.0)
    if cn2.shape[0] != xy.shape[0]:
        raise ValueError(f"the curve-number field has {cn2.shape[0]} values and "
                         f"the mesh has {xy.shape[0]} nodes.")
    rows = ["#X Y CN2 (curve number, AMC-II)",
            *(f"{a:.3f} {b:.3f} {c:.3f}"
              for (a, b), c in zip(xy[:, :2], cn2))]
    # WHICH rainfall-runoff model reads the scatter is a choice among the four the
    # dictionary offers, so the template that wants one states it; a curve-number
    # field is the SCS model's input either way.
    return ({"ANTECEDENT_MOISTURE_CONDITIONS": int(value["antecedent_moisture"]),
             "OPTION_FOR_INITIAL_ABSTRACTION_RATIO": int(
                 value["initial_abstraction"]),
             "FORMATTED_DATA_FILE_2": CN_MAP_FILENAME},
            {CN_MAP_FILENAME: "\n".join(rows) + "\n"})


#: The SCS antecedent-moisture words, and the condition each one names: the
#: wet/dry vocabulary a question is asked in and the I/II/III the literature
#: uses both resolve to the integer the ANTECEDENT MOISTURE CONDITIONS keyword
#: takes. ONE table, so a value typed and a value chosen mean the same condition.
_AMC_CONDITIONS: Mapping[str, int] = MappingProxyType({
    "dry": 1, "i": 1, "1": 1,
    "normal": 2, "ii": 2, "2": 2,
    "wet": 3, "iii": 3, "3": 3,
})
#: The friction law the infiltration surface is written under: its roughness
#: column is Manning n, so the law is the Manning one and not a choice.
_MANNING_LAW = 4


def Infiltration(*, mesh: Any, landcover: Any, table: Any, unmapped: Any,  # noqa: N802
                 uniform_cn: Any, steep_slope_correction: Any,
                 antecedent_moisture: Any, initial_abstraction: Any
                 ) -> Mapping[str, Any]:
    """The infiltration surface: a curve number and a roughness at every node,
    read off the land cover at the accepted mesh's own nodes at fill time.

    ``table`` maps a land-cover class to ``(CN2, Manning n, label)``, ``unmapped``
    is the row a class outside it takes; ``uniform_cn`` overrides the curve
    numbers alone, never the roughness."""
    return MappingProxyType({
        "mesh": mesh, "landcover": landcover, "table": table, "unmapped": unmapped,
        "uniform_cn": uniform_cn, "steep_slope_correction": steep_slope_correction,
        "antecedent_moisture": antecedent_moisture,
        "initial_abstraction": initial_abstraction})


def _huang(cn2: float, slope: float) -> float:
    """The steep-slope curve number the engine's own runoff routine would apply,
    were its branch compiled in: ``CN2 * (322.79 + 15.63 a) / (a + 323.52)`` for
    a slope ``a`` in [0.14, 1.4] m/m, held at the ends, capped at 100."""
    alpha = min(max(float(slope), 0.14), 1.4)
    factor = 1.0 if float(slope) < 0.14 else (322.79 + 15.63 * alpha) / (alpha + 323.52)
    return min(100.0, float(cn2) * factor)


def _infiltration(value: Mapping[str, Any]) -> tuple[Mapping[str, Any],
                                                     Mapping[str, Any]]:
    """The surface -> the runoff keywords and the friction zones, with their files.

    The steep-slope correction is applied here because the installed engine has
    its own branch compiled off; the slopes are the mesh's own bed gradients."""
    from trid3nt_server.workflows.mesh.shared.nodes import (
        accepted_mesh_nodes,
        node_slopes_from_mesh,
        sample_layer_at_nodes,
    )

    points_utm, cells, bed, lonlat = accepted_mesh_nodes(value["mesh"])
    table = {int(code): tuple(row) for code, row in dict(value["table"]).items()}
    rows = [table.get(int(round(float(code))), tuple(value["unmapped"]))
            for code in sample_layer_at_nodes(value["landcover"], lonlat)]
    uniform = value.get("uniform_cn")
    cn2 = [float(uniform) if uniform is not None else float(row[0]) for row in rows]
    if value.get("steep_slope_correction"):
        slopes = node_slopes_from_mesh(points_utm, cells, bed)
        cn2 = [_huang(cn, slope) for cn, slope in zip(cn2, slopes)]
    word = str(value["antecedent_moisture"]).strip().lower()
    if word not in _AMC_CONDITIONS:
        raise ValueError(
            f"antecedent_moisture {value['antecedent_moisture']!r} is not an SCS "
            "condition; the three are 'dry' (AMC I), 'normal' (AMC II) and 'wet' "
            "(AMC III).")
    runoff, runoff_files = _runoff({
        "node_xy": points_utm[:, :2], "cn2": cn2,
        "antecedent_moisture": _AMC_CONDITIONS[word],
        "initial_abstraction": value["initial_abstraction"]})
    friction, friction_files = _friction({
        "law": _MANNING_LAW, "manning_per_node": [float(row[1]) for row in rows]})
    return ({**runoff, **friction}, {**runoff_files, **friction_files})


def Friction(*, law: Any, manning_per_node: Any) -> Mapping[str, Any]:  # noqa: N802
    """Distributed bottom friction: a law per zone, and each node's zone."""
    return MappingProxyType({"law": law, "manning_per_node": manning_per_node})


def _friction(value: Mapping[str, Any]) -> tuple[Mapping[str, Any],
                                                 Mapping[str, Any]]:
    """Per-node Manning -> the zone laws, the zone map, and the keywords naming them.

    Distinct values become zones; the laws file is TERMINATED or the scan runs on."""
    import numpy as np

    values = np.round(np.clip(np.asarray(value["manning_per_node"], dtype=float),
                              0.005, 1.0), 3)
    unique = sorted({float(v) for v in values})
    zone_of = {v: i + 1 for i, v in enumerate(unique)}
    laws = ["* rain-on-grid distributed Manning (per land cover)",
            "* zone  law      coef    vegetation",
            *(f"{zone_of[v]} MANNING {v:.3f} NULL" for v in unique), "END"]
    zones = [f"{i} {zone_of[float(v)]}" for i, v in enumerate(values, start=1)]
    return ({"LAW_OF_BOTTOM_FRICTION": int(value["law"]),
             "FRICTION_DATA": True,
             "FRICTION_DATA_FILE": FRICTION_LAWS_FILENAME,
             "ZONES_FILE": ZONES_FILENAME},
            {FRICTION_LAWS_FILENAME: "\n".join(laws) + "\n",
             ZONES_FILENAME: "\n".join(zones) + "\n"})


def Rating(*, at_boundary: Any, of_boundaries: Any,  # noqa: N802
           rows: Any, note: Any) -> Mapping[str, Any]:
    """One boundary's stage-discharge curve, and which boundary reads it."""
    return MappingProxyType({"at_boundary": at_boundary,
                             "of_boundaries": of_boundaries,
                             "rows": rows, "note": note})


def _rating(value: Mapping[str, Any]) -> tuple[Mapping[str, Any],
                                               Mapping[str, Any]]:
    """The derived Z(Q) -> the curve keywords and the file the engine reads it from.

    The selector is one number per liquid boundary, in the walk order."""
    # ``read_fic_curves.f`` reads a block per curve: a header naming the
    # boundary, a UNITS line it skips without checking, then two columns until a
    # blank or a ``#``. Under a ``Q(n)`` header the first column is the
    # discharge, which is the order the rows arrive in.
    at = int(value["at_boundary"])
    rows = [(float(q), float(z)) for q, z in value["rows"]]
    lines = [f"#{value['note']}", f"Q({at}) Z({at})", "m3/s m",
             *(f"{q:.6f} {z:.4f}" for q, z in rows)]
    # ``bord.f`` reads the curve at every prescribed-depth boundary whose entry is
    # 1, so the selector is one number per liquid boundary in the walk order.
    return ({"STAGE_DISCHARGE_CURVES": [
                 1 if n == at else 0
                 for n in range(1, int(value["of_boundaries"]) + 1)],
             "STAGE_DISCHARGE_CURVES_FILE": RATING_FILENAME},
            {RATING_FILENAME: "\n".join(lines) + "\n"})


def Hyetograph(*, blocks: Any, until_s: Any, fortran: Any  # noqa: N802
               ) -> Mapping[str, Any]:
    """A real gross storm, read per timestep out of a block file."""
    return MappingProxyType({"blocks": blocks, "until_s": until_s,
                             "fortran": fortran})


def _hyetograph(value: Mapping[str, Any]) -> tuple[Mapping[str, Any],
                                                   Mapping[str, Any]]:
    """The storm blocks -> the data file and the user Fortran that reads them.

    Each block is ``[t_end_s, gross_mm]``, with a dry tail past the last instant."""
    if not value["blocks"]:
        # A constant design rate drives this run, so no block file is read and
        # the engine's own compiled branch stands.
        return ({}, {})
    rows: list[tuple[float, float]] = []
    previous = 0.0
    for t_end, millimetres in value["blocks"]:
        t_end, millimetres = float(t_end), float(millimetres)
        if t_end <= previous:
            raise ValueError("hyetograph times must strictly increase; got "
                             f"t_end={t_end} after {previous}.")
        if millimetres < 0.0:
            raise ValueError("hyetograph interval rainfall must be >= 0; got "
                             f"{millimetres} mm.")
        rows.append((t_end, millimetres))
        previous = t_end
    if not rows:
        raise ValueError("the hyetograph has no intervals.")
    tail = float(value["until_s"]) + 3600.0
    if rows[-1][0] < tail:
        rows.append((tail, 0.0))
    lines = ["#HYETOGRAPH FILE (block type; mm per interval)",
             "#T (s) RAINFALL (mm)", "0.",
             *(f"{t:.3f} {mm:.5f}" for t, mm in rows)]
    return ({"FORMATTED_DATA_FILE_1": HYETOGRAPH_FILENAME,
             # QUOTED by the writer: a value opening on '/' would be a comment to
             # DAMOCLES, which erases the keyword AND swallows the line after it.
             "FORTRAN_FILE": str(value["fortran"])},
            {HYETOGRAPH_FILENAME: "\n".join(lines) + "\n"})


T2D = Module("telemac2d")
T2D.VARIABLES = VARIABLES
T2D.LISTING = LISTING
T2D.composites(releases=_releases, wind=_wind, continue_from=_continue_from,
               oil=_oil, rain=_rain, coupling=_coupling,
               boundaries=_boundaries, runoff=_runoff, friction=_friction,
               infiltration=_infiltration, rating=_rating, hyetograph=_hyetograph,
               time_origin=_time_origin, tracer_names=_tracer_names)
T2D.outputs(**PRIMITIVES, drogues=read_drogues)
