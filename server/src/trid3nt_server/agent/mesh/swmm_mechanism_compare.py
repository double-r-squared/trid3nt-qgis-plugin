"""Mechanism-comparison engine core for the SWMM knob-comparison templates.

Where ``swmm_deck_runner`` fetches ONE cited published ``.inp`` and ``run_swmm``
synthesizes a quasi-2D DEM mesh, this core authors SMALL SYNTHETIC decks that
isolate a single SWMM mechanism and vary ONE knob across a handful of variants,
solving each through the SAME headless ``swmm5_run`` + continuity honesty gate the
deck runner uses (reused verbatim from ``swmm_deck_runner``). The product is an
OVERLAY chart that visually demonstrates the knob (method A vs B vs C in one
figure) plus typed per-variant scalars.

Honesty (loud): the decks are AUTHORED mechanism stubs (a single subcatchment, a
wet-well, a pond-outlet), NOT a user AOI - their coordinates are schematic, so the
comparison emits CHARTS + typed scalars, never a georeferenced map. Every number
comes from a real parsed solver output (invariant 1). The synthetic basis is
labeled ``SyntheticInput(basis="default_demo")`` by the composer; each variant
realizes a mechanism proven by the cited EPA / openswmm published examples.

Deck syntax is the version-verified form for the installed SWMM (swmm-toolkit
0.17.0): a ``[REPORT] NODES/LINKS/SUBCATCHMENTS ALL`` block is REQUIRED for the
binary ``.out`` to carry the series the chart reads; weir ``[XSECTIONS]`` need a
positive width; LID types use the short codes (RB / RD / VS / GR); a curb-length
buildup normalizer needs a positive ``CurbLen`` on the subcatchment row; flow
diversion under DYNWAVE is a raised-invert relief pipe (a ``[DIVIDERS]`` node is
inert under dynamic-wave routing).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from trid3nt_server.agent.mesh.swmm_deck_runner import (
    DeckSolveResult,
    SWMMDeckError,
    link_flow_series,
    node_depth_series,
    solve_deck_text,
    subcatchment_runoff_series,
)

logger = logging.getLogger("trid3nt_server.agent.mesh.swmm_mechanism_compare")

__all__ = [
    "Variant",
    "ComparisonBuild",
    "SolvedVariant",
    "subcatchment_pollutant_series",
    "solve_variants",
    "build_subcatchment_runoff",
    "build_node_hydraulics",
    "build_pump_control",
    "build_lid_performance",
    "build_wq_buildup_washoff",
    "SUBCATCHMENT_COMPARE_MODES",
    "NODE_HYDRAULICS_SCENARIOS",
    "LID_TYPES",
    "WQ_COMPARE_MODES",
]


# --------------------------------------------------------------------------- #
# Series carriers.
# --------------------------------------------------------------------------- #
#: One chart series: (legend_label, kind, object_name). kind in
#: {"node","link","subcatchment","pollutant"}; pollutant object is "<sub>|<pol>".
ChartSeries = tuple[str, str, str]


@dataclass(frozen=True)
class Variant:
    """One knob-value: the label + its authored ``.inp`` deck + charted series."""

    label: str
    inp_text: str
    chart: tuple[ChartSeries, ...]
    note: str = ""


@dataclass(frozen=True)
class ComparisonBuild:
    """A complete comparison the composer solves + charts.

    ``extra_fn`` is an optional per-variant hook (label, solve result, series
    map) -> a dict of family-specific scalars folded onto the variant's ``extra``.
    ``basis_*`` fields provenance the synthetic deck + the published mechanism the
    variants realize. ``series_is_flow`` gates the trapezoidal volume the composer
    reports as each variant's ``total_value`` (meaningful only for a flow series).
    """

    comparison_kind: str
    knob_name: str
    knob_values: tuple[str, ...]
    variants: tuple[Variant, ...]
    chart_title: str
    chart_caption: str
    x_title: str
    y_title: str
    flow_units: str
    series_units: str
    mass_balance_tol_pct: float
    demonstration_note: str
    basis_param: str
    basis_note: str
    basis_source: str
    headline_extra: dict = field(default_factory=dict)
    series_is_flow: bool = False
    extra_fn: Callable[[str, DeckSolveResult, dict], dict] | None = None


@dataclass
class SolvedVariant:
    """A solved variant: its result + read series + generic scalars."""

    variant: Variant
    result: DeckSolveResult
    series: dict[str, list[tuple[float, float]]]
    primary_peak: float
    primary_peak_min: float
    total_value: float
    extra: dict


# --------------------------------------------------------------------------- #
# Series readers (extend the deck-runner readers with a pollutant reader).
# --------------------------------------------------------------------------- #
def subcatchment_pollutant_series(
    out_path: str, sub: str, pollutant: str
) -> list[tuple[float, float]]:
    """Pollutant-concentration-vs-minutes series for a subcatchment (pollutograph)."""
    from swmm_api import SwmmOutput

    from trid3nt_server.agent.mesh.swmm_deck_runner import _minutes_series

    with SwmmOutput(out_path) as out:
        try:
            ser = out.get_part("subcatchment", sub, pollutant)
        except Exception as exc:  # noqa: BLE001
            logger.debug("subcatchment_pollutant_series(%s,%s): %s", sub, pollutant, exc)
            return []
    return _minutes_series(ser)


def _read_series(out_path: str, kind: str, obj: str) -> list[tuple[float, float]]:
    if kind == "node":
        return node_depth_series(out_path, obj)
    if kind == "link":
        return link_flow_series(out_path, obj)
    if kind == "subcatchment":
        return subcatchment_runoff_series(out_path, obj)
    if kind == "pollutant":
        sub, pol = obj.split("|", 1)
        return subcatchment_pollutant_series(out_path, sub, pol)
    return []


def _trapz_volume(series: list[tuple[float, float]]) -> float:
    """Trapezoidal integral of a flow series (flow-units * minutes -> * 60 = volume)."""
    if len(series) < 2:
        return 0.0
    vol = 0.0
    for (t0, q0), (t1, q1) in zip(series, series[1:]):
        vol += 0.5 * (q0 + q1) * (t1 - t0) * 60.0
    return vol


# --------------------------------------------------------------------------- #
# Solve loop (reuses solve_deck_text + the continuity honesty gate).
# --------------------------------------------------------------------------- #
def solve_variants(build: ComparisonBuild) -> list[SolvedVariant]:
    """Solve every variant, read its charted series, compute generic scalars.

    Raises the typed ``SWMMDeckError`` from the shared solver (a continuity breach
    on ANY variant fails the whole comparison loudly - a silently-wrong variant is
    never charted).
    """
    solved: list[SolvedVariant] = []
    for i, v in enumerate(build.variants):
        res = solve_deck_text(
            v.inp_text,
            mass_balance_tolerance_pct=build.mass_balance_tol_pct,
            stem=f"{build.comparison_kind}-{i}",
        )
        series: dict[str, list[tuple[float, float]]] = {}
        for slabel, kind, obj in v.chart:
            series[slabel] = _read_series(res.out_path, kind, obj)
        prim = series.get(v.chart[0][0], []) if v.chart else []
        pk = max((val for _, val in prim), default=0.0)
        pkmin = next((t for t, val in prim if val == pk), 0.0)
        total = _trapz_volume(prim) if build.series_is_flow else 0.0
        extra = build.extra_fn(v.label, res, series) if build.extra_fn else {}
        solved.append(SolvedVariant(v, res, series, pk, pkmin, total, extra))
        logger.info(
            "solve_variants %s[%s] cont=%+.3f%% peak=%.4g@%.1fmin total=%.4g",
            build.comparison_kind, v.label, res.continuity_error_pct, pk, pkmin, total,
        )
    return solved


# --------------------------------------------------------------------------- #
# Shared deck scaffolding (the version-verified syntax).
# --------------------------------------------------------------------------- #
def _options(
    *,
    infiltration: str = "HORTON",
    routing: str = "DYNWAVE",
    end_time: str = "06:00:00",
    report_step: str = "00:02:00",
    routing_step: str = "00:00:15",
    allow_ponding: str = "NO",
    flow_units: str = "CFS",
) -> str:
    return (
        "[TITLE]\nsynthetic mechanism comparison (schematic - not georeferenced)\n\n"
        "[OPTIONS]\n"
        f"FLOW_UNITS           {flow_units}\n"
        f"INFILTRATION         {infiltration}\n"
        f"FLOW_ROUTING         {routing}\n"
        "START_DATE           01/01/2020\n"
        "START_TIME           00:00:00\n"
        "END_DATE             01/01/2020\n"
        f"END_TIME             {end_time}\n"
        f"REPORT_STEP          {report_step}\n"
        "WET_STEP             00:01:00\n"
        "DRY_STEP             01:00:00\n"
        f"ROUTING_STEP         {routing_step}\n"
        f"ALLOW_PONDING        {allow_ponding}\n"
        "LINK_OFFSETS         DEPTH\n"
        "MIN_SLOPE            0\n\n"
        "[REPORT]\n"
        "INPUT NO\n"
        "SUBCATCHMENTS ALL\n"
        "NODES ALL\n"
        "LINKS ALL\n\n"
    )


#: The shared design storm - a single-peak ~1 h hyetograph (intensity in/hr) that
#: drives the rainfall-forced families. One storm for every variant so the KNOB is
#: the only difference (a demo design storm, labeled default_demo by the composer).
_DESIGN_STORM = (
    "[RAINGAGES]\n"
    "RG1  INTENSITY  0:05  1.0  TIMESERIES  TS1\n\n"
    "[TIMESERIES]\n"
    "TS1  0:00  0.0\n"
    "TS1  0:15  1.5\n"
    "TS1  0:30  3.5\n"
    "TS1  0:45  1.5\n"
    "TS1  1:00  0.5\n"
    "TS1  2:00  0.0\n\n"
)

#: A larger storm variant (for the storm-size sensitivity in the LID family).
_DESIGN_STORM_LARGE = _DESIGN_STORM.replace(
    "TS1  0:30  3.5", "TS1  0:30  7.0"
).replace("TS1  0:15  1.5", "TS1  0:15  3.0").replace(
    "TS1  0:45  1.5", "TS1  0:45  3.0"
)

#: A longer, more intense ~2.5 h storm whose peak intensity exceeds pervious
#: infiltration capacity - so on a pervious catchment the infiltration-EXCESS
#: runoff (and thus the loss method) drives the whole response.
_DESIGN_STORM_INTENSE = (
    "[RAINGAGES]\n"
    "RG1  INTENSITY  0:05  1.0  TIMESERIES  TS1\n\n"
    "[TIMESERIES]\n"
    "TS1  0:00  0.0\n"
    "TS1  0:20  1.0\n"
    "TS1  0:40  2.5\n"
    "TS1  1:00  3.0\n"
    "TS1  1:20  2.5\n"
    "TS1  1:40  1.5\n"
    "TS1  2:00  0.6\n"
    "TS1  2:30  0.0\n\n"
)


# --------------------------------------------------------------------------- #
# Family 1: single-subcatchment runoff comparison (infiltration method / dev).
# --------------------------------------------------------------------------- #
SUBCATCHMENT_COMPARE_MODES = ("infiltration_method", "development_intensity")

# Representative (NOT cross-calibrated) default parameters for each method - the
# spread across methods reflects the practitioner's real method+parameter choice.
_INFIL_LINES = {
    "HORTON": "[INFILTRATION]\nS1  1.5  0.1  4.0  7  0\n",        # f0,fmin,decay,dry,max
    "GREEN_AMPT": "[INFILTRATION]\nS1  4.0  0.15  0.35\n",        # suction,Ks,IMD
    "CURVE_NUMBER": "[INFILTRATION]\nS1  88  0.1  7\n",           # CN, -, dry
}


def _subcatchment_deck(
    *, imperv: float, infiltration_opt: str, infil_line: str, storm: str = _DESIGN_STORM
) -> str:
    return (
        _options(infiltration=infiltration_opt, end_time="08:00:00")
        + storm
        + f"[SUBCATCHMENTS]\nS1  RG1  J1  5  {imperv:g}  500  0.5  0\n\n"
        + "[SUBAREAS]\nS1  0.015  0.24  0.05  0.05  25  OUTLET\n\n"
        + infil_line
        + "\n[JUNCTIONS]\nJ1  0  4  0  0  0\n\n"
        + "[OUTFALLS]\nO1  0  FREE  NO\n\n"
        + "[CONDUITS]\nC1  J1  O1  400  0.01  0  0  0\n\n"
        + "[XSECTIONS]\nC1  CIRCULAR  2.0  0  0  0  1\n"
    )


def build_subcatchment_runoff(compare: str) -> ComparisonBuild:
    """Rows 1 (infiltration method) + 2 (pre/post-development) - runoff on one
    subcatchment + storm, varying either the infiltration method or the
    imperviousness. Primary series: subcatchment runoff (a hydrograph overlay)."""
    storm = _DESIGN_STORM
    if compare == "infiltration_method":
        # A pervious (undeveloped) catchment isolates the loss method: with 0%
        # impervious, ALL runoff is infiltration-excess, so the method fully
        # controls the hydrograph. Intense storm exceeds infiltration capacity.
        storm = _DESIGN_STORM_INTENSE
        specs = [
            ("Horton", "HORTON", _INFIL_LINES["HORTON"], 0.0),
            ("Green-Ampt", "GREEN_AMPT", _INFIL_LINES["GREEN_AMPT"], 0.0),
            ("Curve Number", "CURVE_NUMBER", _INFIL_LINES["CURVE_NUMBER"], 0.0),
        ]
        knob_name = "infiltration_method"
        title = "Runoff hydrograph by infiltration method (same pervious subcatchment + storm)"
        caption = (
            "Horton vs Green-Ampt vs Curve Number on ONE pervious (undeveloped) 5-acre "
            "subcatchment under the same intense storm - the loss method is the only "
            "knob. Each method uses representative (not cross-calibrated) default "
            "parameters, so the spread reflects the practitioner's method+parameter "
            "choice. EPA SWMM5 Reference Manual Vol.I (Hydrology), infiltration chapter."
        )
        source = "EPA SWMM5 Reference Manual Vol.I (Hydrology), infiltration chapter"
    elif compare == "development_intensity":
        specs = [
            ("pre-development (5% imperv, pasture)", "CURVE_NUMBER",
             "[INFILTRATION]\nS1  74  0.5  7\n", 5.0),
            ("post-development (75% imperv)", "CURVE_NUMBER",
             "[INFILTRATION]\nS1  85  0.5  7\n", 75.0),
        ]
        knob_name = "development_intensity"
        title = "Runoff hydrograph pre- vs post-development (imperviousness knob)"
        caption = (
            "The SAME parcel as pasture (5% impervious) vs developed (75% impervious) "
            "under one design storm - post-development peaks higher and arrives "
            "sooner. EPA SWMM Applications Manual, Example 1 (post-development runoff)."
        )
        source = "EPA SWMM Applications Manual EPA/600/R-09/000, Example 1"
    else:  # pragma: no cover - guarded by the template Literal
        raise SWMMDeckError("SWMM_DECK_PARSE_FAILED", message=f"unknown compare mode: {compare}")

    variants = tuple(
        Variant(
            label=lbl,
            inp_text=_subcatchment_deck(
                imperv=imp, infiltration_opt=opt, infil_line=line, storm=storm),
            chart=(("runoff", "subcatchment", "S1"),),
        )
        for lbl, opt, line, imp in specs
    )
    return ComparisonBuild(
        comparison_kind="subcatchment_runoff",
        knob_name=knob_name,
        knob_values=tuple(s[0] for s in specs),
        variants=variants,
        chart_title=title,
        chart_caption=caption,
        x_title="minutes from start",
        y_title="subcatchment runoff (CFS)",
        flow_units="CFS",
        series_units="CFS",
        mass_balance_tol_pct=10.0,
        demonstration_note=(
            "Synthetic single-subcatchment demonstration on a schematic deck (not a "
            "georeferenced site). The runoff hydrographs + peak/volume numbers are the "
            "product; the mechanism follows the cited EPA example."
        ),
        basis_param=knob_name,
        basis_note=f"synthetic demo subcatchment + design storm; mechanism per {source}",
        basis_source=source,
        series_is_flow=True,
    )


# --------------------------------------------------------------------------- #
# Family 2: node hydraulics (outlet family / diversion / surcharge+ponding).
# --------------------------------------------------------------------------- #
NODE_HYDRAULICS_SCENARIOS = ("outlet_family", "flow_diversion", "surcharge_ponding")

_STEADY_INFLOW = (
    "[TIMESERIES]\nINF  0:00  0\nINF  1:00  15\nINF  3:00  15\nINF  4:00  0\nINF  8:00  0\n"
)


def _pond_outlet_deck(outlet_body: str) -> str:
    return (
        _options(end_time="08:00:00", routing_step="00:00:10", allow_ponding="YES")
        + "[STORAGE]\nP1  0  6  0  FUNCTIONAL  1000  0  0\n\n"
        + "[OUTFALLS]\nO1  0  FREE  NO\n\n"
        + "[INFLOWS]\nP1  FLOW  INF\n\n"
        + outlet_body
        + "\n"
        + _STEADY_INFLOW
    )


_OUTLET_BODIES = {
    "transverse weir": (
        "[WEIRS]\nW1  P1  O1  TRANSVERSE  1.0  3.33  NO  0\n"
        "[XSECTIONS]\nW1  RECT_OPEN  2.0  3.0  0  0\n", "W1"),
    "V-notch weir": (
        "[WEIRS]\nW1  P1  O1  V-NOTCH  1.0  2.5  NO  0\n"
        "[XSECTIONS]\nW1  TRIANGULAR  2.0  3.0  0  0\n", "W1"),
    "circular orifice": (
        "[ORIFICES]\nR1  P1  O1  SIDE  1.0  0.65  NO  0\n"
        "[XSECTIONS]\nR1  CIRCULAR  1.0  0  0  0\n", "R1"),
    "rating-curve outlet": (
        "[OUTLETS]\nT1  P1  O1  1.0  TABULAR/DEPTH  RC1  NO\n"
        "[CURVES]\nRC1  Rating\nRC1  0  0\nRC1  0.5  3\nRC1  1.0  8\nRC1  2.0  20\n", "T1"),
}


def _diversion_deck() -> str:
    # DYNWAVE-correct diversion: a relief pipe with a RAISED invert (3 ft) that only
    # engages once the junction head rises above it; the main pipe carries low flow.
    return (
        _options(end_time="08:00:00", routing_step="00:00:10", allow_ponding="YES")
        + "[JUNCTIONS]\nJ1  2  8  0  0  0\n\n"
        + "[OUTFALLS]\nOMAIN  0  FREE  NO\nORELIEF  0  FREE  NO\n\n"
        + "[INFLOWS]\nJ1  FLOW  INF\n\n"
        + "[CONDUITS]\n"
        + "C_MAIN  J1  OMAIN  200  0.01  0  0  0\n"
        + "C_RELIEF  J1  ORELIEF  200  0.01  3.0  0  0\n\n"
        + "[XSECTIONS]\n"
        + "C_MAIN  CIRCULAR  1.0  0  0  0  1\n"
        + "C_RELIEF  CIRCULAR  1.5  0  0  0  1\n"
        + _STEADY_INFLOW
    )


def _surcharge_deck(allow_ponding: str) -> str:
    # Undersized 0.5 ft pipe from a subcatchment-fed junction with a ponded area.
    return (
        _options(end_time="06:00:00", routing_step="00:00:10", allow_ponding=allow_ponding)
        + _DESIGN_STORM
        + "[SUBCATCHMENTS]\nS1  RG1  J1  10  85  500  0.5  0\n\n"
        + "[SUBAREAS]\nS1  0.015  0.1  0.05  0.05  25  OUTLET\n\n"
        + "[INFILTRATION]\nS1  3.0  0.5  4.0  7  0\n\n"
        + "[JUNCTIONS]\nJ1  0  6  0  0  2000\n\n"
        + "[OUTFALLS]\nO1  0  FREE  NO\n\n"
        + "[CONDUITS]\nC1  J1  O1  400  0.02  0  0  0\n\n"
        + "[XSECTIONS]\nC1  CIRCULAR  0.5  0  0  0  1\n"
    )


def build_node_hydraulics(scenario: str) -> ComparisonBuild:
    """Rows 3 (surcharge/ponding), 4 (outlet structure family), 5 (flow diversion)
    - node-hydraulics comparisons on schematic pond/junction stubs."""
    if scenario == "outlet_family":
        variants = tuple(
            Variant(label=lbl, inp_text=_pond_outlet_deck(body),
                    chart=((f"{lbl} discharge", "link", ln), ("pond stage", "node", "P1")))
            for lbl, (body, ln) in _OUTLET_BODIES.items()
        )
        return ComparisonBuild(
            comparison_kind="outlet_structure",
            knob_name="structure_type",
            knob_values=tuple(_OUTLET_BODIES.keys()),
            variants=variants,
            chart_title="Outlet discharge by structure type (same pond + inflow)",
            chart_caption=(
                "Transverse weir vs V-notch weir vs circular orifice vs rating-curve "
                "outlet draining the SAME storage node under the same steady inflow - "
                "the structure is the only knob. openswmm.org weirs/orifices references."
            ),
            x_title="minutes from start", y_title="outlet discharge (CFS)",
            flow_units="CFS", series_units="CFS", mass_balance_tol_pct=10.0,
            demonstration_note=(
                "Synthetic pond-outlet demonstration (schematic, not georeferenced). "
                "The overlaid discharge curves + peak numbers are the product."
            ),
            basis_param="structure_type",
            basis_note="synthetic storage node + steady inflow; native SWMM link types",
            basis_source="openswmm.org (weirs / orifices in SWMM5 references)",
            series_is_flow=True,
        )
    if scenario == "flow_diversion":
        v = Variant(
            label="main vs relief split",
            inp_text=_diversion_deck(),
            chart=(("main pipe", "link", "C_MAIN"), ("relief pipe", "link", "C_RELIEF")),
        )
        return ComparisonBuild(
            comparison_kind="flow_diversion",
            knob_name="diversion_split",
            knob_values=("main vs relief split",),
            variants=(v,),
            chart_title="Flow diversion: main vs relief split as inflow rises",
            chart_caption=(
                "A junction split between a main pipe and a raised-invert relief pipe. "
                "As inflow rises the relief pipe engages once head passes its 3 ft "
                "invert - the two link hydrographs show how flow splits. DYNWAVE-correct "
                "(a [DIVIDERS] node is inert under dynamic-wave routing)."
            ),
            x_title="minutes from start", y_title="pipe flow (CFS)",
            flow_units="CFS", series_units="CFS", mass_balance_tol_pct=10.0,
            demonstration_note=(
                "Synthetic diversion-junction demonstration (schematic, not "
                "georeferenced). The main/relief hydrograph pair is the product."
            ),
            basis_param="diversion_split",
            basis_note="synthetic junction + raised-invert relief pipe (DYNWAVE diversion)",
            basis_source="SWMM control-rules / diversion-structure reference (Innovyze mirror)",
            series_is_flow=True,
        )
    if scenario == "surcharge_ponding":
        variants = (
            Variant(label="no ponding (spills lost)", inp_text=_surcharge_deck("NO"),
                    chart=(("junction depth", "node", "J1"),)),
            Variant(label="allow ponding (surface storage)", inp_text=_surcharge_deck("YES"),
                    chart=(("junction depth", "node", "J1"),)),
        )

        def _extra(label, res, series):  # noqa: ANN001
            return {"n_flooded_nodes": res.n_flooded_nodes,
                    "n_surcharged_conduits": res.n_surcharged_conduits}

        return ComparisonBuild(
            comparison_kind="surcharge_ponding",
            knob_name="allow_ponding",
            knob_values=("no ponding (spills lost)", "allow ponding (surface storage)"),
            variants=variants,
            chart_title="Node surcharge/ponding: undersized pipe, ponding OFF vs ON",
            chart_caption=(
                "An undersized 0.5 ft pipe backs up under the design storm. With "
                "ALLOW_PONDING off the surcharge spills and is lost; with ponding on it "
                "is held as surface storage at the node - node depth overlaid. EPA SWMM5 "
                "Reference Manual Vol.II (Hydraulics), node flooding / Allow Ponding."
            ),
            x_title="minutes from start", y_title="node depth (ft)",
            flow_units="CFS", series_units="ft", mass_balance_tol_pct=12.0,
            demonstration_note=(
                "Synthetic undersized-pipe demonstration (schematic, not georeferenced). "
                "The node-depth overlay + flooded/surcharged tallies are the product."
            ),
            basis_param="allow_ponding",
            basis_note="synthetic undersized conduit + ponded junction",
            basis_source="EPA SWMM5 Reference Manual Vol.II (Hydraulics), Allow Ponding",
            series_is_flow=False,
            extra_fn=_extra,
        )
    raise SWMMDeckError(  # pragma: no cover
        "SWMM_DECK_PARSE_FAILED", message=f"unknown node-hydraulics scenario: {scenario}")


# --------------------------------------------------------------------------- #
# Family 3: wet-well pump + control-scheme comparison (Rows 6/7/8).
# --------------------------------------------------------------------------- #
# Pump2 curve = flow vs wet-well DEPTH (the correct type for a lift station; a
# Pump3 head-difference curve yields ~0 flow against a free outfall).
_PUMP_CURVE = "[CURVES]\nPC1  Pump2\nPC1  0  0\nPC1  3  6\nPC1  6  12\nPC1  10  18\n\n"

_PUMP_INFLOW = (
    "[TIMESERIES]\nINF  0:00  1\nINF  0:30  8\nINF  2:00  8\nINF  3:00  14\n"
    "INF  4:00  6\nINF  8:00  6\n"
)

# Hysteresis deadbands (separate ON / OFF rules, no ELSE) so pumps cycle cleanly
# rather than chatter. The three schemes differ in HOW the three pumps are staged.
_CONTROL_SCHEMES = {
    "fixed setpoint (all pumps together)": """[CONTROLS]
