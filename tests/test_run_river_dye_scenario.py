"""P4 tests for the TELEMAC river-dye LLM surface: the ``telemac_river_dye``
template - its declared PARAMS, its DATA chain and the fill/run door it hands
them to - over the shared river part it lists.

Exercised in ISOLATION with geocode / fetch_river_geometry / NWM / run_solver /
boto3 / postprocess / publish all MOCKED (no network, no docker, no TELEMAC).
These pin:

  1. Tool registration + metadata (workflow_dispatch, uncacheable).
  2. Wire-arg normalization: the AOI rules, the three release-point shapes, the
     release-point / reach-seed decoupling, the contaminant promotion.
  3. Declared bounds + the non-numeric refusal replacing the old inline clamps.
  4. The sequence the door builds: its shape, where the run is HELD, and that
     it validates.
  5. The chain geocode -> seed -> carrier discharge -> run -> solve -> products,
     with the manifest's case section carrying what the sheet resolved to.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from tests.reach_chain import MESH_ROLES, install_reach_chain
from trid3nt_contracts.telemac_contracts import (
    TELEMAC_DYE_STYLE,
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
    from trid3nt_server.workflows.telemac.templates.river_dye.river_dye import telemac_river_dye

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
    from trid3nt_server.workflows.telemac.templates.river_dye.river_dye import telemac_river_dye

    out = asyncio.run(telemac_river_dye())
    assert out["status"] == "error"
    assert out["error_code"] == "TELEMAC_PARAMS_INCOMPLETE"


def test_numeric_garbage_bbox_refuses_typed():
    """A bbox that is numeric-ish but unusable dead-ends typed, never guesses."""
    from trid3nt_server.workflows.telemac.templates.river_dye.river_dye import telemac_river_dye

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
    from trid3nt_server.workflows.telemac.templates.river_dye.river_dye import telemac_river_dye

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
    """The sheet the invocation resolves, over EVERY row the template declares.

    A river template composes the shared part's rows with its own, so the sheet
    is the workflow's rather than the template's own class body.
    """
    from trid3nt_server.workflows.runtime import resolve_params

    return asyncio.run(resolve_params(_workflow().params,
                                      {"location": "X", **supplied}))


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
    from trid3nt_server.workflows.runtime import GateRefusedError

    with pytest.raises(GateRefusedError):
        _resolve(reach_length_km="a lot")


def test_an_absent_carrier_discharge_leaves_a_derived_provenance_row():
    """The user has to see that dilution is governed by a fetched value."""
    from trid3nt_server.workflows.runtime import provenance_entries

    row = next(r for r in provenance_entries(_resolve(), _workflow().params)
               if r.param == "discharge_m3s")
    assert row.basis == "derived"
    assert "National Water Model" in (row.note or "")


def _peak_layer(**overrides: Any) -> TelemacDyeLayerURI:
    from trid3nt_contracts.common import SyntheticInput

    return TelemacDyeLayerURI(
        layer_id="L", name="Peak dye concentration (reach)", layer_type="raster",
        uri="s3://runs/x.tif", dye_cmax_mgl=4.9, dye_peak_time_s=200.0,
        dye_curve_time_s=[0.0, 100.0, 200.0, 300.0],
        dye_curve_cmax_mgl=[0.0, 1.2, 4.9, 2.1],
        synthetic_inputs=[SyntheticInput(
            param="mesh_bed", value="Copernicus GLO-30", basis="fetched",
            consequence="physics")],
        **overrides)


def test_the_dye_chart_is_the_run_s_own_history_not_two_points():
    """Every frame the solver wrote is a point; the peak is one of them."""
    from trid3nt_server.workflows.telemac.templates.river_dye.river_dye import build_dye_chart

    payload = build_dye_chart(result=_peak_layer(), params={"location": "the reach"})
    values = payload["vega_lite_spec"]["data"]["values"]
    assert [v["t_s"] for v in values] == [0.0, 100.0, 200.0, 300.0]
    assert max(v["dye_mgl"] for v in values) == 4.9


def test_the_dye_chart_caption_names_the_bed_the_run_actually_read():
    from trid3nt_server.workflows.telemac.templates.river_dye.river_dye import build_dye_chart

    payload = build_dye_chart(result=_peak_layer(), params={"location": "the reach"})
    assert "Copernicus GLO-30" in payload["caption"]
    assert "idealized" not in payload["caption"].lower()


def test_a_run_with_no_persisted_history_draws_no_chart():
    """No curve is the honest answer; two invented points are not."""
    from trid3nt_server.workflows.telemac.templates.river_dye.river_dye import build_dye_chart

    bare = _peak_layer()
    bare.dye_curve_time_s = None
    bare.dye_curve_cmax_mgl = None
    assert build_dye_chart(result=bare, params={"location": "the reach"}) is None


# ===========================================================================
# (4) The sequence the door builds.
# ===========================================================================
def test_the_sequence_validates_and_holds_the_run_after_the_fill():
    from trid3nt_server.workflows.runtime import validate_plan
    from trid3nt_server.workflows.runtime.plan import Gate

    wf = _workflow()
    pl = wf.plan
    validate_plan(pl, wf.params, wf.data)

    steps = list(pl.declared())
    assert [s.label for s in steps] == [
        "reach", "seed", "carrier_discharge", "mesh", "measure_mesh_coverage",
        "decay", "settled", "sheet", "solve", "plume"]
    # NO gate: the review is the door's view of the sheet it just filled, so the
    # run is HELD there rather than in front of a step that has not run.
    assert not any(isinstance(s, Gate) for s in steps)
    assert [s.label for s in steps if s.self_gating] == ["sheet"]
    assert steps[0].rebinds_domain          # the geocode binds the reach AOI
    assert steps[-2].consequential          # the run is the consequential node
    assert steps[-1].charts[0].name == "dye_concentration"


def test_the_declared_data_is_the_chain_in_declaration_order():
    """The shared part's chain, then the row this question adds to it."""
    from trid3nt_server.workflows.runtime import DataRef, data_rows
    from trid3nt_server.workflows.telemac.templates.river_dye.river_dye import DATA
    from trid3nt_server.workflows.telemac.templates.shared import river

    rows = data_rows(DATA)
    # CLASS-BODY ORDER is the declaration's own, and the chain reads down it.
    assert [d.name for d in rows] == ["rivers", "centerline", "ends", "window",
                                      "water", "mapped_water", "reach_polygon",
                                      "dem", "rain"]
    by_name = {d.name: d for d in rows}
    # Row-to-row dataflow written as a plain identifier binds as the same
    # late-bound ref an out-of-body DATA.<row> yields.
    assert by_name["ends"].producer.kwargs["line"] == DataRef("centerline")
    assert by_name["reach_polygon"].producer.kwargs["polygon"] == DataRef(
        "mapped_water")
    assert river.DATA.rivers == DataRef("rivers")
    # None of these is superseded by a supplied artifact.
    assert all(d.producer.supplied_uri is None for d in rows)
    # No producer here declares a ladder: gridMET-vs-user-rate is a branch on the
    # ask inside one producer, not a fallback chain, and a declared ladder that
    # never fired would be indistinguishable from one that did.
    assert all(d.producer.ladder_rungs == () for d in rows)


