"""The TELEMAC-2D wrapper: its dictionary, its composites, and its outputs.

Each composite is one value standing for a keyword group, so the group cannot half-arrive; the
outputs are the primitive set over the module's own variables.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Mapping, Sequence

from trid3nt_server.inputs.series import align
from trid3nt_server.workflows.runtime.temporal import RATE, STATE, Series

from ..authoring.atmosphere import (ATMOSPHERE_READS, Atmosphere,
                                     expand_atmosphere)
from .coupling import couples
from .module import Module, Output, SlotRefused
from .outputs import PRIMITIVES, read_drogues

__all__ = ["T2D", "Atmosphere", "Boundaries", "Breach", "Friction", "Infiltration",
           "MODULE_OUTPUT", "Oil", "Storm", "Rating", "Runoff", "Sources",
           "TracerNames", "Wind", "SOURCES_FILENAME"]

# A signed component reads about zero, so its ramp diverges and the legend is symmetric; a depth-like field is floored where it stops being water; terrain is relief the run did not make.
_SIGNED = {"kind": "mesh", "ramp": "rdbu", "units": "m/s", "center": 0.0}
_TERRAIN = {"kind": "mesh", "ramp": "terrain", "units": "m"}
_LEVEL = {"kind": "mesh", "ramp": "blues", "units": "m"}

# What the module writes, by VARIABLES FOR GRAPHIC PRINTOUTS mnemonic. ``T`` is the tracer row (one per NAMES OF TRACERS entry plus coupled appends). ``FLUX`` is printed, not written: the discharge across each liquid boundary in the water-volume balance.
MODULE_OUTPUT: Mapping[str, Output] = MappingProxyType({
    "U": Output("VELOCITY U", "m/s", style=_SIGNED),
    "V": Output("VELOCITY V", "m/s", style=_SIGNED),
    "H": Output("WATER DEPTH", "m",
                style={"kind": "mesh", "ramp": "ylgnbu", "units": "m", "floor": 0}),
    "S": Output("FREE SURFACE", "m", style=_LEVEL),
    "B": Output("BOTTOM", "m", style=_TERRAIN, varies=False),
    # Froude divides by sqrt(depth), so never-wet nodes carry values in the thousands; they are outside the wet mask the legend and measures use, so no cap clips the real peak.
    "F": Output("FROUDE NUMBER", "",
                style={"kind": "mesh", "ramp": "magma", "floor": 0}),
    "Q": Output("SCALAR FLOWRATE", "m2/s",
                style={"kind": "mesh", "ramp": "viridis", "units": "m2/s",
                       "floor": 0}),
    "M": Output("SCALAR VELOCITY", "m/s",
                style={"kind": "mesh", "ramp": "viridis", "units": "m/s",
                       "floor": 0}),
    # LECDON clears both wind rows where the deck states no wind, so such a run does not carry them.
    "X": Output("WIND ALONG X", "m/s", style=_SIGNED),
    "Y": Output("WIND ALONG Y", "m/s", style=_SIGNED),
    "T": Output("TRACER", "",
                style={"kind": "mesh", "ramp": "reds", "floor": 0}),
    "FLUX": Output("FLUX BOUNDARY", "m3/s"),
})
LISTING: frozenset[str] = frozenset({"FLUX"})

# The point-source time series the SOURCES FILE names.
SOURCES_FILENAME = "river_sources.txt"
# Distributed fields of a rain-fed catchment: per-node curve number, friction zones and laws, the block hyetograph, and the outlet's Z(Q).
CN_MAP_FILENAME = "rog_cn_map.dat"
FRICTION_LAWS_FILENAME = "rog_friction.tbl"
ZONES_FILENAME = "rog_zones.dat"
HYETOGRAPH_FILENAME = "rog_hyeto.txt"
RATING_FILENAME = "rog_rating.txt"
# The directory the engine compiles for patched Fortran; the keyword names a directory, so manifest and steering statement share the word.
USER_FORTRAN_DIR = "user_fortran"
OIL_STEERING_FILENAME = "oil_spill.txt"
DROGUES_FILENAME = "drogues.txt"

# How far past the last simulated instant a forcing series runs, so time interpolation never reads off its end.
_SERIES_TAIL_S = 100.0
# The gap a step change is written across: a series is interpolated linearly, so a stopping release states both sides of the step.
_STEP_GAP_S = 0.1


def Sources(*, window_s: Any = None) -> Mapping[str, Any]:  # noqa: N802
    """The SOURCES FILE for the point sources the deck states by name: where water enters, how long each discharges, and the series horizon.

    The settled ``source`` fills the coordinate keywords; ``window_s`` is a number or the name of the input holding one.
    """
    # How much is the engine's own keyword, one element per source in its positional order, read off the sheet. A finite ``window_s`` is held then stepped to nothing so the slug passes; ``None`` holds the discharge flat for the run.
    return MappingProxyType({
        "window_s": window_s, "settled": "settled",
        "q": "WATER_DISCHARGE_OF_SOURCES",
        "tracers": "VALUES_OF_THE_TRACERS_AT_THE_SOURCES"})


def _sources(value: Mapping[str, Any]) -> tuple[Mapping[str, Any],
                                                Mapping[str, Any]]:
    """The stated source keywords -> the series file the engine reads them over.

    The tracer array is flattened by position: every tracer of the first source, then the second, as the engine reads it back.
    """
    discharges = [float(q) for q in value["q"]]
    tracers = [float(v) for v in value["tracers"]]
    if not discharges or len(tracers) % len(discharges):
        raise ValueError(
            f"{len(tracers)} tracer values do not divide across "
            f"{len(discharges)} sources; VALUES OF THE TRACERS AT THE SOURCES "
            "carries every tracer of the first source, then of the second.")
    return ({"SOURCES_FILE": SOURCES_FILENAME},
            {SOURCES_FILENAME: _series(discharges, tracers, value["window_s"],
                                       value["settled"]["until_s"])})


def TracerNames(*, names: Any) -> Mapping[str, Any]:  # noqa: N802
    """The tracer names, the first called what the settled ``source`` is called: a user's name for the point replaces the template's word."""
    return MappingProxyType({"names": names, "named_by": "source"})


def _tracer_names(value: Mapping[str, Any]
                  ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """The NAMES OF TRACERS list, its first entry renamed when a name came; a name is 32 characters (name in 16, unit after)."""
    names = [str(name) for name in value["names"]]
    placed = value.get("named_by")
    given = placed.get("name") if isinstance(placed, Mapping) else None
    if given and names:
        first = names[0].ljust(32)
        names[0] = f"{str(given).strip()[:16]:<16}{first[16:]}".rstrip()
    return ({"NAMES_OF_TRACERS": names}, {})


def Wind(*, speed_mps: Any, from_deg: Any,  # noqa: N802 - a value constructor
         drag: Any = None) -> Mapping[str, Any]:
    """SPEED AND DIRECTION OF WIND's pair, the direction as weather states it: the compass bearing the wind blows FROM."""
    return MappingProxyType({"speed_mps": speed_mps, "from_deg": from_deg,
                             "drag": drag})


