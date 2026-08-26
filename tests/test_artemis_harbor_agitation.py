"""Offline unit tests for the artemis_harbor_agitation engine template (ADR 0237).

No solver / no network: registration shape + arg-guard rejection paths only.
The physics-through-the-image proof lives in the Dockerfile build-time smoke +
the live E2E; this is the offline-suite guard that the tool is registered as an
engine template and rejects ill-posed args before any dispatch.
"""
from __future__ import annotations

import asyncio

#: What the declared bed producer hands the deck writer: the staged raster's URI.
#: A domain solved on real bathymetry refuses without one, because the worker
#: holds no fetcher of its own any more.
_STAGED_BED = {"uri": "s3://trid3nt-cache/cache/static-30d/ncei_dem_mosaic/test.tif",
               "source": "noaa_ncei_dem_all"}


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


def test_a_supplied_structure_is_meshed_whatever_form_it_arrived_in():
    """A drawn line and a fetched layer must be the same barrier by deck time.

    Both routes go through the one supplied-geometry reader, which is the
    no-double-middleware law at our own front door: the solve cannot tell, and
    must not be able to tell, whether the caller sketched the breakwater or
    fetched the surveyed one.
    """
    from trid3nt_server.workflows.telemac.steps.agitation import write_agitation_deck

    aoi = {"slug": "aoi", "name": "aoi", "lon": -87.38, "lat": 46.54,
           "bbox": (-87.392, 46.528, -87.368, 46.550)}
    drawn = [[-87.39, 46.53], [-87.37, 46.54]]
    deck = asyncio.run(write_agitation_deck(bed=_STAGED_BED, 
        aoi=aoi, wave_mode="diffraction", bathy_source="noaa_greatlakes",
        structure=drawn, mesh_resolution_m=30.0))
    assert deck["config"]["breakwater_polylines"] == [
        [[-87.39, 46.53], [-87.37, 46.54]]]

    # the draw gate's reply shape reaches the same deck
    sketched = asyncio.run(write_agitation_deck(bed=_STAGED_BED, 
        aoi=aoi, wave_mode="diffraction", bathy_source="noaa_greatlakes",
        structure={"type": "Feature",
                   "geometry": {"type": "LineString", "coordinates": drawn}},
        mesh_resolution_m=30.0))
    assert (sketched["config"]["breakwater_polylines"]
            == deck["config"]["breakwater_polylines"])


def test_an_unfilled_structure_slot_asks_for_nothing():
    """Absence is an ANSWER, not a reason to go looking.

    The step interprets the slot and nothing else: an unfilled slot puts no
    structure on the deck, so nothing downstream can read a request that was
    never made.
    """
    from trid3nt_server.workflows.telemac.steps.agitation import (
        write_agitation_deck,
    )

    aoi = {"slug": "aoi", "name": "aoi", "lon": -87.38, "lat": 46.54,
           "bbox": (-87.392, 46.528, -87.368, 46.550)}
    deck = asyncio.run(write_agitation_deck(bed=_STAGED_BED, 
        aoi=aoi, wave_mode="diffraction", bathy_source="noaa_greatlakes"))
    assert deck["breakwater_polylines"] is None
    assert "breakwater_polylines" not in deck["config"]


def test_the_structure_row_reports_the_solve_not_the_request():
    """The deck says what was ASKED for; only the solve knows what was MESHED.

    The real-bathymetry builder meshes a schematic barrier when the deck names
    none, so reading the request back would report open water on a domain that
    carries a barrier. Three answers, and none of them may read alike: meshed
    nothing, meshed something nobody asked for, and did not say.
    """
    from trid3nt_server.workflows.telemac.steps.agitation import (
        _structure_row,
        write_agitation_deck,
    )

    aoi = {"slug": "aoi", "name": "aoi", "lon": -87.38, "lat": 46.54,
           "bbox": (-87.392, 46.528, -87.368, 46.550)}
    deck = asyncio.run(write_agitation_deck(bed=_STAGED_BED, 
        aoi=aoi, wave_mode="diffraction", bathy_source="noaa_greatlakes"))

    confirmed = _structure_row(deck, {"structure_present": False, "bw_label": ""})
    assert confirmed.value is None and "OPEN WATER" in confirmed.note

    meshed = _structure_row(
        deck, {"structure_present": True,
               "bw_label": "schematic demo breakwater (labeled)"})
    assert meshed.value == "not_supplied_but_meshed"
    assert "did NOT run open water" in meshed.note
    assert "schematic demo breakwater" in meshed.note
    assert "OPEN WATER" not in meshed.note, (
        "a domain carrying an unrequested barrier must never read as open water")

    silent = _structure_row(deck, {})
    assert silent.value is None and "UNMEASURED" in silent.note
    assert "OPEN WATER" not in silent.note, (
        "an unmeasured structure is not the same fact as no structure")


def test_the_step_module_makes_no_network_call_of_its_own():
    """The breakwater-class guard: a step INTERPRETS declarations, it never fetches."""
    import inspect

    from trid3nt_server.workflows.telemac.steps import agitation as mod

    source = inspect.getsource(mod)
    for primitive in ("urllib", "urlopen", "requests.get", "requests.post", "httpx",
                      "put_object", "_get_s3_client"):
        assert primitive not in source, (
            f"{primitive!r} is back in the agitation step module: a step that "
            "fetches bypasses the router's cache, ladders and provenance")


def test_only_diffraction_gets_a_real_harbour():
    """Resonance and shoal are the ANALYTIC verification domains.

    A real-bathymetry request for either falls back to the labeled analytic
    domain rather than fabricating a harbour outline around the geocode.
    """
    from trid3nt_server.workflows.telemac.steps.agitation import write_agitation_deck

    aoi = {"slug": "aoi", "name": "aoi", "lon": -87.38, "lat": 46.54,
           "bbox": (-87.392, 46.528, -87.368, 46.550)}
    deck = asyncio.run(write_agitation_deck(bed=_STAGED_BED, 
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
