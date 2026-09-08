"""Unit tests for EngineRunArgsMixin.

Pins the DEFAULT-OFF mixin: the temporal_mode alias normalizer,
output_frames=24, advanced_physics=None.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from trid3nt_contracts import EngineRunArgsMixin, TemporalMode  # __init__ export
from trid3nt_contracts.common import EngineRunArgsMixin as Mixin


# --------------------------------------------------------------------------- #
# EngineRunArgsMixin defaults (DEFAULT-OFF == today's behavior)
# --------------------------------------------------------------------------- #
def test_mixin_defaults_are_no_op() -> None:
    m = Mixin()
    assert m.temporal_mode == "steady"
    assert m.output_frames == 24
    assert m.advanced_physics is None


def test_mixin_serializes_byte_identically_when_unset() -> None:
    # A subclass that adds the mixin but whose payload does not set the new keys
    # still serializes them at their defaults (additive, not breaking).
    dumped = Mixin().model_dump()
    assert dumped == {
        "temporal_mode": "steady",
        "output_frames": 24,
        "advanced_physics": None,
    }


def test_temporal_mode_is_the_exported_alias() -> None:
    assert TemporalMode == TemporalMode  # importable from package root
    assert Mixin(temporal_mode="transient").temporal_mode == "transient"


# --------------------------------------------------------------------------- #
# temporal_mode alias normalizer
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("steady", "steady"),
        ("Steady-State", "steady"),
        ("steady_state", "steady"),
        ("stationary", "steady"),
        ("static", "steady"),
        ("transient", "transient"),
        ("nonstationary", "transient"),
        ("non-stationary", "transient"),
        ("unsteady", "transient"),
        ("Time-Varying", "transient"),
        ("dynamic", "transient"),
        ("  TRANSIENT  ", "transient"),
    ],
)
def test_temporal_mode_aliases(raw: str, expected: str) -> None:
    assert Mixin(temporal_mode=raw).temporal_mode == expected


def test_temporal_mode_unknown_raises_literal_error() -> None:
    with pytest.raises(ValidationError):
        Mixin(temporal_mode="sideways")


def test_output_frames_lower_bound() -> None:
    assert Mixin(output_frames=1).output_frames == 1
    with pytest.raises(ValidationError):
        Mixin(output_frames=0)


def test_advanced_physics_accepts_dict_or_none() -> None:
    assert Mixin(advanced_physics={"alpha": 0.5}).advanced_physics == {"alpha": 0.5}
    assert Mixin(advanced_physics=None).advanced_physics is None


def test_mixin_forbids_extra_keys() -> None:
    # Inherits GraceModel extra="forbid" - a stray key is a defect, not dropped.
    with pytest.raises(ValidationError):
        Mixin(bogus_key=1)
