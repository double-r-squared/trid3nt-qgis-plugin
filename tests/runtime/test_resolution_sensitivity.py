"""The resolution-sensitivity label: which PUBLISHED reads a coarse mesh gets wrong.

The mechanism is skeleton-level: a template declares which of the quantities it
publishes sit in which measured class, and the run's own resolution lever decides
which sentence it gets. The DIRECTION tests drive the real resolver, because the
user/default distinction is a property of the rows it produces and a hand-built
row can assert a fiction.
"""

from __future__ import annotations

import asyncio

import pytest

from trid3nt_server.workflows.runtime.resolver import resolve_params
from trid3nt_server.workflows.runtime.resolution import (
    CLASSES,
    SensitivityDecl,
    sensitivity_notes,
)


class _Row:
    def __init__(self, name: str, basis: str) -> None:
        self.name, self.basis = name, basis


class _Spec:
    param = "target_resolution_m"


class _Meta:
    resolution_specs = (_Spec(),)


_DECL = SensitivityDecl((("water_depth", "extent"),
                         ("inundation_depth", "peak")))


def test_a_declaration_refuses_a_class_nobody_can_read() -> None:
    with pytest.raises(ValueError) as excinfo:
        SensitivityDecl((("dye_concentration", "vibes"),))
    assert "vibes" in str(excinfo.value)
    assert all(c in str(excinfo.value) for c in CLASSES)


#: A template whose resolution lever is optional on the USER door and whose
#: derivations are pure, so the rows under test are the real resolver's output
#: and no network is touched.
_TEMPLATE, _LEVER = "telemac_do_sag", "mesh_resolution_m"
_QUANTITY = "dissolved_oxygen"


def _resolved_rows(**supplied):
    """The REAL rows: what ``resolve_params`` seats for this template's params."""
    from trid3nt_server.tools import TOOL_REGISTRY

    workflow = TOOL_REGISTRY[_TEMPLATE].fn.workflow
    rows = asyncio.run(resolve_params(workflow.params, supplied)).rows()
    return workflow, rows


def test_a_default_spacing_run_is_labeled_a_bound() -> None:
    """The un-refined run is the case the evidence was measured on."""
    workflow, rows = _resolved_rows(location="Eel River near Scotia, California")

    row = next(r for r in rows if r.name == _LEVER)
    assert row.basis == "default_demo" and row.value is not None, (
        "the edge is always an explicit value; nobody supplied one, so the "
        "labeled default fills it and its BASIS is what separates a run the user "
        "refined from one left where the template put it")

    notes = sensitivity_notes(
        workflow.sensitivity, workflow.metadata, {_QUANTITY}, rows,
        mesh_size_m=250.0)
    assert len(notes) == 1, "one mesh is one fact, not one note per quantity"
    note = notes[0]
    assert note.startswith("RESOLUTION-LIMITED, TREAT AS A BOUND:")
    assert _QUANTITY in note
    assert "250 m" in note
    assert _LEVER in note, "the note names the lever to turn"
    assert "unsafe direction" in note


def test_a_refined_run_says_refined_is_not_converged() -> None:
    workflow, rows = _resolved_rows(location="Eel River near Scotia, California",
                                    **{_LEVER: 25.0})

    row = next(r for r in rows if r.name == _LEVER)
    assert row.basis == "user" and row.value == 25.0

    notes = sensitivity_notes(
        workflow.sensitivity, workflow.metadata, {_QUANTITY}, rows,
        mesh_size_m=25.0)
    assert len(notes) == 1
    assert notes[0].startswith("RESOLUTION-SENSITIVE:")
    assert "not a demonstrated convergence" in notes[0]
    assert "25 m" in notes[0]


def test_a_granularity_stated_as_a_keyword_is_read_off_the_fill() -> None:
    """A lever the module carries as a KEYWORD has no param row, so the note
    reads the solved deck's own slots: a run whose plane count the user stated
    is refined, and one left at the deck's own is a bound."""
    from trid3nt_server.tools import TOOL_REGISTRY

    workflow = TOOL_REGISTRY["telemac3d_stratified_flow"].fn.workflow
    lever = workflow.metadata.resolution_specs[0].param
    published = {"water_temperature"}
    rows = asyncio.run(resolve_params(workflow.params, {})).rows()

    bound = sensitivity_notes(workflow.sensitivity, workflow.metadata, published,
                              rows, fill={lever: "template: STEERING"})
    assert bound[0].startswith("RESOLUTION-LIMITED, TREAT AS A BOUND:")
    refined = sensitivity_notes(workflow.sensitivity, workflow.metadata,
                                published, rows, fill={lever: "user"})
    assert refined[0].startswith("RESOLUTION-SENSITIVE:")


