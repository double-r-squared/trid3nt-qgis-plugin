"""The TELEMAC-2D wrapper: its dictionary, its composites, and its outputs.

The wrapper asserts NO value of its own. Each composite is ONE value standing for
a keyword group, so the group cannot half-arrive; the outputs are the primitive
set over the module's own variables, because what a result file holds is the
module's fact."""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Mapping, Sequence

from trid3nt_server.inputs.series import align
from trid3nt_server.workflows.runtime import Ref
from trid3nt_server.workflows.runtime.temporal import RATE, STATE

from ..authoring.atmosphere import Atmosphere, expand_atmosphere
from .coupling import couples
from .module import Module, Output
from .outputs import PRIMITIVES, read_drogues

__all__ = ["T2D", "Atmosphere", "Boundaries", "Friction",
           "Infiltration", "MODULE_OUTPUT", "Oil", "Rain", "Storm",
           "Rating", "Runoff", "Sources", "TimeOrigin",
           "TracerNames", "Wind", "SOURCES_FILENAME"]

#: A signed component reads about zero, so its ramp diverges there and the
#: legend is ranged symmetrically; a depth-like field is floored where it stops
#: being water and a terrain field is relief the run did not make.
_SIGNED = {"kind": "mesh", "ramp": "rdbu", "units": "m/s", "center": 0.0}
_TERRAIN = {"kind": "mesh", "ramp": "terrain", "units": "m"}
_LEVEL = {"kind": "mesh", "ramp": "blues", "units": "m"}

