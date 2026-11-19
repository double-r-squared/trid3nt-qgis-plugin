"""Offline tests for the telemac_rain_on_soil template: Green-Ampt infiltration over the soil's texture.

No solver, no network. The deck is written by the real fill over a stood-in accepted mesh, with the soil's parameter
rasters and the land cover sampled at its nodes stood in by hand-picked values; every number asserted is worked by
hand from those values. The soil ingestion runs the real derive over recorded SoilGrids windows.
"""

from __future__ import annotations

import pathlib
from types import MappingProxyType, SimpleNamespace

import numpy as np
import pytest

from trid3nt_server.inputs.slots import SOIL, needs_of
from trid3nt_server.inputs.soil import Soil, soil
from trid3nt_server.workflows.runtime.module import SlotRefused
from trid3nt_server.workflows.telemac.authoring.selafin_io import BOUNDARY_CONDITIONS_FILE, GEOMETRY_FILE
from seated import row_inputs

_FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "green_ampt"

#: Two triangles of 100 m2 each over four nodes: nodes 1 and 2 hold both, so each carries 66.67 m2 and nodes 0 and 3
#: 33.33 m2 - an area-weighted mean is (v0 + 2 v1 + 2 v2 + v3) / 6.
_NODES = (np.array([[0.0, 0.0], [20.0, 0.0], [0.0, 10.0], [20.0, 10.0]]),
          np.array([[0, 1, 2], [1, 3, 2]]),
          np.array([1.0, 0.8, 1.2, 1.0]),
          np.array([[-83.4, 35.0], [-83.39, 35.0], [-83.4, 35.01], [-83.39, 35.01]]))
#: What each raster holds under the four nodes: Table 2 rows for loam, sandy loam, silt loam and clay (K cm/h,
#: suction cm, effective porosity), and the land cover forest, urban, water and a class the table does not carry.
_UNDER = {
    "s3://soil/k.tif": [0.34, 1.09, 0.65, 0.03],
    "s3://soil/suction.tif": [8.89, 11.01, 16.68, 31.63],
    "s3://soil/porosity.tif": [0.463, 0.453, 0.501, 0.475],
    "s3://soil/effective.tif": [0.434, 0.412, 0.486, 0.385],
    "s3://cache/lc.tif": [42.0, 24.0, 11.0, 999.0],
}
_SOIL = Soil(MappingProxyType({
    "green_ampt_hydraulic_conductivity": {"uri": "s3://soil/k.tif"},
    "green_ampt_wetting_front_suction": {"uri": "s3://soil/suction.tif"},
    "green_ampt_porosity": {"uri": "s3://soil/porosity.tif"},
    "green_ampt_effective_porosity": {"uri": "s3://soil/effective.tif"}}))
_OUTLET = {"at_boundary": 1, "of_boundaries": 1, "rows": [[0.0, 0.8], [2.5, 1.1], [5.0, 1.4]],
           "note": "derived Z(Q) at liquid boundary 1"}


def _mesh():
    return {"artifact": SimpleNamespace(utm_epsg=32617, bbox=(-83.47, 35.02, -83.36, 35.10), name="coweeta creek",
                                        probes={"area_km2": 2.5, "edge_length_m": {"min": 40.0, "max": 300.0}}),
            "mesh_id": "M1",
            "engine_files": {GEOMETRY_FILE: "s3://cache/mesh/M1/mesh.slf",
                             BOUNDARY_CONDITIONS_FILE: "s3://cache/mesh/M1/mesh.cli"},
            "display_uri": "s3://cache/mesh/M1/mesh.2dm", "node_count": 4, "element_count": 2, "min_edge_m": 40.0,
            "provenance": {"bed_source": "3dep 100%", "sizing_source": "nhdplus_hr",
                           "domain_source": "fetch_watershed basin (1 part(s))"}}