def test_a_quantity_the_run_did_not_publish_is_not_labeled() -> None:
    """A note about a product that is not there points at nothing."""
    notes = sensitivity_notes(
        _DECL, _Meta(), {"water_depth"},
        [_Row("target_resolution_m", "derived")], mesh_size_m=250.0)
    assert "inundation_depth" not in notes[0]
    assert "water_depth" in notes[0]


def test_no_declaration_and_nothing_published_are_both_no_note() -> None:
    assert sensitivity_notes(SensitivityDecl(), _Meta(), {"water_depth"}, []) == ()
    assert sensitivity_notes(_DECL, _Meta(), set(), []) == ()


@pytest.mark.parametrize("template,quantity,cls", [
    ("telemac_do_sag", "dissolved_oxygen", "location"),
    ("telemac_dye_release", "dye_concentration", "peak"),
    ("artemis_harbor_agitation", "agitation_coefficient", "peak"),
    ("telemac3d_stratified_flow", "water_temperature", "gradient"),
])
def test_every_telemac_template_declares_its_sensitive_reads(
        template: str, quantity: str, cls: str) -> None:
    """The three named templates plus the rest of the family, from one evidence set."""
    from trid3nt_server.tools import TOOL_REGISTRY

    workflow = TOOL_REGISTRY[template].fn.workflow
    assert (quantity, cls) in workflow.sensitivity.rows
    # a converged class must NOT be labeled: labeling everything is labeling nothing
    for converged in ("bottom", "free_surface", "sheltering_ratio"):
        assert converged not in dict(workflow.sensitivity.rows)


def test_every_declared_quantity_is_one_a_template_publishes() -> None:
    """A declaration names what the RUN publishes, so every row has to be a
    caption this template gives one of its own reads or a variable its module
    writes - never a field of its own."""
    from trid3nt_server.render.formats import quantity_of
    from trid3nt_server.tools import TOOL_REGISTRY
    from trid3nt_server.workflows.telemac.modules import wrapper_for

    for entry in TOOL_REGISTRY.values():
        workflow = getattr(getattr(entry, "fn", None), "workflow", None)
        if workflow is None or not workflow.sensitivity:
            continue
        captioned = {quantity_of(caption)
                     for caption in workflow.captions.values()}
        written = {quantity_of(row.name)
                   for module in ("telemac2d", "telemac3d", "gaia", "khione",
                                  "tomawac", "waqtel", "artemis")
                   for row in wrapper_for(module).MODULE_OUTPUT.values()}
        for quantity, _cls in workflow.sensitivity.rows:
            assert quantity in captioned | written, (
                f"{workflow.name} declares {quantity!r}, which is neither a "
                "caption it gives a read nor a variable a module writes")


def test_the_spacing_is_read_off_the_mesh_the_run_published() -> None:
    """The edge is a fact of the SOLVE, so the note reads the solve step's own
    record rather than a field on the layer."""
    from trid3nt_server.workflows.runtime.workflow import RunResult
    from trid3nt_server.tools import TOOL_REGISTRY

    workflow = TOOL_REGISTRY[_TEMPLATE].fn.workflow
    run = RunResult(value=None)
    run.results[workflow.solve_step] = {"mesh_size_m": 62.5}
    assert workflow._mesh_size_m(run) == 62.5


def test_the_published_set_is_the_run_s_own_layers_and_charts() -> None:
    """What a run published is read off its own record, so a chart and a layer
    of one quantity are one name."""
    from trid3nt_server.workflows.runtime.workflow import RunResult
    from trid3nt_server.tools import TOOL_REGISTRY

    workflow = TOOL_REGISTRY[_TEMPLATE].fn.workflow
    run = RunResult(value=None)
    run.outputs.append({"quantity": "water_depth"})
    run.outputs.append({"quantity": None})
    run.charts["dissolved_oxygen"] = {}
    assert workflow._published(run) == {"water_depth", "dissolved_oxygen"}
