# ADR 0158 strict-field coverage: the ADR 0148 lesson (a stale image SILENTLY
# dropped unknown build_spec fields; two registered knob templates ran as
# no-ops) applies to the SFINCS build-side job_spec too. These pin the
# top-level strict checks on ``validate_job_spec`` / ``forcing_spec_from_dict``
# / ``build_options_from_dict`` -- unknown fields error loudly, and the
# documented pass-through keys (``quadtree`` / ``return_period_yr`` in
# ``options``) are NOT rejected.

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from services.workers._sfincs_build.deck import (  # noqa: E402
    SFINCSSetupError,
    build_options_from_dict,
    forcing_spec_from_dict,
)
from services.workers._sfincs_build.spec import validate_job_spec  # noqa: E402

_VALID_JOB_SPEC = {
    "schema_version": 1,
    "bbox": [-85.75, 29.55, -85.25, 30.20],
    "nlcd_vintage_year": 2021,
    "inputs": {"dem_uri": "s3://x/dem.tif", "landcover_uri": "s3://x/nlcd.tif"},
    "forcing": {"forcing_type": "pluvial_synthetic", "precip_inches": 6.0},
    "options": {"grid_resolution_m": 30.0},
}


def test_validate_job_spec_accepts_known_fields():
    out = validate_job_spec(dict(_VALID_JOB_SPEC))
    assert out["bbox"] == [-85.75, 29.55, -85.25, 30.20]


def test_validate_job_spec_accepts_legacy_top_level_return_period_yr():
    """return_period_yr at the top level is a documented legacy fallback the
    quadtree design-surge synthesis reads directly -- must not be rejected."""
    spec = dict(_VALID_JOB_SPEC)
    spec["return_period_yr"] = 100
    out = validate_job_spec(spec)
    assert out["return_period_yr"] == 100


def test_validate_job_spec_rejects_unknown_top_level_field():
    spec = dict(_VALID_JOB_SPEC)
    spec["typo_field_name"] = 1.0
    with pytest.raises(ValueError, match="typo_field_name"):
        validate_job_spec(spec)


def test_forcing_spec_from_dict_accepts_known_fields():
    forcing = forcing_spec_from_dict({"forcing_type": "pluvial_synthetic", "precip_inches": 6.0})
    assert forcing.forcing_type == "pluvial_synthetic"


def test_forcing_spec_from_dict_rejects_unknown_field():
    with pytest.raises(SFINCSSetupError) as ei:
        forcing_spec_from_dict({"forcing_type": "pluvial_synthetic", "typo_field_name": 1.0})
    assert ei.value.error_code == "SFINCS_SPEC_UNKNOWN_FIELDS"
    assert "typo_field_name" in str(ei.value)


def test_build_options_from_dict_accepts_known_fields():
    opts = build_options_from_dict({"grid_resolution_m": 50.0})
    assert opts.grid_resolution_m == 50.0


def test_build_options_from_dict_accepts_quadtree_and_return_period_yr_passthrough():
    """quadtree + return_period_yr are consumed by deck_quadtree.py directly off
    spec['options'], not by build_options_from_dict -- must not be rejected."""
    opts = build_options_from_dict(
        {
            "grid_resolution_m": 50.0,
            "quadtree": {"base_resolution_m": 200.0, "coast_refine_level": 3},
            "return_period_yr": 100,
        }
    )
    assert opts.grid_resolution_m == 50.0


def test_build_options_from_dict_rejects_unknown_field():
    with pytest.raises(SFINCSSetupError) as ei:
        build_options_from_dict({"typo_field_name": 1.0})
    assert ei.value.error_code == "SFINCS_SPEC_UNKNOWN_FIELDS"
    assert "typo_field_name" in str(ei.value)
