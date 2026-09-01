"""P4 tests for the TELEMAC river-dye LLM surface: the ``telemac_river_dye``
template (declared PARAMS + DATA + ``plan(p, d)``) over the shared TELEMAC step
family.

Exercised in ISOLATION with geocode / fetch_river_geometry / NWM / run_solver /
boto3 / postprocess / publish all MOCKED (no network, no docker, no TELEMAC).
These pin:

  1. Tool registration + metadata (workflow_dispatch, uncacheable).
  2. Wire-arg normalization: the AOI rules, the three release-point shapes, the
     release-point / reach-seed decoupling, the contaminant promotion.
  3. Declared bounds + the non-numeric refusal replacing the old inline clamps.
  4. The plan: its shape, its gate placement, and that it validates.
  5. The chain geocode -> seed -> carrier discharge -> deck -> solve -> products,
     with the manifest's deck overrides carrying the resolved sheet.
  6. The erodible-bed / GAIA single gate (an armed bed is always sediment).
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from tests.reach_chain import install_reach_chain
from trid3nt_contracts.telemac_contracts import (
    TELEMAC_DYE_STYLE_PRESET,
    TelemacDyeLayerURI,
)

_AOI = (-114.50, 42.52, -114.38, 42.62)  # Twin Falls, Idaho-ish


def _amock(ret):
    async def _inner(*a, **k):
        return ret
    return _inner


class _FakeHandle:
    run_id = "TELERID"
    workflow_name = "local-docker"


class _FakeRunResult:
    run_id = "TELERID"
    status = "complete"
    output_uri = "s3://runs/TELERID/"
    error_code = None
    error_message = None
    cancellation_reason = None


def _fake_peak(run_id: str, reach_name: str) -> TelemacDyeLayerURI:
    return TelemacDyeLayerURI(
        layer_id=f"telemac-dye-peak-{run_id}",
        name=f"Peak dye concentration ({reach_name})",
        layer_type="raster",
        uri=f"s3://runs/{run_id}/telemac_dye_peak.tif",
        style_preset=TELEMAC_DYE_STYLE_PRESET,
        role="primary",
        units="mg/L",
        bbox=list(_AOI),
        dye_cmax_mgl=97.3,
        dye_peak_time_s=420.0,
        plume_reach_m=1830.0,
        active_frames=7,
    )


# ===========================================================================
# (1) Tool registration + metadata.
# ===========================================================================
def test_telemac_river_dye_registered_as_engine_template():
    from trid3nt_server.tools import TOOL_REGISTRY

    entry = TOOL_REGISTRY.get("telemac_river_dye")
    assert entry is not None
    assert entry.metadata.source_class == "workflow_dispatch"
    assert entry.metadata.engine == "telemac"
    assert entry.metadata.tier == "template"
    assert entry.metadata.cacheable is False
    assert entry.metadata.ttl_class == "live-no-cache"
    # Door dissolution: the run_telemac door is DELETED; telemac_river_dye is a
    # standalone retrieval-pool template.
    assert TOOL_REGISTRY.get("run_telemac") is None


def test_docstring_routing_view_fits_the_truncation_budget():
    from trid3nt_server.workflows.telemac.river_dye.river_dye import telemac_river_dye

    head = telemac_river_dye.routing_doc.split("\nReturns:")[0]
    assert len(head) <= 1000
    assert "telemac_do_sag" in telemac_river_dye.routing_doc  # negative routing


# ===========================================================================
# (2) Wire-arg normalization.
# ===========================================================================
def _workflow():
    from trid3nt_server.tools import TOOL_REGISTRY

    return TOOL_REGISTRY["telemac_river_dye"].fn.workflow


def _norm(**kw):

    base: dict[str, Any] = {
        "location": None, "bbox": None, "substance": "dye", "contaminant": None,
        "release_coords": None, "release_lon": None, "release_lat": None,
        "spill_location_latlon": None, "compute_class": None,
        "wind_direction_deg": None, "_release_seeds_reach": None,
        "_seed_release_lon": None, "_seed_release_lat": None,
    }
    base.update(kw)
    return _workflow()._normalize(base)


def test_tool_rejects_neither_location_nor_bbox():
    from trid3nt_server.workflows.telemac.river_dye.river_dye import telemac_river_dye

    out = asyncio.run(telemac_river_dye())
    assert out["status"] == "error"
    assert out["error_code"] == "TELEMAC_PARAMS_INCOMPLETE"


def test_numeric_garbage_bbox_refuses_typed():
    """A bbox that is numeric-ish but unusable dead-ends typed, never guesses."""
    from trid3nt_server.workflows.telemac.river_dye.river_dye import telemac_river_dye

    out = asyncio.run(telemac_river_dye(bbox="1,2"))
    assert out["status"] == "error"
    assert out["error_code"] == "TELEMAC_PARAMS_INVALID"


def test_a_place_name_in_the_bbox_field_is_salvaged_into_location():
    """Models put a place name in `bbox`; shifting it beats dead-ending the call.

    The salvage only fires when there is no `location` to contradict it.
    """
    supplied, err = _norm(bbox="Twin Falls, Idaho")
    assert err is None
    assert supplied["location"] == "Twin Falls, Idaho"
    assert "bbox" not in supplied


def test_location_wins_when_both_an_aoi_and_a_place_are_supplied():
    """A fabricated bbox alongside a real place name meshed the wrong water body;
    the geocoded place is ground truth and a user-drawn AOI arrives via case state."""
    supplied, err = _norm(location="Twin Falls, Idaho", bbox=list(_AOI))
    assert err is None
    assert supplied["location"] == "Twin Falls, Idaho"
    assert "bbox" not in supplied


@pytest.mark.parametrize("kwargs", [
    {"release_coords": [-114.31, 42.58]},
    {"release_lon": -114.31, "release_lat": 42.58},
    {"spill_location_latlon": "42.58,-114.31"},
])
def test_every_release_point_shape_reaches_the_same_param(kwargs):
    supplied, err = _norm(location="X", **kwargs)
    assert err is None
    assert supplied["release_coords"] == (-114.31, 42.58)


def test_a_malformed_release_point_refuses_it_never_falls_back():
    from trid3nt_server.workflows.telemac.river_dye.river_dye import telemac_river_dye

    out = asyncio.run(telemac_river_dye(location="X", release_coords="somewhere"))
    assert out["error_code"] == "TELEMAC_PARAMS_INVALID"
    out = asyncio.run(telemac_river_dye(location="X", release_coords=[200.0, 10.0]))
    assert out["error_code"] == "TELEMAC_PARAMS_INVALID"


def test_the_reach_seed_is_the_call_release_and_only_the_call_release():
    """A CALL-provided release also seeds the reach; a DRAWN click moves the
    source only, so the meshed water body cannot change under the user.

    The split is structural: coercions run on the wire args, before any door and
    so before any gate, which is why a drawn point can reach ``release_coords``
    and reach ``reach_seed_coords`` by no path at all."""
    call, _ = _norm(location="X", release_lon=-114.31, release_lat=42.58)
    assert call["reach_seed_coords"] == (-114.31, 42.58)

    drawn, _ = _norm(location="X")
    assert "release_coords" not in drawn and "reach_seed_coords" not in drawn


def test_a_non_tracer_contaminant_beats_a_tracer_substance():
    supplied, _ = _norm(location="X", substance="dye", contaminant="crude oil")
    assert supplied["substance"] == "crude oil"
    kept, _ = _norm(location="X", substance="sewage", contaminant="water")
    assert kept["substance"] == "sewage"


def test_an_invented_compute_class_refuses_at_the_ladder():
    """A rung the dispatcher cannot serve is REFUSED, not quietly re-seated.

    It used to become 'medium' with a log line and no provenance row, so a caller
    who asked for a bigger box got a smaller solve and nothing on any surface a
    reader looks at said so.
    """
    supplied, err = _norm(location="X", compute_class="dye_spill")
    assert supplied == {}
    assert err["status"] == "error"
    assert err["error_code"] == "TELEMAC_COMPUTE_CLASS_UNKNOWN"
    assert "dye_spill" in err["error_message"]


def test_a_wind_bearing_wraps_rather_than_clamping():
    supplied, _ = _norm(location="X", wind_direction_deg=370.0)
    assert supplied["wind_direction_deg"] == pytest.approx(10.0)


# ===========================================================================
# (3) Declared bounds replace the inline clamps.
# ===========================================================================
def _resolve(**supplied):
    from trid3nt_server.workflows.lib import resolve_params
    from trid3nt_server.workflows.telemac.river_dye.river_dye import PARAMS

    return asyncio.run(resolve_params(PARAMS, {"location": "X", **supplied}))


def test_declared_bounds_clamp_and_label_the_domain_extent():
    """An out-of-window reach length is clamped AND the clamp is on the record."""
    p = _resolve(reach_length_km=50.0)
    assert p.value_of("reach_length_km") == 15.0
    assert "CLAMPED" in p.row("reach_length_km").note

    p2 = _resolve(reach_length_km=6.0)
    assert p2.value_of("reach_length_km") == 6.0
    assert "CLAMPED" not in p2.row("reach_length_km").note


def test_declared_bounds_keep_the_source_inside_the_reach():
    """spill_fraction=1.0 planted the source ON the outflow boundary and aborted
    the solve; the declared bound is what keeps it strictly interior."""
    assert _resolve(spill_fraction=1.0).value_of("spill_fraction") == 0.9
    assert _resolve(spill_fraction=0.0).value_of("spill_fraction") == 0.05
    assert _resolve(sim_duration_s=999999.0).value_of("sim_duration_s") == 14400.0
    assert _resolve(source_q_m3s=100.0).value_of("source_q_m3s") == 30.0


def test_a_non_numeric_bounded_arg_refuses_it_is_never_defaulted():
    from trid3nt_server.workflows.lib import GateRefusedError

    with pytest.raises(GateRefusedError):
        _resolve(reach_length_km="a lot")


def test_an_absent_carrier_discharge_leaves_a_derived_provenance_row():
    """The user has to see that dilution is governed by a fetched value."""
    from trid3nt_server.workflows.lib import provenance_entries
    from trid3nt_server.workflows.telemac.river_dye.river_dye import PARAMS

    row = next(r for r in provenance_entries(_resolve(), PARAMS)
               if r.param == "discharge_m3s")
    assert row.basis == "derived"
    assert "National Water Model" in (row.note or "")


# ===========================================================================
# (4) The plan value.
# ===========================================================================
def test_the_plan_validates_and_gates_before_the_solve():
    from trid3nt_server.workflows.lib import validate_plan
    from trid3nt_server.workflows.lib.plan import Gate

    wf = _workflow()
    p = _resolve()
    pl = wf.plan
    validate_plan(pl, wf.params, wf.data)

    steps = list(pl.declared())
    assert [s.label for s in steps][2:] == [
        "reach", "seed", "carrier_discharge", "mesh", "measure_mesh_coverage",
        "deck", "solve", "plume"]
    # Both gates precede every step, so nothing consumes a value the review can
    # still revise.
    assert all(isinstance(s, Gate) for s in steps[:2])
    assert not any(isinstance(s, Gate) for s in steps[2:])
    assert steps[2].rebinds_domain          # the geocode binds the reach AOI
    assert steps[-2].consequential          # the solve is the consequential node
    assert steps[-1].charts[0].name == "dye_concentration"


def test_the_declared_data_is_the_chain_in_class_body_order():
    from trid3nt_server.workflows.lib import DataRef, data_rows
    from trid3nt_server.workflows.telemac.river_dye.river_dye import DATA

    rows = data_rows(DATA)
    # CLASS-BODY ORDER is the declaration's own, and the chain reads down it.
    assert [d.name for d in rows] == ["rivers", "centerline", "ends", "window",
                                      "banks", "mapped_banks", "reach_polygon",
                                      "bed", "rain"]
    by_name = {d.name: d for d in rows}
    # Row-to-row dataflow written as a plain identifier binds as the same
    # late-bound ref an out-of-body DATA.<row> yields.
    assert by_name["ends"].producer.kwargs["line"] == DataRef("centerline")
    assert by_name["reach_polygon"].producer.kwargs["polygon"] == DataRef(
        "mapped_banks")
    assert DATA.rivers == DataRef("rivers")
    # None of these is superseded by a supplied artifact.
    assert all(d.producer.supplied_uri is None for d in rows)
    # No producer here declares a ladder: gridMET-vs-user-rate is a branch on the
    # ask inside one producer, not a fallback chain, and a declared ladder that
    # never fired would be indistinguishable from one that did.
    assert all(d.producer.ladder_rungs == () for d in rows)


def test_an_unknown_data_row_is_an_attribute_error_at_the_line_that_wrote_it():
    from trid3nt_server.workflows.telemac.river_dye.river_dye import DATA

    with pytest.raises(AttributeError):
        DATA.centreline


# ===========================================================================
# (5) The chain: dispatch + manifest overrides + layer return.
# ===========================================================================
def _install_step_mocks(captured: dict):
    from trid3nt_server.workflows.solver import solver as solver_mod
    from trid3nt_server.workflows.telemac import postprocess_telemac as pp_mod
    from trid3nt_server.workflows.telemac import release_layer as rel_mod
    from trid3nt_server.workflows.telemac import results_mesh_seam as seam_mod
    from trid3nt_server.workflows.shared import run_products as products_mod
    from trid3nt_server.workflows.telemac.steps import products as prod_steps
    from trid3nt_server.workflows.mesh import step as mesh_step
    from trid3nt_server.workflows.telemac.steps import reach as reach_steps
    from trid3nt_server.workflows.telemac.steps import forcing as forcing_steps
    from trid3nt_server.workflows.telemac.steps import solve as solve_steps
    from trid3nt_server.workflows.telemac.steps import deck as deck_steps

    def _fake_registry_fn(name):
        if name == "geocode_location":
            def _geo(q, **_k):
                captured["geocode_query"] = q
                return {"name": "Twin Falls, Idaho", "latitude": 42.5629,
                        "longitude": -114.4609}
            return _geo
        if name == "fetch_river_geometry":
            def _river(*, bbox, **_k):
                captured["river_bbox"] = bbox
                class _L:
                    uri = "s3://cache/river.fgb"
                return _L()
            return _river
        raise AssertionError(f"unexpected tool {name}")

    def _fake_seed(uri):
        captured["seed_uri"] = uri
        return (-114.31, 42.58)  # a mid-reach point on the Snake

    async def _fake_mesh(*, mesh, name=None):
        """The mesh session stands in: this chain test is about the chain.

        The artifact reports the edge the ask named, so the mesh contributes
        nothing to the timestep and this chain's deck is the historical one.
        """
        from trid3nt_server.workflows.mesh.artifact import MeshArtifact

        captured["mesh_ask"] = dict(mesh)
        artifact = MeshArtifact(
            mesh_id="MESH01", name="reach", mode="om2d",
            display_uri="s3://cache/mesh/MESH01/mesh.2dm",
            slf_uri="s3://cache/mesh/MESH01/river.slf",
            crs_authid="EPSG:32611", has_bathymetry=True, utm_epsg=32611,
            node_count=800, element_count=1400,
            bbox=(-114.4, 42.5, -114.2, 42.7),
            probes={"bed_fit": {"bed_top_m": 900.0, "bed_drop_m": 3.0}},
            # The polygon the mesh was CUT from, which is what a supplied release
            # point is tested against - the chain's own sectioned banks.
            provenance={"spec": {"extent": dict(mesh["fields"]).get("extent")}})
        return {"artifact": artifact, "mesh_id": "MESH01",
                "slf_uri": "s3://cache/mesh/MESH01/river.slf",
                "cli_uri": "s3://cache/mesh/MESH01/river.cli",
                "display_uri": "s3://cache/mesh/MESH01/mesh.2dm",
                "topology_uri": "s3://cache/mesh/MESH01/mesh_topology.json",
                "node_count": 800, "element_count": 1400, "min_edge_m": None,
                "provenance": {"dem_source": "cop-dem-glo-30"}}

    def _fake_stage(case, run_tag, **_kw):
        captured["case"] = case
        captured["run_tag"] = run_tag
        return f"s3://cache/telemac/{run_tag}/manifest.json"

    _write_deck = deck_steps.write_reach_deck

    async def _capture_deck(**kw):
        """The real author, with the SHEET it serialized kept for inspection.

        The sheet stopped travelling to the worker when the deck flipped, so the
        assertions below read it where it is written rather than off a manifest
        that no longer carries it.
        """
        out = await _write_deck(**kw)
        captured["reach"] = out["deck"]
        return out

    async def _capture_marker(_emitter, *, lon, lat, user_supplied, **_kw):
        captured["release_marker"] = {"lon": lon, "lat": lat,
                                      "user_supplied": user_supplied}
        return False

    def _fake_run_solver(*, solver, model_setup_uri, compute_class):
        captured["solver"] = solver
        captured["model_setup_uri"] = model_setup_uri
        captured["compute_class"] = compute_class
        return _FakeHandle()

    def _fake_postprocess(slf_path, *, run_id, utm_epsg, reach_name, **_kw):
        captured["pp_run_id"] = run_id
        captured["pp_utm_epsg"] = utm_epsg
        return [_fake_peak(run_id, reach_name)], {"dye_cmax_mgl": 97.3}

    def _fake_publish(raw_peak, run_id, location_name, mesh_meta, substance,
                      substance_class, synthetic_inputs):
        captured["published"] = True
        captured["publish_substance"] = substance
        captured["publish_substance_class"] = substance_class
        captured["synthetic_inputs"] = synthetic_inputs
        return raw_peak.model_copy(update={"uri": "https://tiles/dye_peak.png",
                                           "synthetic_inputs": synthetic_inputs,
                                           **mesh_meta})

    return [
        patch.object(reach_steps, "registry_fn", _fake_registry_fn),
        patch.object(reach_steps, "river_seed_from_geometry", _fake_seed),
        # The mesh session stands in, so its display face is a uri nothing wrote:
        # what the mesh holds of the reach is measured in its own test module.
        patch.object(reach_steps, "_meshed_fraction", lambda mesh, centerline: 1.0),
        patch.object(deck_steps, "write_reach_deck", _capture_deck),
        patch.object(deck_steps, "read_topology",
                     lambda _uri: {"roles": {"inflow": [1], "outflow": [2]},
                                   "liquid_boundary_order": ["outflow", "inflow"]}),
        patch.object(deck_steps, "read_centerline_utm",
                     lambda _src, _epsg, **_kw: __import__("numpy").array(
                         [[0.0, 0.0], [6000.0, 0.0]])),
        patch.object(deck_steps, "_stage_authored",
                     lambda _rundir, run_tag, names: [
                         {"gs_uri": f"s3://cache/telemac/{run_tag}/{n}", "dest": n}
                         for n in names]),
        patch.object(mesh_step, "build_declared_mesh", _fake_mesh),
        patch.object(forcing_steps, "_nwm_nearest_streamflow",
                     lambda lon, lat, valid_time=None: {
                         "m3s": 312.0, "reference_time": "2026-01-01T12:00:00+00:00",
                         "product": "analysis_assim", "layer": None}),
        patch.object(solve_steps, "stage_manifest", _fake_stage),
        patch.object(solve_steps, "read_run_metrics",
                     lambda rid: {"utm_epsg": 32611}),
        patch.object(prod_steps, "download_result_selafin",
                     lambda rid: "/tmp/telemac/does-not-matter.slf"),
        patch.object(prod_steps, "_publish_peak_layer", _fake_publish),
        patch.object(pp_mod, "postprocess_telemac", _fake_postprocess),
        patch.object(seam_mod, "publish_results_mesh_via_seam", _amock(0)),
        patch.object(rel_mod, "publish_release_point", _capture_marker),
        patch.object(products_mod, "persist_run_products", _amock([])),
        patch.object(solver_mod, "run_solver", _fake_run_solver),
        patch.object(solver_mod, "wait_for_completion", _amock(_FakeRunResult())),
        patch.object(solver_mod, "set_emitter_binding", lambda *a, **k: None),
    ]


def _run_tool(tmp_path, monkeypatch, captured: dict, overrides=(), banks=None,
              **kwargs):
    from trid3nt_server.workflows.telemac.river_dye.river_dye import telemac_river_dye

    monkeypatch.setenv("TRID3NT_DEV_PERSISTENCE_DIR", str(tmp_path / "persistence"))
    install_reach_chain(monkeypatch, tmp_path, captured, banks=banks)
    mocks = [*_install_step_mocks(captured), *overrides]
    for m in mocks:
        m.start()
    try:
        return asyncio.run(telemac_river_dye(**kwargs))
    finally:
        for m in reversed(mocks):
            m.stop()


def test_the_chain_geocodes_dispatches_and_stages_the_resolved_sheet(
        tmp_path, monkeypatch):
    captured: dict = {}
    peak = _run_tool(tmp_path, monkeypatch, captured,
                     location="Twin Falls, Idaho", spill_fraction=0.4,
                     spill_duration_s=600.0, dye_concentration_mgl=250.0,
                     reach_length_km=4.0, sim_duration_s=1800.0)

    assert isinstance(peak, TelemacDyeLayerURI)
    assert peak.uri == "https://tiles/dye_peak.png"
    assert peak.dye_cmax_mgl == pytest.approx(97.3)
    assert peak.mesh_size_m is not None

    # The place was GEOCODED, never hand-typed.
    assert captured["geocode_query"] == "Twin Falls, Idaho"
    assert captured["solver"] == "telemac_river_dye"
    assert captured["model_setup_uri"].endswith("manifest.json")
    # Download + postprocess ran under the SOLVER's run_id, so outputs land under
    # the real run prefix rather than the manifest tag.
    assert captured["pp_run_id"] == "TELERID"
    assert captured["pp_utm_epsg"] == 32611

    reach = captured["reach"]
    assert reach["spill_frac"] == pytest.approx(0.4)
    assert reach["pulse_window_s"] == pytest.approx(600.0)
    assert reach["dye_conc_mgl"] == pytest.approx(250.0)
    assert reach["distance_km"] == pytest.approx(4.0)
    assert reach["duration_s"] == pytest.approx(1800.0)
    assert reach["seed_lon"] == pytest.approx(-114.31, abs=1e-4)
    assert reach["seed_lat"] == pytest.approx(42.58, abs=1e-4)
    assert reach["nav_direction"] == "DM"
    # The carrier discharge the NWM lookup resolved reached the boundary
    # condition, and the layer says where it came from.
    assert reach["inflow_q_m3s"] == pytest.approx(312.0)
    q_row = next(r for r in peak.synthetic_inputs if r.param == "discharge_m3s")
    assert q_row.basis == "fetched" and "Water Model" in (q_row.real_source_if_any or "")


def test_a_prefetched_flowline_is_reused_instead_of_refetched(tmp_path, monkeypatch):
    captured: dict = {}
    provided = "s3://trid3nt-cache/cache/static-30d/river_geometry/prefetched.fgb"
    peak = _run_tool(tmp_path, monkeypatch, captured,
                     location="Twin Falls, Idaho", river_geometry_uri=provided)
    assert isinstance(peak, TelemacDyeLayerURI)
    assert captured["seed_uri"] == provided
    assert "river_bbox" not in captured  # fetch_river_geometry never ran


def test_the_seed_falls_back_to_the_centroid_when_extraction_misses(
        tmp_path, monkeypatch):
    """The worker NLDI-snaps the centroid, so the degrade is honest, not a dead end."""
    from trid3nt_server.workflows.telemac.steps import reach as reach_steps

    captured: dict = {}
    peak = _run_tool(
        tmp_path, monkeypatch, captured, location="Twin Falls, Idaho",
        overrides=[patch.object(reach_steps, "river_seed_from_geometry",
                                lambda uri: None)])
    assert isinstance(peak, TelemacDyeLayerURI)
    reach = captured["reach"]
    assert reach["seed_lon"] == pytest.approx(-114.4609, abs=1e-3)
    assert reach["seed_lat"] == pytest.approx(42.5629, abs=1e-3)


def _real_centerline_read() -> dict:
    """Let the REAL centerline reader run, over the chain's own navigated line.

    The module's own stub answers with a synthetic polyline, which says nothing
    about where a release derived along the DECLARED reach actually lands.
    """
    from trid3nt_server.workflows.mesh.shared.nodes import read_centerline_utm
    from trid3nt_server.workflows.telemac.steps import deck as deck_steps

    return {"overrides": [patch.object(deck_steps, "read_centerline_utm",
                                       read_centerline_utm)]}


def test_the_reach_is_navigated_EXACTLY_ONCE(tmp_path, monkeypatch):
    """ONE centerline acquisition. A second navigate resolved beside the declared
    row walked a different seed for a different distance, so the line the section
    was cut between and the line the deck read described different rivers - and a
    release derived along the second one landed outside the meshed domain."""
    captured: dict = {}
    _run_tool(tmp_path, monkeypatch, captured, location="Twin Falls, Idaho",
              reach_length_km=4.0)
    assert len(captured["navigates"]) == 1
    assert captured["navigates"][0]["distance_km"] == 4.0


def test_a_supplied_seed_point_is_the_one_the_centerline_is_navigated_from(
        tmp_path, monkeypatch):
    """Naming where the substance enters the water names which stretch to model,
    so the ONE navigate starts there rather than at the flowline midpoint."""
    captured: dict = {}
    _run_tool(tmp_path, monkeypatch, captured, location="Twin Falls, Idaho",
              release_coords=[-124.10, 40.50], **_real_centerline_read())
    assert captured["navigates"][0]["seed_point"] == [-124.10, 40.50]
    assert captured["reach"]["seed_lon"] == pytest.approx(-124.10)
    # the supplied point was settled against THAT centerline, and the deck says so
    assert captured["reach"]["release_lon"] == pytest.approx(-124.10)
    assert captured["release_marker"]["user_supplied"] is True


def test_a_derived_release_sits_on_the_DECLARED_centerline(tmp_path, monkeypatch):
    """No release point placed -> the source walks spill_fraction along the same
    line the mesh was built over, which is what makes it inside the domain by
    construction rather than by luck."""
    from shapely.geometry import LineString, Point

    from tests.reach_chain import CENTERLINE

    captured: dict = {}
    _run_tool(tmp_path, monkeypatch, captured, location="Twin Falls, Idaho",
              spill_fraction=0.5, **_real_centerline_read())
    marker = captured["release_marker"]
    assert marker["user_supplied"] is False
    line = LineString(CENTERLINE["coordinates"])
    assert line.distance(Point(marker["lon"], marker["lat"])) < 1e-4
    # ... and the deck states the FRACTION rather than a coordinate, because a
    # release row that reads "user" over a derived point is the dishonest one.
    assert captured["reach"]["spill_frac"] == 0.5
    assert "release_lon" not in captured["reach"]


def test_a_step_failure_maps_to_the_typed_error_envelope(tmp_path, monkeypatch):
    from trid3nt_server.workflows.telemac.steps import solve as solve_steps
    from trid3nt_server.workflows.telemac.steps import deck as deck_steps
    from trid3nt_server.workflows.telemac.steps.errors import TelemacDyeScenarioError

    async def _boom(**_kw):
        raise TelemacDyeScenarioError("TELEMAC_DYE_RUN_FAILED",
                                      "solve did not complete")

    captured: dict = {}
    out = _run_tool(tmp_path, monkeypatch, captured, location="Twin Falls, Idaho",
                    overrides=[patch.object(solve_steps, "solve_reach", _boom)])
    assert out["status"] == "error"
    assert out["error_code"] == "TELEMAC_DYE_RUN_FAILED"


# --- the banks WINDOW, and the coverage it is judged by ---------------------- #
def test_the_banks_are_queried_over_the_centerline_padded_by_a_stated_distance(
        tmp_path, monkeypatch):
    """The query window has to reach a far channel behind a mid-river island, so
    it is the centerline's extent grown by a DISTANCE - three kilometres, written
    on the row - and not the line's own tight bounds."""
    from tests.reach_chain import CENTERLINE_BBOX

    captured: dict = {}
    _run_tool(tmp_path, monkeypatch, captured, location="Twin Falls, Idaho")

    asked = captured["banks_bbox"]
    # 3 km of latitude in degrees. The straight test stretch has zero height, so
    # the tool's own degenerate-layer floor (0.001 deg) rides under the pad; the
    # window still reaches the full stated distance on every side.
    dy = 3000.0 / 111_320.0
    floor = 0.001
    assert dy <= (CENTERLINE_BBOX[1] - asked[1]) <= dy + floor
    assert dy <= (asked[3] - CENTERLINE_BBOX[3]) <= dy + floor
    # The pad is a DISTANCE: the same 3 km costs more degrees of longitude at
    # 40.5 N than it does of latitude.
    assert (CENTERLINE_BBOX[0] - asked[0]) > (asked[3] - CENTERLINE_BBOX[3])
    assert (asked[2] - CENTERLINE_BBOX[2]) > (asked[3] - CENTERLINE_BBOX[3])


