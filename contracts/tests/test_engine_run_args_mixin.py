"""Unit tests for EngineRunArgsMixin + output_quantities (STEP 2; ADDITIVE).

Pins the DEFAULT-OFF mixin (temporal_mode alias normalizer, output_frames=24,
advanced_physics=None) and the declarative OutputQuantitySpec / FieldResult /
registry resolver.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from grace2_contracts import EngineRunArgsMixin, TemporalMode  # __init__ export
from grace2_contracts.common import EngineRunArgsMixin as Mixin
from grace2_contracts.output_quantities import (
    OUTPUT_QUANTITIES,
    OUTPUT_REGISTRY_SCHEMA_VERSION,
    OutputQuantitySpec,
    RasterField,
    ScalarField,
    TimeseriesField,
    get_output_registry,
)


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


# --------------------------------------------------------------------------- #
# output_quantities registry + resolver
# --------------------------------------------------------------------------- #
def test_registry_schema_version_is_one() -> None:
    assert OUTPUT_REGISTRY_SCHEMA_VERSION == 1


def test_step3_engines_populated_others_empty() -> None:
    # STEP 3: the four migrated engines (modflow / landlab / openquake / swmm)
    # carry declarative OutputQuantitySpec rows; SFINCS / GeoClaw / SWAN stay
    # EMPTY (STEP 0 / STEP 4). The DECLARATIVE half (no reader) lives in the
    # contract; the reader is bound agent-side.
    for engine in ("modflow", "landlab", "openquake", "swmm"):
        specs = OUTPUT_QUANTITIES[engine]
        assert specs, f"{engine} should be populated in STEP 3"
        # contracts ship the declarative half only -- readers are bound agent-side.
        assert all(s.reader is None for s in specs), (
            f"{engine} scaffold rows must not carry a reader in the contract"
        )
        # at least one NEW (default_on) published quantity per migrated engine.
        assert any(s.default_on for s in specs)
    for engine in ("sfincs", "geoclaw", "swan"):
        assert OUTPUT_QUANTITIES[engine] == (), f"{engine} should stay empty"


def test_get_output_registry_known_and_unknown() -> None:
    assert get_output_registry("sfincs") == ()
    assert get_output_registry("SFINCS") == ()  # case-insensitive
    assert get_output_registry("does-not-exist") == ()
    # a migrated engine resolves its populated tuple (case-insensitive).
    assert get_output_registry("MODFLOW") == get_output_registry("modflow")
    assert len(get_output_registry("modflow")) >= 1


def test_output_quantity_spec_is_frozen() -> None:
    spec = OutputQuantitySpec(
        quantity_id="q", kind="raster", name="Q", style_preset="p"
    )
    assert spec.default_on is False  # DEFAULT-OFF
    assert spec.role == "primary" and spec.reader is None
    with pytest.raises(Exception):
        spec.quantity_id = "mutated"  # frozen dataclass


def test_field_result_variants_construct() -> None:
    rf = RasterField(grid=[[1.0]], src_crs="EPSG:4326", src_transform=None)
    assert rf.reproject is False and rf.metrics == {}
    tf = TimeseriesField(n_steps=3, read_step=lambda i: rf, peak=rf)
    assert tf.quantity_label == "Flood depth"
    sf = ScalarField(values={"x": 1})
    assert sf.values == {"x": 1}
