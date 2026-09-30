"""The run holds ONE mapping and everything reads it by plain name.

A composite reads what it needs off the mapping itself, a template passes only
literals and names, and every name a template writes is checked at the fill:
one nothing of the run is called is refused there, by name."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import trid3nt_server
from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.workflows.runtime import resolve_params
from trid3nt_server.workflows.runtime.fill import (REJECTED, Fill, fill,
                                                   production)
from trid3nt_server.workflows.telemac.modules import T2D
from trid3nt_server.workflows.telemac.modules.module import SlotRefused
from trid3nt_server.workflows.telemac.modules.telemac2d import Sources
from trid3nt_server.workflows.telemac.workflow import Measured, _read_named


def _workflow(name: str):
    return TOOL_REGISTRY[name].fn.workflow


def _refused_at_fill(workflow, name: str) -> None:
    state = asyncio.run(fill(Fill(workflow=workflow), {}))
    verdict = state.inputs[name]
    assert verdict.state == REJECTED and verdict.code == "INPUT_UNNAMED"
    assert repr(name) in verdict.reason
    assert not state.ready


def test_a_composite_expands_from_the_mapping_with_only_a_literal_passed():
    """The horizon is the settle's and the discharge and the tracer are the
    keywords', so the template hands the composite its one real choice."""
    slots, files = T2D.COMPOSITES["sources"].apply(
        Sources(window_s=120.0),
        {"settled": {"until_s": 600.0}, "WATER_DISCHARGE_OF_SOURCES": [8.0],
         "VALUES_OF_THE_TRACERS_AT_THE_SOURCES": [100.0]})
    assert dict(slots) == {"SOURCES_FILE": "river_sources.txt"}
    assert files["river_sources.txt"].splitlines()[3:] == [
        "0.000 8 100", "120.000 8 100", "120.100 0 0", "700.000 0 0"]


def test_a_composite_reading_a_name_the_mapping_lacks_refuses_by_name():
    with pytest.raises(SlotRefused, match="'settled'"):
        T2D.COMPOSITES["sources"].apply(Sources(window_s=120.0), {})


def test_a_misspelled_input_in_a_recipe_is_refused_by_name_at_fill(monkeypatch):
    from trid3nt_server.mesh.tool import mesh_op, tool
    from trid3nt_server.workflows.telemac.templates.rain_on_grid import (
        rain_on_grid as template)

    monkeypatch.setattr(template, "MESH", tool.build_mesh(
        mesher="om2d", kind="unstructured_tri", extent="domain",
        resolution_m="mesh_resolution_m",
        ops=[mesh_op("set_bed", source="bedd", condition="pit_fill")]))
    _refused_at_fill(_workflow("telemac_rain_on_grid"), "bedd")


def test_a_misspelled_input_in_a_measurement_is_refused_by_name_at_fill(
        monkeypatch):
    from trid3nt_server.workflows.telemac.templates.channel_dredging import (
        channel_dredging as template)

    monkeypatch.setattr(template, "_DREDGE", Measured(
        "dredge", kind="dredge",
        reads={**template._DREDGE.reads, "grade_depth_m": "design_dpeth_m"},
        asked=template._DREDGE.asked))
    _refused_at_fill(_workflow("telemac_channel_dredging"), "design_dpeth_m")


def test_a_misspelled_input_in_a_composite_is_refused_by_name_at_fill(
        monkeypatch):
    from trid3nt_server.workflows.telemac.templates.dye_release import (
        dye_release as template)

    class MISSPELT(T2D):
        sources = Sources(window_s="spill_duraton_s")

    monkeypatch.setattr(template, "STEERING", MISSPELT)
    _refused_at_fill(_workflow("telemac_dye_release"), "spill_duraton_s")


def test_a_measurement_reads_a_template_input_by_name():
    from trid3nt_server.workflows.telemac.templates.channel_dredging import (
        channel_dredging as template)

    workflow = _workflow("telemac_channel_dredging")
    state = Fill(workflow=workflow, params=asyncio.run(
        resolve_params(workflow.params, {"design_depth_m": 7.5})))
    env = production(state)
    env.run.update(dredge_area="the fairway", dump_area="the dump site")
    read = asyncio.run(_read_named(env, dict(template._DREDGE.reads)))
    assert read == {"areas": {"dredge_area": "the fairway",
                              "dump_area": "the dump site"},
                    "grade_depth_m": 7.5}


def test_no_placeholder_read_is_left_in_the_server():
    root = Path(trid3nt_server.__file__).parent
    left = [f"{path.relative_to(root)}: {word}"
            for path in sorted(root.rglob("*.py"))
            for word in ("Ref(", "ParamRef", "DataRef")
            if word in path.read_text(encoding="utf-8")]
    assert left == []


def test_every_template_names_only_what_its_run_is_called():
    """A word an op's own signature takes as a string - set_bed's condition -
    stays a word; every other name a template writes is one of its run's."""
    from trid3nt_server.mesh.recipe import input_names
    from trid3nt_server.workflows.telemac.templates.rain_on_grid import (
        rain_on_grid as template)

    names = input_names(template.MESH)
    assert "bed" in names and "pit_fill" not in names
    workflows = [tool.fn.workflow for tool in TOOL_REGISTRY.values()
                 if hasattr(getattr(tool.fn, "workflow", None), "unnamed")]
    assert workflows
    assert {wf.name: wf.unnamed() for wf in workflows
            if wf.unnamed()} == {}
