"""Offline unit tests for the telemac_rain_on_grid engine template.

No solver / no network: registration shape, the declared step sequence, the
wire-signature door contract, the settle over an accepted mesh and the deck the
fill states over it. Live end-to-end (mesh acquisition + solve + depth COG) is
the telemac_rain_on_grid canary and its refined variant, whose packets are the
evidence.
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest


def test_registered_on_the_model_surface():
    """The catchment front is LIVE: its outlet boundary is declared on the mesh
    ask and the hydrograph is read off the run's own listing, so nothing is left
    for a park to state."""
    import trid3nt_server.main as _main
    from trid3nt_server.workflows.telemac.templates.rain_on_grid.rain_on_grid import (
        telemac_rain_on_grid,
    )

    _main._import_tools_registry()
    from trid3nt_server.tools import TOOL_REGISTRY

    assert "telemac_rain_on_grid" in TOOL_REGISTRY
    assert telemac_rain_on_grid.parked is None

    md = telemac_rain_on_grid.workflow.metadata
    assert md.engine == "telemac"
    assert md.tier == "template"
    assert md.cacheable is False
    assert md.ttl_class == "live-no-cache"
    assert md.source_class == "workflow_dispatch"
    specs = {r.param for r in (md.resolution_specs or ())}
    assert "mesh_min_edge_m" in specs


def test_the_outlet_boundary_is_declared_as_an_op_on_the_mesh_recipe():
    """The one liquid boundary a catchment has is DECLARED where every other boundary
    role is - as a ``set_boundary_roles`` op on the recipe, at the delineation's
    snapped outlet - rather than resolved by a server step between mesh and authoring."""
    from trid3nt_server.workflows.telemac.templates.rain_on_grid.rain_on_grid import MESH

    op = [o for o in MESH.ops if o.fn == "set_boundary_roles"]
    assert len(op) == 1
    roles = dict(op[0].kwargs)
    assert set(roles) == {"rating_curve"}
    assert roles["rating_curve"]["type"] == "Point"
    assert roles["rating_curve"]["coordinates"].path == "basin.snapped_pour_point"


def test_docstring_carries_the_godara_envelope():
    import trid3nt_server.workflows.telemac.templates.rain_on_grid.rain_on_grid as rog_module
    from trid3nt_server.workflows.telemac.templates.rain_on_grid.rain_on_grid import (
        telemac_rain_on_grid,
    )

    # the applicability envelope's citation lives on the module's own docstring.
    assert "Godara" in (rog_module.__doc__ or "")

    # and the applicability CLASS (single-storm, small steep catchments) rides the
    # model-facing rendered docstring, not just an internal comment.
    doc = telemac_rain_on_grid.__doc__ or ""
    assert "RAIN" in doc and "watershed" in doc.lower()
    assert "hydrograph" in doc.lower()
    assert "SCS" in doc or "curve-number" in doc.lower() or "curve number" in doc.lower()
    assert "SINGLE-STORM" in doc and "steep catchments" in doc


def test_corpus_yaml_present_and_routes():
    from pathlib import Path

    import yaml

    import trid3nt_server.workflows.telemac.templates.rain_on_grid as pkg

    corpus = Path(pkg.__file__).parent / "corpus.yaml"
    assert corpus.exists()
    data = yaml.safe_load(corpus.read_text())
    assert "telemac_rain_on_grid" in data
    assert any("runoff" in q.lower() for q in data["telemac_rain_on_grid"])


def test_the_declared_plan_is_the_rain_on_grid_sequence():
    """aoi -> mesh -> settled -> sheet -> solve -> outputs, and the sequence
    VALIDATES against its own declared params and data. The infiltration surface
    is no step of its own: the fill produces it."""
    from trid3nt_server.workflows.runtime.validate import validate_plan
    from trid3nt_server.workflows.telemac.templates.rain_on_grid.rain_on_grid import (
        telemac_rain_on_grid,
    )

    workflow = telemac_rain_on_grid.workflow
    plan = workflow.plan
    assert [step.label for step in plan.declared()] == [
        "aoi", "mesh", "settled", "sheet", "solve", "outputs"]
    validate_plan(plan, workflow.params, workflow.data)


def test_the_outputs_are_the_depth_and_the_flux_across_the_outlet():
    """The depth animates and its envelope is the map; the flux the engine printed
    across the outlet is charted and placed as the station that carries it, under
    the one name the calibration seam pairs against a gauge."""
    from trid3nt_server.workflows.publishing import quantity_of
    from trid3nt_server.workflows.telemac.templates.rain_on_grid.rain_on_grid import (
        ANSWER, CAPTIONS, OUTPUTS,
    )

    assert [(p.kind, p.variable, p.publish) for p in OUTPUTS] == [
        ("field", "H", "animate"), ("max_over_time", "H", "layer"),
        ("series", "FLUX", "chart"), ("series", "FLUX", "station")]
    assert quantity_of(CAPTIONS["FLUX"]) == "outlet_hydrograph"
    assert {"peak_discharge_m3s", "runoff_volume_m3", "rainfall_volume_m3",
            "runoff_coefficient", "max_depth_p99_m",
            "peak_is_window_truncated"} <= set(ANSWER)


def test_constant_door_params_off_wire_scenario_and_user_ones_present():
    """CONSTANT-door params never reach the model-facing schema; the scenario and user
    ones - the storm, the granularity lever, the mesh slot - do."""
    import inspect

    from trid3nt_server.workflows.runtime import doors
    from trid3nt_server.workflows.telemac.templates.rain_on_grid.rain_on_grid import (
        telemac_rain_on_grid as fn,
    )

    constants = {p.name for p in fn.workflow.params if p.door == doors.CONSTANT}
    assert constants, "telemac_rain_on_grid declares no constants; the check is vacuous"
    wire = set(inspect.signature(fn).parameters)
    assert not (constants & wire), f"puts {constants & wire} on the model-facing wire"

    scenario_or_user = {p.name for p in fn.workflow.params
                        if p.door in (doors.SCENARIO, doors.USER)}
    assert scenario_or_user <= wire
    assert {"pour_point", "mesh_min_edge_m", "antecedent_moisture",
           "design_storm_mm_per_hr"} <= wire
    # No "mesh" slot: a supplied mesh reaches a run through the mesh ROUTER at
    # the build door, not through a template's own context slot - a second
    # resolver inside a model template is the silent-adoption defect D-9 forbids.
    assert "mesh" not in wire


def test_resolve_rain_event_design_storm_rung_no_window():
    from trid3nt_server.workflows.telemac.templates.rain_on_grid.storm import (
        resolve_rain_event,
    )

    # no sim_duration_hr asked -> the storm's OWN duration stands.
    out = resolve_rain_event(window=None, intensity_mm_per_hr=25.0,
                             storm_duration_hr=6.0, sim_duration_hr=None)
    assert out["kind"] == "design_storm"
    assert out["blocks"] is None and out["series"] is None
    assert out["time_varying"] is False
    assert out["duration_s"] == 6.0 * 3600.0
    assert out["duration_basis"] == "storm"

    # sim_duration_hr asked -> it wins over the storm's own duration.
    out2 = resolve_rain_event(window=None, intensity_mm_per_hr=25.0,
                              storm_duration_hr=6.0, sim_duration_hr=10.0)
    assert out2["duration_s"] == 10.0 * 3600.0
    assert out2["duration_basis"] == "user"


def test_resolve_rain_event_malformed_window_refuses():
    from trid3nt_server.workflows.runtime.domain import (
        Domain,
        bind_domain,
        reset_domain,
    )
    from trid3nt_server.workflows.telemac.errors import TelemacError
    from trid3nt_server.workflows.telemac.templates.rain_on_grid.storm import (
        resolve_rain_event,
    )

    token = bind_domain(Domain(bbox=(-83.47, 35.02, -83.36, 35.10)))
    try:
        with pytest.raises(TelemacError) as ei:
            resolve_rain_event(window="no-separator", intensity_mm_per_hr=25.0,
                               storm_duration_hr=6.0, sim_duration_hr=None)
        assert ei.value.error_code == "TELEMAC_RAIN_WINDOW_INVALID"
    finally:
        reset_domain(token)


def _hyetograph(monkeypatch, precip_mm: list[float]) -> dict:
    from trid3nt_server.tools import TOOL_REGISTRY
    from trid3nt_server.workflows.runtime.domain import (
        Domain,
        bind_domain,
        reset_domain,
    )
    from trid3nt_server.workflows.telemac.templates.rain_on_grid.storm import (
        resolve_rain_event,
    )

    monkeypatch.setitem(
        TOOL_REGISTRY, "fetch_aorc_precip",
        type("S", (), {"fn": staticmethod(
            lambda **kw: {"precip_mm": precip_mm})})())
    token = bind_domain(Domain(bbox=(-83.47, 35.02, -83.42, 35.06)))
    try:
        return resolve_rain_event(window="2015-12-23/2015-12-24",
                                  intensity_mm_per_hr=25.0, storm_duration_hr=6.0,
                                  sim_duration_hr=None)
    finally:
        reset_domain(token)


def test_resolve_rain_event_hyetograph_rung_builds_hourly_blocks(monkeypatch):
    """A real window fetches the hourly record and drives the run with it."""
    out = _hyetograph(monkeypatch, [3.0, 12.5, 0.0])
    assert out["kind"] == "hyetograph"
    assert out["series"] == [3.0, 12.5, 0.0]
    assert out["blocks"] == [[3600.0, 3.0], [7200.0, 12.5], [10800.0, 0.0]]
    assert out["duration_s"] == 3 * 3600.0   # hyetograph span dominates the no-ask
    assert out["time_varying"] is True


def test_a_record_of_one_rate_is_a_constant_storm_with_a_date_on_it(monkeypatch):
    """Two distinct wet rates are a shape only the block file states; one rate
    is the engine's own constant branch, whatever the record's length."""
    assert _hyetograph(monkeypatch, [5.0, 5.0, 5.0, 0.0])["time_varying"] is False
    assert _hyetograph(monkeypatch, [2.0, 8.0, 15.0, 6.0, 1.0])["time_varying"] is True


@pytest.mark.asyncio
async def test_the_aoi_is_the_box_around_the_outlet_not_the_geocoded_place():
    """A supplied pour point derives the AOI, never a geocoded place bbox: a town
    box need not contain the upstream catchment, so the acquisition never reaches
    for a geocoder and the basin's shape is the terrain's answer."""
    from trid3nt_server.workflows.inputs import Point
    from trid3nt_server.workflows.inputs.aoi import acquire_aoi
    from trid3nt_server.workflows.telemac.templates.rain_on_grid.declarations import (
        POUR_POINT_BUFFER_DEG,
    )

    pp = (-83.40402, 35.05746)
    out = await acquire_aoi(location="Otto, North Carolina", bbox=None,
                            around=Point(*pp, "outlet"),
                            half_deg=POUR_POINT_BUFFER_DEG, default_name="watershed")
    aoi = out["bbox"]
    assert aoi[0] < pp[0] < aoi[2] and aoi[1] < pp[1] < aoi[3]
    # each side under the 0.3-deg D8 clamp, centred on the outlet
    assert (aoi[2] - aoi[0]) <= 0.3 and (aoi[3] - aoi[1]) <= 0.3
    assert abs((aoi[2] - aoi[0]) - 2 * POUR_POINT_BUFFER_DEG) < 1e-9
    assert (out["lon"], out["lat"]) == pp
    assert out["name"] == "Otto, North Carolina" and out["slug"] == "otto_north_carolina"


def _accepted_catchment_mesh():
    """The mesh step's record for an accepted catchment, as the author reads it."""
    from types import SimpleNamespace

    return {
        "artifact": SimpleNamespace(
            utm_epsg=32617, bbox=(-83.47, 35.02, -83.36, 35.10),
            name="coweeta creek",
            probes={"area_km2": 2.5, "edge_length_m": {"min": 40.0, "max": 300.0}}),
        "mesh_id": "M1", "slf_uri": "s3://cache/mesh/M1/mesh.slf",
        "cli_uri": "s3://cache/mesh/M1/mesh.cli",
        "topology_uri": "s3://cache/mesh/M1/mesh_topology.json",
        "display_uri": "s3://cache/mesh/M1/mesh.2dm",
        "node_count": 4, "element_count": 2, "min_edge_m": 40.0,
        "provenance": {"bed_source": "3dep 100%", "sizing_source": "nhdplus_hr",
                       "domain_source": "supplied polygon domain (1 part(s))"},
    }