#: What the module WRITES, by the mnemonic VARIABLES FOR GRAPHIC PRINTOUTS
#: spells: the name the result file carries it under, its unit and how it draws.
#: ``T`` is the TRACER row - one per NAMES OF TRACERS entry, plus whatever a
#: coupled module appends behind them. ``FLUX`` is the one the module PRINTS
#: rather than writes: the discharge across each liquid boundary, in its own
#: water-volume balance.
MODULE_OUTPUT: Mapping[str, Output] = MappingProxyType({
    "U": Output("VELOCITY U", "m/s", style=_SIGNED),
    "V": Output("VELOCITY V", "m/s", style=_SIGNED),
    "H": Output("WATER DEPTH", "m",
                style={"kind": "mesh", "ramp": "ylgnbu", "units": "m", "floor": 0}),
    "S": Output("FREE SURFACE", "m", style=_LEVEL),
    "B": Output("BOTTOM", "m", style=_TERRAIN, varies=False),
    # The Froude number divides the speed by the square root of the depth, so a
    # node the run never wet divides by a vanishing depth and carries a value in
    # the thousands. Those nodes are outside the wet mask the legend and the
    # measures are both taken over, so the ramp reads the river without a cap
    # clipping the real peak.
    "F": Output("FROUDE NUMBER", "",
                style={"kind": "mesh", "ramp": "magma", "floor": 0}),
    "Q": Output("SCALAR FLOWRATE", "m2/s",
                style={"kind": "mesh", "ramp": "viridis", "units": "m2/s",
                       "floor": 0}),
    "M": Output("SCALAR VELOCITY", "m/s",
                style={"kind": "mesh", "ramp": "viridis", "units": "m/s",
                       "floor": 0}),
    # The wind the run was driven by, as the engine wrote it onto the mesh.
    # LECDON clears both rows where the deck states no wind, so a run without
    # one simply does not carry them.
    "X": Output("WIND ALONG X", "m/s", style=_SIGNED),
    "Y": Output("WIND ALONG Y", "m/s", style=_SIGNED),
    "T": Output("TRACER", "",
                style={"kind": "mesh", "ramp": "reds", "floor": 0}),
    "FLUX": Output("FLUX BOUNDARY", "m3/s"),
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


def Sources(*, at: Any, until_s: Any, window_s: Any = None  # noqa: N802
            ) -> Mapping[str, Any]:
    """The SOURCES FILE for the point sources the deck states BY NAME: WHERE the
    water enters, how long each one discharges, and the horizon its series is
    written over.

    ``at`` is the PLACEMENT this composite discharges at, so the stage that
    settles it onto a node is the workflow's to build off this statement; the
    deck reads the settled point back by name for the engine's own coordinate
    keywords. A MAPPING, not an object: the sheet's one ref walk descends
    mappings."""
    # HOW MUCH is the engine's own keyword - one element per source, in its own
    # positional order - so it is read off the sheet rather than restated here.
    # ``window_s`` is a finite release, held and then stepped to nothing so the
    # slug advects and passes, while ``None`` is a permitted discharge held flat
    # for the whole run.
    return MappingProxyType({
        "at": at, "window_s": window_s, "until_s": until_s,
        "q": Ref("WATER_DISCHARGE_OF_SOURCES"),
        "tracers": Ref("VALUES_OF_THE_TRACERS_AT_THE_SOURCES")})


def _sources(value: Mapping[str, Any]) -> tuple[Mapping[str, Any],
                                                Mapping[str, Any]]:
    """The stated source keywords -> the series file the engine reads them over.

    The tracer array is flattened by position: every tracer of the first source,
    then every tracer of the second, which is how the engine reads it back."""
    discharges = [float(q) for q in value["q"]]
    tracers = [float(v) for v in value["tracers"]]
    if not discharges or len(tracers) % len(discharges):
        raise ValueError(
            f"{len(tracers)} tracer values do not divide across "
            f"{len(discharges)} sources; VALUES OF THE TRACERS AT THE SOURCES "
            "carries every tracer of the first source, then of the second.")
    return ({"SOURCES_FILE": SOURCES_FILENAME},
            {SOURCES_FILENAME: _series(discharges, tracers, value["window_s"],
                                       value["until_s"])})


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
    """SPEED AND DIRECTION OF WIND's own pair, the direction as weather states
    one: the COMPASS bearing the wind blows FROM."""
    return MappingProxyType({"speed_mps": speed_mps, "from_deg": from_deg,
                             "drag": drag})


def Oil(*, presets: Any, named: Any, at: Any,  # noqa: N802
        release_step: Any) -> Mapping[str, Any]:
    """The oil module riding on top of the tracer solve: which preset, released
    where and at which step.

    ``presets`` is the table the deck carries; ``named`` picks one row of it. How
    many floats are written how often is MAXIMUM NUMBER OF DROGUES and PRINTOUT
    PERIOD FOR DROGUES, which the deck states by their own names."""
    # PRINTOUT PERIOD FOR DROGUES counts TIME STEPS, not seconds, so the bounds
    # sidecar restates no range for it: a period is plausible against a run's own
    # step count, which is not a number the table can hold, and the dictionary's
    # own integer type is the whole of what bounds it.
    return MappingProxyType({"presets": presets, "named": named, "at": at,
                             "release_step": release_step})


def _series(discharges: Sequence[float], tracers: Sequence[float],
            window_s: Any, until_s: Any) -> str:
    """The sources time series, on the scenario's own absolute clock.

    A continued run carries the SAME scenario forward, never a re-release."""
    per_source = len(tracers) // len(discharges)
    horizon = float(until_s) + _SERIES_TAIL_S
    breaks = {0.0, horizon}
    if window_s is not None:
        breaks |= {float(window_s), float(window_s) + _STEP_GAP_S}
    times = sorted(t for t in breaks if t <= horizon)
    columns = ["T"] + [f"Q({i})" for i in range(1, len(discharges) + 1)] + [
        f"TR({i},{j})" for i in range(1, len(discharges) + 1)
        for j in range(1, per_source + 1)]
    units = ["s"] + ["m3/s"] * len(discharges) + ["mg/l"] * len(tracers)
    rows = ["#", " ".join(columns), " ".join(units)]
    for t in times:
        # A finite release is held, then stepped to nothing so the slug advects
        # and passes; no window at all is a permitted discharge held flat.
        on = 1.0 if window_s is None or t <= float(window_s) else 0.0
        values = [on * v for v in list(discharges) + list(tracers)]
        rows.append(" ".join([f"{t:.3f}"] + [f"{v:.6g}" for v in values]))
    return "\n".join(rows) + "\n"


def _wind(value: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """The wind -> the pair the module already carries, and the term it arms."""
    if not value["speed_mps"]:
        # A wind of no speed is not a wind. Stating one would put the whole wind
        # block in the deck for a run nobody asked a wind about.
        return ({}, {})
    return ({"WIND": True,
             "SPEED_AND_DIRECTION_OF_WIND": [float(value["speed_mps"]),
                                             _engine_bearing(value["from_deg"])],
             **({} if value.get("drag") is None else
                {"COEFFICIENT_OF_WIND_INFLUENCE": float(value["drag"])})}, {})


def _engine_bearing(from_deg: Any) -> float:
    """A compass bearing the wind blows FROM -> the direction the keyword takes.

    The dictionary states its own convention at the keyword: degrees 0 to 360
    with 0 at y = 0, x = +infinity, counted the way an angle is - so it is the
    direction the wind blows TOWARD, from +x, while weather names the quarter it
    comes from, from north, the other way round."""
    return float(270.0 - float(from_deg)) % 360.0


def _oil(value: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """The oil module: its steering file, its per-run Fortran, and the files the
    floats are written to and from."""
    from ..authoring.oil import release_routine, steering_text

    presets = value["presets"]
    name = str(value["named"]).strip().lower().replace(" ", "_")
    if name not in presets:
        raise ValueError(f"{value['named']!r} is not an oil preset this deck "
                         f"carries; the presets are {sorted(presets)}.")
    x, y = value["at"]
    return ({"FORTRAN_FILE": USER_FORTRAN_DIR,
             "OIL_SPILL_STEERING_FILE": OIL_STEERING_FILENAME,
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


#: WHAT A PRESCRIBED LIST IS READ IN, by the thing the .cli quad prescribes. The
#: engine fixes it per list, so it is the unit a slot that fills one converts its
#: record to as well - stated here once, where the list is written.
PRESCRIBED_UNITS: Mapping[str, str] = MappingProxyType(
    {"flowrate": "m3/s", "elevation": "m"})


def Boundaries(*, measured: Any, tracers: Any) -> Mapping[str, Any]:  # noqa: N802
    """The three PRESCRIBED lists, in the order the engine numbers its boundaries,
    and the LIQUID BOUNDARIES FILE for every one a record measured over time.

    ``measured`` is the settle's own record - the mesh's walk, the flow the
    inflow run carries, the level the outflow holds and the run's clock;
    ``tracers`` is one value each."""
    return MappingProxyType({"measured": measured, "tracers": tracers})


def _boundaries(value: Mapping[str, Any]) -> tuple[Mapping[str, Any],
                                                   Mapping[str, Any]]:
    """The measured boundary walk -> the three lists the engine reads down, and
    the file it reads a measured boundary's own column out of instead.

    WHICH list carries a value comes from the ``.cli`` quad, not the role. A
    boundary whose record reported a WINDOW writes its column; the engine reads
    the steering list for every column the file does not carry, so a run states
    both and each boundary is read off whichever one measured it."""
    from trid3nt_server.workflows.mesh.topology import FREE_EXIT_ROLE

    from ..authoring.boundaries import (
        LIQUID_BOUNDARIES_FILENAME,
        column_name,
        liquid_boundaries_file,
    )

    measured = value["measured"]
    order = list(measured["liquid_boundary_order"])
    prescribes = list(measured["liquid_boundary_prescribes"])
    per_tracer = [float(v) for v in value["tracers"]]
    windows = {"flowrate": measured.get("inflow_q_series"),
               "elevation": measured.get("outflow_stage_series")}
    step_s = float(measured.get("time_step_s") or 0.0)
    start_s = float(measured.get("start_time_s") or 0.0)
    flowrates: list[float] = []
    elevations: list[float] = []
    tracers: list[float] = []
    columns: list[tuple[str, str, Any]] = []
    for number, (role, what) in enumerate(zip(order, prescribes), start=1):
        if what not in ("flowrate", "elevation") and not (
                what == "nothing" and role == FREE_EXIT_ROLE):
            raise ValueError(
                f"liquid boundary {number} ({role!r}) carries a .cli code quad "
                f"that prescribes {what!r}, so nothing this deck writes at that "
                "number would be read; the boundary file and the steering file "
                "would describe different boundaries. A face meant to state no "
                f"condition carries the {FREE_EXIT_ROLE!r} role, which says so.")
        if what == "flowrate" and measured["inflow_q_m3s"] is None:
            raise ValueError(
                f"liquid boundary {number} ({role!r}) prescribes a flowrate and "
                "this run imposed none: the discharge slot was not filled, so "
                "there is no flow to write at that boundary. State the flow on "
                "the carrier slot, or name a source that reaches this water.")
        # A free exit reads NEITHER list, so both carry a placeholder that keeps
        # the lists in the measured order rather than shifting past it.
        flowrates.append(float(measured["inflow_q_m3s"])
                         if what == "flowrate" else 0.0)
        elevations.append(float(measured["outflow_stage_m"])
                          if what == "elevation" else 0.0)
        tracers += per_tracer
        window = windows.get(what)
        if window is not None:
            unit = PRESCRIBED_UNITS[what]
            # A FLOW is a rate and a STAGE is a level, so the two move onto the
            # engine's own step by different rules; both open where this run does.
            columns.append((column_name(what, number), unit,
                            align(window, onto=step_s, opening_at=start_s,
                                  quantity=RATE if what == "flowrate" else STATE,
                                  units=unit).series))
    # A CLOSED body has no liquid boundary to prescribe anything at, and a run
    # with no tracers prescribes none. An EMPTY list is not that statement - it
    # is a keyword with nothing after it, which DAMOCLES reads as the next
    # line's business - so a list with nothing in it is not written at all.
    keywords: dict[str, Any] = {
        **({} if not flowrates else {"PRESCRIBED_FLOWRATES": flowrates}),
        **({} if not elevations else {"PRESCRIBED_ELEVATIONS": elevations}),
        **({} if not tracers else {"PRESCRIBED_TRACERS_VALUES": tracers})}
    if not columns:
        return (keywords, {})
    return ({**keywords, "LIQUID_BOUNDARIES_FILE": LIQUID_BOUNDARIES_FILENAME},
            {LIQUID_BOUNDARIES_FILENAME: liquid_boundaries_file(
                columns, start_s=start_s,
                until_s=float(measured["until_s"]), tail_s=_SERIES_TAIL_S,
                note=str(measured.get("discharge_note") or ""))})


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
        "manning_per_node": [float(row[1]) for row in rows]})
    return ({**runoff, **friction}, {**runoff_files, **friction_files})


def Friction(*, manning_per_node: Any) -> Mapping[str, Any]:  # noqa: N802
    """Distributed bottom friction: the roughness at each node, as its zone.

    The LAW the zones are read under is the deck's own LAW OF BOTTOM FRICTION;
    this file's roughness column is Manning n, so a deck naming it states 4."""
    return MappingProxyType({"manning_per_node": manning_per_node})


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
    return ({"FRICTION_DATA": True,
             "FRICTION_DATA_FILE": FRICTION_LAWS_FILENAME,
             "ZONES_FILE": ZONES_FILENAME},
            {FRICTION_LAWS_FILENAME: "\n".join(laws) + "\n",
             ZONES_FILENAME: "\n".join(zones) + "\n"})


def Rating(*, measured: Any) -> Mapping[str, Any]:  # noqa: N802
    """One boundary's stage-discharge curve, and which boundary reads it.

    ``measured`` is the curve the workflow derives against the mesh, so the
    stage that derives it is the workflow's to build off this statement; what
    the engine reads is that measurement's own four rows."""
    return MappingProxyType({"at_boundary": Ref(f"{measured.path}.at_boundary"),
                             "of_boundaries": Ref(f"{measured.path}.of_boundaries"),
                             "rows": Ref(f"{measured.path}.rows"),
                             "note": Ref(f"{measured.path}.note"),
                             "measured": measured})


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


#: The interval a measured rainfall record is accumulated over. Every gross-rain
#: product this run is driven by publishes hourly totals, and a storm read per
#: timestep out of a block file is read as the total over the block it is in.
_STORM_INTERVAL_S = 3600.0


def Storm(*, mm_per_day: Any, hours: Any, until_s: Any,  # noqa: N802
          series: Any = None, record: Any = None, tracers: Any = 0,
          fortran: Any = None) -> Mapping[str, Any]:
    """The storm over the domain: a measured record, or a constant design rate.

    A stated ``series`` of hourly gross millimetres wins; else ``record``, the
    same hours as a source published them; with neither, the constant rate over
    ``hours`` drives the run, in RAIN OR EVAPORATION IN MM PER DAY's own unit."""
    return MappingProxyType({"mm_per_day": mm_per_day, "hours": hours,
                             "until_s": until_s, "series": series,
                             "record": record, "tracers": tracers,
                             "fortran": fortran})


def _storm(value: Mapping[str, Any]) -> tuple[Mapping[str, Any],
                                              Mapping[str, Any]]:
    """The storm -> either the block file and its reader, or the rate keywords.

    A record is blocks of ``[t_end_s, gross_mm]`` assembled from its own hourly
    totals, with a dry tail past the last simulated instant so the recession limb
    has somewhere to fall; a design rate is the engine's own constant branch."""
    series = value.get("series")
    if series is None:
        series = value.get("record")
    if series is None:
        rate = value["mm_per_day"]
        if rate is None:
            return ({}, {})
        return ({"RAIN_OR_EVAPORATION": True,
                 "RAIN_OR_EVAPORATION_IN_MM_PER_DAY": float(rate),
                 **_rain_tracers(value["tracers"]),
                 # A window is stated only when it CLOSES inside the run: a storm
                 # that outlasts the horizon never stops, and a keyword saying so
                 # would be an end nothing reaches.
                 **({} if value["hours"] is None
                    or float(value["hours"]) * _STORM_INTERVAL_S
                    >= float(value["until_s"])
                    else {"DURATION_OF_RAIN_OR_EVAPORATION_IN_HOURS":
                          float(value["hours"])})}, {})
    millimetres = [max(0.0, float(v)) for v in series]
    if len(millimetres) < 2:
        raise ValueError(f"a measured storm carries {len(millimetres)} interval(s); "
                         "a hyetograph needs at least two. Widen the window, or "
                         "state a design rate.")
    blocks = [(float((i + 1) * _STORM_INTERVAL_S), millimetres[i])
              for i in range(len(millimetres))]
    keywords, files = _blocks_file(blocks, value["until_s"], value["fortran"])
    return ({**keywords, **_rain_tracers(value["tracers"])}, files)


def _blocks_file(blocks: Any, until_s: Any,
                 fortran: Any) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """Gross rain per interval -> the block file and the routine that reads it.

    Each block is ``[t_end_s, gross_mm]``; the tail past the last simulated
    instant is dry, so a storm that stops inside the run stops in the file too."""
    rows: list[tuple[float, float]] = []
    previous = 0.0
    for t_end, millimetres in blocks:
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
    tail = float(until_s) + _STORM_INTERVAL_S
    if rows[-1][0] < tail:
        rows.append((tail, 0.0))
    if not fortran:
        raise ValueError(
            "a measured storm is read per timestep by a user Fortran routine and "
            "this run names none; state the routine the image bakes, or drive the "
            "run with a design rate.")
    lines = ["#HYETOGRAPH FILE (block type; mm per interval)",
             "#T (s) RAINFALL (mm)", "0.",
             *(f"{t:.3f} {mm:.5f}" for t, mm in rows)]
    return ({# The engine's rain source term is gated on this keyword: prosou.f
             # calls no runoff routine without it, so a deck naming the block
             # file and the routine that reads it and leaving this unwritten
             # states a storm the engine never applies. The rate keyword stays
             # unstated - the routine reads every interval off the file, and the
             # window keyword is not read on this branch at all.
             "RAIN_OR_EVAPORATION": True,
             "FORMATTED_DATA_FILE_1": HYETOGRAPH_FILENAME,
             # QUOTED by the writer: a value opening on '/' would be a comment to
             # DAMOCLES, which erases the keyword AND swallows the line after it.
             "FORTRAN_FILE": str(fortran)},
            {HYETOGRAPH_FILENAME: "\n".join(lines) + "\n"})


def _rain_tracers(tracers: Any) -> Mapping[str, Any]:
    """The rainwater concentrations DAMOCLES demands, one per tracer, or nothing.

    An EMPTY list is not the statement that a run carries no tracer - it is a
    keyword with nothing after it, which DAMOCLES reads as the next line."""
    count = int(tracers or 0)
    return {} if not count else {
        "VALUES_OF_TRACERS_IN_THE_RAIN": [0.0] * count}


T2D = Module("telemac2d")
T2D.MODULE_OUTPUT = MODULE_OUTPUT
T2D.LISTING = LISTING
T2D.PRINTOUTS = "VARIABLES_FOR_GRAPHIC_PRINTOUTS"
T2D.CADENCE = "GRAPHIC_PRINTOUT_PERIOD"
T2D.TRACER = "T"
T2D.ARMS = MappingProxyType({
    "SPEED_AND_DIRECTION_OF_WIND": "WIND",
    "RAIN_OR_EVAPORATION_IN_MM_PER_DAY": "RAIN_OR_EVAPORATION"})
T2D.composites(sources=_sources, wind=_wind,
               atmosphere=expand_atmosphere,
               oil=_oil, rain=_rain, coupling=couples(water_column=False),
               boundaries=_boundaries, runoff=_runoff, friction=_friction,
               infiltration=_infiltration, rating=_rating, storm=_storm,
               time_origin=_time_origin, tracer_names=_tracer_names)
T2D.reads(**PRIMITIVES, drogues=read_drogues)
