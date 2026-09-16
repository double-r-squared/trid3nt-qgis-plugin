"""Offline unit tests for the telemac_rain_on_grid engine template.

No solver / no network: registration shape, the domain and bed slots it stands
on, the mesh band it states for itself, the declared step sequence, and the deck
the fill writes over an accepted mesh. Live end-to-end (the traced catchment +
solve + hydrograph) is the telemac_rain_on_grid canary and its refined variant,
whose packets are the evidence.
"""

from __future__ import annotations

import numpy as np
import pytest


def test_registered_on_the_model_surface():
    """The catchment front is LIVE, and the granularity it declares is the
    runtime's own lever at this question's own default."""
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
    assert specs == {"mesh_resolution_m"}


def _rows():
    from trid3nt_server.workflows.runtime import data_rows
    from trid3nt_server.workflows.telemac.templates.rain_on_grid.rain_on_grid import (
        DATA,
    )

    return {row.name: row for row in data_rows(DATA)}


def test_the_catchment_is_one_domain_row_its_producer_traced():
    """The five-row reach chain is one slot: a pour point in, the basin and the
    outlet run it drains through out. A drawn basin fills the same slot."""
    rows = _rows()
    domain = rows["domain"]
    assert domain.role == "domain" and domain.geometry == "polygon"
    assert domain.producer.runner == "fetch_watershed"
    assert domain.fills_from_user


def test_the_bed_is_one_row_on_the_ground_the_trace_ran_over():
    """ONE GROUND: the basin is delineated on the same pinned bare-earth product
    at the same cell the nodes are painted from, so the routing and the
    elevations cannot describe two different grounds."""
    from trid3nt_server.workflows.telemac.templates.rain_on_grid.rain_on_grid import (
        _TERRAIN_RESOLUTION_M,
        _TERRAIN_SOURCE,
    )

    rows = _rows()
    bed = rows["bed"]
    assert bed.role == "bed"
    assert bed.producer.runner == "fetch_dem"
    assert bed.producer.kwargs["source"] == _TERRAIN_SOURCE == "3dep"
    assert bed.producer.kwargs["resolution_m"] == _TERRAIN_RESOLUTION_M
    traced = rows["domain"].producer.kwargs
    assert traced["dem_source"] == _TERRAIN_SOURCE
    assert traced["resolution_m"] == _TERRAIN_RESOLUTION_M
    # exactly one bed row: the workflow's own stages refuse a second.
    assert [r.name for r in _rows().values() if r.role == "bed"] == ["bed"]


def test_the_outlet_is_the_run_the_domain_carries_not_a_point_this_template_places():
    """The catchment's one liquid boundary comes off the RUNS slot - the stretch
    of the divide the producer measured, typed rating_curve, or the run the user
    drew - so nothing downstream branches on which way it arrived."""
    from trid3nt_server.workflows.telemac.templates.rain_on_grid.rain_on_grid import (
        MESH,
    )

    op = [o for o in MESH.ops if o.fn == "set_boundary_roles"]
    assert len(op) == 1
    assert set(op[0].kwargs) == {"runs"}
    assert op[0].kwargs["runs"].path == "runs"