#: Four nodes over a bed that FALLS toward the outlet nodes (1 and 3): the
#: outlet's rating curve is a uniform-flow depth, so a flat ground has no stage
#: to hold. The land cover under them: forest, urban, water, and a class the
#: table does not carry.
_NODES = (np.array([[0.0, 0.0], [20.0, 0.0], [0.0, 10.0], [20.0, 10.0]]),
          np.array([[0, 1, 2], [1, 3, 2]]),
          np.array([1.0, 0.8, 1.2, 1.0]),
          np.array([[-83.4, 35.0], [-83.39, 35.0], [-83.4, 35.01], [-83.39, 35.01]]))
_CLASSES = {(-83.4, 35.0): 42, (-83.39, 35.0): 24, (-83.4, 35.01): 11,
            (-83.39, 35.01): 999}


@pytest.fixture()
def rog_run(monkeypatch, tmp_path):
    """``settle_catchment`` and the fill over it, with the accepted mesh's reads
    and the land-cover raster stood in for.

    The DECK is real: every keyword and every file the template states is resolved
    here, without the container the serializer writes them through."""
    import trid3nt_server.workflows.mesh.shared.nodes as nodes_mod
    from trid3nt_server.workflows.telemac.authoring import assembler as asm_mod

    monkeypatch.setenv("TRID3NT_RUNS_DIR", str(tmp_path))
    monkeypatch.setattr(asm_mod, "read_topology", lambda _uri: {
        "roles": {"rating_curve": [1, 3]},
        "liquid_boundary_order": ["rating_curve"],
        "liquid_boundary_prescribes": ["elevation"]})
    monkeypatch.setattr(nodes_mod, "read_accepted_mesh_nodes",
                        lambda _uri, utm_epsg=None: _NODES)
    monkeypatch.setattr(
        nodes_mod, "sample_raster_at_nodes",
        lambda _path, lonlat, interp="nearest": np.array(
            [_CLASSES[(round(float(a), 2), round(float(b), 2))] for a, b in lonlat],
            dtype=float))
    monkeypatch.setattr("trid3nt_server.tools.cache.read_object_bytes_s3",
                        lambda _uri: b"")

    async def _fill(rain: dict, **params):
        from trid3nt_server.workflows.telemac.modules import fill
        from trid3nt_server.workflows.telemac.templates.rain_on_grid.declarations import (
            LANDCOVER_CN_MANNING, LANDCOVER_UNMAPPED,
        )
        from trid3nt_server.workflows.telemac.templates.rain_on_grid.rain_on_grid import (
            STEERING,
        )

        mesh = _accepted_catchment_mesh()
        settled = await asm_mod.settle_catchment(
            catchment=mesh, rain=rain, landcover={"uri": "s3://cache/lc.tif"},
            roughness=LANDCOVER_CN_MANNING, unmapped=LANDCOVER_UNMAPPED,
            time_step_s=3.0)
        sheet = fill(STEERING, produced={"settled": settled, "mesh": mesh},
                     params={"time_step_s": 3.0, "curve_number": None,
                             "steep_slope_correction": False,
                             "antecedent_moisture": "normal", **params})
        return settled, sheet

    return _fill