def test_a_reach_no_polygon_maps_refuses_as_unmapped_not_as_an_empty_section(
        tmp_path, monkeypatch):
    """The measurement sits between the fetch and the cut, so a reach nothing maps
    fails on its own cause instead of arriving at the section as empty geometry."""
    from tests.reach_chain import BANKS_ELSEWHERE

    captured: dict = {}
    out = _run_tool(tmp_path, monkeypatch, captured, location="Twin Falls, Idaho",
                    banks=BANKS_ELSEWHERE)
    assert out["status"] == "error"
    assert out["error_code"] == "REACH_BANKS_UNMAPPED"


def test_a_partly_mapped_reach_proceeds_and_says_how_much_was_mapped(
        tmp_path, monkeypatch):
    """NO invented threshold: above zero the run proceeds, carrying the MEASURED
    fraction so a reader is never left assuming the flowline-only stretches were
    modelled."""
    from tests.reach_chain import BANKS_GAPPED

    captured: dict = {}
    peak = _run_tool(tmp_path, monkeypatch, captured, location="Twin Falls, Idaho",
                     banks=BANKS_GAPPED)
    assert isinstance(peak, TelemacDyeLayerURI)
    note = peak.fallback_note or ""
    assert "50.0%" in note
    assert "flowline" in note