def Oil(*, presets: Any, named: Any,  # noqa: N802
        release_step: Any) -> Mapping[str, Any]:
    """The oil module riding on the tracer solve: which preset, released at the settled ``source`` and at which step.

    ``presets`` is the deck's table; ``named`` picks a row. Float count and period are MAXIMUM NUMBER OF DROGUES and PRINTOUT PERIOD FOR DROGUES, stated by name.
    """
    # PRINTOUT PERIOD FOR DROGUES counts time steps, so the bounds sidecar restates no range: plausibility depends on the run's step count, and the integer type is the only bound.
    return MappingProxyType({"presets": presets, "named": named, "at": "source",
                             "release_step": release_step})


def _series(discharges: Sequence[float], tracers: Sequence[float],
            window_s: Any, until_s: Any) -> str:
    """The sources time series, on the scenario's absolute clock.

    A continued run carries the same scenario forward, never a re-release.
    """
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
        # A finite release is held then stepped to nothing so the slug passes; no window is a permitted discharge held flat.
        on = 1.0 if window_s is None or t <= float(window_s) else 0.0
        values = [on * v for v in list(discharges) + list(tracers)]
        rows.append(" ".join([f"{t:.3f}"] + [f"{v:.6g}" for v in values]))
    return "\n".join(rows) + "\n"