def test_an_unknown_data_row_is_an_attribute_error_at_the_line_that_wrote_it():
    from trid3nt_server.workflows.telemac.templates.shared import river

    with pytest.raises(AttributeError):
        river.DATA.centreline


# ===========================================================================
# (5) The chain: dispatch + manifest overrides + layer return.
# ===========================================================================
def _install_step_mocks(captured: dict):
    from trid3nt_server.workflows.solver import solver as solver_mod
    from trid3nt_server.workflows.telemac.products import postprocess_telemac as pp_mod
    from trid3nt_server.workflows.telemac import release_layer as rel_mod
    from trid3nt_server.workflows.telemac import results_mesh_seam as seam_mod
    from trid3nt_server.workflows.shared import run_products as products_mod
    from trid3nt_server.workflows.telemac.products import products as prod_mod
    from trid3nt_server.workflows.mesh import step as mesh_step
    from trid3nt_server.workflows.telemac.helpers import reach as reach_mod
    from trid3nt_server.workflows.telemac.helpers import forcing as forcing_mod
    from trid3nt_server.workflows.telemac.solving import solve as solve_mod
    from trid3nt_server.workflows.telemac.authoring import assembler as asm_mod
    from trid3nt_server.workflows.telemac.authoring import serializer as ser_mod

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
        nothing to the timestep and this chain's steering file is the historical one.
        """
        from trid3nt_server.workflows.mesh.artifact import MeshArtifact

        from trid3nt_server.workflows.mesh.artifact import measured_min_edge_m

        captured["mesh_ask"] = dict(mesh)
        artifact = MeshArtifact(
            mesh_id="MESH01", name="reach", mode="om2d",
            display_uri="s3://cache/mesh/MESH01/mesh.2dm",
            slf_uri="s3://cache/mesh/MESH01/river.slf",
            cli_uri="s3://cache/mesh/MESH01/river.cli",
            topology_uri="s3://cache/mesh/MESH01/mesh_topology.json",
            recipe_uri="s3://cache/mesh/MESH01/mesh_recipe.jsonl",
            crs_authid="EPSG:32611", has_bathymetry=True, utm_epsg=32611,
            node_count=800, element_count=1400,
            bbox=(-114.4, 42.5, -114.2, 42.7),
            # The polygon the mesh was CUT from, which is what a supplied release
            # point is tested against - the chain's own sectioned water - beside
            # the row that painted its bed.
            provenance={"recipe": {"extent": dict(mesh).get("extent")},
                        "bed_source": "cop-dem-glo-30"})
        return {"artifact": artifact, "mesh_id": artifact.mesh_id,
                "slf_uri": artifact.slf_uri, "cli_uri": artifact.cli_uri,
                "topology_uri": artifact.topology_uri,
                "display_uri": artifact.display_uri,
                "recipe_uri": artifact.recipe_uri,
                "node_count": artifact.node_count,
                "element_count": artifact.element_count,
                "min_edge_m": measured_min_edge_m(artifact),
                "provenance": dict(artifact.provenance)}

    def _fake_stage(case, run_tag, **_kw):
        captured["case"] = case
        captured["run_tag"] = run_tag
        return f"s3://cache/telemac/{run_tag}/manifest.json"

    _settle_reach = asm_mod.settle_reach

    async def _capture_settle(**kw):
        """The real settle, with what it MEASURED kept for inspection."""
        out = await _settle_reach(**kw)
        captured["settled"] = out
        return out

    def _capture_deck(sheet, rundir, *, steering=None):
        """The serializer stands in: the deck it would write is what is read here.

        Writing it is a docker round trip into the image, and what a chain test
        proves is which values reached which keyword.
        """
        captured["deck"] = dict(sheet.resolved())
        captured["deck_files"] = dict(sheet.files)
        return {"steering": steering or "t2d_river.cas"}

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
        patch.object(reach_mod, "registry_fn", _fake_registry_fn),
        patch.object(reach_mod, "river_seed_from_geometry", _fake_seed),
        # The mesh session stands in, so its display face is a uri nothing wrote:
        # what the mesh holds of the reach is measured in its own test module.
        patch.object(reach_mod, "_meshed_fraction", lambda mesh, centerline: 1.0),
        patch.object(asm_mod, "settle_reach", _capture_settle),
        patch.object(ser_mod, "serialize", _capture_deck),
        patch.object(asm_mod, "read_topology",
                     lambda _uri: {
                         "roles": dict(MESH_ROLES),
                         "liquid_boundary_order": ["outflow", "inflow"],
                         "liquid_boundary_prescribes": ["elevation",
                                                        "flowrate"]}),
        patch.object(asm_mod, "read_centerline_utm",
                     lambda _src, _epsg, **_kw: __import__("numpy").array(
                         [[0.0, 0.0], [6000.0, 0.0]])),
        patch.object(asm_mod, "_upload_authored",
                     lambda _rundir, run_tag, names, prefix: [
                         {"gs_uri": f"s3://cache/{prefix}/{run_tag}/{n}", "dest": n}
                         for n in names]),
        patch.object(asm_mod, "_write_manifest", _fake_stage),
        patch.object(mesh_step, "build_declared_mesh", _fake_mesh),
        patch.object(forcing_mod, "_nwm_nearest_streamflow",
                     lambda lon, lat, valid_time=None: {
                         "m3s": 312.0, "reference_time": "2026-01-01T12:00:00+00:00",
                         "product": "analysis_assim", "layer": None}),
        patch.object(solve_mod, "read_run_metrics",
                     lambda rid: {"utm_epsg": 32611}),
        patch.object(prod_mod, "download_result_selafin",
                     lambda rid: "/tmp/telemac/does-not-matter.slf"),
        patch.object(prod_mod, "_publish_peak_layer", _fake_publish),
        patch.object(pp_mod, "postprocess_telemac", _fake_postprocess),
        patch.object(seam_mod, "publish_results_mesh_via_seam", _amock(0)),
        patch.object(rel_mod, "publish_release_point", _capture_marker),
        patch.object(products_mod, "persist_run_products", _amock([])),
        patch.object(solver_mod, "run_solver", _fake_run_solver),
        patch.object(solver_mod, "wait_for_completion", _amock(_FakeRunResult())),
        patch.object(solver_mod, "set_emitter_binding", lambda *a, **k: None),
    ]


def _run_tool(tmp_path, monkeypatch, captured: dict, overrides=(), water=None,
              **kwargs):
    from trid3nt_server.workflows.telemac.templates.river_dye.river_dye import telemac_river_dye

    monkeypatch.setenv("TRID3NT_DEV_PERSISTENCE_DIR", str(tmp_path / "persistence"))
    install_reach_chain(monkeypatch, tmp_path, captured, water=water)
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

    settled, deck = captured["settled"], captured["deck"]
    assert settled["spill_fraction"] == pytest.approx(0.4)
    assert settled["seed_lon"] == pytest.approx(-114.31, abs=1e-4)
    assert settled["seed_lat"] == pytest.approx(42.58, abs=1e-4)
    assert captured["navigates"][0]["direction"] == "DM"
    assert captured["navigates"][0]["distance_km"] == 4.0
    # The scenario reached the KEYWORDS: the pulse's concentration at the source,
    # the horizon, and the window the sources series steps at.
    assert deck["VALUES OF THE TRACERS AT THE SOURCES"] == [pytest.approx(250.0)]
    assert deck["DURATION"] == pytest.approx(1800.0)
    series = captured["deck_files"]["river_sources.txt"].splitlines()
    assert [row.split()[0] for row in series[3:]] == [
        "0.000", "600.000", "600.100", "1900.000"]
    # The carrier discharge the NWM lookup resolved reached the boundary
    # condition, and the layer says where it came from.
    assert settled["inflow_q_m3s"] == pytest.approx(312.0)
    assert deck["PRESCRIBED FLOWRATES"] == [0.0, pytest.approx(312.0)]
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
    from trid3nt_server.workflows.telemac.helpers import reach as reach_mod

    captured: dict = {}
    peak = _run_tool(
        tmp_path, monkeypatch, captured, location="Twin Falls, Idaho",
        overrides=[patch.object(reach_mod, "river_seed_from_geometry",
                                lambda uri: None)])
    assert isinstance(peak, TelemacDyeLayerURI)
    settled = captured["settled"]
    assert settled["seed_lon"] == pytest.approx(-114.4609, abs=1e-3)
    assert settled["seed_lat"] == pytest.approx(42.5629, abs=1e-3)


def _real_centerline_read() -> dict:
    """Let the REAL centerline reader run, over the chain's own navigated line.

    The module's own stub answers with a synthetic polyline, which says nothing
    about where a release derived along the DECLARED reach actually lands.
    """
    from trid3nt_server.workflows.mesh.shared.nodes import read_centerline_utm
    from trid3nt_server.workflows.telemac.authoring import assembler as asm_mod

    return {"overrides": [patch.object(asm_mod, "read_centerline_utm",
                                       read_centerline_utm)]}


def test_the_reach_is_navigated_EXACTLY_ONCE(tmp_path, monkeypatch):
    """ONE centerline acquisition. A second navigate resolved beside the declared
    row walked a different seed for a different distance, so the line the section
    was cut between and the line the author read described different rivers - and a
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
    assert captured["settled"]["seed_lon"] == pytest.approx(-124.10)
    # the supplied point was settled against THAT centerline, and the run says so
    assert captured["settled"]["release_lon"] == pytest.approx(-124.10)
    assert captured["settled"]["release_user_supplied"] is True
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
    # ... and the run states the FRACTION rather than a user coordinate, because
    # a release row that reads "user" over a derived point is the dishonest one.
    assert captured["settled"]["spill_fraction"] == 0.5
    assert captured["settled"]["release_user_supplied"] is False


