"""Unit tests for ``set_swmm_parameters`` (LANE D, no network).

The parent ``.inp`` is built by copying the REAL, already-committed
``tests/fixtures/swmm_wq/wq_smoke.inp`` (2 subcatchments C_0_0/C_0_1) into a
per-test tmp path via ``swmm_api`` and adding a Horton ``[INFILTRATION]``
entry per subcatchment (the shared fixture is never mutated -- only read).
All setter calls pass ``_work_dir`` (a private test seam) so every write
lands under ``tmp_path``, zero network, zero shared state.

Coverage:
1.  ``test_registered`` -- TOOL_REGISTRY entry + metadata (cacheable=False,
    live-no-cache, read_only_hint=False).
2.  ``test_imperviousness_set_global`` -- op="set" with no ``subcatchments``
    key touches ALL subcatchments; before/after read back from the written
    child .inp.
3.  ``test_n_imperv_scale_zone`` -- op="scale" restricted to one
    subcatchment leaves the other subcatchment's SUBAREAS n_imperv untouched
    (zone isolation).
4.  ``test_infil_rate_max_set`` -- Horton infiltration parameter write +
    read-back.
5a. ``test_meaningless_value_hard_error`` -- imperviousness 150% (outside
    0-100) raises the typed ``BoundsViolation`` (physically meaningless).
5b. ``test_out_of_plausible_range_warns`` -- an atypical-but-physical n_imperv
    (1.5, above the 0.01-0.9 band) warns (in_range=false) and proceeds.
6.  ``test_unknown_parameter_raises``.
7.  ``test_missing_infiltration_entry_raises`` -- a subcatchment with no
    [INFILTRATION] row cannot be targeted (v1 modifies existing entries only).
8.  ``test_op_requires_value_or_factor``.
9.  ``test_parent_untouched`` -- copy-on-write proof: the parent .inp's
    bytes are byte-identical before and after the call.
10. ``test_manifest_written`` -- ``child_setup_uri`` resolves to a readable
    JSON manifest carrying the changes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

swmm_api = pytest.importorskip("swmm_api")

from swmm_api import read_inp_file  # noqa: E402
from swmm_api.input_file.sections import InfiltrationHorton  # noqa: E402

from trid3nt_server.tools import TOOL_REGISTRY  # noqa: E402
from trid3nt_server.tools.simulation._setter_envelope import (  # noqa: E402
    BoundsViolation,
    SetterInputError,
)
from trid3nt_server.tools.simulation.set_swmm_parameters import (  # noqa: E402
    set_swmm_parameters,
)

_FIXTURE_INP = Path(__file__).parent / "fixtures" / "swmm_wq" / "wq_smoke.inp"


@pytest.fixture()
def parent_inp(tmp_path: Path) -> Path:
    """A per-test COPY of the shared fixture .inp, with a Horton
    [INFILTRATION] row added for C_0_0 only (C_0_1 deliberately left without
    one, to exercise the "missing infiltration entry" typed error)."""
    inp = read_inp_file(str(_FIXTURE_INP))
    inp.add_obj(
        InfiltrationHorton(
            subcatchment="C_0_0", rate_max=75.0, rate_min=13.0, decay=4.0,
            time_dry=7.0, volume_max=0.0,
        )
    )
    parent_dir = tmp_path / "parent"
    parent_dir.mkdir()
    parent_path = parent_dir / "model.inp"
    inp.write_file(str(parent_path))
    return parent_path


def test_registered() -> None:
    assert "set_swmm_parameters" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["set_swmm_parameters"]
    assert entry.metadata.cacheable is False
    assert entry.metadata.ttl_class == "live-no-cache"
    assert entry.metadata.read_only_hint is False
    assert entry.metadata.open_world_hint is False
    assert entry.metadata.idempotent_hint is False


def test_imperviousness_set_global(parent_inp: Path, tmp_path: Path) -> None:
    result = set_swmm_parameters(
        parent_model_uri=str(parent_inp),
        changes=[{"parameter": "imperviousness", "op": "set", "value": 55.0}],
        _work_dir=str(tmp_path / "work"),
    )
    assert result["engine"] == "swmm"
    [entry] = result["changes_applied"]
    assert entry["param"] == "imperviousness"
    assert entry["scope"] == "global"
    assert entry["before"] == pytest.approx(100.0)
    assert entry["after"] == pytest.approx(55.0)
    assert entry["unit"] == "%"
    [plaus] = result["plausibility"]
    assert plaus["in_range"] is True

    child_inp = Path(result["child_setup_uri"][len("file://"):]).parent / "model" / "model.inp"
    reread = read_inp_file(str(child_inp))
    assert reread["SUBCATCHMENTS"]["C_0_0"].imperviousness == pytest.approx(55.0)
    assert reread["SUBCATCHMENTS"]["C_0_1"].imperviousness == pytest.approx(55.0)


def test_n_imperv_scale_zone(parent_inp: Path, tmp_path: Path) -> None:
    result = set_swmm_parameters(
        parent_model_uri=str(parent_inp),
        changes=[{
            "parameter": "n_imperv", "op": "scale", "factor": 2.0,
            "subcatchments": ["C_0_0"],
        }],
        _work_dir=str(tmp_path / "work"),
    )
    [entry] = result["changes_applied"]
    assert entry["scope"] == "zone:C_0_0"
    assert entry["before"] == pytest.approx(0.012)
    assert entry["after"] == pytest.approx(0.024)

    child_inp = Path(result["child_setup_uri"][len("file://"):]).parent / "model" / "model.inp"
    reread = read_inp_file(str(child_inp))
    assert reread["SUBAREAS"]["C_0_0"].n_imperv == pytest.approx(0.024)
    # C_0_1 (not in the zone) is untouched.
    assert reread["SUBAREAS"]["C_0_1"].n_imperv == pytest.approx(0.012)


def test_infil_rate_max_set(parent_inp: Path, tmp_path: Path) -> None:
    result = set_swmm_parameters(
        parent_model_uri=str(parent_inp),
        changes=[{
            "parameter": "infil_rate_max", "op": "set", "value": 50.0,
            "subcatchments": ["C_0_0"],
        }],
        _work_dir=str(tmp_path / "work"),
    )
    [entry] = result["changes_applied"]
    assert entry["before"] == pytest.approx(75.0)
    assert entry["after"] == pytest.approx(50.0)
    assert entry["unit"] == "mm/hr"

    child_inp = Path(result["child_setup_uri"][len("file://"):]).parent / "model" / "model.inp"
    reread = read_inp_file(str(child_inp))
    assert reread["INFILTRATION"]["C_0_0"].rate_max == pytest.approx(50.0)


def test_meaningless_value_hard_error(parent_inp: Path, tmp_path: Path) -> None:
    # 150% impervious is outside 0-100 -- physically meaningless -> hard error.
    with pytest.raises(BoundsViolation) as excinfo:
        set_swmm_parameters(
            parent_model_uri=str(parent_inp),
            changes=[{"parameter": "imperviousness", "op": "set", "value": 150.0}],
            _work_dir=str(tmp_path / "work"),
        )
    assert excinfo.value.error_code == "PARAM_BOUNDS_VIOLATION"
    assert excinfo.value.retryable is False


def test_out_of_plausible_range_warns(parent_inp: Path, tmp_path: Path) -> None:
    # n_imperv=1.5 is above the plausible Manning-n band (0.01-0.9) but
    # positive/physical: warn-and-proceed (in_range=false), not a hard reject.
    result = set_swmm_parameters(
        parent_model_uri=str(parent_inp),
        changes=[{"parameter": "n_imperv", "op": "set", "value": 1.5}],
        _work_dir=str(tmp_path / "work"),
    )
    [entry] = result["changes_applied"]
    assert entry["after"] == pytest.approx(1.5)
    [plaus] = result["plausibility"]
    assert plaus["in_range"] is False
    assert "WARNING" in plaus["note"]


def test_unknown_parameter_raises(parent_inp: Path, tmp_path: Path) -> None:
    with pytest.raises(SetterInputError):
        set_swmm_parameters(
            parent_model_uri=str(parent_inp),
            changes=[{"parameter": "not_a_real_param", "op": "set", "value": 1.0}],
            _work_dir=str(tmp_path / "work"),
        )


def test_missing_infiltration_entry_raises(parent_inp: Path, tmp_path: Path) -> None:
    # C_0_1 was deliberately left without an [INFILTRATION] row.
    with pytest.raises(SetterInputError, match="INFILTRATION"):
        set_swmm_parameters(
            parent_model_uri=str(parent_inp),
            changes=[{
                "parameter": "infil_rate_max", "op": "set", "value": 50.0,
                "subcatchments": ["C_0_1"],
            }],
            _work_dir=str(tmp_path / "work"),
        )


def test_op_requires_value_or_factor(parent_inp: Path, tmp_path: Path) -> None:
    with pytest.raises(SetterInputError):
        set_swmm_parameters(
            parent_model_uri=str(parent_inp),
            changes=[{"parameter": "imperviousness", "op": "set"}],
            _work_dir=str(tmp_path / "work"),
        )
    with pytest.raises(SetterInputError):
        set_swmm_parameters(
            parent_model_uri=str(parent_inp),
            changes=[{"parameter": "imperviousness", "op": "scale"}],
            _work_dir=str(tmp_path / "work"),
        )


def test_parent_untouched(parent_inp: Path, tmp_path: Path) -> None:
    before_bytes = parent_inp.read_bytes()
    set_swmm_parameters(
        parent_model_uri=str(parent_inp),
        changes=[{"parameter": "imperviousness", "op": "set", "value": 12.0}],
        _work_dir=str(tmp_path / "work"),
    )
    assert parent_inp.read_bytes() == before_bytes


def test_manifest_written(parent_inp: Path, tmp_path: Path) -> None:
    import json

    result = set_swmm_parameters(
        parent_model_uri=str(parent_inp),
        changes=[{"parameter": "imperviousness", "op": "set", "value": 42.0}],
        _work_dir=str(tmp_path / "work"),
    )
    manifest_path = Path(result["child_setup_uri"][len("file://"):])
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text())
    assert manifest["engine"] == "swmm"
    assert manifest["parent_model"] == str(parent_inp)
    assert manifest["changes_applied"][0]["param"] == "imperviousness"
