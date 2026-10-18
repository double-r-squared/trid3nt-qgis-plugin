"""The vertical-structure template as VALUES over the engine-neutral slots.

Offline. The question is what a column does over the depth a 2D model averages
away, and after the domain wave it asks that of ANY body of water: the domain is
a polygon somebody drew or holds, the bed is one source the caller can supersede,
and nothing in the deck names a reach, a place or a bounding box.
"""

from __future__ import annotations

import inspect

import pytest
from seated import value_inputs, row_inputs


def _module():
    from trid3nt_server.workflows.telemac.templates.stratified_flow import (
        stratified_flow,
    )

    return stratified_flow


def _workflow():
    from trid3nt_server.tools import TOOL_REGISTRY

    return TOOL_REGISTRY["telemac3d_stratified_flow"].fn.workflow


# -- what the run stands on --------------------------------------------------- #

def test_the_data_rows_are_the_engine_neutral_slots_this_run_stands_on():
    """No fetcher maps every pond, so the polygon is a SLOT: drawn on the canvas,
    picked, or a layer the caller already holds. One bed the match composes, and
    no runs row - a lake IS a closed body, and the mesh takes its roles from the
    domain, which measures none."""
    from trid3nt_server.inputs.slots import BED, DOMAIN, LEVEL

    rows = row_inputs(_module().INPUTS)
    assert [d.name for d in rows] == ["domain", "bed", "level"]
    by_name = {d.name: d for d in rows}
    assert by_name["domain"].slot == DOMAIN
    assert by_name["domain"].data_class == "hydrography"
    assert by_name["domain"].producer is None
    assert by_name["domain"].span_km is None
    assert by_name["bed"].slot == BED
    assert by_name["bed"].data_class == "bathymetry"
    assert by_name["bed"].producer is None
    assert by_name["level"].slot == LEVEL
    assert by_name["level"].data_class == "water level series"
    assert by_name["level"].producer is None
    # Both slots reach the wire: what the user supplies supersedes the match.
    assert by_name["domain"].fills_from_user and by_name["bed"].fills_from_user


def test_the_seed_names_which_body_of_water_and_is_a_point_never_a_place():
    """A place name is geocoded to a point before it reaches the match, so what
    ranks the domain is a lon/lat off the Point the seed was ingested into."""
    from trid3nt_server.inputs import Point

    domain = {d.name: d for d in row_inputs(_module().INPUTS)}["domain"]
    assert domain.coercion["near"] == "seed"
    seed = next(p for p in value_inputs(_workflow()) if p.name == "seed")
    # Stated or not: a drawn domain never reads it, and a fetched one refuses without it.
    assert seed.type is Point and seed.stated_only and not seed.required


def test_the_level_is_ranked_against_the_domain():
    """A body of water opens at a LEVEL somebody measured, the way a river opens at
    a carrier discharge: one reading off the nearest gauge that watched this
    water, a plain need row the match ranks."""

    level = {d.name: d for d in row_inputs(_module().INPUTS)}["level"]
    assert level.producer is None
    assert level.data_class == "water level series"


# -- the plan the workflow owns ------------------------------------------------ #


def test_the_plane_count_is_the_modules_keyword_and_the_planner_reads_it():
    """The plane count is NUMBER OF HORIZONTAL LEVELS, and the grid plan and the
    initial column READ that keyword rather than a second number beside it - so a
    user who states another count is planned on the column they are solved over."""

    asserted = _module().STEERING.ASSERTED
    assert asserted["NUMBER_OF_HORIZONTAL_LEVELS"] == 13
    for slot in ("vertical_grid", "column"):
        assert asserted[slot]["levels"] == "NUMBER_OF_HORIZONTAL_LEVELS"


def test_a_stated_plane_count_replans_the_grid_and_the_initial_column():
    """The fill binds the composites' read to whatever the sheet holds, so the
    stretch the grid achieves follows the keyword whether the deck stated the
    count or the user did."""
    from trid3nt_server.workflows.runtime.sheet import fill

    produced = {"settled": {"title": "basin", "time_step_s": 1.0,
                            "level_m": 100.0, "max_depth_m": 120.0}}
    params = {"thermocline_depth_m": 8.0, "warm_temp_c": 25.0,
              "cold_temp_c": 15.0}
    body = _module().STEERING
    stated = fill(body, produced=produced, params=params)
    finer = fill(body, produced=produced, params=params,
                 NUMBER_OF_HORIZONTAL_LEVELS=25)
    assert dict(stated.resolved())["NUMBER OF HORIZONTAL LEVELS"] == 13
    assert dict(finer.resolved())["NUMBER OF HORIZONTAL LEVELS"] == 25
    assert (dict(stated.resolved())["MESH STRETCHING COEFFICIENTS"]
            != dict(finer.resolved())["MESH STRETCHING COEFFICIENTS"])