@pytest.fixture()
def ros_run(monkeypatch, tmp_path):
    """The seated settle and the fill over it, with the accepted mesh's reads and every raster stood in for."""
    import trid3nt_server.tools.mesh.shared.nodes as nodes_mod
    from trid3nt_server.workflows.telemac.authoring import accepted_mesh as mesh_mod
    from trid3nt_server.workflows.telemac.authoring import opening as asm_mod

    monkeypatch.setenv("TRID3NT_RUNS_DIR", str(tmp_path))
    monkeypatch.setattr(nodes_mod, "read_accepted_mesh_nodes", lambda _uri, utm_epsg=None: _NODES)
    monkeypatch.setattr(mesh_mod, "read_accepted_mesh_nodes", lambda _uri, utm_epsg=None: _NODES)
    under = dict(_UNDER)
    monkeypatch.setattr(nodes_mod, "sample_layer_at_nodes",
                        lambda layer, lonlat, interp=None, fill_holes=True: np.array(under[layer["uri"]], dtype=float))

    async def _fill(keywords=None, under_k=None, **params):
        from trid3nt_server.workflows.telemac.modules import fill
        from trid3nt_server.workflows.telemac.templates.rain_on_soil.rain_on_soil import STEERING

        if under_k is not None:
            under["s3://soil/k.tif"] = under_k
        mesh = _mesh()
        asked = {"design_storm_mm_per_day": 600.0, "rain_series_mm": None, **params}
        settled = await asm_mod.open_water(
            mesh=mesh,
            files={"engine_files": dict(mesh["engine_files"]),
                   "topology": {"roles": {"rating_curve": [1, 3]}, "liquid_boundary_order": ["rating_curve"],
                                "liquid_boundary_prescribes": ["elevation"]}},
            duration_s=STEERING.ASSERTED["DURATION"], geometry=STEERING.ASSERTED["GEOMETRY_FILE"],
            boundary=STEERING.ASSERTED["BOUNDARY_CONDITIONS_FILE"], result=STEERING.ASSERTED["RESULTS_FILE"],
            mesh_resolution_m=40.0)
        return fill(STEERING, **(keywords or {}), settled=settled["keywords"],
                    produced={"settled": settled, "mesh": mesh, "outlet": _OUTLET,
                              "landcover": {"uri": "s3://cache/lc.tif"}, "soil": _SOIL, "rain": None},
                    params=asked)

    return _fill


def test_registered_with_the_soil_row_and_green_ampt_on_the_deck():
    from trid3nt_server.tools import TOOL_REGISTRY
    from trid3nt_server.workflows.telemac.templates.rain_on_soil.rain_on_soil import STEERING, telemac_rain_on_soil

    assert TOOL_REGISTRY["telemac_rain_on_soil"].metadata.tier == "template"
    rows = {row.name: row for row in row_inputs(telemac_rain_on_soil.workflow)}
    assert rows["soil"].slot is SOIL and rows["soil"].data_class == "soil" and rows["soil"].observes == "sand"
    assert STEERING.ASSERTED["RAINFALL_RUNOFF_MODEL"] == 3
    assert not {"SUCTION", "SATURATED_WATER_CONTENT", "INITIAL_WATER_CONTENT"} & set(STEERING.ASSERTED)


def test_the_soil_row_asks_its_silt_clay_and_organic_carbon_beside_it():
    assert [name for name, _word, _shape in needs_of(SOIL, "")] == ["silt", "clay", "organic_carbon"]


def test_a_soil_need_reaches_soilgrids_alone_asking_the_property_in_its_own_word(monkeypatch):
    import trid3nt_server.tools.search.match as match_mod

    rows = {name: row for name, row in match_mod.sources_with_coverage() if row.data_class == "soil"}
    need = match_mod.Need(slot="soil", data_class="soil", of="sand", lon=-83.4, lat=35.05)
    assert {name for name, row in rows.items() if not match_mod.excluded(need, row, name)} == {"fetch_soilgrids"}
    monkeypatch.setattr(match_mod, "picked_row", lambda _choice: rows["fetch_soilgrids"])
    choice = SimpleNamespace(picked="fetch_soilgrids")
    assert match_mod.ask_for(choice, {}, None, None, {"of": "organic_carbon"})["soil_property"] == "soc"
    assert match_mod.ask_for(choice, {}, None, None, {"of": "sand"})["soil_property"] == "sand"


