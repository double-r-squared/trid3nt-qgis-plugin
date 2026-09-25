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
        WATER_TEMPERATURE=20.0, WATER_SALINITY=0.0,
        CONSTANT_OF_DEGRADATION_OF_ORGANIC_LOAD_K1=0.3,
        CONSTANT_OF_NITRIFICATION_KINETIC_K4=0.0,
        FORMULA_FOR_COMPUTING_K2=0, K2_REAERATION_COEFFICIENT=0.5,
        O2_SATURATION_DENSITY_OF_WATER__CS_=9.1, BENTHIC_DEMAND=0.0,
        PHOTOSYNTHESIS_P=0.0, VEGETAL_RESPIRATION_R=0.0)])
    # One declared tracer, three the O2 process appends behind it.
    assert [row.name for row in sheet.tracers] == [
        "DYE", "DISSOLVED O2", "ORGANIC LOAD", "NH4 LOAD"]
    assert sheet.printouts() == {
        "VARIABLES FOR GRAPHIC PRINTOUTS": "U,V,H,S,B,F,Q,M,X,Y,T1,T2,T3,T4"}
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


def _run(telemac_result, monkeypatch, dye=None, unrowed=None) -> dict[str, Any]:
    """A two-variable reach result, and the run handle a solve hands the publish.

    ``unrowed`` states variables the file carries that no module row names, as
    ``spelling -> (unit, frames)``."""
    x = [500000.0, 500120.0, 500000.0, 500120.0, 500060.0]
    y = [4400000.0, 4400000.0, 4400110.0, 4400110.0, 4400055.0]
    dye = dye if dye is not None else [[0.0] * 5, [10.0, 80.0, 5.0, 40.0, 60.0]]
    depth = [[2.0] * 5] * len(dye)
    unrowed = dict(unrowed or {})
    telemac_result(varnames=["WATER DEPTH", "DYE", *unrowed], x=x, y=y,
                   varunits=["M", "MG/L", *(unit for unit, _ in unrowed.values())],
                   ikle=[[0, 1, 4], [1, 3, 4], [2, 3, 4], [0, 2, 4]],
                   times=[float(60 * n) for n in range(len(dye))],
                   data={"WATER DEPTH": depth, "DYE": dye,
                         **{name: frames for name, (_, frames) in unrowed.items()}})
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
                               "style": row.style, "varies": row.varies,
                               "has_edge": bool(row.has_edge)}
                              for token, module, row in sheet.published()]}


@pytest.fixture()
def published(monkeypatch, fake_s3, telemac_result):
    """Every deliverable the publish was handed, in the order it was handed."""
    from trid3nt_server.workflows.telemac import workflow as door

    seen: list[Any] = []

    async def _publish(*, run_id, engine, name, items):
        seen.extend(items)
        return Published(layers=(LayerURI(
            layer_id="L", name="Water depth", layer_type="mesh",
            uri="s3://runs/RID/r2d.slf", quantity="water_depth"),))

    monkeypatch.setattr(door, "publish", _publish)
    return seen, _run(telemac_result, monkeypatch)


def test_a_varying_row_is_one_temporal_layer_and_a_still_row_its_frame(published):
    """A row that varies in time publishes ONE layer - the temporal one, styled;
    its caption is the engine's own result-file name."""
    from trid3nt_server.render.formats import Mesh
    from trid3nt_server.workflows.telemac import workflow as door

    seen, run = published
    asyncio.run(door.publish_outputs(run=run, outputs=[], captions={},
                                     params={}))
    # Two variables the result carries, ONE layer each, captioned by the name the
    # result file gives it - so neither caption came from a template.
    assert [item.caption for item in seen] == ["water depth", "dye"]
    depth, dye = seen[0].product, seen[1].product
    assert isinstance(depth, Mesh) and depth.frames == 2 and depth.t is None
    assert depth.group == "WATER DEPTH" and dye.group == "DYE"
    assert seen[0].style["ramp"] == "ylgnbu"