def test_the_mesh_is_a_band_whose_rim_is_locked_at_the_size_word():
    """A catchment is triangulated fine along its channels and coarse on the
    hillslopes, so this question states its own recipe; the rim is locked
    between the sizing and the gradation, because no sizing function the library
    has measures the domain's own outline."""
    from trid3nt_server.workflows.telemac.templates.rain_on_grid.rain_on_grid import (
        MESH,
    )

    assert [o.fn for o in MESH.ops] == [
        "distance_sizing_from_line_function", "set_rim_size",
        "enforce_mesh_gradation", "delete_boundary_faces",
        "delete_faces_connected_to_one_face",
        "make_mesh_boundaries_traversable", "fix_mesh", "set_bed",
        "set_boundary_roles"]
    assert MESH.extent.path == "domain"
    assert MESH.resolution_m.name == "mesh_resolution_m"
    sizing = next(o for o in MESH.ops if o.fn == "distance_sizing_from_line_function")
    assert sizing.kwargs["line_file"].path == "rivers"
    assert sizing.kwargs["max_edge_length"].name == "mesh_max_edge_m"
    bed = next(o for o in MESH.ops if o.fn == "set_bed")
    assert bed.kwargs["source"].path == "bed"
    assert bed.kwargs["condition"] == "pit_fill"


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
    assert "RAIN" in doc and "catchment" in doc.lower()
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
    """The workflow owns its stages off the domain and bed slots; what this
    template adds is the outlet's own curve. mesh -> outlet -> settled -> sheet
    -> solve -> outputs, and the sequence VALIDATES against its own declared
    params and data."""
    from trid3nt_server.workflows.runtime.validate import validate_plan
    from trid3nt_server.workflows.telemac.templates.rain_on_grid.rain_on_grid import (
        telemac_rain_on_grid,
    )

    workflow = telemac_rain_on_grid.workflow
    plan = workflow.plan
    assert [step.label for step in plan.declared()] == [
        "mesh", "outlet", "settled", "sheet", "solve", "outputs"]
    assert workflow.plan_decl.owns_stages
    validate_plan(plan, workflow.params, workflow.data)


def test_the_outputs_are_the_flux_across_the_outlet_the_user_placed():
    """The depth is the module's to publish; what this template lists is the read
    the user gives a place - the flux the engine printed across the outlet,
    charted and placed as the station that carries it, under the one name the
    calibration seam pairs against a gauge."""
    from trid3nt_server.render.formats import quantity_of
    from trid3nt_server.workflows.telemac.templates.rain_on_grid.rain_on_grid import (
        ANSWER, CAPTIONS, OUTPUTS,
    )

    assert [(p.kind, p.variable, p.publish) for p in OUTPUTS] == [
        ("series", "FLUX", "chart"), ("series", "FLUX", "station")]
    assert quantity_of(CAPTIONS["FLUX"]) == "outlet_hydrograph"
    assert {"peak_discharge_m3s", "runoff_volume_m3", "rainfall_volume_m3",
            "runoff_coefficient", "max_depth_p99_m",
            "peak_is_window_truncated"} <= set(ANSWER)


def test_constant_door_params_off_wire_and_the_two_slots_on_it():
    """CONSTANT-door params never reach the model-facing schema; the question's
    own scenario and user rows do, and so do the domain and bed slots - a basin
    the user draws and a survey they own supersede the producers this template
    prefers."""
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
    assert {"pour_point", "mesh_resolution_m", "antecedent_moisture",
            "design_storm_mm_per_hr", "rain_series_mm", "domain", "bed"} <= wire
    # No "mesh" slot: a supplied mesh reaches a run through the mesh ROUTER at
    # the build door, not through a template's own context slot - a second
    # resolver inside a model template is the silent-adoption defect D-9 forbids.
    assert "mesh" not in wire


def test_no_domain_twin_or_keyword_twin_is_declared():
    """A template declares no param the module's dictionary or the runtime
    already describes: the AOI it used to carry is the domain slot, the cadence
    and the step are the deck's keywords, the granularity is the runtime's."""
    from trid3nt_server.workflows.telemac.templates.rain_on_grid.rain_on_grid import (
        telemac_rain_on_grid as fn,
    )

    declared = {p.name for p in fn.workflow.params}
    assert not (declared & {"location", "bbox", "output_interval_min",
                            "time_step_s", "mesh_min_edge_m", "mesh_grade",
                            "bed_dem_resolution_m", "river_source",
                            "landcover_dataset", "rain_window",
                            "sim_duration_hr"})


_NODES = (np.array([[0.0, 0.0], [20.0, 0.0], [0.0, 10.0], [20.0, 10.0]]),
          np.array([[0, 1, 2], [1, 3, 2]]),
          np.array([1.0, 0.8, 1.2, 1.0]),
          np.array([[-83.4, 35.0], [-83.39, 35.0], [-83.4, 35.01], [-83.39, 35.01]]))