def _wind(value: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    if not value["speed_mps"]:
        # A wind of no speed states no wind block.
        return ({}, {})
    return ({"WIND": True,
             "SPEED_AND_DIRECTION_OF_WIND": [float(value["speed_mps"]),
                                             _engine_bearing(value["from_deg"])],
             **({} if value.get("drag") is None else
                {"COEFFICIENT_OF_WIND_INFLUENCE": float(value["drag"])})}, {})


def _engine_bearing(from_deg: Any) -> float:
    """A compass bearing the wind blows FROM -> the direction the keyword takes.

    The dictionary counts degrees 0 to 360 from +x like an angle, i.e. the direction the wind blows TOWARD; weather names the quarter it comes from, from north.
    """
    return float(270.0 - float(from_deg)) % 360.0


def _oil(value: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """The oil module: its steering file, per-run Fortran, and the float files written to and from."""
    from ..authoring.oil import release_routine, steering_text

    presets = value["presets"]
    name = str(value["named"]).strip().lower().replace(" ", "_")
    if name not in presets:
        raise ValueError(f"{value['named']!r} is not an oil preset this deck "
                         f"carries; the presets are {sorted(presets)}.")
    x, y = value["at"]["at"]
    return ({"FORTRAN_FILE": USER_FORTRAN_DIR,
             "OIL_SPILL_STEERING_FILE": OIL_STEERING_FILENAME,
             "ASCII_DROGUES_FILE": DROGUES_FILENAME},
            {OIL_STEERING_FILENAME: steering_text(name, presets[name]),
             f"{USER_FORTRAN_DIR}/oil_flot.f": release_routine(
                 int(value["release_step"]), float(x), float(y))})


# What a prescribed list is read in, by what the .cli quad prescribes. The engine fixes it per list, so slots filling one convert their record to it.
PRESCRIBED_UNITS: Mapping[str, str] = MappingProxyType(
    {"flowrate": "m3/s", "elevation": "m"})


def Boundaries(*, tracers: Any) -> Mapping[str, Any]:  # noqa: N802
    """The three prescribed lists, in the engine's boundary numbering, and the LIQUID BOUNDARIES FILE for every one a record measured over time.

    ``measured`` is the settle's record (mesh walk, inflow flow, outflow level, clock); ``tracers`` is one value each, a keyword's name standing for its list or an input's name.
    """
    return MappingProxyType({"measured": "settled", "tracers": tracers})


def _boundaries(value: Mapping[str, Any]) -> tuple[Mapping[str, Any],
                                                   Mapping[str, Any]]:
    """The measured boundary walk -> the three lists the engine reads down, and the file holding a measured boundary's column.

    Which list carries a value comes from the ``.cli`` quad, not the role. The engine reads the steering list for every column the file lacks, so a run states both.
    """
    from ..authoring.topology import FREE_EXIT_ROLE

    from ..authoring.boundaries import (
        LIQUID_BOUNDARIES_FILENAME,
        TRACER,
        column_name,
        liquid_boundaries_file,
    )

    measured = value["measured"]
    order = list(measured["liquid_boundary_order"])
    prescribes = list(measured["liquid_boundary_prescribes"])
    # A tracer measured over its window arrives as a series; the steering list still carries one number per tracer per boundary (read where the file has no column), so a series lumps to what it opened at.
    per_tracer = [getattr(v, "forcing", v) for v in value["tracers"]]
    lumped = [float(v.values[0]) if isinstance(v, Series) else float(v)
              for v in per_tracer]
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
        # A free exit reads neither list; placeholders keep the lists in measured order.
        flowrates.append(float(measured["inflow_q_m3s"])
                         if what == "flowrate" else 0.0)
        elevations.append(float(measured["outflow_stage_m"])
                          if what == "elevation" else 0.0)
        tracers += lumped
        # The water arriving at a feeding face carries the measured tracer, so a series is written at flow-prescribing boundaries only.
        if what == "flowrate":
            for position, found in enumerate(per_tracer, start=1):
                if isinstance(found, Series):
                    columns.append((
                        column_name(TRACER, number, position), found.units,
                        align(found, onto=step_s, opening_at=start_s,
                              quantity=STATE, units=found.units).series))
        window = windows.get(what)
        if window is not None:
            unit = PRESCRIBED_UNITS[what]
            # A flow is a rate and a stage is a level, so they move onto the engine's step by different rules; both open where the run does.
            columns.append((column_name(what, number), unit,
                            align(window, onto=step_s, opening_at=start_s,
                                  quantity=RATE if what == "flowrate" else STATE,
                                  units=unit).series))
    if lumped:
        _tracer_note(per_tracer)
    # A closed body or tracerless run prescribes nothing. An empty list is a keyword with nothing after it, which DAMOCLES reads as the next line's business, so it is not written.
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


def _tracer_note(per_tracer: Sequence[Any]) -> None:
    """Say on the journal whether what the water arrives carrying was measured over the window or held as one number.

    A question decided by an inflow tracer is decided by that difference.
    """
    from trid3nt_server.workflows.runtime import journal_note

    driven = [n for n, v in enumerate(per_tracer, start=1) if isinstance(v, Series)]
    if driven:
        journal_note(
            f"the inflow tracer(s) {driven} are driven by a measured series "
            "over the whole window, read out of the liquid boundaries file.")
        return
    journal_note(
        "the inflow tracers are STATED: each is held at one value for the "
        "whole window, because no record on this water measured one over time "
        "at a feeding face. A record from another reach is a different site's "
        "water and is not substituted for it.")


def Runoff(*, node_xy: Any, cn2: Any, antecedent_moisture: Any,  # noqa: N802
           initial_abstraction: Any) -> Mapping[str, Any]:
    """The engine's SCS curve-number infiltration, per mesh node."""
    return MappingProxyType({"node_xy": node_xy, "cn2": cn2,
                             "antecedent_moisture": antecedent_moisture,
                             "initial_abstraction": initial_abstraction})


# The ranges a value is usually found in: outside one it is used as stated with a note; only an impossible value refuses.
_USUAL_CN = (1.0, 100.0)
_USUAL_MANNING = (0.005, 1.0)


def _checked(name: str, values: Any, *, above: float,
             at_most: float | None = None,
             usual: tuple[float, float]) -> Any:
    import numpy as np

    from trid3nt_server.workflows.runtime import journal_note

    array = np.asarray(values, dtype=float)
    impossible = ~np.isfinite(array) | (array <= above)
    if at_most is not None:
        impossible |= array > at_most
    if impossible.any():
        scale = (f"greater than {above:g}" if at_most is None
                 else f"greater than {above:g} and at most {at_most:g}")
        raise SlotRefused(
            f"a {name} of {float(array[impossible][0]):g} is impossible - it is "
            f"{scale} - so no run starts on it; state the {name} again.")
    unusual = (array < usual[0]) | (array > usual[1])
    if unusual.any():
        journal_note(
            f"{int(unusual.sum())} {name} value(s), {float(array[unusual][0]):g} "
            f"among them, are outside the usual {usual[0]:g}-{usual[1]:g} range "
            "and are used as stated.")
    return array


def _runoff(value: Mapping[str, Any]) -> tuple[Mapping[str, Any],
                                               Mapping[str, Any]]:
    """The curve-number field -> the runoff keywords and the scatter they name.

    The scatter points are the mesh nodes, so the interpolation is identity.
    """
    import numpy as np

    xy = np.asarray(value["node_xy"], dtype=float)
    cn2 = _checked("curve number CN2", value["cn2"], above=0.0, at_most=100.0,
                   usual=_USUAL_CN)
    if cn2.shape[0] != xy.shape[0]:
        raise ValueError(f"the curve-number field has {cn2.shape[0]} values and "
                         f"the mesh has {xy.shape[0]} nodes.")
    rows = ["#X Y CN2 (curve number, AMC-II)",
            *(f"{a:.3f} {b:.3f} {c:.3f}"
              for (a, b), c in zip(xy[:, :2], cn2))]
    # Which of the dictionary's four rainfall-runoff models reads the scatter is the template's choice; a curve-number field is SCS input either way.
    return ({"ANTECEDENT_MOISTURE_CONDITIONS": int(value["antecedent_moisture"]),
             "OPTION_FOR_INITIAL_ABSTRACTION_RATIO": int(
                 value["initial_abstraction"]),
             "FORMATTED_DATA_FILE_2": CN_MAP_FILENAME},
            {CN_MAP_FILENAME: "\n".join(rows) + "\n"})


# The SCS antecedent-moisture words (wet/dry and I/II/III) resolve to the integer the ANTECEDENT MOISTURE CONDITIONS keyword takes, in one table.
_AMC_CONDITIONS: Mapping[str, int] = MappingProxyType({
    "dry": 1, "i": 1, "1": 1,
    "normal": 2, "ii": 2, "2": 2,
    "wet": 3, "iii": 3, "3": 3,
})


def Infiltration(*, landcover: Any, table: Any, unmapped: Any,  # noqa: N802
                 uniform_cn: Any, steep_slope_correction: Any,
                 antecedent_moisture: Any, initial_abstraction: Any
                 ) -> Mapping[str, Any]:
    """The infiltration surface: a curve number and a roughness at every node, read off the land cover at the mesh's nodes at fill time.

    ``table`` maps a class to ``(CN2, Manning n, label)``; ``unmapped`` is the row for a class outside it; ``uniform_cn`` overrides curve numbers only.
    """
    return MappingProxyType({
        "mesh": "mesh", "landcover": landcover, "table": table, "unmapped": unmapped,
        "uniform_cn": uniform_cn, "steep_slope_correction": steep_slope_correction,
        "antecedent_moisture": antecedent_moisture,
        "initial_abstraction": initial_abstraction})


def _huang(cn2: float, slope: float) -> float:
    """The steep-slope curve number the engine's runoff routine would apply were its branch compiled in: ``CN2 * (322.79 + 15.63 a) / (a + 323.52)`` for slope ``a`` in [0.14, 1.4] m/m, held at the ends, capped at 100."""
    alpha = min(max(float(slope), 0.14), 1.4)
    factor = 1.0 if float(slope) < 0.14 else (322.79 + 15.63 * alpha) / (alpha + 323.52)
    return min(100.0, float(cn2) * factor)


def _infiltration(value: Mapping[str, Any]) -> tuple[Mapping[str, Any],
                                                     Mapping[str, Any]]:
    """The surface -> the runoff keywords and the friction zones, with their files.

    The steep-slope correction is applied here because the installed engine has its own branch compiled off.
    """
    from trid3nt_server.tools.mesh.shared.nodes import (
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

    The file's column is Manning n, so a deck naming the LAW OF BOTTOM FRICTION states 4.
    """
    return MappingProxyType({"manning_per_node": manning_per_node})


def _friction(value: Mapping[str, Any]) -> tuple[Mapping[str, Any],
                                                 Mapping[str, Any]]:
    """Per-node Manning -> the zone laws, zone map and naming keywords; distinct values become zones, and the laws file is TERMINATED or the scan runs on."""
    import numpy as np

    values = np.round(_checked("Manning n", value["manning_per_node"], above=0.0,
                               usual=_USUAL_MANNING), 3)
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


def Breach(*, line: Any, width_m: Any, opens_at_s: Any, duration_s: Any,  # noqa: N802
           final_bed_m: Any, growth: Any, initial_width_m: Any = None
           ) -> Mapping[str, Any]:
    """A dyke breach along a drawn lon/lat ``line`` on the crest: opened at ``opens_at_s``, lowered to ``final_bed_m`` over ``duration_s``, across ``width_m``, growing laterally by the engine's option ``growth``."""
    return MappingProxyType({"line": line, "width_m": width_m,
                             "opens_at_s": opens_at_s, "duration_s": duration_s,
                             "final_bed_m": final_bed_m, "growth": growth,
                             "initial_width_m": initial_width_m})


def _breach(value: Any, *, run: Mapping[str, Any]
            ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """One breach or several -> the breaches data file in the mesh's metres.

    The lon/lat line is projected into the settled mesh's zone.
    """
    from pyproj import Transformer

    from ..authoring.breaches import BREACHES_FILENAME, text

    epsg = (run.get("settled") or {}).get("utm_epsg")
    if epsg is None:
        raise ValueError("a breach is placed in the mesh's own metres and this "
                         "run has settled no mesh to project its line into.")
    to_mesh = Transformer.from_crs(4326, int(epsg), always_xy=True)
    breaches = [value] if isinstance(value, Mapping) else list(value)
    widths = [b.get("initial_width_m") is not None for b in breaches]
    if any(widths) and not all(widths):
        raise ValueError("the engine reads an initial width for every breach or "
                         "for none; state one on each breach or on none.")
    placed = [{**dict(b), "line_xy": [to_mesh.transform(float(lon), float(lat))
                                      for lon, lat in b["line"]]}
              for b in breaches]
    return ({"BREACHES_DATA_FILE": BREACHES_FILENAME,
             **({"INITIAL_WIDTHS_OF_BREACHES": True} if all(widths) else {})},
            {BREACHES_FILENAME: text(placed, initial_widths=all(widths))})


def Rating(*, measured: Any) -> Mapping[str, Any]:  # noqa: N802
    """One boundary's stage-discharge curve, and which boundary reads it.

    ``measured`` names the curve the workflow derives against the mesh; the engine reads its four rows.
    """
    return MappingProxyType({"measured": measured})


def _rating(value: Mapping[str, Any]) -> tuple[Mapping[str, Any],
                                               Mapping[str, Any]]:
    """The derived Z(Q) -> the curve keywords and the file the engine reads it from.

    The selector is one number per liquid boundary, in walk order.
    """
    # ``read_fic_curves.f`` reads a block per curve: a header naming the boundary, a UNITS line it skips, then two columns until a blank or ``#``; under a ``Q(n)`` header the first column is discharge.
    curve = value["measured"]
    at = int(curve["at_boundary"])
    rows = [(float(q), float(z)) for q, z in curve["rows"]]
    lines = [f"#{curve['note']}", f"Q({at}) Z({at})", "m3/s m",
             *(f"{q:.6f} {z:.4f}" for q, z in rows)]
    # ``bord.f`` reads the curve at every prescribed-depth boundary whose entry is 1.
    return ({"STAGE_DISCHARGE_CURVES": [
                 1 if n == at else 0
                 for n in range(1, int(curve["of_boundaries"]) + 1)],
             "STAGE_DISCHARGE_CURVES_FILE": RATING_FILENAME},
            {RATING_FILENAME: "\n".join(lines) + "\n"})


# The interval a measured rainfall record is accumulated over: gross-rain products publish hourly totals, and a block file is read as the total over the block.
_STORM_INTERVAL_S = 3600.0


def Storm(*, mm_per_day: Any, hours: Any,  # noqa: N802
          series: Any = None, record: Any = None, tracers: Any = 0,
          fortran: Any = None) -> Mapping[str, Any]:
    """The storm over the domain: a measured record, or a constant design rate.

    A stated hourly gross-mm ``series`` wins, else ``record``; with neither, the constant rate over ``hours`` (RAIN OR EVAPORATION IN MM PER DAY's unit).
    """
    return MappingProxyType({"mm_per_day": mm_per_day, "hours": hours,
                             "settled": "settled", "series": series,
                             "record": record, "tracers": tracers,
                             "fortran": fortran})


def _storm(value: Mapping[str, Any]) -> tuple[Mapping[str, Any],
                                              Mapping[str, Any]]:
    """The storm -> either the block file and its reader, or the rate keywords.

    A record is ``[t_end_s, gross_mm]`` blocks with a dry tail past the last instant so the recession limb can fall; a design rate is the engine's constant branch.
    """
    series = value.get("series")
    if series is None:
        record = value.get("record")
        series = (record.get("precip_mm") if isinstance(record, Mapping)
                  else getattr(record, "precip_mm", None))
    if series is None:
        rate = value["mm_per_day"]
        if rate is None:
            return ({}, {})
        return ({"RAIN_OR_EVAPORATION": True,
                 "RAIN_OR_EVAPORATION_IN_MM_PER_DAY": float(rate),
                 **_rain_tracers(value["tracers"]),
                 # A window is stated only when it closes inside the run; a storm outlasting the horizon never stops.
                 **({} if value["hours"] is None
                    or float(value["hours"]) * _STORM_INTERVAL_S
                    >= float(value["settled"]["until_s"])
                    else {"DURATION_OF_RAIN_OR_EVAPORATION_IN_HOURS":
                          float(value["hours"])})}, {})
    millimetres = [max(0.0, float(v)) for v in series]
    if len(millimetres) < 2:
        raise ValueError(f"a measured storm carries {len(millimetres)} interval(s); "
                         "a hyetograph needs at least two. Widen the window, or "
                         "state a design rate.")
    blocks = [(float((i + 1) * _STORM_INTERVAL_S), millimetres[i])
              for i in range(len(millimetres))]
    keywords, files = _blocks_file(blocks, value["settled"]["until_s"], value["fortran"])
    return ({**keywords, **_rain_tracers(value["tracers"])}, files)


def _blocks_file(blocks: Any, until_s: Any,
                 fortran: Any) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """Gross rain per interval -> the block file and the routine that reads it.

    Blocks are ``[t_end_s, gross_mm]``; the tail past the last instant is dry.
    """
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
    # RAIN_OR_EVAPORATION gates the rain source term (prosou.f): without it a deck naming the block file
    # and routine states a storm the engine never applies. The rate keyword stays unstated (the routine
    # reads every interval off the file) and the window keyword is unread on this branch.
    return ({
             "RAIN_OR_EVAPORATION": True,
             "FORMATTED_DATA_FILE_1": HYETOGRAPH_FILENAME,
             # Quoted by the writer: a value opening on '/' is a DAMOCLES comment, erasing the keyword and swallowing the next line.
             "FORTRAN_FILE": str(fortran)},
            {HYETOGRAPH_FILENAME: "\n".join(lines) + "\n"})


def _rain_tracers(tracers: Any) -> Mapping[str, Any]:
    """The rainwater concentrations DAMOCLES demands, one per tracer, or nothing.

    An empty list is a keyword with nothing after it, which DAMOCLES reads as the next line.
    """
    count = int(tracers or 0)
    return {} if not count else {
        "VALUES_OF_TRACERS_IN_THE_RAIN": [0.0] * count}


T2D = Module("telemac2d")
T2D.MODULE_OUTPUT = MODULE_OUTPUT
# The hydrodynamic clock is one keyword: the window itself.
T2D.CLOCK = ("DURATION",)
T2D.LISTING = LISTING
T2D.PRINTOUTS = "VARIABLES_FOR_GRAPHIC_PRINTOUTS"
T2D.CADENCE = "GRAPHIC_PRINTOUT_PERIOD"
T2D.TRACER = "T"
T2D.ARMS = MappingProxyType({
    "SPEED_AND_DIRECTION_OF_WIND": "WIND",
    "RAIN_OR_EVAPORATION_IN_MM_PER_DAY": "RAIN_OR_EVAPORATION"})
T2D.composites(reads={
    "sources": ("window_s", "settled", "q", "tracers"),
    "wind": ("speed_mps", "from_deg", "drag"), "atmosphere": ATMOSPHERE_READS,
    "oil": ("named", "at", "release_step"),
    "boundaries": ("measured", "tracers"),
    "breach": ("line",),
    "runoff": ("node_xy", "cn2", "initial_abstraction"),
    "friction": ("manning_per_node",),
    "infiltration": ("mesh", "landcover", "uniform_cn", "steep_slope_correction",
                     "initial_abstraction"),
    "rating": ("measured",),
    "storm": ("mm_per_day", "hours", "settled", "series", "record", "tracers"),
    "tracer_names": ("named_by",)},
               sources=_sources, wind=_wind, breach=_breach, atmosphere=expand_atmosphere,
               oil=_oil, coupling=couples(water_column=False),
               boundaries=_boundaries, runoff=_runoff, friction=_friction,
               infiltration=_infiltration,
               rating=_rating, storm=_storm, tracer_names=_tracer_names)
T2D.reads(**PRIMITIVES, drogues=read_drogues)
# A settled release point is where the one source enters, in the mesh's metres; a measured sample is what the one declared tracer opens at.
T2D.FILLED_BY = MappingProxyType({
    "source": {"ABSCISSAE_OF_SOURCES": lambda placed: [placed["at"][0]],
               "ORDINATES_OF_SOURCES": lambda placed: [placed["at"][1]]},
    "observe": {"INITIAL_VALUES_OF_TRACERS": lambda sample: [sample.value]}})