RULE ON1
IF NODE WW DEPTH > 4.0
THEN PUMP PUMP1 STATUS = ON
RULE OFF1
IF NODE WW DEPTH < 2.0
THEN PUMP PUMP1 STATUS = OFF
RULE ON2
IF NODE WW DEPTH > 4.0
THEN PUMP PUMP2 STATUS = ON
RULE OFF2
IF NODE WW DEPTH < 2.0
THEN PUMP PUMP2 STATUS = OFF
RULE ON3
IF NODE WW DEPTH > 4.0
THEN PUMP PUMP3 STATUS = ON
RULE OFF3
IF NODE WW DEPTH < 2.0
THEN PUMP PUMP3 STATUS = OFF
""",
    "duty/standby depth-staged": """[CONTROLS]
RULE ON1
IF NODE WW DEPTH > 2.5
THEN PUMP PUMP1 STATUS = ON
RULE OFF1
IF NODE WW DEPTH < 1.0
THEN PUMP PUMP1 STATUS = OFF
RULE ON2
IF NODE WW DEPTH > 5.0
THEN PUMP PUMP2 STATUS = ON
RULE OFF2
IF NODE WW DEPTH < 3.0
THEN PUMP PUMP2 STATUS = OFF
RULE ON3
IF NODE WW DEPTH > 7.5
THEN PUMP PUMP3 STATUS = ON
RULE OFF3
IF NODE WW DEPTH < 5.5
THEN PUMP PUMP3 STATUS = OFF
""",
    "multi-condition (AND rule)": """[CONTROLS]