def test_a_temporal_layer_is_ranged_over_the_record_not_its_last_frame(
        monkeypatch, fake_s3, telemac_result):
    """A tracer that peaks then flushes is ranged over every frame: ranged on the
    last frame alone the legend collapses and the animation paints blank."""
    from trid3nt_server.render.formats import Published
    from trid3nt_server.workflows.telemac import workflow as door

    seen: list[Any] = []

    async def _publish(*, run_id, engine, name, items):
        seen.extend(items)
        return Published(layers=(LayerURI(
            layer_id="L", name="Dye", layer_type="mesh",
            uri="s3://runs/RID/r2d.slf", quantity="dye"),))

    monkeypatch.setattr(door, "publish", _publish)
    run = _run(telemac_result, monkeypatch,
               dye=[[0.0] * 5, [25.0] * 5, [0.0] * 5])
    asyncio.run(door.publish_outputs(run=run, outputs=[], captions={},
                                     params={}))
    dye = next(item.product for item in seen if item.caption == "dye")
    assert dye.frames == 3
    # The tracer's own visible edge is the bottom and its RECORD peak the top.
    assert dye.value_range[1] == pytest.approx(25.0)
    # The tracer row declares its own floor, and a declared floor pins the bottom.
    assert dye.value_range[0] == pytest.approx(0.0)


def test_the_froude_row_is_ranged_over_the_wet_nodes_and_not_capped():
    """|u|/sqrt(gh) at a node with no depth is a number in the thousands and the
    water beside it is subcritical. Those nodes are outside the WET MASK the
    legend is taken over, so the ramp reads the river with no percentile cap
    clipping the real peak off the top."""
    import numpy as np

    from trid3nt_server.render import presets
    from trid3nt_server.workflows.telemac.modules.outputs import _drawn
    from trid3nt_server.workflows.telemac.modules.telemac2d import MODULE_OUTPUT

    style = MODULE_OUTPUT["F"].style
    assert "range" not in style, "the wet mask ranges this row, not a cap"
    # A reach record's own shape: a subcritical field with a handful of drying
    # nodes carrying the edge value the solver leaves there.
    record = np.concatenate([np.linspace(0.0, 0.46, 9995), np.full(5, 4179.0)])
    wet = np.concatenate([np.full(9995, True), np.full(5, False)])
    lo, hi = presets.measured_range(_drawn(record, wet), style)
    assert lo == 0.0, "the declared floor still pins the bottom"
    assert 0.4 < hi < 1.0, hi
    # and the real peak is NOT clipped: the top is what the wet nodes reached.
    assert hi >= 0.46


def test_a_row_the_result_does_not_carry_is_skipped_and_a_placed_read_refuses(
        published):
    """The table is what the module writes; what a run actually wrote is what it
    carries. A missing row is skipped - a missing PLACED read is a refusal."""
    from trid3nt_server.workflows.telemac import workflow as door
    from trid3nt_server.workflows.telemac.modules.outputs import OutputEmpty

    seen, run = published
    asyncio.run(door.publish_outputs(run=run, outputs=[], captions={},
                                     params={}))
    # FREE SURFACE, BOTTOM, FROUDE NUMBER and the rest are rows this run did not
    # write, and nothing was published of them.
    assert {item.caption for item in seen} == {"water depth", "dye"}
    with pytest.raises(OutputEmpty):
        asyncio.run(door.publish_outputs(
            run=run, outputs=[series("S").chart()],
            captions={"S": "free surface"}, params={}))


def _surfacing(monkeypatch, surfaced=None) -> list[Any]:
    """Stand in for the publish: every mesh item it is handed surfaces as a layer
    under its caption's quantity, or only the quantities ``surfaced`` names."""
    from trid3nt_server.render.formats import Mesh, quantity_of
    from trid3nt_server.workflows.telemac import workflow as door

    seen: list[Any] = []

    async def _publish(*, run_id, engine, name, items):
        seen.extend(items)
        return Published(layers=tuple(
            LayerURI(layer_id=f"L{n}", name=item.caption, layer_type="mesh",
                     uri="s3://runs/RID/r2d.slf",
                     quantity=quantity_of(item.caption))
            for n, item in enumerate(items)
            if isinstance(item.product, Mesh)
            and (surfaced is None or quantity_of(item.caption) in surfaced)))

    monkeypatch.setattr(door, "publish", _publish)
    return seen


def _notes(run) -> list[str]:
    from trid3nt_server.workflows.runtime import journal
    from trid3nt_server.workflows.telemac import workflow as door

    token = journal.bind_notes()
    try:
        asyncio.run(door.publish_outputs(run=run, outputs=[], captions={},
                                         params={}))
    finally:
        notes = journal.drain_notes(token)
    return notes