def test_a_reach_whose_far_END_is_unmapped_refuses_at_the_cut(
        tmp_path, monkeypatch):
    """Coverage above zero is not the same fact as a domain with two transects.
    Mapped water that stops halfway leaves the downstream end standing on the
    polygon's own bank, and a boundary role cannot be prescribed across an edge
    the cut never made - so the refusal names the geometry rather than arriving
    at the mesher as an empty face."""
    from tests.reach_chain import BANKS_HALF

    captured: dict = {}
    out = _run_tool(tmp_path, monkeypatch, captured, location="Twin Falls, Idaho",
                    banks=BANKS_HALF)
    assert out["status"] == "error"
    assert out["error_code"] == "SECTION_END_FACE_UNMEASURED"
    assert "downstream cut left no transect" in out["error_message"]


def test_an_unmapped_reach_refuses_terminally_naming_the_three_supply_paths():
    """A reach nothing maps has no domain, and no rung to retry with: the refusal
    names the three ways a domain is SUPPLIED and offers no retry args."""
    from trid3nt_server.workflows.telemac.steps.errors import ReachBanksUnmapped

    exc = ReachBanksUnmapped()
    assert exc.error_code == "REACH_BANKS_UNMAPPED"
    assert getattr(exc, "retryable", False) is False
    assert not hasattr(exc, "suggestions")
    for path in ("Draw or supply the reach polygon", "name a case layer",
                 "pick a reach with mapped water coverage"):
        assert path in str(exc)


