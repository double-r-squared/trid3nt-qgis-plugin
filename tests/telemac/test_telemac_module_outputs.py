"""Outputs belong to the module: what a run writes, how it draws, what it is
called.

Offline: the result a read opens is stated through the ``telemac_result``
fixture and the publisher is stood in for, so what is proved is which rows reach
which layer - and which rows never reach a template at all."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from typing import Any

import pytest

from trid3nt_contracts.execution import LayerURI

from trid3nt_server.render.formats import Published
from trid3nt_server.workflows.telemac.modules import (
    GAIA,
    T2D,
    T3D,
    WAQTEL,
    SlotRefused,
    fill,
)
from trid3nt_server.workflows.telemac.modules.outputs import Solved, series
from trid3nt_server.workflows.telemac.modules.telemac2d import Boundaries

TEMPLATES = (Path(__file__).resolve().parents[2] / "trid3nt_server" / "workflows"
             / "telemac" / "templates")

#: What a module says about itself and a template may not restate.
_MODULES_OWN = ("VARIABLES_FOR_GRAPHIC_PRINTOUTS",
                "VARIABLES_FOR_3D_GRAPHIC_PRINTOUTS",
                "VARIABLES_FOR_2D_GRAPHIC_PRINTOUTS")


def _recipes() -> list[Path]:
    return sorted(p / f"{p.name}.py" for p in TEMPLATES.iterdir()
                  if p.is_dir() and (p / f"{p.name}.py").is_file())


def test_no_template_states_a_style_or_a_printout_list():
    """A template asks the question; it does not choose what is shown. The
    printout list and the style of every variable are the module's own."""
    named = []
    for recipe in _recipes():
        for node in ast.walk(ast.parse(recipe.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Name) and (node.id in _MODULES_OWN
                                               or node.id.endswith("_STYLE")):
                named.append(f"{recipe.name}:{node.lineno} {node.id}")
    assert named == []


def test_the_printouts_keyword_is_the_table_and_the_run_s_own_tracers():
    """The keyword is GENERATED: the module's rows in the dictionary's order,
    then one token per tracer the run carries - the carrier's declared ones and
    the ones a coupled module appends behind them."""
    sheet = fill(T2D, NUMBER_OF_TRACERS=1,
                 NAMES_OF_TRACERS=["DYE             MG/L"],
                 coupling=[WAQTEL.o2(
                     water_temp_c=20.0, salinity_ppt=0.0, k1_per_day=0.3,
                     k4_per_day=0.0, k2_per_day=0.5, k2_formula=0,
                     saturation_mgl=9.1, benthic_demand=0.0,
                     photosynthesis_p=0.0, respiration_r=0.0)])
    # One declared tracer, three the O2 process appends behind it.
    assert [row.name for row in sheet.tracers] == [
        "DYE", "DISSOLVED O2", "ORGANIC LOAD", "NH4 LOAD"]
    assert sheet.printouts() == {
        "VARIABLES FOR GRAPHIC PRINTOUTS": "U,V,H,S,B,F,Q,M,T1,T2,T3,T4"}
    # A 3D module spells its tracer token its own way, and writes into its own
    # keyword; a module that writes no result of its own writes no keyword.
    assert T3D.printouts(tracers=1) == {
        "VARIABLES FOR 3D GRAPHIC PRINTOUTS": "Z,U,V,W,TA1"}
    assert WAQTEL.printouts() == {}


def test_a_token_the_keyword_does_not_spell_refuses_before_the_engine_reads_it():
    """Every generated token is checked against the dictionary's own choices, so
    a table the engine would not spell refuses here rather than in the Fortran."""
    original = dict(GAIA.MODULE_OUTPUT)
    GAIA.MODULE_OUTPUT = {**original, "QS": original["E"]}
    try:
        with pytest.raises(SlotRefused, match="does not\n?\\s*spell"):
            GAIA.printouts()
    finally:
        GAIA.MODULE_OUTPUT = original


def _run(telemac_result, monkeypatch) -> dict[str, Any]:
    """A two-variable reach result, and the run handle a solve hands the publish."""
    x = [500000.0, 500120.0, 500000.0, 500120.0, 500060.0]
    y = [4400000.0, 4400000.0, 4400110.0, 4400110.0, 4400055.0]
    depth = [[2.0] * 5, [2.5] * 5]
    dye = [[0.0] * 5, [10.0, 80.0, 5.0, 40.0, 60.0]]
    telemac_result(varnames=["WATER DEPTH", "DYE"], x=x, y=y,
                   ikle=[[0, 1, 4], [1, 3, 4], [2, 3, 4], [0, 2, 4]],
                   times=[0.0, 60.0],
                   data={"WATER DEPTH": depth, "DYE": dye})
    monkeypatch.setattr(
        "trid3nt_server.workflows.solver.solver.download_result",
        lambda run_id, basename, error_code=None: "/tmp/does-not-matter.slf")
    sheet = fill(T2D, NUMBER_OF_TRACERS=1,
                 NAMES_OF_TRACERS=["DYE             MG/L"])
    return {"run_id": "RID", "utm_epsg": 32610, "result_basename": "r2d.slf",
            "module": "telemac2d", "name": "reach",
            "tracer_names": ["DYE             MG/L"],
            "started_at": "2026-01-01T00:00:00+00:00",
            "module_output": [{"token": token, "module": module,
                               "style": row.style, "varies": row.varies}
                              for token, module, row in sheet.published()]}


@pytest.fixture()
def published(monkeypatch, fake_s3, telemac_result):
    """Every deliverable the publish was handed, in the order it was handed."""
    from trid3nt_server.workflows.telemac import workflow as door

    seen: list[Any] = []

    async def _publish(*, run_id, engine, name, items):
        seen.extend(items)
        return Published(primary=LayerURI(
            layer_id="L", name="Water depth", layer_type="mesh",
            uri="s3://runs/RID/r2d.slf", quantity="water_depth"))

    monkeypatch.setattr(door, "publish", _publish)
    return seen, _run(telemac_result, monkeypatch)


def test_every_row_the_result_carries_is_painted_and_a_varying_one_animated(
        published):
    """A variable's still is the FINAL FRAME and its caption is the engine's own
    result-file name; a row that varies in time animates beside it."""
    from trid3nt_server.render.formats import Mesh
    from trid3nt_server.workflows.telemac import workflow as door

    seen, run = published
    asyncio.run(door.publish_outputs(run=run, outputs=[], captions={},
                                     answer={}, params={}))
    # Two variables the result carries, each a still and an animation, captioned
    # by the name the result file gives it - so both are one quantity on one
    # scale, and neither caption came from a template.
    assert [item.caption for item in seen] == [
        "water depth", "water depth", "dye", "dye"]
    still, moving = seen[0].product, seen[1].product
    assert isinstance(still, Mesh) and still.t == 60.0 and still.frames is None
    assert moving.frames == 2 and moving.group == "WATER DEPTH"
    assert seen[0].style["ramp"] == "ylgnbu"


def test_a_row_the_result_does_not_carry_is_skipped_and_a_placed_read_refuses(
        published):
    """The table is what the module writes; what a run actually wrote is what it
    carries. A missing row is skipped - a missing PLACED read is a refusal."""
    from trid3nt_server.workflows.telemac import workflow as door
    from trid3nt_server.workflows.telemac.modules.outputs import OutputEmpty

    seen, run = published
    asyncio.run(door.publish_outputs(run=run, outputs=[], captions={},
                                     answer={}, params={}))
    # FREE SURFACE, BOTTOM, FROUDE NUMBER and the rest are rows this run did not
    # write, and nothing was published of them.
    assert {item.caption for item in seen} == {"water depth", "dye"}
    with pytest.raises(OutputEmpty):
        asyncio.run(door.publish_outputs(
            run=run, outputs=[series("S").chart()],
            captions={"S": "free surface"}, answer={}, params={}))


def test_a_coupled_module_states_the_tracers_it_appends_and_its_own_table():
    """A coupled module's rows are published beside the host's, and the tracers
    it appends to the carrier's result are its statement, not the carrier's."""
    sheet = fill(T2D, NUMBER_OF_TRACERS=1,
                 NAMES_OF_TRACERS=["MARKER          MG/L"],
                 boundaries=Boundaries(measured={
                     "liquid_boundary_order": [], "liquid_boundary_prescribes": [],
                     "inflow_q_m3s": 1.0, "outflow_stage_m": 0.0}, tracers=[]),
                 coupling=[GAIA.suspended(
                     geometry="a.slf", boundary="a.cli", mass_balance=True,
                     d50_um=30.0, concentration_mgl=250.0, transport_formula=3,
                     advection_scheme=[1])])
    assert [row.name for row in sheet.tracers] == ["MARKER", "NCOH SEDIMENT"]
    rows = sheet.published()
    # The bed itself is the carrier's row; GAIA rows what the sediment adds.
    assert [token for token, module, _ in rows if module == "gaia"] == [
        "E", "D50", "TOB"]
    # The appended class takes the style GAIA states for it, not the carrier's
    # own tracer row.
    appended = next(row for token, _, row in rows if token == "T2")
    assert appended.style["ramp"] == "oranges"
    # A bed with no suspension appends nothing at all.
    assert GAIA.APPENDS({"slots": {"bed": {}}}) == ()


def test_the_card_carries_every_variable_each_deck_writes():
    """The keyword is generated, so no slot carries it: the card states what
    each deck writes, expanded, in the module's own order."""
    from trid3nt_server.workflows.telemac.workflow import card_rows

    sheet = fill(T2D, NUMBER_OF_TRACERS=1,
                 NAMES_OF_TRACERS=["MARKER          MG/L"],
                 coupling=[GAIA.bed(
                     geometry="a.slf", boundary="a.cli", mass_balance=True,
                     gradation=None, presets={}, d50_um=200.0, thickness_m=5.0,
                     formula=1, hiding_factor_formula=1,
                     morphological_factor=10.0)])
    rows = {row.name: row for row in card_rows(sheet)}
    assert rows["telemac2d.VARIABLES_FOR_GRAPHIC_PRINTOUTS"].value == [
        "VELOCITY U", "VELOCITY V", "WATER DEPTH", "FREE SURFACE", "BOTTOM",
        "FROUDE NUMBER", "SCALAR FLOWRATE", "SCALAR VELOCITY", "MARKER"]
    assert rows["gaia.VARIABLES_FOR_GRAPHIC_PRINTOUTS"].value == [
        "CUMUL BED EVOL", "MEAN DIAMETER M", "BED SHEAR STRESS"]
