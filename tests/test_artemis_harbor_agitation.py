"""Offline unit tests for the artemis_harbor_agitation engine template (ADR 0237).

No solver / no network: registration shape + arg-guard rejection paths only.
The physics-through-the-image proof lives in the Dockerfile build-time smoke +
the live E2E; this is the offline-suite guard that the tool is registered as an
engine template and rejects ill-posed args before any dispatch.
"""
from __future__ import annotations

import asyncio


def test_artemis_harbor_agitation_registered_as_engine_template():
    from trid3nt_server.tools import TOOL_REGISTRY
    entry = TOOL_REGISTRY.get("artemis_harbor_agitation")
    assert entry is not None, "artemis_harbor_agitation must be registered"
    m = entry.metadata
    assert m.engine == "telemac" and m.tier == "template"
    assert m.cacheable is False and m.ttl_class == "live-no-cache"
    specs = {r.param for r in (m.resolution_specs or ())}
    assert "target_resolution_m" in specs


def test_artemis_solver_registered():
    from trid3nt_server.workflows.solver.solver import (
        LOCAL_SOLVER_SPEC_REGISTRY,
        SOLVER_WORKFLOW_REGISTRY,
    )
    assert "artemis_agitation" in SOLVER_WORKFLOW_REGISTRY
    assert "artemis_agitation" in LOCAL_SOLVER_SPEC_REGISTRY


def test_tool_rejects_neither_location_nor_bbox():
    from trid3nt_server.workflows.telemac.agitation.agitation import artemis_harbor_agitation
    out = asyncio.run(artemis_harbor_agitation())
    assert isinstance(out, dict) and out["status"] == "error"
    assert out["error_code"] == "ARTEMIS_PARAMS_INCOMPLETE"


def test_tool_rejects_invalid_bbox():
    from trid3nt_server.workflows.telemac.agitation.agitation import artemis_harbor_agitation
    out = asyncio.run(artemis_harbor_agitation(bbox=[1.0, 2.0]))  # too few numbers
    assert isinstance(out, dict) and out["status"] == "error"
    assert out["error_code"] == "ARTEMIS_PARAMS_INVALID"


def test_mode_classification_from_prompt():
    """The declared coercion reads the question class off the ask, or takes it."""
    from trid3nt_server.workflows.telemac.agitation.agitation_mode import agitation_mode
    coerce = agitation_mode()
    assert coerce({"location": "does the basin resonate at the swell period"}
                  )["wave_mode"] == "resonance"
    assert coerce({"location": "waves focusing over the reef"})["wave_mode"] == "shoal"
    # NO signal in either field emits NOTHING: a value emitted here would resolve
    # through the USER door and stamp the declared default as user-supplied.
    assert coerce({"location": "does the breakwater shelter the berths"}) == {}
    # an explicit value wins over the phrasing, and an unknown one falls back to it
    assert coerce({"location": "anything", "wave_mode": "shoal"})["wave_mode"] == "shoal"
    assert coerce({"location": "seiche", "wave_mode": "nonsense"}
                  )["wave_mode"] == "resonance"


def test_the_unspoken_mode_resolves_to_the_declared_default():
    """Abstaining changes the row's provenance, never the class the run models."""
    _, sheet = _sheet(wave_mode=None, location="does the breakwater shelter the berths")
    row = sheet.row("wave_mode")
    assert row.value == "diffraction"
    assert (row.door, row.basis) == ("question", "default_demo")

# ===========================================================================
# The DECLARATION: the plan value, the deck, and the structure.
# ===========================================================================
def _sheet(**overrides):
    from trid3nt_server.tools import TOOL_REGISTRY
    from trid3nt_server.workflows.lib.resolver import resolve_params

    workflow = TOOL_REGISTRY["artemis_harbor_agitation"].fn.workflow
    args = {"bbox": [-87.392, 46.528, -87.368, 46.550], "wave_mode": "diffraction",
            "wave_period_s": 8.0, "wave_height_m": 2.0, "reflection_coef": 0.5,
            "target_resolution_m": 30.0, "bathy_source": "noaa_greatlakes",
            **overrides}
    supplied, err = workflow._normalize(args)
    assert err is None, err
    return workflow, asyncio.run(resolve_params(workflow.params, supplied))


def test_the_declared_plan_is_the_open_water_sequence():
    from trid3nt_server.workflows.lib.validate import validate_plan

    workflow, _sheet_unused = _sheet()
    plan = workflow.plan
    assert [step.label for step in plan.declared()] == [
        "form", "aoi", "deck", "solve", "agitation"]
    validate_plan(plan, workflow.params, workflow.data)