_DESIGN_STORM = {"kind": "design_storm", "intensity_mm_per_hr": 25.0,
                 "duration_s": 21600.0, "rain_duration_s": 21600.0,
                 "series": None, "blocks": None, "time_varying": False,
                 "note": "a CONSTANT design storm", "duration_basis": "storm"}


def test_a_constant_storm_states_a_deck_and_names_no_fortran(rog_run):
    settled, sheet = asyncio.run(rog_run(_DESIGN_STORM))
    facts = settled["server_facts"]
    assert facts["utm_epsg"] == 32617
    assert facts["npoin"] == 4 and facts["nelem"] == 2
    assert facts["result_slf"] == "r2d_rog.slf"

    deck = dict(sheet.resolved())
    assert deck["GEOMETRY FILE"] == "rog.slf"
    assert deck["BOUNDARY CONDITIONS FILE"] == "rog.cli"
    assert deck["RESULTS FILE"] == "r2d_rog.slf"
    assert deck["FORMATTED DATA FILE 2"] == "rog_cn_map.dat"
    assert deck["RAINFALL-RUNOFF MODEL"] == 1
    assert deck["ANTECEDENT MOISTURE CONDITIONS"] == 2
    # a constant-rain run compiles nothing, so the keyword is ABSENT: the
    # worker's strict gate reads a present one as a directory it must compile.
    assert "FORTRAN FILE" not in deck
    # The level the outlet holds comes from the DERIVED curve beside the deck,
    # at the number the mesh's own walk gave that boundary.
    assert deck["STAGE-DISCHARGE CURVES"] == [1]
    assert deck["STAGE-DISCHARGE CURVES FILE"] == "rog_rating.txt"
    assert "PRESCRIBED ELEVATIONS" not in deck
    assert "PRESCRIBED FLOWRATES" not in deck
    assert sheet.files["rog_rating.txt"].splitlines()[0].startswith(
        "#derived Z(Q) at liquid boundary 1")
    # every file the deck names is stated beside it
    assert set(sheet.files) == {"rog_cn_map.dat", "rog_friction.tbl",
                                "rog_zones.dat", "rog_rating.txt"}


