"""The ``telemac_river_dye`` template: its PARAMS, its DATA chain, its fill/run door.

Exercised in ISOLATION with every fetch, the solver, the store, the postprocess
and the publish mocked - no network, no docker, no engine. Pinned: registration
and metadata, the wire-arg normalization and its refusals, the declared bounds,
the sequence the door builds and where the run is HELD, and the chain end to end."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from tests._fakes.reach_chain import MESH_ROLES, install_reach_chain
from trid3nt_contracts.execution import AnswerLayerURI

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


def _fake_result() -> dict[str, Any]:
    """A solved reach the reads open: five nodes in UTM zone 11, four frames of
    ONE tracer that arrives, peaks at 97.3 mg/L in the second frame, and drains."""
    import numpy as np

    x = np.array([720000.0, 720120.0, 720000.0, 720120.0, 720060.0])
    y = np.array([4718000.0, 4718000.0, 4718110.0, 4718110.0, 4718055.0])
    dye = np.array([[0.0, 0.0, 0.0, 0.0, 0.0],
                    [10.0, 97.3, 5.0, 40.0, 60.0],
                    [2.0, 30.0, 1.0, 50.0, 20.0],
                    [0.0, 0.0, 0.0, 0.0, 0.0]])
    depth = np.full((4, 5), 2.0)
    ikle = np.array([[0, 1, 4], [1, 3, 4], [2, 3, 4], [0, 2, 4]])
    return {"varnames": ["WATER DEPTH", "DYE"], "npoin": 5, "nelem": 4,
            "nplan": 1, "npoin2": 5, "nelem2": 4, "x": x, "y": y,
            "ikle": ikle, "ikle2": ikle, "x_origin": 0, "y_origin": 0,
            "times": np.array([0.0, 420.0, 840.0, 1260.0]),
            "data": {"WATER DEPTH": depth, "DYE": dye}}


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


def _workflow():
    from trid3nt_server.tools import TOOL_REGISTRY

    return TOOL_REGISTRY["telemac_river_dye"].fn.workflow


def _norm(**kw):

    base: dict[str, Any] = {
        "location": None, "bbox": None, "release": None, "compute_class": None,
        "wind_direction_deg": None, "input_mode": "auto",
    }
    base.update(kw)
    return asyncio.run(_workflow()._normalize(base))


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


@pytest.mark.parametrize("value", [
    [-114.31, 42.58],
    "42.58,-114.31",
    {"coordinates": [-114.31, 42.58], "name": "outfall-a"},
])
def test_every_release_point_form_reaches_the_one_point_slot(value):
    from trid3nt_server.workflows.inputs import Point

    supplied, err = _norm(location="X", release=value)
    assert err is None
    got = supplied["release"]
    assert isinstance(got, Point) and (got.lon, got.lat) == (-114.31, 42.58)


def test_a_malformed_release_point_refuses_it_never_falls_back():
    from trid3nt_server.workflows.telemac.templates.river_dye.river_dye import telemac_river_dye

    out = asyncio.run(telemac_river_dye(location="X", release=[200.0, 10.0]))
    assert out["error_code"] == "TELEMAC_PARAMS_INVALID"
    out = asyncio.run(telemac_river_dye(location="X", release={"name": "x"}))
    assert out["error_code"] == "TELEMAC_PARAMS_INVALID"


def test_the_release_is_the_one_point_slot_and_it_seeds_the_reach():
    """The release names which stretch to model: the one centerline is navigated
    from it, and the wire carries no second spelling of the same point."""
    import inspect

    from trid3nt_server.tools import TOOL_REGISTRY

    wf = _workflow()
    steps = {s.label: s for s in wf.plan.declared()}
    assert steps["seed"].kwargs["supplied"].name == "release"
    assert steps["settled"].kwargs["release"].name == "release"
    wire = set(inspect.signature(TOOL_REGISTRY["telemac_river_dye"].fn).parameters)
    assert "release" in wire
    assert not {"release_coords", "release_lat", "release_lon",
                "spill_location_latlon", "reach_seed_coords"} & wire


def test_an_invented_compute_class_refuses_at_the_ladder():
    """A rung the dispatcher cannot serve is REFUSED, not quietly re-seated.

    Re-seating it gave a caller who asked for a bigger box a smaller solve, with
    nothing on any surface a reader looks at saying so."""
    supplied, err = _norm(location="X", compute_class="dye_spill")
    assert supplied == {}
    assert err["status"] == "error"
    assert err["error_code"] == "TELEMAC_COMPUTE_CLASS_UNKNOWN"
    assert "dye_spill" in err["error_message"]


def test_a_wind_bearing_wraps_rather_than_clamping():
    supplied, _ = _norm(location="X", wind_direction_deg=370.0)
    assert supplied["wind_direction_deg"] == pytest.approx(10.0)


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


def test_the_granularity_lever_reads_back_beside_the_edge_the_mesh_was_built_at():
    """An asked edge the mesher answers differently has to be readable on the answer.

    ``mesh_size_m`` is the MEASURED minimum edge of the accepted mesh, so the lever's
    own row is what makes the two comparable."""
    from trid3nt_server.workflows.runtime import merge_provenance, provenance_entries

    workflow = _workflow()
    sheet = _resolve(mesh_resolution_m=10.0)
    metrics = workflow.answer(_peak_layer(mesh_size_m=7.763).model_copy(
        update={"synthetic_inputs": merge_provenance(
            [], provenance_entries(sheet, workflow.params))}))
    assert metrics["dye_cmax_mgl"] == 4.9
    assert metrics["mesh_size_m"] == 7.763
    assert metrics["mesh_resolution_m"] == 10.0
    assert "supplied on this invocation" in metrics["mesh_resolution_note"]


def _peak_layer(**answer: Any) -> AnswerLayerURI:
    """The layer a published outputs list leads with, carrying the answer."""
    return AnswerLayerURI(
        layer_id="L", name="Peak dye concentration (reach)", layer_type="raster",
        uri="s3://runs/x.tif", quantity="dye_concentration",
        answer={"dye_cmax_mgl": 4.9, "dye_peak_time_s": 200.0, **answer})


def test_the_sequence_validates_and_holds_the_run_after_the_fill():
    from trid3nt_server.workflows.runtime import validate_plan

    wf = _workflow()
    pl = wf.plan
    validate_plan(pl, wf.params, wf.data)

    steps = list(pl.declared())
    assert [s.label for s in steps] == [
        "reach", "seed", "carrier_discharge", "mesh", "measure_mesh_coverage",
        "settled", "sheet", "solve", "outputs"]
    # The review is the door's VIEW of the sheet it just filled, so the run is
    # held on the fill itself rather than in front of a step that has not run.
    assert [s.label for s in steps if s.self_gating] == ["sheet"]
    assert steps[0].rebinds_domain          # the geocode binds the reach AOI
    assert steps[-2].consequential          # the run is the consequential node
    # The outputs step carries the template's list as values: what is read off
    # the solved run, how each is published, and what the run answers with.
    listed = steps[-1].kwargs["outputs"]
    assert [(p.kind, p.variable, p.publish) for p in listed] == [
        ("field", "T1", "animate"), ("max_over_time", "T1", "layer"),
        ("series", "T1", "chart")]
    assert steps[-1].kwargs["captions"] == {"T1": "dye concentration"}
    assert set(steps[-1].kwargs["answer"]) == {
        "dye_cmax_mgl", "dye_peak_time_s", "plume_reach_m", "active_frames",
        "mesh_size_m"}


def test_the_declared_data_is_the_chain_in_declaration_order():
    """The reach chain, restated as this template's own rows, then the rain."""
    from trid3nt_server.workflows.runtime import DataRef, data_rows
    from trid3nt_server.workflows.telemac.templates.river_dye.river_dye import DATA

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
    assert DATA.rivers == DataRef("rivers")
    # None of these is superseded by a supplied artifact.
    assert all(d.producer.supplied_uri is None for d in rows)
    # No producer here declares a ladder: gridMET-vs-user-rate is a branch on the
    # ask inside one producer, not a fallback chain, and a declared ladder that
    # never fired would be indistinguishable from one that did.
    assert all(d.producer.ladder_rungs == () for d in rows)