def test_a_pinned_breakwater_suppresses_the_osm_lookup():
    """The caller named the structure; going and finding a different one would
    model something else."""
    from unittest.mock import patch

    from trid3nt_server.workflows.telemac.steps.agitation import write_agitation_deck

    aoi = {"slug": "aoi", "name": "aoi", "lon": -87.38, "lat": 46.54,
           "bbox": (-87.392, 46.528, -87.368, 46.550)}
    with patch("trid3nt_server.workflows.telemac.steps.agitation."
               "fetch_osm_breakwaters") as fetch:
        deck = asyncio.run(write_agitation_deck(
            aoi=aoi, wave_mode="diffraction", bathy_source="noaa_greatlakes",
            breakwater=[-87.39, 46.53, -87.37, 46.54], mesh_resolution_m=30.0))
    fetch.assert_not_called()
    assert deck["breakwater_pinned"] is True
    assert deck["config"]["breakwater"] == [-87.39, 46.53, -87.37, 46.54]
    assert "breakwater_polylines" not in deck["config"]


def test_an_exhausted_osm_fetch_falls_back_to_a_LABELED_schematic():
    """An upstream outage degrades to a schematic that SAYS it is one.

    The row is what keeps the answer honest: kd_sheltered from a schematic barrier
    is a different number than from the surveyed one, and nothing else on the
    layer would say which was meshed.
    """
    from unittest.mock import patch

    from trid3nt_server.workflows.telemac.steps.agitation import (
        _structure_row,
        write_agitation_deck,
    )

    aoi = {"slug": "aoi", "name": "aoi", "lon": -87.38, "lat": 46.54,
           "bbox": (-87.392, 46.528, -87.368, 46.550)}
    with patch("trid3nt_server.workflows.telemac.steps.agitation."
               "fetch_osm_breakwaters", return_value=[]):
        deck = asyncio.run(write_agitation_deck(
            aoi=aoi, wave_mode="diffraction", bathy_source="noaa_greatlakes"))
    assert deck["breakwater_polylines"] is None
    row = _structure_row(deck)
    assert row.value == "schematic_demo" and row.basis == "default_demo"
    assert "LABELED schematic" in row.note


def test_only_diffraction_gets_a_real_harbour():
    """Resonance and shoal are the ANALYTIC verification domains.

    A real-bathymetry request for either falls back to the labeled analytic
    domain rather than fabricating a harbour outline around the geocode.
    """
    from trid3nt_server.workflows.telemac.steps.agitation import write_agitation_deck

    aoi = {"slug": "aoi", "name": "aoi", "lon": -87.38, "lat": 46.54,
           "bbox": (-87.392, 46.528, -87.368, 46.550)}
    deck = asyncio.run(write_agitation_deck(
        aoi=aoi, wave_mode="resonance", bathy_source="noaa_greatlakes"))
    assert deck["real_bathymetry"] is False
    assert "bbox" not in deck["config"]
    assert "seiche ladder" in deck["bathy_label"]


def test_the_agitation_chart_plots_the_workers_own_transect():
    from trid3nt_contracts.telemac_contracts import (
        TELEMAC_AGITATION_STYLE_PRESET,
        ArtemisAgitationLayerURI,
    )
    from trid3nt_server.workflows.telemac.agitation.agitation import (
        build_agitation_chart,
    )

    layer = ArtemisAgitationLayerURI(
        layer_id="x", name="Wave agitation Kd (aoi)", layer_type="raster",
        uri="s3://b/k.tif", style_preset=TELEMAC_AGITATION_STYLE_PRESET,
        role="primary", kd_max=3.947, kd_sheltered=0.09, kd_exposed=0.352,
        wave_period_s=8.0, agitation_curve_m=[-1747.9, -1710.1, -1686.8],
        agitation_curve_kd=[1.256, 1.459, 0.86],
        agitation_curve_kind="diffraction_transect")
    payload = build_agitation_chart(result=layer, params={})
    values = payload["vega_lite_spec"]["data"]["values"]
    assert [row["kd"] for row in values] == [1.256, 1.459, 0.86]
    assert "Distance along the transect (m)" == \
        payload["vega_lite_spec"]["encoding"]["x"]["title"]
    assert "sheltered the berths by a factor of 3.91" in payload["caption"]
    bare = layer.model_copy(update={"agitation_curve_m": None,
                                    "agitation_curve_kd": None})
    assert build_agitation_chart(result=bare, params={}) is None