def test_the_infiltration_surface_is_read_off_the_land_cover_at_the_fill(rog_run):
    """The curve number and the roughness at every node come off ONE table for
    the class under it, an unmapped class takes the open-land row, and the sheet
    records the surface as the composite's own."""
    _settled, sheet = asyncio.run(rog_run(_DESIGN_STORM))
    cn_rows = sheet.files["rog_cn_map.dat"].splitlines()[1:]
    assert [row.split()[2] for row in cn_rows] == ["80.000", "89.000", "100.000",
                                                    "75.000"]
    zones = dict(line.split() for line in sheet.files["rog_zones.dat"].splitlines())
    laws = {line.split()[0]: line.split()[2]
            for line in sheet.files["rog_friction.tbl"].splitlines()
            if line[:1].isdigit()}
    assert [laws[zones[str(i)]] for i in range(1, 5)] == ["0.200", "0.100", "0.040",
                                                          "0.050"]
    deck = dict(sheet.resolved())
    assert deck["LAW OF BOTTOM FRICTION"] == 4
    assert str(sheet.filled["FORMATTED_DATA_FILE_2"].provenance) == \
        "producer: infiltration"
    assert str(sheet.filled["ZONES_FILE"].provenance) == "producer: infiltration"


def test_a_uniform_curve_number_overrides_the_field_and_keeps_the_roughness(rog_run):
    _settled, sheet = asyncio.run(rog_run(_DESIGN_STORM, curve_number=70.0,
                                          antecedent_moisture="wet"))
    cn_rows = sheet.files["rog_cn_map.dat"].splitlines()[1:]
    assert {row.split()[2] for row in cn_rows} == {"70.000"}
    assert "0.200" in sheet.files["rog_friction.tbl"]
    assert dict(sheet.resolved())["ANTECEDENT MOISTURE CONDITIONS"] == 3


