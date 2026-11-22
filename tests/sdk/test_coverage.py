"""Coverage is data and every coverage step is declared: a shortfall no declared step resolves warns (strict off) or
refuses naming its code and remedies (strict on), and a node with no bed value refuses under both.

The beds here are SYNTHETIC rasters written by the test (an isolated verification gate, not a product run): one
constant elevation over the dye release's drawn ring with a square of cells left unvalued.
"""

from __future__ import annotations

import numpy as np
import pytest

from trid3nt import case
from trid3nt.inputs.coverage import ACCEPT, INTERPOLATE, MERGE, Step

from tests.sdk.test_fill_deck import _stubbed

#: The zero-fetch dye release with its bed left for each test to hand.
_DYE = dict(
    release=[-122.669784, 45.518485], spill_duration_s=300.0, event_time="2024-03-01T12:00:00Z",
    mesh_resolution_m=40.0, DURATION=1800.0, discharge=56.6, level=0.0,
    domain=[[-122.6769, 45.5153], [-122.6615, 45.5153], [-122.6615, 45.5198], [-122.6769, 45.5198]])

_RING = {"type": "Polygon", "coordinates": [[[-122.6769, 45.5153], [-122.6615, 45.5153], [-122.6615, 45.5198],
                                             [-122.6769, 45.5198], [-122.6769, 45.5153]]]}


def _spotty(path, *, hole=(slice(16, 26), slice(36, 46))):
    """A -4 m surface over the ring, 80 x 40 cells of about 20 m, ``hole`` left unvalued (a 200 m square by default)."""
    import rasterio
    from rasterio.transform import from_bounds

    values = np.full((40, 80), -4.0, dtype="float32")
    values[hole] = np.nan
    with rasterio.open(path, "w", driver="GTiff", width=80, height=40, count=1, dtype="float32", nodata=float("nan"),
                       crs="EPSG:4326", transform=from_bounds(-122.68, 45.514, -122.66, 45.521, 80, 40)) as out:
        out.write(values, 1)
    return str(path)


def _names(remedies):
    return [remedy.name for remedy in remedies]


def test_a_spotty_bed_with_no_step_warns_with_its_coverage_and_remedies_and_runs(monkeypatch, tmp_path):
    calls = _stubbed(monkeypatch)
    c = case(tmp_path / "spotty")
    m = c.telemac_dye_release(**_DYE, bed=_spotty(tmp_path / "bed.tif"), steps={"bed": ()})
    form = m.fill()
    assert form.ready and form.checked and form.refusal is None
    (short,) = form.warnings
    assert short.code == "BED_WET_HOLE" and short.input == "bed" and short.cells > 0 and 0 < short.share < 1
    assert not short.accepted and short.farthest_px > 0
    names = _names(short.remedies)
    assert names[-2:] == [INTERPOLATE, ACCEPT] and MERGE in names
    assert all(isinstance(remedy, Step) for remedy in short.remedies)
    assert short.remedies[-2].arg("max_distance") == short.farthest_px
    assert short.remedies[-1].arg("code") == "BED_WET_HOLE"
    assert all(remedy.arg("source") for remedy in short.remedies if remedy.name == MERGE)
    covered = form.coverage["bed"]
    assert covered.shortfall == short and covered.steps == () and covered.unmeasured == short.share
    assert covered.text and any(line.startswith("Over the WATER") for line in covered.text)
    result = m.run()
    assert result.run_id == "RUNFILLDECK" and "solve" in calls


def test_the_same_bed_under_strict_refuses_naming_the_code_and_remedies(monkeypatch, tmp_path):
    _stubbed(monkeypatch)
    c = case(tmp_path / "spotty", strict=True)
    m = c.telemac_dye_release(**_DYE, bed=_spotty(tmp_path / "bed.tif"), steps={"bed": ()})
    form = m.fill()
    assert not form.ready and form.refusal[0] == "BED_WET_HOLE" and form.warnings == ()
    short = form.coverage["bed"].shortfall
    assert short.code == "BED_WET_HOLE" and _names(short.remedies)[-2:] == [INTERPOLATE, ACCEPT]
    failed = m.run()
    assert failed.code == "BED_WET_HOLE"
    # The model's own strictness wins over its case's.
    m.strict = False
    assert m.fill().ready


