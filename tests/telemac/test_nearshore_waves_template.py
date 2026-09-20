"""The nearshore-wave template: the world it stands on, and the deck it hands
TOMAWAC.

Offline: nothing here starts a solve. What is pinned is the sea state reaching
the boundary keywords as three separate numbers, the run length the module's own
spelling gives it, the result file it is read off, the open edge the spectrum
enters across, and the answers being the module's own rows rather than a
template's arithmetic.
"""

from __future__ import annotations

import pytest

from trid3nt_server.workflows.runtime import Ref
from trid3nt_server.workflows.runtime.data import BED, DOMAIN, EXTENT, LEVEL, WAVE
from trid3nt_server.workflows.runtime.plan import declared_reads
from trid3nt_server.workflows.telemac.modules import WAC, fill
from trid3nt_server.workflows.telemac.modules.tomawac import RESULT_FILENAME

_TOOL = "tomawac_nearshore_waves"
#: The hour the deck marches, as the two keywords the module spells it in.
_HOUR_S = 3600.0


def _workflow():
    from trid3nt_server.tools import TOOL_REGISTRY

    return TOOL_REGISTRY[_TOOL].fn.workflow


def _rows():
    return {row.name: row for row in _workflow().data}


def _step(label):
    return next(step for step in _workflow().plan.declared()
                if step.label == label)


def test_the_world_is_the_water_a_coastline_leaves_inside_the_window():
    """A wave run solves over water, and nothing maps a stretch of open coast as
    a polygon: the question asks for the EDGE and the slot cuts the drawn window
    with it, exactly as the harbour question does."""
    rows = _rows()
    assert rows["extent"].role == EXTENT
    assert rows["extent"].geometry == "rectangle"
    domain = rows["domain"]
    assert (domain.role, domain.data_class, domain.observes, domain.geometry) \
        == (DOMAIN, "hydrography", "coastline", "polyline")
    assert domain.producer is None
    assert rows["bed"].role == BED
    assert rows["bed"].data_class == "bathymetry"


def test_the_sea_state_is_a_row_on_the_reserved_wave_slot():
    """The boundary is forced at something somebody MEASURED, so it is a DATA row
    of the class a buoy network publishes, ranked at the point the question names
    - not a number the template states."""
    wave = _rows()["wave"]
    assert wave.role == WAVE
    assert wave.data_class == "wave series"
    assert wave.producer is None
    assert wave.coercion["near"] == Ref("seed")


def test_the_buoys_are_what_answers_the_sea_state_row():
    """The class the row states has to reach a source that measures it here; a
    row no source serves is a question that cannot be asked."""
    from trid3nt_server.tools.search.match import (
        Need, match, sources_with_coverage)

    wave = _rows()["wave"]
    choice = match(Need(slot="wave", data_class=wave.data_class,
                        lon=-75.75, lat=36.18,
                        opens="2026-02-10T12:00:00Z",
                        until="2026-02-10T13:00:00Z"), sources_with_coverage())
    assert choice.picked == "fetch_ndbc_buoys"


def test_the_boundary_keywords_read_the_slot_by_name():
    """Three separate keywords, each reading the value the wave slot's ingestion
    made for it: a height as measured, a FREQUENCY off the period, and the
    bearing the waves run TOWARD. The deck states no conversion of its own."""
    from trid3nt_server.workflows.telemac.templates.nearshore_waves import (
        nearshore_waves as template)

    deck = template.STEERING
    assert deck.ASSERTED["BOUNDARY_SIGNIFICANT_WAVE_HEIGHT"] == Ref("wave.height_m")
    assert deck.ASSERTED["BOUNDARY_PEAK_FREQUENCY"] == Ref("wave.peak_frequency_hz")
    assert deck.ASSERTED["BOUNDARY_MAIN_DIRECTION_1"] == Ref("wave.direction_deg")
    # The water the depths are counted down from is the tide slot's, as one
    # value: TOMAWAC carries no free surface of its own.
    assert _rows()["level"].role == LEVEL
    assert deck.ASSERTED["INITIAL_STILL_WATER_LEVEL"] == Ref("level.value")