RULE ON1
IF NODE WW DEPTH > 2.5
THEN PUMP PUMP1 STATUS = ON
RULE OFF1
IF NODE WW DEPTH < 1.0
THEN PUMP PUMP1 STATUS = OFF
RULE ON2
IF NODE WW DEPTH > 4.0
AND LINK PUMP1 FLOW > 5.0
THEN PUMP PUMP2 STATUS = ON
RULE OFF2
IF NODE WW DEPTH < 2.5
THEN PUMP PUMP2 STATUS = OFF
""",
}


def _pump_deck(controls: str) -> str:
    return (
        _options(end_time="08:00:00", routing_step="00:00:10", allow_ponding="YES")
        + "[STORAGE]\nWW  0  10  0  FUNCTIONAL  1500  0  0\n\n"
        + "[OUTFALLS]\nO1  0  FREE  NO\nO2  0  FREE  NO\nO3  0  FREE  NO\n\n"
        + "[INFLOWS]\nWW  FLOW  INF\n\n"
        + "[PUMPS]\n"
        + "PUMP1  WW  O1  PC1  OFF  0  0\n"
        + "PUMP2  WW  O2  PC1  OFF  0  0\n"
        + "PUMP3  WW  O3  PC1  OFF  0  0\n\n"
        + _PUMP_CURVE
        + controls
        + "\n"
        + _PUMP_INFLOW
    )


def _pump_cycles(series: list[tuple[float, float]]) -> int:
    """Count OFF->ON transitions in a pump flow series (cycling frequency)."""
    cycles = 0
    prev_on = False
    for _, q in series:
        on = q > 1e-6
        if on and not prev_on:
            cycles += 1
        prev_on = on
    return cycles


def build_pump_control() -> ComparisonBuild:
    """Rows 6/7/8 - a three-pump wet well under one inflow, comparing control
    schemes (fixed setpoint vs depth-staged duty/standby vs multi-condition AND
    rule). Primary series: wet-well depth; run-time + cycling in ``extra``."""
    variants = tuple(
        Variant(
            label=lbl, inp_text=_pump_deck(ctrl),
            chart=(("wet-well depth", "node", "WW"),
                   ("PUMP1 flow", "link", "PUMP1"),
                   ("PUMP2 flow", "link", "PUMP2")),
        )
        for lbl, ctrl in _CONTROL_SCHEMES.items()
    )

    def _extra(label, res, series):  # noqa: ANN001
        p1 = series.get("PUMP1 flow", [])
        p2 = series.get("PUMP2 flow", [])
        n = max(len(p1), 1)
        run_frac = sum(1 for _, q in p1 if q > 1e-6) / n
        return {
            "pump1_cycles": _pump_cycles(p1),
            "pump2_cycles": _pump_cycles(p2),
            "pump1_run_fraction": round(run_frac, 3),
        }

    return ComparisonBuild(
        comparison_kind="pump_control",
        knob_name="control_scheme",
        knob_values=tuple(_CONTROL_SCHEMES.keys()),
        variants=variants,
        chart_title="Wet-well depth by pump control scheme (same inflow, 3 pumps)",
        chart_caption=(
            "One wet well, three identical pumps, same inflow - only the CONTROLS rule "
            "changes. Fixed setpoint (all pumps together) vs depth-staged duty/standby "
            "vs a multi-condition AND rule; wet-well depth overlaid, run-fraction + "
            "cycle count per scheme in the numbers. openswmm.org Topic 10083 (3-pump "
            "control rules)."
        ),
        x_title="minutes from start", y_title="wet-well depth (ft)",
        flow_units="CFS", series_units="ft", mass_balance_tol_pct=12.0,
        demonstration_note=(
            "Synthetic lift-station demonstration (schematic, not georeferenced). The "
            "wet-well depth overlay + per-scheme cycling/run-fraction are the product."
        ),
        basis_param="control_scheme",
        basis_note="synthetic wet well + 3 pump curves; CONTROLS rule per scheme",
        basis_source="openswmm.org Topic 10083 (VSP control rules for 3 pumps)",
        series_is_flow=False,
        extra_fn=_extra,
    )


# --------------------------------------------------------------------------- #
# Family 4: LID performance comparison (Rows 10/11/12).
# --------------------------------------------------------------------------- #
LID_TYPES = ("green_roof", "vegetative_swale", "rainbarrel_vs_disconnect")

# LID control blocks (short type codes; version-verified layer stacks).
_LID_CONTROLS = {
    "green_roof": (
        "[LID_CONTROLS]\nGR1  GR\n"
        "GR1  SURFACE  6  0.0  0.1  1.0  5\n"
        "GR1  SOIL  12  0.5  0.2  0.1  10  3.5  20\n"
        "GR1  DRAINMAT  3  0.5  0.1\n", "GR1"),
    "rain_barrel": (
        "[LID_CONTROLS]\nRB1  RB\n"
        "RB1  STORAGE  36  1.0  0  0\n"
        "RB1  DRAIN  1.0  0.5  0  6  0  0\n", "RB1"),
    "rooftop_disconnect": (
        "[LID_CONTROLS]\nRD1  RD\n"
        "RD1  SURFACE  0  0  0.1  1.0  5\n"
        "RD1  DRAIN  1.0  0.5  0  0\n", "RD1"),
    "vegetative_swale": (
        "[LID_CONTROLS]\nVS1  VS\n"
        "VS1  SURFACE  12  0.0  0.24  2.0  5\n", "VS1"),
}
# LID_USAGE row: Subcatch LID Number Area Width InitSat FromImp ToPerv. FromImp
# (7th field) routes that % of the subcatchment's IMPERVIOUS runoff onto the LID -
# the lever that makes storage/drain LIDs (barrel, disconnect) treat roof runoff.
_LID_USAGE = {
    "green_roof": "S1  GR1  1  18000  0  0  0  0",
    "rain_barrel": "S1  RB1  25  90  0  0  45  0",
    "rooftop_disconnect": "S1  RD1  1  12000  60  0  70  0",
    "vegetative_swale": "S1  VS1  1  12000  60  0  70  0",
}


def _lid_deck(*, lid_key: str | None, storm: str) -> str:
    lid_sections = ""
    if lid_key is not None:
        ctrl, _ = _LID_CONTROLS[lid_key]
        lid_sections = ctrl + "\n[LID_USAGE]\n" + _LID_USAGE[lid_key] + "\n\n"
    return (
        _options(end_time="06:00:00", routing_step="00:00:15")
        + storm
        + "[SUBCATCHMENTS]\nS1  RG1  J1  2  60  400  0.5  0\n\n"
        + "[SUBAREAS]\nS1  0.015  0.24  0.05  0.05  25  OUTLET\n\n"
        + "[INFILTRATION]\nS1  3.0  0.5  4.0  7  0\n\n"
        + lid_sections
        + "[JUNCTIONS]\nJ1  0  4  0  0  0\n\n"
        + "[OUTFALLS]\nO1  0  FREE  NO\n\n"
        + "[CONDUITS]\nC1  J1  O1  300  0.01  0  0  0\n\n"
        + "[XSECTIONS]\nC1  CIRCULAR  2.0  0  0  0  1\n"
    )


def build_lid_performance(lid_type: str) -> ComparisonBuild:
    """Rows 10 (green roof), 11 (rain barrel vs rooftop disconnect), 12 (veg swale)
    - runoff with vs without a LID control on one subcatchment + storm."""
    if lid_type == "rainbarrel_vs_disconnect":
        variants = (
            Variant("no LID (baseline)", _lid_deck(lid_key=None, storm=_DESIGN_STORM),
                    (("runoff", "subcatchment", "S1"),)),
            Variant("rain barrel", _lid_deck(lid_key="rain_barrel", storm=_DESIGN_STORM),
                    (("runoff", "subcatchment", "S1"),)),
            Variant("rooftop disconnection", _lid_deck(lid_key="rooftop_disconnect", storm=_DESIGN_STORM),
                    (("runoff", "subcatchment", "S1"),)),
        )
        title = "Rooftop runoff: baseline vs rain barrel vs rooftop disconnection"
        caption = (
            "A residential lot's roof runoff with no control vs a rain barrel "
            "(fixed-volume cistern) vs simple rooftop disconnection (drain to pervious "
            "yard) under one design storm - the two simplest LID types by layer count. "
            "openswmm.org LID underdrain / EPA SWMM5 LID layer table."
        )
        source = "openswmm.org LID references + EPA SWMM5 LID layer table"
        knob_vals = ("no LID (baseline)", "rain barrel", "rooftop disconnection")
    elif lid_type in ("green_roof", "vegetative_swale"):
        pretty = "green roof" if lid_type == "green_roof" else "vegetative swale"
        variants = (
            Variant("no LID (baseline)", _lid_deck(lid_key=None, storm=_DESIGN_STORM),
                    (("runoff", "subcatchment", "S1"),)),
            Variant(f"with {pretty}", _lid_deck(lid_key=lid_type, storm=_DESIGN_STORM),
                    (("runoff", "subcatchment", "S1"),)),
        )
        if lid_type == "green_roof":
            title = "Rooftop runoff: conventional roof vs green roof (detention)"
            caption = (
                "A subcatchment's roof runoff with conventional roofing vs a green roof "
                "(Surface + Soil + DrainMat) under one design storm - the green roof "
                "detains and slows runoff. openswmm.org Topic 15497 (simple green roof)."
            )
            source = "openswmm.org Topic 15497 (simple green roof example)"
        else:
            title = "Runoff: lined channel vs vegetative swale (conveyance LID)"
            caption = (
                "Runoff routed conventionally vs through a vegetated swale (a "
                "surface-layer conveyance LID) under one design storm - the swale slows "
                "and attenuates the peak. openswmm.org Topic 29954 (vegetative swales)."
            )
            source = "openswmm.org Topic 29954 (how vegetative swales work in SWMM)"
        knob_vals = ("no LID (baseline)", f"with {pretty}")
    else:  # pragma: no cover
        raise SWMMDeckError("SWMM_DECK_PARSE_FAILED", message=f"unknown lid_type: {lid_type}")

    def _extra(label, res, series):  # noqa: ANN001
        return {}

    return ComparisonBuild(
        comparison_kind="lid_performance",
        knob_name="lid_type",
        knob_values=knob_vals,
        variants=variants,
        chart_title=title,
        chart_caption=caption,
        x_title="minutes from start", y_title="subcatchment runoff (CFS)",
        flow_units="CFS", series_units="CFS", mass_balance_tol_pct=10.0,
        demonstration_note=(
            "Synthetic single-subcatchment LID demonstration (schematic, not "
            "georeferenced). The with/without runoff overlay + reduction numbers are "
            "the product."
        ),
        basis_param="lid_type",
        basis_note=f"synthetic subcatchment + LID control; mechanism per {source}",
        basis_source=source,
        series_is_flow=True,
        extra_fn=_extra,
    )


# --------------------------------------------------------------------------- #
# Family 5: WQ buildup normalization + washoff comparison (Row 9).
# --------------------------------------------------------------------------- #
WQ_COMPARE_MODES = ("normalization", "washoff")


def _wq_deck(*, norm: str, washoff: str, curb_len: float) -> str:
    wash = "TSS  EXP  0.1  1.2  0  0" if washoff == "EXP" else "TSS  EMC  100  0  0  0"
    # SUBCATCHMENTS 8th field CurbLen drives the CURB normalizer; AREA ignores it.
    return (
        _options(end_time="06:00:00", routing_step="00:00:15")
        + _DESIGN_STORM
        + f"[SUBCATCHMENTS]\nS1  RG1  J1  4  70  600  0.5  {curb_len:g}\n\n"  # 8th field = CurbLen
        + "[SUBAREAS]\nS1  0.015  0.24  0.05  0.05  25  OUTLET\n\n"
        + "[INFILTRATION]\nS1  3.0  0.5  4.0  7  0\n\n"
        + "[JUNCTIONS]\nJ1  0  4  0  0  0\n\n"
        + "[OUTFALLS]\nO1  0  FREE  NO\n\n"
        + "[CONDUITS]\nC1  J1  O1  400  0.01  0  0  0\n\n"
        + "[XSECTIONS]\nC1  CIRCULAR  2.0  0  0  0  1\n\n"
        + "[POLLUTANTS]\nTSS  MG/L  0  0  0  0  NO\n\n"
        + "[LANDUSES]\nURBAN\n\n"
        + "[COVERAGES]\nS1  URBAN  100\n\n"
        + f"[BUILDUP]\nURBAN  TSS  POW  50  1.0  0  {norm}\n\n"
        + f"[WASHOFF]\nURBAN  {wash}\n"
    )


def build_wq_buildup_washoff(compare: str) -> ComparisonBuild:
    """Row 9 - TSS pollutograph on one subcatchment + storm, varying either the
    buildup normalizer (AREA vs CURB) or the washoff method (EXP vs EMC)."""
    if compare == "normalization":
        variants = (
            Variant("area-normalized buildup", _wq_deck(norm="AREA", washoff="EXP", curb_len=10.0),
                    (("TSS", "pollutant", "S1|TSS"),)),
            Variant("curb-length-normalized buildup", _wq_deck(norm="CURB", washoff="EXP", curb_len=10.0),
                    (("TSS", "pollutant", "S1|TSS"),)),
        )
        knob_name = "buildup_normalization"
        title = "TSS pollutograph: area- vs curb-length-normalized buildup (EXP washoff)"
        caption = (
            "The SAME subcatchment + storm with pollutant buildup normalized per unit "
            "AREA vs per unit CURB LENGTH (200 ft curb) - the normalizer rescales the "
            "buildup mass and therefore the washoff concentration. EPA SWMM Applications "
            "Manual, Example 5 (runoff water quality)."
        )
        knob_vals = ("area-normalized buildup", "curb-length-normalized buildup")
    elif compare == "washoff":
        variants = (
            Variant("exponential washoff", _wq_deck(norm="AREA", washoff="EXP", curb_len=10.0),
                    (("TSS", "pollutant", "S1|TSS"),)),
            Variant("event-mean concentration", _wq_deck(norm="AREA", washoff="EMC", curb_len=10.0),
                    (("TSS", "pollutant", "S1|TSS"),)),
        )
        knob_name = "washoff_method"
        title = "TSS pollutograph: exponential washoff vs event-mean-concentration"
        caption = (
            "The SAME subcatchment + storm with EXP washoff (buildup-driven first "
            "flush - a rising/falling pollutograph) vs a flat event-mean concentration "
            "(EMC, constant 100 mg/L, no first flush). EPA SWMM Applications Manual, "
            "Example 5 (runoff water quality)."
        )
        knob_vals = ("exponential washoff", "event-mean concentration")
    else:  # pragma: no cover
        raise SWMMDeckError("SWMM_DECK_PARSE_FAILED", message=f"unknown wq compare mode: {compare}")

    def _extra(label, res, series):  # noqa: ANN001
        pg = series.get("TSS", [])
        return {"peak_concentration_mgL": round(max((v for _, v in pg), default=0.0), 3)}

    return ComparisonBuild(
        comparison_kind="wq_buildup_washoff",
        knob_name=knob_name,
        knob_values=knob_vals,
        variants=variants,
        chart_title=title,
        chart_caption=caption,
        x_title="minutes from start", y_title="TSS concentration (mg/L)",
        flow_units="CFS", series_units="mg/L", mass_balance_tol_pct=10.0,
        demonstration_note=(
            "Synthetic single-subcatchment water-quality demonstration (schematic, not "
            "georeferenced). The overlaid TSS pollutographs + peak concentrations are "
            "the product."
        ),
        basis_param=knob_name,
        basis_note="synthetic subcatchment + design storm; EPA Applications Manual Example 5",
        basis_source="EPA SWMM Applications Manual EPA/600/R-09/000, Example 5",
        series_is_flow=False,
        extra_fn=_extra,
    )
