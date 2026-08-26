"""The resolution-sensitivity label: which answers a coarse mesh reads wrong.

The mechanism is skeleton-level, so these pin it at the library rather than
through any one engine: a template declares which of its ANSWER fields sit in
which measured class, and the run's own sheet decides which of the two sentences
it gets.
"""

from __future__ import annotations

import pytest

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


def test_a_default_spacing_run_is_labeled_a_bound() -> None:
    """The un-refined run is the case the evidence was measured on."""
    notes = sensitivity_notes(
        _DECL, _Meta(),
        _Result(flooded_land_km2=0.03, inundation_peak_depth_m=0.15,
                mesh_size_m=250.0),
        [_Row("target_resolution_m", "derived")])
    assert len(notes) == 1, "one mesh is one fact, not one note per field"
    note = notes[0]
    assert note.startswith("RESOLUTION-LIMITED, TREAT AS A BOUND:")
    assert "flooded_land_km2" in note and "inundation_peak_depth_m" in note
    assert "250 m" in note
    assert "target_resolution_m" in note, "the note names the lever to turn"
    assert "unsafe direction" in note


def test_a_refined_run_says_refined_is_not_converged() -> None:
    notes = sensitivity_notes(
        _DECL, _Meta(),
        _Result(flooded_land_km2=0.03, inundation_peak_depth_m=0.15,
                mesh_size_m=60.0),
        [_Row("target_resolution_m", "user")])
    assert len(notes) == 1
    assert notes[0].startswith("RESOLUTION-SENSITIVE:")
    assert "not a demonstrated convergence" in notes[0]
    assert "60 m" in notes[0]


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
    ("coastal_tidal_surge", "flooded_land_km2", "extent"),
    ("tomawac_wave_field", "hs_upwind_m", "gradient"),
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