def test_an_accept_line_naming_the_code_resolves_the_shortfall(monkeypatch, tmp_path):
    _stubbed(monkeypatch)
    m = case(tmp_path / "spotty", strict=True).telemac_dye_release(
        **_DYE, bed=_spotty(tmp_path / "bed.tif"), steps={"bed": [(ACCEPT, {"code": "BED_WET_HOLE"})]})
    form = m.fill()
    assert form.ready and form.warnings == ()
    assert form.coverage["bed"].shortfall.accepted


def test_the_steps_declared_in_the_script_leave_no_warning_and_state_the_shares(monkeypatch, tmp_path):
    _stubbed(monkeypatch)
    c = case(tmp_path / "spotty", strict=True)
    m = c.telemac_dye_release(**_DYE, bed=_spotty(tmp_path / "bed.tif"),
                              steps={"bed": [MERGE, (INTERPOLATE, {"algorithm": "linear", "max_distance": 50})]})
    form = m.fill()
    assert form.ready and form.warnings == ()
    covered = form.coverage["bed"]
    assert covered.shortfall is None and covered.unmeasured == 0.0
    shares = dict(covered.shares)
    assert shares["filled"] > 0 and sum(shares.values()) == pytest.approx(1.0, abs=1e-3)
    assert any("by linear interpolation reaching at most 50 px" in line for line in covered.text)


def test_the_operations_called_in_the_script_answer_the_shortfall_themselves(monkeypatch, tmp_path):
    from trid3nt.tools.derive.interpolate_surface import interpolate_surface

    _stubbed(monkeypatch)
    c = case(tmp_path / "spotty", strict=True)
    uri, _entry = c.take(_spotty(tmp_path / "bed.tif"))
    with c.bound():
        filled = interpolate_surface(uri, region=_RING, max_distance=50)
    m = c.telemac_dye_release(**_DYE, bed=filled, steps={"bed": ()})
    form = m.fill()
    assert form.ready and form.warnings == () and form.coverage["bed"].shortfall is None


def test_a_template_call_states_its_default_steps_and_fills_as_before(monkeypatch, tmp_path):
    _stubbed(monkeypatch)
    c = case(tmp_path / "spotty", strict=True)
    bare = c.telemac_dye_release(**_DYE, bed=_spotty(tmp_path / "bed.tif"))
    assert bare.steps == {"bed": (Step(MERGE), Step(INTERPOLATE))}
    form = bare.fill()
    assert form.ready and form.warnings == () and dict(form.coverage["bed"].shares)["filled"] > 0
    stated = c.telemac_dye_release(**_DYE, bed=_spotty(tmp_path / "bed.tif"), steps={"bed": [MERGE, INTERPOLATE]})
    assert stated.steps == bare.steps and stated.version != bare.version
    assert stated.fill().coverage["bed"] == form.coverage["bed"]


def test_a_step_the_slot_does_not_run_and_a_bad_argument_refuse_on_set(tmp_path):
    from trid3nt.errors import DeclarationError

    m = case(tmp_path / "steps").telemac_dye_release(**_DYE)
    with pytest.raises(DeclarationError):
        m.steps = {"discharge": [INTERPOLATE]}
    with pytest.raises(DeclarationError):
        m.steps = {"bed": [(INTERPOLATE, {"algorithm": "kriging"})]}
    with pytest.raises(DeclarationError):
        m.steps = {"bed": ["fit_reach"]}
    with pytest.raises(DeclarationError):
        m.strict = "yes"


def test_a_node_left_with_no_bed_value_refuses_under_both_settings(tmp_path):
    """The real launch up to the deck: the mesh's nodes inside the unvalued square have no elevation."""
    spotty = _spotty(tmp_path / "bed.tif")
    off = case(tmp_path / "off").telemac_dye_release(**_DYE, bed=spotty, steps={"bed": ()})
    on = case(tmp_path / "on", strict=True).telemac_dye_release(
        **_DYE, bed=spotty, steps={"bed": [(ACCEPT, {"code": "BED_WET_HOLE"})]})
    for model in (off, on):
        form = model.fill()
        assert not form.ready and form.refusal[0] == "MESH_BED_UNPAINTED", form.refusal
        assert form.deck is None and not form.checked