def test_the_deck_is_calm_and_states_no_wind_keyword_at_all():
    """CALM is the half of the pair the thermocline survives in, and a zero speed
    writes nothing: TELEMAC-3D has no combined speed-and-direction keyword, so the
    wind a user asks for is the engine's own components, set by their names."""
    from trid3nt_server.workflows.runtime.sheet import fill

    asserted = _module().STEERING.ASSERTED
    assert not {"WIND", "WIND_VELOCITY_ALONG_X", "WIND_VELOCITY_ALONG_Y"} & set(asserted)
    produced = {"settled": {"title": "basin", "time_step_s": 1.0,
                            "level_m": 100.0, "max_depth_m": 40.0}}
    sheet = fill(_module().STEERING, produced=produced,
                 params={"thermocline_depth_m": 8.0, "warm_temp_c": 25.0,
                         "cold_temp_c": 15.0})
    assert [name for name in dict(sheet.resolved()) if "WIND" in name] == []


# -- what the template no longer declares -------------------------------------- #

#: What this question asks that nothing else answers for it. Everything else is
#: the dictionary's keyword or the runtime's lever.
_OWN_PARAMS = {"seed", "warm_temp_c", "cold_temp_c", "thermocline_depth_m",
               "mesh_resolution_m"}


def test_the_template_declares_only_its_own_question_and_one_restated_lever():
    """A keyword twin, a domain twin and a lever are each declared once elsewhere.
    The one lever restated here carries no default: the edge is the data's."""
    from seated import SEATED

    workflow = _workflow()
    declared = {p.name for p in value_inputs(workflow)}
    assert declared == _OWN_PARAMS | set(SEATED)
    rows = {p.name: p for p in value_inputs(workflow)}
    edge = rows["mesh_resolution_m"]
    assert (edge.default, edge.bounds, edge.stated_only) == (None, None, True)


@pytest.mark.parametrize("gone", ["location", "bbox", "mesh_min_edge_m",
                                  "time_step_s", "output_interval_min",
                                  "sim_duration_hours", "sim_duration_s",
                                  "levels", "wind_speed_mps",
                                  "wind_direction_deg",
                                  "tracer_advection_scheme",
                                  "max_advection_iterations"])
def test_the_dissolved_params_are_gone(gone):
    """Each is a keyword the dictionary describes or a slot the runtime declares."""
    assert gone not in {p.name for p in value_inputs(_workflow())}


def test_the_model_surface_takes_a_body_of_water_and_never_a_place():
    """Nothing on the wire names a place or a box: the domain IS the argument, and
    a place name is geocoded to a shape before it gets here."""
    from trid3nt_server.tools import TOOL_REGISTRY

    fn = TOOL_REGISTRY["telemac3d_stratified_flow"].fn
    args = set(inspect.signature(fn).parameters)
    assert {"domain", "bed", "level", "seed"} <= args
    assert not args & {"location", "bbox"}


def test_the_routing_text_is_about_a_body_of_water():
    """The question class is the vertical structure of WATER; naming a reach or a
    lake alone is what the domain wave took out."""
    from pathlib import Path

    import yaml

    routing = yaml.safe_load(
        (Path(_module().__file__).parent / "routing.yaml").read_text(
            encoding="utf-8"))["routing"]
    assert "reach" not in routing.lower()
    assert "`domain`" in routing and "`bed`" in routing and "`seed`" in routing
    for word in ("lake", "reservoir", "pond"):
        assert word in routing.lower()


def test_the_package_holds_the_template_its_corpus_and_its_routing():
    """A template package carries no module of its own: what was a clip and a gauge
    reader are a slot's ingestion and a fetcher row now."""
    from pathlib import Path

    package = Path(_module().__file__).parent
    assert sorted(p.name for p in package.iterdir()
                  if p.is_file() and not p.name.startswith(".")) == [
        "__init__.py", "corpus.yaml", "routing.yaml", "stratified_flow.py"]