def test_a_step_failure_maps_to_the_typed_error_envelope(tmp_path, monkeypatch):
    from trid3nt_server.workflows.telemac.solving import solve as solve_mod
    from trid3nt_server.workflows.telemac.helpers.errors import TelemacDyeScenarioError

    async def _boom(**_kw):
        raise TelemacDyeScenarioError("TELEMAC_DYE_RUN_FAILED",
                                      "solve did not complete")

    captured: dict = {}
    out = _run_tool(tmp_path, monkeypatch, captured, location="Twin Falls, Idaho",
                    overrides=[patch.object(solve_mod, "solve_reach", _boom)])
    assert out["status"] == "error"
    assert out["error_code"] == "TELEMAC_DYE_RUN_FAILED"


# --- the water WINDOW, and the coverage it is judged by --------------------- #
def test_the_water_is_queried_over_the_centerline_padded_by_a_stated_distance(
        tmp_path, monkeypatch):
    """The query window has to reach a far channel behind a mid-river island, so
    it is the centerline's extent grown by a DISTANCE - three kilometres, written
    on the row - and not the line's own tight bounds."""
    from tests.reach_chain import CENTERLINE_BBOX

    captured: dict = {}
    _run_tool(tmp_path, monkeypatch, captured, location="Twin Falls, Idaho")

    asked = captured["water_bbox"]
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
    from tests.reach_chain import WATER_ELSEWHERE

    captured: dict = {}
    out = _run_tool(tmp_path, monkeypatch, captured, location="Twin Falls, Idaho",
                    water=WATER_ELSEWHERE)
    assert out["status"] == "error"
    assert out["error_code"] == "REACH_WATER_UNMAPPED"