def test_the_outlets_curve_is_derived_under_the_roughness_the_deck_writes_there(rog_run):
    """The outlet nodes sit on urban and unmapped ground, so the curve's
    coefficient is the median of the roughnesses the zones file gives them."""
    settled, _sheet = asyncio.run(rog_run(_DESIGN_STORM))
    assert "normal depth over the measured outlet section at Manning 0.075" in \
        settled["rating"]["note"]


def test_the_outputs_are_exactly_what_the_run_writes_and_was_handed(rog_run):
    """What a reader may OPEN is the sheet's own answer plus the mesh it was
    handed and the deck it read - nothing listed that the run does not carry."""
    from trid3nt_server.tools import TOOL_REGISTRY

    settled, sheet = asyncio.run(rog_run(_DESIGN_STORM))
    door = TOOL_REGISTRY["telemac_rain_on_grid"].fn.workflow.plan_decl
    handed = [row["dest"] for row in settled["mesh_inputs"]]
    assert {*door.results, *handed, door.steering_file, "full_listing.log",
            "telemac_metrics.json", *sheet.files} == {
        "r2d_rog.slf", "rog.slf", "rog.cli", "full_listing.log",
        "telemac_metrics.json", "t2d_rog.cas", "rog_cn_map.dat",
        "rog_friction.tbl", "rog_zones.dat", "rog_rating.txt"}