def test_every_variable_the_result_wrote_is_published_even_where_no_row_names_it(
        monkeypatch, fake_s3, telemac_result):
    """The RESULT FILE is the list. A variable the engine wrote and the module's
    table rows no name for - a spelling ending in a full stop among them - is
    published under the spelling the file itself carries; a row is what styles
    a layer, never what decides it is on the map."""
    from trid3nt_server.render.formats import Mesh

    seen = _surfacing(monkeypatch)
    run = _run(telemac_result, monkeypatch,
               unrowed={"SOLID ICE THICK.": ("[SI]", [[0.0] * 5, [0.4] * 5]),
                        "FRAZIL THETA0": ("[SI]", [[0.0] * 5, [0.2] * 5])})
    notes = _notes(run)
    captions = [item.caption for item in seen]
    assert captions == ["water depth", "dye", "solid ice thick.",
                        "frazil theta0"]
    unrowed = [item.product for item in seen if item.caption.endswith("thick.")]
    assert isinstance(unrowed[0], Mesh) and unrowed[0].group == "SOLID ICE THICK."
    # The unit is the record's own, because no row declares one for it.
    assert unrowed[0].units == "[si]"
    assert [n for n in notes if "'SOLID ICE THICK.'" in n and "published" in n]
    assert [n for n in notes if "'FRAZIL THETA0'" in n and "published" in n]


def test_a_variable_the_engine_never_wrote_reaches_no_layer_and_the_note_says_so(
        monkeypatch, fake_s3, telemac_result):
    """UNWRITTEN NEVER: a slot the engine allocated and never wrote carries no
    number at all, so no layer paints it - and the journal says so by the
    file's own spelling, full stop included, rather than dropping it unsaid."""
    nan = float("nan")
    seen = _surfacing(monkeypatch)
    run = _run(telemac_result, monkeypatch,
               unrowed={"SOLID ICE THICK.": ("[SI]", [[nan] * 5, [nan] * 5])})
    notes = _notes(run)
    assert "solid ice thick." not in [item.caption for item in seen]
    said = [n for n in notes if "'SOLID ICE THICK.'" in n]
    assert said and "no layer carries it" in said[0], notes
    assert "never wrote it" in said[0], said
    assert not [n for n in said if "published under" in n]


def test_a_slot_the_engine_source_marks_unwritten_is_neither_asked_nor_published(
        monkeypatch, fake_s3, telemac_result):
    """ONE SET, BOTH DIRECTIONS: a slot in UNWRITTEN is never a token the
    printouts keyword asks for, and a file that carries it - finite values and
    all, as a scratch array leaves it - puts it on no layer; the journal names
    it with the engine source line that marks it."""
    from trid3nt_server.workflows.telemac.modules.khione import KHIONE

    for stated in ({}, {"HEAT_BUDGET": True, "DYNAMIC_ICE_COVER": True}):
        assert not set(KHIONE.UNWRITTEN) & set(KHIONE.written(stated))
        asked = fill(KHIONE, **stated).printouts()
        for value in asked.values():
            assert not set(KHIONE.UNWRITTEN) & set(value.split(","))
    marked = KHIONE.UNWRITTEN["COV_THF"]
    seen = _surfacing(monkeypatch)
    run = _run(telemac_result, monkeypatch,
               unrowed={marked.spelling: ("[SI]", [[0.3] * 5, [0.7] * 5]),
                        "SOLID ICE THICK.": ("[SI]", [[0.0] * 5, [0.4] * 5])})
    run = {**run, "module": "khione", "module_output": [],
           "result_basename": KHIONE.RESULT_FILE}
    notes = _notes(run)
    captions = [item.caption for item in seen]
    assert marked.spelling.lower() not in captions, captions
    assert "solid ice thick." in captions
    said = [n for n in notes if repr(marked.spelling) in n]
    assert said and "no layer carries it" in said[0], notes
    assert "point_khione.f" in said[0] and "DEPRECATED" in said[0], said


def test_a_note_never_claims_a_layer_the_publish_did_not_surface(
        monkeypatch, fake_s3, telemac_result):
    """A note is written FROM the layer list: a variable the publish did not
    surface is never said to be published, whatever the reads handed it."""
    _surfacing(monkeypatch, surfaced={"water_depth", "dye"})
    run = _run(telemac_result, monkeypatch,
               unrowed={"SOLID ICE THICK.": ("[SI]", [[0.0] * 5, [0.4] * 5])})
    said = [n for n in _notes(run) if "'SOLID ICE THICK.'" in n]
    assert said and "no layer carries it" in said[0], said
    assert not [n for n in said if "published under" in n]