@pytest.mark.asyncio
async def test_the_conductivity_is_written_per_node_in_mm_per_hour(ros_run):
    sheet = await ros_run(keywords={"INITIAL_WATER_CONTENT": 0.15})
    deck = dict(sheet.resolved())
    assert deck["RAINFALL-RUNOFF MODEL"] == 3
    assert deck["FORMATTED DATA FILE 2"] == "rog_ks_map.dat"
    rows = sheet.files["rog_ks_map.dat"].splitlines()
    assert rows[0].startswith("#X Y KS")
    # cm/h x 10 = mm/h, at the mesh's own metres
    assert [row.split() for row in rows[1:]] == [["0.000", "0.000", "3.4000"], ["20.000", "0.000", "10.9000"],
                                                 ["0.000", "10.000", "6.5000"], ["20.000", "10.000", "0.3000"]]
    zones = dict(line.split() for line in sheet.files["rog_zones.dat"].splitlines())
    laws = {line.split()[0]: line.split()[2] for line in sheet.files["rog_friction.tbl"].splitlines()
            if line[:1].isdigit()}
    assert [laws[zones[str(i)]] for i in range(1, 5)] == ["0.200", "0.100", "0.040", "0.050"]
    assert "rog_cn_map.dat" not in sheet.files and "ANTECEDENT MOISTURE CONDITIONS" not in deck


@pytest.mark.asyncio
async def test_suction_and_saturated_content_are_derived_area_weighted_off_the_soil(ros_run):
    sheet = await ros_run(keywords={"INITIAL_WATER_CONTENT": 0.15})
    deck = dict(sheet.resolved())
    # (8.89 + 2 x 11.01 + 2 x 16.68 + 31.63) / 6 = 15.98 cm = 0.1598 m; (0.434 + 2 x 0.412 + 2 x 0.486 + 0.385) / 6
    assert deck["SUCTION"] == pytest.approx((8.89 + 2 * 11.01 + 2 * 16.68 + 31.63) / 6 / 100, abs=1e-9)
    assert deck["SATURATED WATER CONTENT"] == pytest.approx((0.434 + 2 * 0.412 + 2 * 0.486 + 0.385) / 6, abs=1e-9)
    assert sheet.filled["SUCTION"].provenance.origin.value == "derived"
    assert str(sheet.filled["SATURATED_WATER_CONTENT"].provenance) == "derived: soil"
    assert deck["INITIAL WATER CONTENT"] == 0.15


@pytest.mark.asyncio
async def test_a_stated_suction_or_saturated_content_wins_over_the_soil(ros_run):
    sheet = await ros_run(keywords={"INITIAL_WATER_CONTENT": 0.15, "SUCTION": 0.25, "SATURATED_WATER_CONTENT": 0.45})
    deck = dict(sheet.resolved())
    assert (deck["SUCTION"], deck["SATURATED WATER CONTENT"]) == (0.25, 0.45)
    assert sheet.filled["SUCTION"].provenance.origin.value == "user"


@pytest.mark.asyncio
async def test_an_unstated_initial_water_content_refuses_naming_it(ros_run):
    with pytest.raises(SlotRefused) as refused:
        await ros_run()
    assert "INITIAL WATER CONTENT is unstated" in str(refused.value)


@pytest.mark.asyncio
async def test_an_initial_content_over_the_saturated_refuses(ros_run):
    with pytest.raises(SlotRefused) as refused:
        await ros_run(keywords={"INITIAL_WATER_CONTENT": 0.6})
    assert "no soil" in str(refused.value)


@pytest.mark.asyncio
async def test_a_node_the_soil_states_no_conductivity_under_refuses(ros_run):
    with pytest.raises(SlotRefused) as refused:
        await ros_run(keywords={"INITIAL_WATER_CONTENT": 0.15}, under_k=[0.34, float("nan"), 0.65, 0.03])
    assert "1 of 4 mesh nodes" in str(refused.value) and "-83.39000" in str(refused.value)


