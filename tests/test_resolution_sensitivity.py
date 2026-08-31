"""The resolution-sensitivity label: which answers a coarse mesh reads wrong.

The mechanism is skeleton-level, so these pin it at the library rather than
through any one engine: a template declares which of its ANSWER fields sit in
which measured class, and the run's own sheet decides which of the two sentences
it gets.

The two DIRECTION tests drive the real resolver rather than a hand-built row: the
user/default distinction is a property of the sheet the resolver produces, and a
row written by hand can assert a shape the resolver never emits.
"""

from __future__ import annotations

import asyncio

import pytest

from trid3nt_server.workflows.lib.resolver import resolve_params
from trid3nt_server.workflows.lib.resolution import (
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


class _Result:
    def __init__(self, **kw) -> None:
        self.__dict__.update(kw)


_DECL = SensitivityDecl((("flooded_land_km2", "extent"),
                         ("inundation_peak_depth_m", "peak")))


def test_a_declaration_refuses_a_class_nobody_can_read() -> None:
    with pytest.raises(ValueError) as excinfo:
        SensitivityDecl((("dye_cmax_mgl", "vibes"),))
    assert "vibes" in str(excinfo.value)
    assert all(c in str(excinfo.value) for c in CLASSES)


#: A template whose resolution lever is optional on the USER door and whose
#: derivations are pure, so the sheet under test is the real resolver's output
#: and no network is touched.
_TEMPLATE, _LEVER, _FIELD = "telemac_do_sag", "mesh_resolution_m", "do_min_distance_m"


def _resolved_sheet(**supplied):
    """The REAL sheet: what ``resolve_params`` seats for this template's params."""
    from trid3nt_server.tools import TOOL_REGISTRY

    workflow = TOOL_REGISTRY[_TEMPLATE].fn.workflow
    sheet = asyncio.run(resolve_params(workflow.params, supplied)).rows()
    return workflow, sheet


def test_a_default_spacing_run_is_labeled_a_bound() -> None:
    """The un-refined run is the case the evidence was measured on."""
    workflow, sheet = _resolved_sheet(location="Eel River near Scotia, California")

    row = next(r for r in sheet if r.name == _LEVER)
    assert row.basis == "default_demo" and row.value is not None, (
        "the edge is always an explicit sheet value; nobody supplied one, so the "
        "labeled default fills it and its BASIS is what separates a run the user "
        "refined from one left where the template put it")

    notes = sensitivity_notes(
        workflow.sensitivity, workflow.metadata,
        _Result(**{_FIELD: 4200.0}, mesh_size_m=250.0), sheet)
    assert len(notes) == 1, "one mesh is one fact, not one note per field"
    note = notes[0]
    assert note.startswith("RESOLUTION-LIMITED, TREAT AS A BOUND:")
    assert _FIELD in note
    assert "250 m" in note
    assert _LEVER in note, "the note names the lever to turn"
    assert "unsafe direction" in note


def test_a_refined_run_says_refined_is_not_converged() -> None:
    workflow, sheet = _resolved_sheet(location="Eel River near Scotia, California",
                                      **{_LEVER: 25.0})

    row = next(r for r in sheet if r.name == _LEVER)
    assert row.basis == "user" and row.value == 25.0

    notes = sensitivity_notes(
        workflow.sensitivity, workflow.metadata,
        _Result(**{_FIELD: 4200.0}, mesh_size_m=25.0), sheet)
    assert len(notes) == 1
    assert notes[0].startswith("RESOLUTION-SENSITIVE:")
    assert "not a demonstrated convergence" in notes[0]
    assert "25 m" in notes[0]


def test_a_field_the_run_did_not_produce_is_not_labeled() -> None:
    """A note about a number that is not there points at nothing."""
    notes = sensitivity_notes(
        _DECL, _Meta(), _Result(flooded_land_km2=0.03, mesh_size_m=250.0),
        [_Row("target_resolution_m", "derived")])
    assert "inundation_peak_depth_m" not in notes[0]
    assert "flooded_land_km2" in notes[0]


def test_no_declaration_is_no_note() -> None:
    assert sensitivity_notes(SensitivityDecl(), _Meta(),
                             _Result(mesh_size_m=250.0), []) == ()
    assert sensitivity_notes(_DECL, _Meta(), _Result(mesh_size_m=250.0), []) == ()


@pytest.mark.parametrize("template,field,cls", [
    ("telemac_do_sag", "do_min_distance_m", "location"),
    ("telemac_river_dye", "dye_cmax_mgl", "peak"),
    ("artemis_harbor_agitation", "kd_max", "peak"),
    ("telemac3d_stratified_flow", "stratification_dt", "gradient"),
])
def test_every_telemac_template_declares_its_sensitive_answers(
        template: str, field: str, cls: str) -> None:
    """The three NATE named plus the rest of the family, from the same evidence."""
    from trid3nt_server.tools import TOOL_REGISTRY

    workflow = TOOL_REGISTRY[template].fn.workflow
    assert (field, cls) in workflow.sensitivity.rows
    # a converged class must NOT be labeled: labeling everything is labeling nothing
    for converged in ("do_min_mgl", "hs_max_m", "sheltering_ratio"):
        assert converged not in dict(workflow.sensitivity.rows)