def test_a_spelling_two_result_files_carry_is_one_layer_under_one_label():
    """A coupled module writes its host's tracers into its own file as well: the
    spelling is ONE layer, off the first row naming it, so no two layers share
    a label; the repeat is kept to be journalled against the layer list."""
    from types import SimpleNamespace

    from trid3nt_contracts.execution import LayerURI as Layer

    from trid3nt_server.workflows.runtime import journal
    from trid3nt_server.workflows.telemac import workflow as door

    files = {"telemac2d": ("r2d.slf", ["WATER DEPTH", "FRAZIL"]),
             "khione": ("khione.slf", ["FRAZIL", "FRAZIL S"])}

    def _solved(module):
        file, names = files[module]
        return SimpleNamespace(
            result_file=file, result={"varnames": names},
            body=door.wrapper_for(module),
            variable=lambda token: ({"H": "WATER DEPTH", "T1": "FRAZIL",
                                     "F1": "FRAZIL", "SF1": "FRAZIL S"}[token],
                                    ""))

    run = {"module": "telemac2d", "module_output": [
        {"token": token, "module": module, "varies": True}
        for token, module in (("H", "telemac2d"), ("T1", "telemac2d"),
                              ("F1", "khione"), ("SF1", "khione"))]}
    written = door._written(run, _solved)
    drawn = [(w.file, w.spelling) for w in written if w.first is None]
    assert drawn == [("r2d.slf", "WATER DEPTH"), ("r2d.slf", "FRAZIL"),
                     ("khione.slf", "FRAZIL S")]
    echo = [w for w in written if w.first is not None]
    assert [(w.file, w.spelling, w.first) for w in echo] == [
        ("khione.slf", "FRAZIL", "r2d.slf")]
    token = journal.bind_notes()
    try:
        door._account(written, [Layer(layer_id="F", name="Frazil",
                                      layer_type="mesh", uri="s3://r/x.slf",
                                      quantity="frazil")], {})
    finally:
        notes = journal.drain_notes(token)
    assert [n for n in notes if n.startswith("khione.slf wrote 'FRAZIL', which "
                                             "r2d.slf wrote as well")]


def test_a_coupled_module_states_the_tracers_it_appends_and_its_own_table():
    """A coupled module's rows are published beside the host's, and the tracers
    it appends to the carrier's result are its statement, not the carrier's."""
    sheet = fill(T2D, NUMBER_OF_TRACERS=1,
                 NAMES_OF_TRACERS=["MARKER          MG/L"],
                 boundaries=Boundaries(measured={
                     "liquid_boundary_order": [], "liquid_boundary_prescribes": [],
                     "inflow_q_m3s": 1.0, "outflow_stage_m": 0.0}, tracers=[]),
                 coupling=[GAIA.suspended(
                     geometry="a.slf", boundary="a.cli", MASS_BALANCE=True,
                     CLASSES_SEDIMENT_DIAMETERS=[3e-05], concentration_mgl=250.0, SUSPENSION_TRANSPORT_FORMULA_FOR_ALL_SANDS=3,
                     SCHEME_FOR_ADVECTION_OF_SUSPENDED_SEDIMENTS=[1])])
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
                     geometry="a.slf", boundary="a.cli", MASS_BALANCE=True,
                     gradation=None, presets={}, CLASSES_SEDIMENT_DIAMETERS=[0.0002], LAYERS_INITIAL_THICKNESS=[5.0],
                     BED_LOAD_TRANSPORT_FORMULA_FOR_ALL_SANDS=1, HIDING_FACTOR_FORMULA=1,
                     MORPHOLOGICAL_FACTOR=10.0)])
    rows = {row.name: row for row in card_rows(sheet)}
    assert rows["telemac2d.VARIABLES_FOR_GRAPHIC_PRINTOUTS"].value == [
        "VELOCITY U", "VELOCITY V", "WATER DEPTH", "FREE SURFACE", "BOTTOM",
        "FROUDE NUMBER", "SCALAR FLOWRATE", "SCALAR VELOCITY", "WIND ALONG X",
        "WIND ALONG Y", "MARKER"]
    assert rows["gaia.VARIABLES_FOR_GRAPHIC_PRINTOUTS"].value == [
        "CUMUL BED EVOL", "MEAN DIAMETER M", "BED SHEAR STRESS"]