def test_an_unknown_data_row_is_an_attribute_error_at_the_line_that_wrote_it():
    from trid3nt_server.workflows.telemac.templates.river_dye.river_dye import DATA

    with pytest.raises(AttributeError):
        DATA.centreline


def _install_step_mocks(captured: dict):
    from trid3nt_server.emission import publish as emission_publish
    from trid3nt_server.workflows.solver import solver as solver_mod
    from trid3nt_server.workflows.publishing import cog as cog_mod
    from trid3nt_server.workflows.publishing import publish as publish_mod
    from trid3nt_server.workflows.shared import run_products as products_mod
    from trid3nt_server.workflows.telemac.modules import outputs as outputs_mod
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

    async def _fake_mesh(*, mesh, name=None, supplied=None, tool=None):
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

    async def _capture_marker(_emitter, pt, *, basis, **_kw):
        captured["release_marker"] = {"lon": pt.lon, "lat": pt.lat, "name": pt.name,
                                      "user_supplied": basis == "user"}
        return False

    def _fake_run_solver(*, solver, model_setup_uri, compute_class):
        captured["solver"] = solver
        captured["model_setup_uri"] = model_setup_uri
        captured["compute_class"] = compute_class
        return _FakeHandle()

    def _fake_download(run_id, basename, error_code=None):
        """The solved result's download: what run it was asked under is kept."""
        captured["pp_run_id"] = run_id
        captured["pp_basename"] = basename
        return "/tmp/telemac/does-not-matter.slf"

    def _fake_upload(local, run_id, bucket, *, dest_filename, **_kw):
        captured["uploaded"] = dest_filename
        return f"s3://runs/{run_id}/{dest_filename}"

    def _fake_publish_layer(*, layer_uri, layer_id, style=None, **_kw):
        captured["published"] = layer_id
        captured["publish_style"] = style
        return "https://tiles/dye_peak.png"

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
        patch.object(solve_mod, "download_result", _fake_download),
        patch.object(outputs_mod, "read_selafin", lambda _path: _fake_result()),
        patch.object(cog_mod, "upload_cog", _fake_upload),
        patch.object(emission_publish, "publish_layer", _fake_publish_layer),
        patch.object(publish_mod, "publish_results_mesh_via_seam", _amock(0)),
        patch.object(asm_mod, "publish_point", _capture_marker),
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

    assert isinstance(peak, AnswerLayerURI)
    assert peak.uri == "https://tiles/dye_peak.png"
    # The answer is read off the solved result the outputs list named: the
    # tracer's envelope peak, when it peaked, and the accepted mesh's edge.
    assert peak.answer["dye_cmax_mgl"] == pytest.approx(97.3)
    assert peak.answer["dye_peak_time_s"] == pytest.approx(420.0)
    assert peak.answer["active_frames"] == 2
    assert peak.answer["mesh_size_m"] is not None
    assert peak.quantity == "dye_concentration"
    assert peak.name.startswith("Peak dye concentration (")
    assert peak.legend is not None and peak.legend.vmax == pytest.approx(97.3)
    assert captured["published"] == "telemac-dye_concentration-TELERID"
    assert captured["uploaded"] == "dye_concentration.tif"

    # The place was GEOCODED, never hand-typed.
    assert captured["geocode_query"] == "Twin Falls, Idaho"
    assert captured["solver"] == "telemac"
    assert captured["model_setup_uri"].endswith("manifest.json")
    # The result was read under the SOLVER's run_id, so outputs land under the
    # real run prefix rather than the manifest tag.
    assert captured["pp_run_id"] == "TELERID"
    assert captured["pp_basename"] == "r2d_river.slf"

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
    # condition, and the sheet's own row says it was derived from the model.
    assert settled["inflow_q_m3s"] == pytest.approx(312.0)
    assert deck["PRESCRIBED FLOWRATES"] == [0.0, pytest.approx(312.0)]
    q_row = next(r for r in peak.synthetic_inputs if r.param == "discharge_m3s")
    assert q_row.basis == "derived" and "Water Model" in (q_row.note or "")