#: The land cover under those four nodes: forest, urban, water, and a class the
#: table does not carry.
_CLASSES = {(-83.4, 35.0): 42, (-83.39, 35.0): 24, (-83.4, 35.01): 11,
            (-83.39, 35.01): 999}

#: What the outlet's own step hands the deck: the boundary that holds the curve,
#: how many the solver walks, and the Z(Q) rows themselves.
_OUTLET = {"at_boundary": 1, "of_boundaries": 1,
           "rows": [[0.0, 0.8], [2.5, 1.1], [5.0, 1.4]],
           "note": "derived Z(Q) at liquid boundary 1"}


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
                       "domain_source": "fetch_watershed basin (1 part(s))"},
    }


@pytest.fixture()
def rog_run(monkeypatch, tmp_path):
    """The seated ``open_water`` and the fill over it, with the accepted
    mesh's reads and the land-cover raster stood in for.

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
    monkeypatch.setattr(asm_mod, "read_accepted_mesh_nodes",
                        lambda _uri, utm_epsg=None: _NODES)
    monkeypatch.setattr(
        nodes_mod, "sample_raster_at_nodes",
        lambda _path, lonlat, interp="nearest": np.array(
            [_CLASSES[(round(float(a), 2), round(float(b), 2))] for a, b in lonlat],
            dtype=float))
    monkeypatch.setattr("trid3nt_server.tools.cache.read_object_bytes_s3",
                        lambda _uri: b"")

    async def _fill(rain_record=None, **params):
        from trid3nt_server.workflows.telemac.modules import fill
        from trid3nt_server.workflows.telemac.templates.rain_on_grid.rain_on_grid import (
            STEERING,
        )

        mesh = _accepted_catchment_mesh()
        asked = {"curve_number": None, "steep_slope_correction": False,
                 "antecedent_moisture": "normal", "design_storm_mm_per_hr": 25.0,
                 "storm_duration_hr": 6.0, "rain_series_mm": None,
                 "sim_duration_s": 43200.0, **params}
        settled = await asm_mod.open_water(
            mesh=mesh, sim_duration_s=asked["sim_duration_s"],
            geometry=STEERING.ASSERTED["GEOMETRY_FILE"],
            boundary=STEERING.ASSERTED["BOUNDARY_CONDITIONS_FILE"],
            result=STEERING.ASSERTED["RESULTS_FILE"], mesh_resolution_m=40.0)
        sheet = fill(STEERING,
                     produced={"settled": settled, "mesh": mesh, "outlet": _OUTLET,
                               "landcover": {"uri": "s3://cache/lc.tif"},
                               # The record row is CONTEXT: where the analysis
                               # published nothing over this catchment the row
                               # is absent, and a tail off it reads as nothing.
                               "rain": rain_record},
                     params=asked)
        return settled, sheet

    return _fill


@pytest.mark.asyncio
async def test_a_constant_storm_states_a_deck_and_names_no_fortran(rog_run):
    settled, sheet = await rog_run()
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
    # the design rate is the engine's own constant branch, and its window closes
    # inside the run so the recession limb appears.
    assert deck["RAIN OR EVAPORATION"] is True
    assert deck["RAIN OR EVAPORATION IN MM PER DAY"] == pytest.approx(600.0)
    assert deck["DURATION OF RAIN OR EVAPORATION IN HOURS"] == 6.0
    # a constant-rain run compiles nothing, so the keyword is ABSENT: the
    # worker's strict gate reads a present one as a directory it must compile.
    assert "FORTRAN FILE" not in deck
    # The step and the cadence are the SETTLED domain's, off the edge the mesh
    # was built at, rather than numbers this template restates.
    assert deck["TIME STEP"] == settled["time_step_s"]
    # The CADENCE is this template's own opinion of the module's keyword, in
    # steps, and the listing is printed on the same beat as the frames.
    assert deck["GRAPHIC PRINTOUT PERIOD"] == 900
    assert deck["LISTING PRINTOUT PERIOD"] == 900
    assert deck["DURATION"] == 43200.0
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


@pytest.mark.asyncio
async def test_the_infiltration_surface_is_read_off_the_land_cover_at_the_fill(rog_run):
    """The curve number and the roughness at every node come off ONE table for
    the class under it, an unmapped class takes the open-land row, and the sheet
    records the surface as the composite's own."""
    _settled, sheet = await rog_run()
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