def test_the_published_order_is_the_table_order_across_host_and_coupled(
        monkeypatch, fake_s3, telemac_result):
    """Nothing leads: every layer is surfaced, the host's table first and each
    coupled module's behind it, and the run returns its own record."""
    from trid3nt_server.render import layer_uri_emit
    from trid3nt_server.workflows.telemac import workflow as door

    surfaced: list[str] = []

    async def _surface(_emitter, layer, *, role="input", fallbacks=None):
        surfaced.append(layer.name)
        return True

    monkeypatch.setattr(layer_uri_emit, "publish_input_layer", _surface)
    monkeypatch.setattr(
        "trid3nt_server.workflows.solver.solver.download_result",
        lambda run_id, basename, error_code=None: "/tmp/does-not-matter.slf")
    x = [500000.0, 500120.0, 500000.0, 500120.0, 500060.0]
    y = [4400000.0, 4400000.0, 4400110.0, 4400110.0, 4400055.0]
    telemac_result(varnames=["WATER DEPTH", "CUMUL BED EVOL"], x=x, y=y,
                   ikle=[[0, 1, 4], [1, 3, 4], [2, 3, 4], [0, 2, 4]],
                   times=[0.0, 60.0],
                   data={"WATER DEPTH": [[2.0] * 5, [2.5] * 5],
                         "CUMUL BED EVOL": [[0.0] * 5,
                                            [0.1, 0.2, 0.0, -0.1, 0.05]]})
    sheet = fill(T2D, coupling=[GAIA.bed(
        geometry="a.slf", boundary="a.cli", MASS_BALANCE=True, gradation=None,
        presets={}, CLASSES_SEDIMENT_DIAMETERS=[0.0002], LAYERS_INITIAL_THICKNESS=[5.0], BED_LOAD_TRANSPORT_FORMULA_FOR_ALL_SANDS=1,
        HIDING_FACTOR_FORMULA=1, MORPHOLOGICAL_FACTOR=10.0)])
    run = {"run_id": "RID", "utm_epsg": 32610, "result_basename": "r2d.slf",
           "module": "telemac2d", "name": "reach",
           "started_at": "2026-01-01T00:00:00+00:00",
           "module_output": [{"token": token, "module": module,
                              "style": row.style, "varies": row.varies,
                              "has_edge": bool(row.has_edge)}
                             for token, module, row in sheet.published()]}
    record = asyncio.run(door.publish_outputs(run=run, outputs=[], captions={},
                                              params={}))

    # The host's row is first and the coupled module's is behind it - the order
    # the two tables state, one temporal layer each.
    assert surfaced == ["Water depth over time (reach)",
                        "Cumul bed evol over time (reach)"]
    # The return is the run's record: the mesh every group rides, no group bound
    # and nothing measured on it.
    assert record.uri.endswith("/RID/r2d.slf")
    assert record.style == {"kind": "reference"}
    assert record.layer_id == "telemac-RID"


def test_the_card_carries_a_generated_keyword_once_per_deck():
    """A keyword the sheet GENERATES is not an engine default: it states the
    module's own variable table once, and never again under the advanced fold."""
    from trid3nt_server.workflows.telemac.workflow import card_rows

    sheet = fill(T2D, coupling=[GAIA.bed(
        geometry="a.slf", boundary="a.cli", MASS_BALANCE=True, gradation=None,
        presets={}, CLASSES_SEDIMENT_DIAMETERS=[0.0002], LAYERS_INITIAL_THICKNESS=[5.0], BED_LOAD_TRANSPORT_FORMULA_FOR_ALL_SANDS=1,
        HIDING_FACTOR_FORMULA=1, MORPHOLOGICAL_FACTOR=10.0)])
    printouts = [row for row in card_rows(sheet)
                 if row.name.endswith(".VARIABLES_FOR_GRAPHIC_PRINTOUTS")
                 or row.name == "VARIABLES_FOR_GRAPHIC_PRINTOUTS"]
    assert [row.name for row in printouts] == [
        "telemac2d.VARIABLES_FOR_GRAPHIC_PRINTOUTS",
        "gaia.VARIABLES_FOR_GRAPHIC_PRINTOUTS"]
    assert all(row.source_badge.endswith("own variable table")
               for row in printouts)


def test_every_module_that_appends_rows_is_on_the_modules_page():
    """A module that writes no table of its own still states what it APPENDS, so
    the page carries it - and the hook hands back exactly what it declared."""
    from trid3nt_server.workflows.telemac.modules import WRAPPERS

    page = (Path(__file__).resolve().parents[2]
            / "docs" / "modules.md").read_text(encoding="utf-8")
    appending = {module: wrapper for module, wrapper in WRAPPERS.items()
                 if wrapper.APPENDABLE}
    assert set(appending) == {"gaia", "khione", "waqtel"}
    for module, wrapper in sorted(appending.items()):
        assert f"### `{module}`" in page
        for condition, rows in wrapper.APPENDABLE:
            assert f"appended by {condition}: " in page
            for row in rows:
                assert f"`{row.name}`" in page
    for condition, rows in WAQTEL.APPENDABLE:
        process = int(condition.split()[-1])
        assert list(WAQTEL.APPENDS({"process": process})) == list(rows)
