"""ADR 0232 regression: Landlab wires its DECLARED target_resolution_m.

LANDLAB_RES_SPEC was declared on every Landlab template (ADR 0225) but the
staging seam never CALLED enforce_resolution, so an out-of-declared-range
target_resolution_m was silently resampled instead of quoted back. The fix is
ONE enforce call at the universal seam (``stage_landlab_manifest`` -- every
template, composer AND self-staging, funnels its DEM+grid through it).

These tests pin the enforcement in ISOLATION: the out-of-range ask raises the
ADR 0225 typed card BEFORE any S3 client is touched (offline-safe, no MinIO),
and an in-range / native value stays silent.

ASCII only.
"""

from __future__ import annotations

import pytest

from trid3nt_contracts.landlab_contracts import LandlabRunArgs

from trid3nt_server.data.resolution_declared import (
    ResolutionOutOfRangeError,
    enforce_resolution,
)
from trid3nt_server.workflows.landlab.run_landlab import (
    LANDLAB_RES_SPEC,
    stage_landlab_manifest,
)

_BBOX = (-122.5, 45.4, -122.4, 45.5)


def test_sub_floor_resolution_quoted_back_before_s3():
    """A sub-10 m target_resolution_m raises the ADR 0225 card at staging.

    The enforce call is the FIRST statement in stage_landlab_manifest, so the
    raise happens before the boto3 client is built -- no MinIO needed.
    """
    ra = LandlabRunArgs(bbox=_BBOX, target_resolution_m=5.0)
    with pytest.raises(ResolutionOutOfRangeError) as exc:
        stage_landlab_manifest(ra, dem_path="/nonexistent/dem.tif", run_id="r0")
    assert exc.value.error_code == "INVALID_ARG"
    # the quote-back names the declared floor (10 m), never a silent snap
    assert "10" in str(exc.value)


def test_in_range_resolution_passes_enforcement():
    """The 30 m default (and any >=10 m ask) is in the declared window."""
    # enforce is a no-op for in-range values (returns None, no raise)
    assert enforce_resolution(LANDLAB_RES_SPEC, 30.0) is None
    assert enforce_resolution(LANDLAB_RES_SPEC, 10.0) is None
    assert enforce_resolution(LANDLAB_RES_SPEC, 250.0) is None  # no coarse ceiling
    # None (native/autoscaled default) is in-range by construction
    assert enforce_resolution(LANDLAB_RES_SPEC, None) is None


def test_spec_is_the_shared_declared_spec():
    """The staging seam enforces the SHARED LANDLAB_RES_SPEC (not a private one)."""
    assert LANDLAB_RES_SPEC.param == "target_resolution_m"
    assert LANDLAB_RES_SPEC.min_value == 10.0