def test_a_partly_mapped_reach_proceeds_and_says_how_much_was_mapped(
        tmp_path, monkeypatch):
    """NO invented threshold: above zero the run proceeds, carrying the MEASURED
    fraction so a reader is never left assuming the flowline-only stretches were
    modelled."""
    from tests.reach_chain import WATER_GAPPED

    captured: dict = {}
    peak = _run_tool(tmp_path, monkeypatch, captured, location="Twin Falls, Idaho",
                     water=WATER_GAPPED)
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
    from tests.reach_chain import WATER_HALF

    captured: dict = {}
    out = _run_tool(tmp_path, monkeypatch, captured, location="Twin Falls, Idaho",
                    water=WATER_HALF)
    assert out["status"] == "error"
    assert out["error_code"] == "SECTION_END_FACE_UNMEASURED"
    assert "downstream cut left no transect" in out["error_message"]


def test_an_unmapped_reach_refuses_terminally_naming_the_three_supply_paths():
    """A reach nothing maps has no domain, and no rung to retry with: the refusal
    names the three ways a domain is SUPPLIED and offers no retry args."""
    from trid3nt_server.workflows.telemac.helpers.errors import ReachWaterUnmapped

    exc = ReachWaterUnmapped()
    assert exc.error_code == "REACH_WATER_UNMAPPED"
    assert getattr(exc, "retryable", False) is False
    assert not hasattr(exc, "suggestions")
    for path in ("Draw or supply the reach polygon", "name a case layer",
                 "pick a reach with mapped water coverage"):
        assert path in str(exc)