# ===========================================================================
# (6) The erodible-bed / GAIA single gate: an armed bed is ALWAYS sediment.
#     The old false green: substance='scour' fell through classify to 'tracer'
#     while the scour hint independently armed erodible_bed=True, so the deck
#     coupled no GAIA and the run only LOOKED morphodynamic.
# ===========================================================================
@pytest.mark.parametrize("s", [
    "scour", "bed scour below the weir", "erosion", "bed erosion",
    "erodible bed", "bedload", "bed load transport", "bed degradation",
    "channel aggradation", "mobile bed morphodynamics", "bed lowering",
    "morphological change",
])
def test_scour_phrasing_classifies_as_sediment(s):
    from trid3nt_server.workflows.telemac.steps import classify_substance

    cls, payload = classify_substance(s)
    assert cls == "sediment", (s, cls)
    assert isinstance(payload, dict) and payload.get("grain_size", 0) > 0.0


def test_sediment_and_tracer_regression_unchanged():
    from trid3nt_server.workflows.telemac.steps import classify_substance

    assert classify_substance("dye") == ("tracer", None)
    assert classify_substance("water") == ("tracer", None)
    assert classify_substance("oil")[0] == "oil"
    assert classify_substance("sewage")[0] == "decay"
    assert classify_substance("sand")[0] == "sediment"
    assert classify_substance("oily scour")[0] == "oil"        # oil still wins
    assert classify_substance("sewage erosion")[0] == "decay"  # decay still wins