def test_a_prefetched_flowline_is_reused_instead_of_refetched(tmp_path, monkeypatch):
    captured: dict = {}
    provided = "s3://trid3nt-cache/cache/static-30d/river_geometry/prefetched.fgb"
    peak = _run_tool(tmp_path, monkeypatch, captured,
                     location="Twin Falls, Idaho", river_geometry_uri=provided)
    assert isinstance(peak, AnswerLayerURI)
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
    assert isinstance(peak, AnswerLayerURI)
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
    """ONE centerline acquisition.

    A second navigate walks a different seed for a different distance, so the line the
    section was cut between and the line the author read describe different rivers."""
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
              release={"coordinates": [-124.10, 40.50], "name": "outfall-a"},
              **_real_centerline_read())
    assert captured["navigates"][0]["seed_point"] == [-124.10, 40.50]
    assert captured["settled"]["seed_lon"] == pytest.approx(-124.10)
    # the supplied point was settled against THAT centerline, and the run says so
    assert captured["settled"]["release_lon"] == pytest.approx(-124.10)
    assert captured["settled"]["release_user_supplied"] is True
    assert captured["settled"]["release_name"] == "outfall-a"
    assert captured["release_marker"]["user_supplied"] is True
    # the picked name is what the deck calls the tracer; its unit stays
    assert captured["deck"]["NAMES OF TRACERS"] == ["outfall-a       MG/L"]