@pytest.mark.asyncio
async def test_a_measured_storm_compiles_the_green_ampt_routine_reading_the_blocks(ros_run):
    sheet = await ros_run(keywords={"INITIAL_WATER_CONTENT": 0.15}, rain_series_mm=[3.0, 12.5, 0.0])
    deck = dict(sheet.resolved())
    assert deck["FORTRAN FILE"] == "user_fortran" and deck["FORMATTED DATA FILE 1"] == "rog_hyeto.txt"
    routine = sheet.files["user_fortran/runoff_greenampt.f"]
    assert "SUBROUTINE RUNOFF_GREENAMPT" in routine and "INTEGER, PARAMETER ::RAINDEF=3" in routine
    assert "user_fortran/runoff_scs_cn.f" not in sheet.files


def test_the_authored_routine_is_the_engine_s_with_the_block_branch_only():
    from trid3nt_server.workflows.telemac.authoring.storm import block_routine

    name, text = block_routine(3)
    scs_name, scs = block_routine(1)
    assert (name, scs_name) == ("runoff_greenampt.f", "runoff_scs_cn.f")
    assert text.count("RAINDEF=") == 1 and "INTEGER, PARAMETER ::RAINDEF=3" in text
    assert "FCAPA(I)=1.D0+(HF+CC)*(THETAS-THETAI)/ACCINF%R(I)" in text
    assert "FCAPA(I)=FCAPA(I)*KS%R(I)/3600.D0/1000.D0" in text
    assert "INTEGER, PARAMETER ::RAINDEF=3" in scs


def test_the_soil_ingestion_composes_the_derive_over_recorded_soilgrids(tmp_path, monkeypatch):
    from trid3nt_contracts.execution import LayerURI
    import importlib

    derive_mod = importlib.import_module("trid3nt_server.tools.derive.green_ampt_parameters.green_ampt_parameters")

    def layer(fraction):
        return LayerURI(layer_id=f"gulf-{fraction}", name=fraction, layer_type="raster", units="percent",
                        uri=str(_FIXTURES / f"gulf_{fraction}.tif"))

    original = derive_mod.derived
    monkeypatch.setattr(derive_mod, "derived",
                        lambda *a, **k: original(*a, **k, _output_dir=str(tmp_path)))
    found = soil(layer("sand"), silt=layer("silt"), clay=layer("clay"))
    assert set(found.layers) == {"green_ampt_hydraulic_conductivity", "green_ampt_wetting_front_suction",
                                 "green_ampt_porosity", "green_ampt_effective_porosity"}
    assert found.layer("green_ampt_hydraulic_conductivity").units == "cm/h"


@pytest.fixture()
def card(monkeypatch, ros_run):
    """The ask's gate: the cards it drew, answered by ``held['answer']``; the mode is ``held['mode']``."""
    import trid3nt_server.inputs.gate.input_review as review_mod

    held = {"mode": "user_gated", "answer": {}, "cards": []}

    async def gate(*, tool_name, entries, params, present, **_kw):
        held["cards"].append(await present())
        return review_mod.ReviewOutcome(proceed=True, entries=[], params={**params, **held["answer"]}, mode="user_gated")

    monkeypatch.setattr(review_mod, "gate_input_review", gate)
    monkeypatch.setattr(review_mod, "resolve_input_gate_mode", lambda forced=None: held["mode"])
    return held


def _asked(keywords=None):
    from trid3nt_server.workflows.runtime.review import ask_unstated
    from trid3nt_server.workflows.telemac.templates.rain_on_soil.rain_on_soil import STEERING
    from trid3nt_server.workflows.telemac.workflow import stated

    keywords = dict(keywords or {})
    return ask_unstated(steering=STEERING, produced={"soil": _SOIL, "mesh": _mesh()}, workflow="telemac_rain_on_soil",
                        stated=stated(steering=STEERING, keywords=keywords), keywords=keywords)