def test_scour_phrasing_arms_the_erodible_bed_without_an_explicit_knob():
    from trid3nt_server.workflows.telemac.steps import arm_sediment_modules

    erodible, gradation, dredging = arm_sediment_modules(
        "scour below the weir", erodible_bed=None, sediment_gradation=None,
        dredging=None)
    assert erodible is True and gradation is None and dredging is False
    # A graded mixture and a dig rule each force a MOBILE bed to act on.
    assert arm_sediment_modules("graded sand", erodible_bed=None,
                                sediment_gradation=None, dredging=None)[0] is True
    assert arm_sediment_modules("maintenance dredging", erodible_bed=None,
                                sediment_gradation=None, dredging=None)[0] is True
    # An explicit False still wins over the vocabulary.
    assert arm_sediment_modules("scour", erodible_bed=False,
                                sediment_gradation=None, dredging=None)[0] is False


def test_a_scour_prompt_stages_the_sediment_class_and_arms_gaia(
        tmp_path, monkeypatch):
    captured: dict = {}
    peak = _run_tool(tmp_path, monkeypatch, captured,
                     location="Twin Falls, Idaho", substance="scour below the weir")
    assert isinstance(peak, TelemacDyeLayerURI)
    reach = captured["reach"]
    assert reach["substance_class"] == "sediment"   # NOT tracer (the bug)
    assert reach["erodible_bed"] is True


@pytest.mark.parametrize("subst", ["dye", "water", "red dye", "some chemical"])
def test_an_armed_erodible_bed_forces_sediment_over_any_tracer(
        subst, tmp_path, monkeypatch):
    captured: dict = {}
    _run_tool(tmp_path, monkeypatch, captured, location="Twin Falls, Idaho",
              substance=subst, erodible_bed=True)
    reach = captured["reach"]
    assert reach["substance_class"] == "sediment"
    assert reach["erodible_bed"] is True


def test_the_deck_never_stages_an_erodible_tracer(tmp_path, monkeypatch):
    """The honesty-floor invariant, over a matrix: an armed bed is never tracer."""
    for subst in ("dye", "scour", "oil", "sewage", "sand"):
        captured: dict = {}
        _run_tool(tmp_path, monkeypatch, captured, location="Twin Falls, Idaho",
                  substance=subst, erodible_bed=True)
        reach = captured["reach"]
        assert not (reach.get("erodible_bed")
                    and reach.get("substance_class") != "sediment"), (subst, reach)