def test_an_unnamed_release_keeps_the_template_s_own_tracer_name(
        tmp_path, monkeypatch):
    captured: dict = {}
    _run_tool(tmp_path, monkeypatch, captured, location="Twin Falls, Idaho",
              release=[-124.10, 40.50], **_real_centerline_read())
    assert captured["settled"]["release_name"] is None
    assert captured["deck"]["NAMES OF TRACERS"] == ["DYE             MG/L"]


def test_a_derived_release_sits_on_the_DECLARED_centerline(tmp_path, monkeypatch):
    """No release point placed -> the source walks spill_fraction along the same
    line the mesh was built over, which is what makes it inside the domain by
    construction rather than by luck."""
    from shapely.geometry import LineString, Point

    from tests._fakes.reach_chain import CENTERLINE

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
    from tests._fakes.reach_chain import CENTERLINE_BBOX

    captured: dict = {}
    _run_tool(tmp_path, monkeypatch, captured, location="Twin Falls, Idaho")

    from pyproj import Geod

    geod = Geod(ellps="WGS84")
    asked = captured["water_bbox"]
    # The window is asserted in METRES, the unit the row states the pad in. The
    # straight test stretch has zero height, so the tool's own degenerate-layer
    # floor (0.001 deg) rides under the pad; the window still reaches the full
    # 3 km on every side, and at most the floor further.
    lon = CENTERLINE_BBOX[0]
    floor_m = geod.inv(lon, CENTERLINE_BBOX[1], lon, CENTERLINE_BBOX[1] - 0.001)[2]
    south_m = geod.inv(lon, CENTERLINE_BBOX[1], lon, asked[1])[2]
    north_m = geod.inv(lon, CENTERLINE_BBOX[3], lon, asked[3])[2]
    # A decimetre of slack on a 3 km pad: the pad is one degree offset applied to
    # BOTH edges, and a degree is a slightly different distance at each of them.
    slack_m = 0.1
    assert 3000.0 <= south_m <= 3000.0 + floor_m + slack_m
    assert 3000.0 <= north_m <= 3000.0 + floor_m + slack_m
    # The pad is a DISTANCE: the same 3 km costs more degrees of longitude at
    # 40.5 N than it does of latitude.
    assert (CENTERLINE_BBOX[0] - asked[0]) > (asked[3] - CENTERLINE_BBOX[3])
    assert (asked[2] - CENTERLINE_BBOX[2]) > (asked[3] - CENTERLINE_BBOX[3])


def test_a_reach_no_polygon_maps_refuses_as_unmapped_not_as_an_empty_section(
        tmp_path, monkeypatch):
    """The measurement sits between the fetch and the cut, so a reach nothing maps
    fails on its own cause instead of arriving at the section as empty geometry."""
    from tests._fakes.reach_chain import WATER_ELSEWHERE

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
    from tests._fakes.reach_chain import WATER_GAPPED

    captured: dict = {}
    peak = _run_tool(tmp_path, monkeypatch, captured, location="Twin Falls, Idaho",
                     water=WATER_GAPPED)
    assert isinstance(peak, AnswerLayerURI)
    note = peak.fallback_note or ""
    assert "50.0%" in note
    assert "flowline" in note


def test_a_reach_whose_far_END_is_unmapped_refuses_at_the_cut(
        tmp_path, monkeypatch):
    """Coverage above zero is not the same fact as a domain with two transects.

    Mapped water that stops halfway leaves the downstream end on the polygon's own
    bank, and a boundary role cannot be prescribed across an edge the cut never made."""
    from tests._fakes.reach_chain import WATER_HALF

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