def test_a_time_varying_storm_names_the_baked_fortran_on_both_channels(rog_run):
    from trid3nt_server.workflows.telemac.templates.rain_on_grid.rain_on_grid import (
        RAINDEF3_USER_FORTRAN,
    )

    settled, sheet = asyncio.run(rog_run({
        "kind": "hyetograph", "intensity_mm_per_hr": 25.0, "duration_s": 10800.0,
        "series": [3.0, 12.5, 0.0], "time_varying": True,
        "note": "the REAL hourly AORC hyetograph",
        "duration_basis": "hyetograph", "window": "2015-12-23/2015-12-24",
        "blocks": [[3600.0, 3.0], [7200.0, 12.5], [10800.0, 0.0]]}))
    deck = dict(sheet.resolved())
    assert deck["FORTRAN FILE"] == RAINDEF3_USER_FORTRAN
    assert deck["FORMATTED DATA FILE 1"] == "rog_hyeto.txt"
    assert "rog_hyeto.txt" in sheet.files
    assert settled["hyetograph_total_mm"] == 15.5


def test_the_settle_records_where_each_liquid_boundary_sits(rog_run):
    """The role, resolved against the numbering the SOLVER uses - so the flux the
    listing prints under that number is the one a series at the outlet reads -
    with the centroid of its nodes in the mesh's own metres."""
    settled, _sheet = asyncio.run(rog_run(_DESIGN_STORM))
    assert settled["outlet_boundary"] == 1
    assert settled["liquid_boundaries"] == [
        {"number": 1, "role": "rating_curve", "x": 20.0, "y": 5.0}]
    assert settled["landcover"] == {"uri": "s3://cache/lc.tif"}


def test_a_mesh_whose_boundary_took_no_outlet_role_refuses(rog_run, monkeypatch):
    from trid3nt_server.workflows.telemac.authoring import assembler as asm_mod
    from trid3nt_server.workflows.telemac.errors import TelemacError

    monkeypatch.setattr(asm_mod, "read_topology", lambda _uri: {
        "roles": {"inflow": [0]}, "liquid_boundary_order": ["inflow"],
        "liquid_boundary_prescribes": ["flowrate"]})
    with pytest.raises(TelemacError) as ei:
        asyncio.run(rog_run(_DESIGN_STORM))
    assert ei.value.error_code == "TELEMAC_OUTLET_UNSET"