@pytest.mark.asyncio
async def test_the_card_offers_the_derived_pair_labelled_and_asks_the_initial_content_bare(card):
    card["answer"] = {"INITIAL_WATER_CONTENT": 0.27}
    taken = await _asked()
    rows = {row.name: row for row in card["cards"][0].param_sheet.rows}
    assert set(rows) == {"SUCTION", "SATURATED_WATER_CONTENT", "INITIAL_WATER_CONTENT"}
    assert rows["SUCTION"].value == pytest.approx((8.89 + 2 * 11.01 + 2 * 16.68 + 31.63) / 6 / 100, abs=1e-9)
    assert rows["SATURATED_WATER_CONTENT"].value == pytest.approx((0.434 + 2 * 0.412 + 2 * 0.486 + 0.385) / 6, abs=1e-9)
    assert rows["SUCTION"].source_badge == rows["SATURATED_WATER_CONTENT"].source_badge == "derived from soil"
    assert rows["INITIAL_WATER_CONTENT"].value is None and "derived" not in rows["INITIAL_WATER_CONTENT"].source_badge
    # the pair is left to the soil, so the deck's origin stays derived; only the answered value is stated
    assert taken == {"INITIAL_WATER_CONTENT": 0.27}


@pytest.mark.asyncio
async def test_an_answer_over_an_offer_is_stated_and_one_equal_to_it_is_not(card):
    offer = (8.89 + 2 * 11.01 + 2 * 16.68 + 31.63) / 6 / 100
    card["answer"] = {"INITIAL_WATER_CONTENT": 0.27, "SUCTION": 0.25, "SATURATED_WATER_CONTENT": 0.45}
    assert await _asked() == {"INITIAL_WATER_CONTENT": 0.27, "SUCTION": 0.25, "SATURATED_WATER_CONTENT": 0.45}
    card["answer"] = {"INITIAL_WATER_CONTENT": 0.27, "SUCTION": offer}
    assert await _asked() == {"INITIAL_WATER_CONTENT": 0.27}


@pytest.mark.asyncio
async def test_an_unanswered_initial_water_content_refuses_naming_it(card):
    with pytest.raises(SlotRefused) as refused:
        await _asked()
    assert "INITIAL WATER CONTENT is unstated" in str(refused.value)


@pytest.mark.asyncio
async def test_a_run_stating_all_three_is_asked_nothing(card):
    stated = {"INITIAL_WATER_CONTENT": 0.27, "SUCTION": 0.25, "SATURATED_WATER_CONTENT": 0.45}
    assert await _asked(stated) == stated and card["cards"] == []


@pytest.mark.asyncio
async def test_auto_takes_the_derived_pair_and_says_so_and_leaves_the_initial_content_to_refuse(card):
    from trid3nt_server.inputs.run_notes import bind_notes, drain_notes

    card["mode"] = "auto"
    token = bind_notes()
    assert await _asked() == {}
    notes = drain_notes(token)
    assert card["cards"] == []
    assert sum("derived from soil" in note for note in notes) == 2 and not any("INITIAL" in note for note in notes)


@pytest.mark.asyncio
async def test_a_model_reading_only_the_constant_runs_on_it_and_warns_the_series_was_not_used(ros_run):
    from trid3nt_server.errors import INPUT_NOT_USED
    from trid3nt_server.inputs.run_notes import bind_notes, drain_notes

    token = bind_notes()
    sheet = await ros_run(keywords={"INITIAL_WATER_CONTENT": 0.15, "RAINFALL_RUNOFF_MODEL": 2}, rain_series_mm=[3.0, 12.5, 0.0])
    notes = drain_notes(token)
    assert dict(sheet.resolved())["RAINFALL-RUNOFF MODEL"] == 2
    warned = [note for note in notes if note.startswith(INPUT_NOT_USED)]
    assert len(warned) == 1
    assert "storm" in warned[0] and "RAINFALL_RUNOFF_MODEL" in warned[0] and "Horton" not in warned[0]
    assert "3.0" not in warned[0] and "12.5" not in warned[0]


def test_the_unused_input_warning_names_identifiers_only_and_never_a_value():
    from trid3nt_server.errors import INPUT_NOT_USED, unused_input_warning

    text = unused_input_warning(slot="storm", option="RAINFALL_RUNOFF_MODEL")
    assert text.startswith(f"{INPUT_NOT_USED}: ") and "storm" in text and "RAINFALL_RUNOFF_MODEL" in text
    assert text.endswith(".") and "\n" not in text