def test_the_run_length_is_the_product_the_module_spells_it_as():
    """TOMAWAC names no DURATION, so the settle and the window a matched record
    is filtered on both read the step and the count this deck states."""
    assert _workflow().run_window_s({}) == _HOUR_S
    assert _step("settled").kwargs["duration_s"] == [Ref("stated.TIME_STEP"),
                                                     Ref("stated.NUMBER_OF_TIME_STEP")]
    # The floor still moves it: a caller who states a longer run moves the
    # window every series source is matched against.
    assert _workflow().run_window_s({"NUMBER OF TIME STEP": 720}) == 2 * _HOUR_S


def test_the_run_is_read_off_the_file_the_module_spells_its_results_in():
    """The deck names its result under 2D RESULTS FILE, and nothing restates the
    name: the solve declares the same file the primitives open."""
    assert _step("solve").kwargs["results"] == [RESULT_FILENAME]
    assert WAC.RESULT_FILE == RESULT_FILENAME


def test_the_seaward_edge_is_opened_so_the_spectrum_has_somewhere_to_enter():
    """A wave run whose whole rim is wall is a domain no sea state can reach, so
    the recipe designates every ocean stretch of the outline open."""
    from trid3nt_server.workflows.telemac.templates.nearshore_waves import (
        nearshore_waves as template)

    ops = {op.fn: op for op in template.MESH.ops}
    assert "set_bed" in ops
    assert template.MESH_ON == "domain"
    # THE DECK STATES THE DEPTH, never the library. The library's own default is
    # set for a shelf-scale domain and is deeper than every node of a nearshore
    # window, so inheriting it opens nothing and walls the whole rim.
    stated = ops["identify_ocean_boundary_sections"].kwargs["depth_threshold"]
    assert stated.name == "open_depth_threshold_m"
    param = next(p for p in _workflow().params
                 if p.name == "open_depth_threshold_m")
    assert (param.default, param.door) == (-12.0, "scenario")


@pytest.mark.asyncio
async def test_a_window_whose_rim_is_all_wall_refuses_rather_than_solving_zeros(
        monkeypatch, tmp_path):
    """A sea state prescribed across an edge that was never designated enters
    nowhere: the domain opens empty and stays empty, and every wave answer is a
    green zero. The settle names the deck and the depth it stated."""
    import numpy as np

    from trid3nt_server.workflows.telemac.authoring import assembler as asm_mod
    from trid3nt_server.workflows.telemac.errors import TelemacError

    monkeypatch.setenv("TRID3NT_RUNS_DIR", str(tmp_path))
    # A synthetic mesh whose every boundary node is solid wall, which is what a
    # coastal window returns when no stretch of it reaches the stated depth.
    monkeypatch.setattr(asm_mod, "read_topology", lambda _uri: {
        "roles": {}, "liquid_boundary_order": [],
        "liquid_boundary_prescribes": [],
        "states": "this domain names no liquid boundary; its whole boundary "
                  "is solid wall"})
    monkeypatch.setattr(asm_mod, "mesh_nodes",
                        lambda _mesh: (np.zeros((4, 2)), np.zeros(4)))

    with pytest.raises(TelemacError) as raised:
        await asm_mod.open_water(mesh=_closed_mesh(), duration_s=_HOUR_S,
                                 deck=_TOOL, open_depth_threshold_m=-12.0)
    assert raised.value.error_code == "TELEMAC_MESH_CLOSED"
    assert _TOOL in str(raised.value) and "-12 m" in str(raised.value)


def _closed_mesh():
    """A mesh record over a window nothing designated open."""
    from trid3nt_server.workflows.mesh.artifact import MeshArtifact

    artifact = MeshArtifact(
        mesh_id="M01", name="Duck, NC", mode="om2d",
        display_uri="s3://m/M01/mesh.2dm", slf_uri="s3://m/M01/coast.slf",
        cli_uri="s3://m/M01/coast.cli",
        topology_uri="s3://m/M01/mesh_topology.json",
        recipe_uri="s3://m/M01/mesh_recipe.jsonl", crs_authid="EPSG:32618",
        has_bathymetry=True, utm_epsg=32618, node_count=252, element_count=400,
        bbox=(-75.755, 36.170, -75.725, 36.200),
        provenance={"bed_source": "bluetopo"})
    return {"artifact": artifact, "mesh_id": artifact.mesh_id,
            "slf_uri": artifact.slf_uri, "cli_uri": artifact.cli_uri,
            "topology_uri": artifact.topology_uri,
            "display_uri": artifact.display_uri,
            "recipe_uri": artifact.recipe_uri,
            "node_count": artifact.node_count,
            "element_count": artifact.element_count, "min_edge_m": 40.0,
            "provenance": dict(artifact.provenance)}