@pytest.mark.asyncio
async def test_a_uniform_curve_number_overrides_the_field_and_keeps_the_roughness(
        rog_run):
    _settled, sheet = await rog_run(curve_number=70.0, antecedent_moisture="wet")
    cn_rows = sheet.files["rog_cn_map.dat"].splitlines()[1:]
    assert {row.split()[2] for row in cn_rows} == {"70.000"}
    assert "0.200" in sheet.files["rog_friction.tbl"]
    assert dict(sheet.resolved())["ANTECEDENT MOISTURE CONDITIONS"] == 3


@pytest.mark.asyncio
async def test_a_measured_series_drives_the_run_through_the_block_file(rog_run):
    """A record is hourly gross millimetres as it was reported; the blocks and
    the routine that reads them per timestep are the module's, not a step of
    this template's own."""
    from trid3nt_server.workflows.telemac.templates.rain_on_grid.rain_on_grid import (
        RAINDEF3_USER_FORTRAN,
    )

    settled, sheet = await rog_run(rain_series_mm=[3.0, 12.5, 0.0],
                                   sim_duration_s=10800.0)
    deck = dict(sheet.resolved())
    assert deck["FORTRAN FILE"] == RAINDEF3_USER_FORTRAN
    assert deck["FORMATTED DATA FILE 1"] == "rog_hyeto.txt"
    rows = [line.split() for line in sheet.files["rog_hyeto.txt"].splitlines()
            if line[:1].isdigit() and " " in line]
    assert rows[:3] == [["3600.000", "3.00000"], ["7200.000", "12.50000"],
                        ["10800.000", "0.00000"]]
    # the tail past the last simulated instant is DRY, so a storm that stops
    # inside the run stops in the file too.
    assert rows[-1] == [f"{settled['until_s'] + 3600.0:.3f}", "0.00000"]
    assert "DURATION OF RAIN OR EVAPORATION IN HOURS" not in deck


@pytest.mark.asyncio
async def test_the_published_record_drives_the_run_where_the_ask_states_none(rog_run):
    """Two ways to state one storm: a series the caller states WINS, and where
    they state none the hours the record published drive the run."""
    _settled, sheet = await rog_run(rain_record={"precip_mm": [1.0, 4.0, 2.0]},
                                    sim_duration_s=10800.0)
    rows = [line.split() for line in sheet.files["rog_hyeto.txt"].splitlines()
            if line[:1].isdigit() and " " in line]
    assert rows[:3] == [["3600.000", "1.00000"], ["7200.000", "4.00000"],
                        ["10800.000", "2.00000"]]

    _settled, stated = await rog_run(rain_record={"precip_mm": [1.0, 4.0, 2.0]},
                                     rain_series_mm=[9.0, 9.0],
                                     sim_duration_s=10800.0)
    rows = [line.split() for line in stated.files["rog_hyeto.txt"].splitlines()
            if line[:1].isdigit() and " " in line]
    assert rows[:2] == [["3600.000", "9.00000"], ["7200.000", "9.00000"]]


@pytest.mark.asyncio
async def test_the_outputs_are_exactly_what_the_run_writes_and_was_handed(rog_run):
    """What a reader may OPEN is the sheet's own answer plus the mesh it was
    handed and the deck it read - nothing listed that the run does not carry."""
    settled, sheet = await rog_run()
    handed = [row["dest"] for row in settled["mesh_inputs"]]
    assert {*handed, "r2d_rog.slf", "full_listing.log", "telemac_metrics.json",
            *sheet.files} == {
        "r2d_rog.slf", "rog.slf", "rog.cli", "full_listing.log",
        "telemac_metrics.json", "rog_cn_map.dat", "rog_friction.tbl",
        "rog_zones.dat", "rog_rating.txt"}
