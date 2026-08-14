"""Offline tests for the SWMM mechanism-comparison templates (ADR 0151).

Exercises the engine core (the five synthetic-deck builders + the shared solve
loop with its continuity honesty gate) and the composer (the typed
``SWMMComparisonResult`` + overlay-chart assembly) WITHOUT network - the decks are
authored small mechanism stubs solved headless in-process via pyswmm. Guarantees:
every variant solves within its mass-balance tolerance, each knob is DEMONSTRATED
(the compared variants differ), and the composer returns a schematic-only typed
result carrying labeled synthetic provenance.

ASCII only.
"""

from __future__ import annotations

import asyncio

import pytest

from trid3nt_contracts.swmm_contracts import SWMMComparisonResult

from trid3nt_server.agent.mesh import swmm_mechanism_compare as core
from trid3nt_server.agent.workflows.swmm.mechanism_compare.mechanism_compare import (
    run_mechanism_comparison,
)

# Every (builder, kwargs) the templates can request.
_BUILDS = [
    (core.build_subcatchment_runoff, ("infiltration_method",)),
    (core.build_subcatchment_runoff, ("development_intensity",)),
    (core.build_node_hydraulics, ("outlet_family",)),
    (core.build_node_hydraulics, ("flow_diversion",)),
    (core.build_node_hydraulics, ("surcharge_ponding",)),
    (core.build_pump_control, ()),
    (core.build_lid_performance, ("green_roof",)),
    (core.build_lid_performance, ("rainbarrel_vs_disconnect",)),
    (core.build_lid_performance, ("vegetative_swale",)),
    (core.build_lid_performance, ("infiltration_vs_permeable_pavement",)),
    (core.build_wq_buildup_washoff, ("normalization",)),
    (core.build_wq_buildup_washoff, ("washoff",)),
]


def _ids(pair):
    fn, args = pair
    return f"{fn.__name__}{'-'.join(args)}"


@pytest.mark.parametrize("build_fn,args", _BUILDS, ids=[_ids(b) for b in _BUILDS])
def test_every_variant_solves_within_tolerance(build_fn, args):
    """Every variant solves headless and passes the per-build continuity gate."""
    build = build_fn(*args)
    solved = core.solve_variants(build)
    assert solved, f"{build.comparison_kind}: no variants solved"
    for sv in solved:
        assert abs(sv.result.continuity_error_pct) <= build.mass_balance_tol_pct, (
            f"{build.comparison_kind}/{sv.variant.label}: continuity "
            f"{sv.result.continuity_error_pct:+.3f}% over tol {build.mass_balance_tol_pct}%"
        )
        # every variant's primary charted series has real data
        prim = sv.series.get(sv.variant.chart[0][0], [])
        assert len(prim) >= 2, f"{build.comparison_kind}/{sv.variant.label}: thin primary series"


@pytest.mark.parametrize("build_fn,args", _BUILDS, ids=[_ids(b) for b in _BUILDS])
def test_knob_is_demonstrated(build_fn, args):
    """The compared variants differ (a multi-variant comparison must show its knob;
    the single-variant diversion is exempt - its knob is the two-series split)."""
    build = build_fn(*args)
    solved = core.solve_variants(build)
    if len(solved) == 1:
        # diversion: one run, two distinct link series (main vs relief).
        sv = solved[0]
        assert len(sv.variant.chart) >= 2
        series_peaks = [
            max((v for _, v in sv.series.get(lbl, [])), default=0.0)
            for lbl, _, _ in sv.variant.chart
        ]
        assert len({round(p, 3) for p in series_peaks}) >= 2, "diversion split not visible"
    else:
        peaks = {round(sv.primary_peak, 4) for sv in solved}
        assert len(peaks) >= 2, f"{build.comparison_kind}: knob not demonstrated {peaks}"


def test_infiltration_vs_permeable_pavement_discriminates_volume_and_peak():
    """The IT-vs-PP mode encodes the textbook contrast on one footprint: the
    infiltration trench (native seepage, no underdrain) REMOVES volume, while
    permeable pavement (underdrain over a near-lined subgrade) attenuates the PEAK
    but returns most captured water -> IT volume << PP volume, and both peaks are
    below baseline."""
    build = core.build_lid_performance("infiltration_vs_permeable_pavement")
    solved = {sv.variant.label: sv for sv in core.solve_variants(build)}
    base = solved["no LID (baseline)"]
    it = solved["infiltration trench"]
    pp = solved["permeable pavement"]
    # both LIDs cut the peak below baseline
    assert it.primary_peak < base.primary_peak
    assert pp.primary_peak < base.primary_peak
    # infiltration trench removes volume; permeable pavement does not
    assert it.total_value < 0.5 * base.total_value, "IT should roughly halve volume or better"
    assert pp.total_value > 0.7 * base.total_value, "PP underdrain returns most volume"
    # the two LIDs are genuinely distinct (a real discriminating pair, not a re-label)
    assert abs(it.primary_peak - pp.primary_peak) > 0.05


def test_composer_returns_typed_schematic_result():
    """The composer returns a typed SWMMComparisonResult: schematic-only, charted,
    with a variant per knob value and labeled synthetic provenance."""
    async def _run():
        return await run_mechanism_comparison(core.build_lid_performance("green_roof"))

    res = asyncio.run(_run())
    assert isinstance(res, SWMMComparisonResult)
    assert res.schematic_only is True
    assert res.basis == "synthetic"
    assert res.comparison_kind == "lid_performance"
    assert len(res.variants) == 2
    assert res.chart_titles, "no overlay chart emitted"
    assert res.headline.get("knob_demonstrated") is True
    assert res.synthetic_inputs and res.synthetic_inputs[0].basis == "default_demo"
    # every narrated number is a real parsed output (no negative peak/depth).
    for v in res.variants:
        assert v.peak_value >= 0.0 and v.max_node_depth >= 0.0


def test_all_five_families_compose_end_to_end():
    """Each family composes to a valid result through the shared composer."""
    async def _run():
        builds = [
            core.build_subcatchment_runoff("infiltration_method"),
            core.build_node_hydraulics("outlet_family"),
            core.build_pump_control(),
            core.build_lid_performance("rainbarrel_vs_disconnect"),
            core.build_wq_buildup_washoff("washoff"),
        ]
        return [await run_mechanism_comparison(b) for b in builds]

    results = asyncio.run(_run())
    kinds = {r.comparison_kind for r in results}
    assert kinds == {
        "subcatchment_runoff", "outlet_structure", "pump_control",
        "lid_performance", "wq_buildup_washoff",
    }
    for r in results:
        assert r.variants and r.chart_titles and r.series_units