def test_the_deck_states_the_physics_the_question_is_about():
    """Depth-induced breaking is what takes a shoaling wave down; without it the
    run reports a height that keeps growing into water too shallow to hold it,
    and LECDON writes no breaking row at all."""
    sheet = fill(WAC, **{name: value for name, value in
                         _deck().ASSERTED.items()
                         if not any(declared_reads(value, Ref))})
    stated = dict(sheet.resolved())
    assert stated["DEPTH-INDUCED BREAKING DISSIPATION"] == 1
    assert stated["BOTTOM FRICTION DISSIPATION"] == 1
    assert stated["TYPE OF BOUNDARY DIRECTIONAL SPECTRUM"] == 6
    # The domain opens EMPTY and fills from the boundary: a spectrum laid over
    # the whole of it at t = 0 is wave energy nobody measured.
    assert stated["TYPE OF INITIAL DIRECTIONAL SPECTRUM"] == 0
    assert not sheet.required()


def test_the_answers_are_the_module_s_own_rows():
    """Every number a reader is handed is a measure of one variable TOMAWAC
    wrote, so nothing here is a template's arithmetic over the engine's."""
    from trid3nt_server.workflows.telemac.templates.nearshore_waves import (
        nearshore_waves as template)

    named = {name: measure.primitive.variable
             for name, measure in template.ANSWER.items()}
    assert named["hs_max_m"] == "HM0"
    assert named["peak_period_at_station_s"] == "TPD"
    assert named["direction_at_station_deg"] == "DMOY"
    assert named["breaking_rate_max_per_s"] == "BETA"
    assert named["breaker_dissipation_max_m2s"] == "DBR"
    for token in ("HM0", "TPD", "DMOY", "BETA", "DBR"):
        assert token in WAC.MODULE_OUTPUT
    assert [p.variable or p.kind
            for p in template.OUTPUTS] == ["HM0", "TPD", "DMOY", "spectrum"]


def test_every_charted_variable_is_captioned():
    """A chart titled by a token nobody named is a picture a reader cannot read."""
    from trid3nt_server.workflows.telemac.templates.nearshore_waves import (
        nearshore_waves as template)

    assert all((p.variable or p.kind) in template.CAPTIONS
               for p in template.OUTPUTS)


def test_the_spectrum_is_recorded_where_the_station_settled():
    """The three charted numbers are statistics OF a spectrum, so the deck names
    the station as a printout point and publishes the spectrum itself. The
    keyword takes the abscissae apart from the ordinates, and a settled point is
    one value with an order - so each is read off the pair by position."""
    from trid3nt_server.workflows.telemac.templates.nearshore_waves import (
        nearshore_waves as template)

    asserted = template.STEERING.ASSERTED
    assert asserted["ABSCISSAE_OF_SPECTRUM_PRINTOUT_POINTS"] == [
        Ref("station.at.0")]
    assert asserted["ORDINATES_OF_SPECTRUM_PRINTOUT_POINTS"] == [
        Ref("station.at.1")]
    # The spectra are read off the file the deck named, never off the wave field.
    assert asserted["PUNCTUAL_RESULTS_FILE"] != asserted["ED_RESULTS_FILE"]
    read = next(p for p in template.OUTPUTS if p.kind == "spectrum")
    assert read.at is template._STATION and read.publish == "chart"


def _deck():
    from trid3nt_server.workflows.telemac.templates.nearshore_waves import (
        nearshore_waves as template)

    return template.STEERING


@pytest.mark.parametrize("named", ["seed", "station", "mesh_resolution_m",
                                   "event_time", "compute_class"])
def test_the_question_declares_the_levers_a_caller_reaches_it_through(named):
    assert named in {param.name for param in _workflow().params}
