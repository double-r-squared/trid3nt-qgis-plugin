"""Unit tests for the job-A11 pfdf-backed atomic tools.

Covers:
- ``fetch_statsgo_soils`` (USGS STATSGO via pfdf.data.usgs.statsgo)
- ``fetch_nhdplus_nldi_navigate`` (USGS NLDI navigate over NHDPlus v2.1)
- ``fetch_3dep_extra`` (USGS 3DEP non-default resolutions via
  pfdf.data.usgs.tnm.dem)

Coverage:
- All three tools are registered in TOOL_REGISTRY with expected metadata.
- Typed error classes carry ``error_code`` + ``retryable`` per FR-AS-11.
- Input validators reject malformed / out-of-CONUS bbox, unknown fields,
  unknown directions, bad distances, missing seeds, etc.
- ``estimate_payload_mb`` scales with bbox area / distance / direction
  and returns a positive float.
- Live smoke tests (gated by env TRID3NT_TEST_LIVE_PFDF_A11=1) for each:
  small Fort Myers bbox returns a non-empty COG / FlatGeobuf.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.fetchers.terrain.fetch_3dep_extra import (
    SUPPORTED_RESOLUTIONS,
    ThreeDEPExtraError,
    ThreeDEPExtraInputError,
    estimate_payload_mb as _est_3dep,
    fetch_3dep_extra,
)
from trid3nt_server.tools.fetchers.hydrology.fetch_nhdplus_nldi_navigate import (
    NHDPlusNLDIError,
    NHDPlusNLDIInputError,
    estimate_payload_mb as _est_nldi,
    fetch_nhdplus_nldi_navigate,
)
from trid3nt_server.tools.fetchers.soil.fetch_statsgo_soils import (
    STATSGOSoilsError,
    STATSGOSoilsInputError,
    estimate_payload_mb as _est_statsgo,
    fetch_statsgo_soils,
)


_LIVE = os.environ.get("TRID3NT_TEST_LIVE_PFDF_A11") == "1"

# Fort Myers / Caloosahatchee — same demo bbox used across the suite.
_FORT_MYERS_BBOX = (-82.0, 26.4, -81.7, 26.7)
_FORT_MYERS_POINT = (-81.85, 26.55)


# ---------------------------------------------------------------------------
# Registry shape.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name, ttl_class, source_class, supports_global, estimator_name",
    [
        (
            "fetch_statsgo_soils",
            "static-30d",
            "statsgo_soils",
            False,
            "estimate_payload_mb",
        ),
        (
            "fetch_nhdplus_nldi_navigate",
            "static-30d",
            "nhdplus_nldi",
            False,
            "estimate_payload_mb",
        ),
        (
            "fetch_3dep_extra",
            "static-30d",
            "3dep_extra",
            False,
            "estimate_payload_mb",
        ),
    ],
)
def test_a11_tools_registered(
    name: str,
    ttl_class: str,
    source_class: str,
    supports_global: bool,
    estimator_name: str,
) -> None:
    """All three job-A11 tools are present in TOOL_REGISTRY with right metadata."""
    assert name in TOOL_REGISTRY, f"{name} not registered"
    entry = TOOL_REGISTRY[name]
    md = entry.metadata
    assert md.ttl_class == ttl_class
    assert md.source_class == source_class
    assert md.cacheable is True
    # supports_global_query is Wave-1.5; defensive against schema variants.
    sgq = getattr(md, "supports_global_query", None)
    assert sgq == supports_global, f"{name} supports_global_query mismatch"
    pme = getattr(md, "payload_mb_estimator_name", None)
    assert pme == estimator_name, f"{name} payload_mb_estimator_name mismatch"


# ---------------------------------------------------------------------------
# Typed-error envelope (FR-AS-11).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cls, code, retryable",
    [
        (STATSGOSoilsError, "STATSGO_SOILS_ERROR", True),
        (STATSGOSoilsInputError, "STATSGO_SOILS_INPUT_INVALID", False),
        (NHDPlusNLDIError, "NHDPLUS_NLDI_ERROR", True),
        (NHDPlusNLDIInputError, "NHDPLUS_NLDI_INPUT_INVALID", False),
        (ThreeDEPExtraError, "THREE_DEP_EXTRA_ERROR", True),
        (ThreeDEPExtraInputError, "THREE_DEP_EXTRA_INPUT_INVALID", False),
    ],
)
def test_typed_error_envelope(cls: type, code: str, retryable: bool) -> None:
    """Typed exceptions surface error_code + retryable per FR-AS-11."""
    err = cls("boom")
    assert err.error_code == code
    assert err.retryable is retryable
    # Subclasses of base error are still subclasses of RuntimeError.
    assert isinstance(err, RuntimeError)


# ---------------------------------------------------------------------------
# Payload estimators.
# ---------------------------------------------------------------------------


def test_estimate_payload_mb_statsgo_scales_with_bbox() -> None:
    """fetch_statsgo_soils estimator scales linearly with bbox area."""
    small = _est_statsgo(bbox=(-82.0, 26.4, -81.7, 26.7), field="KFFACT")
    big = _est_statsgo(bbox=(-83.0, 26.0, -81.0, 28.0), field="KFFACT")
    assert 0.0 < small < big
    # None bbox returns a safe default.
    assert _est_statsgo(bbox=None) > 0.0


def test_estimate_payload_mb_nldi_scales_with_distance_and_direction() -> None:
    """fetch_nhdplus_nldi_navigate estimator: UT > DM for same distance."""
    dm_50 = _est_nldi(direction="DM", distance_km=50.0)
    ut_50 = _est_nldi(direction="UT", distance_km=50.0)
    dm_200 = _est_nldi(direction="DM", distance_km=200.0)
    assert dm_50 > 0
    assert ut_50 > dm_50, "UT (tributaries) should estimate larger than DM"
    assert dm_200 > dm_50, "longer distance should estimate larger"


def test_estimate_payload_mb_3dep_scales_with_resolution() -> None:
    """fetch_3dep_extra estimator: finer resolutions estimate larger MB."""
    bbox = (-82.0, 26.4, -81.7, 26.7)
    coarse = _est_3dep(bbox=bbox, resolution="1 arc-second")
    fine = _est_3dep(bbox=bbox, resolution="1/9 arc-second")
    lidar = _est_3dep(bbox=bbox, resolution="1 meter")
    assert 0.0 < coarse < fine < lidar


# ---------------------------------------------------------------------------
# Input validation — STATSGO.
# ---------------------------------------------------------------------------


def test_statsgo_rejects_bad_bbox_shape() -> None:
    with pytest.raises(STATSGOSoilsInputError):
        fetch_statsgo_soils(bbox=(1.0, 2.0))  # type: ignore[arg-type]


def test_statsgo_rejects_non_finite_bbox() -> None:
    with pytest.raises(STATSGOSoilsInputError):
        fetch_statsgo_soils(bbox=(float("nan"), 26.4, -81.7, 26.7))


def test_statsgo_rejects_degenerate_bbox() -> None:
    with pytest.raises(STATSGOSoilsInputError):
        fetch_statsgo_soils(bbox=(-82.0, 27.0, -82.0, 27.0))


def test_statsgo_rejects_outside_conus_bbox() -> None:
    """STATSGO is CONUS-only; an Alaska bbox raises STATSGOSoilsInputError."""
    with pytest.raises(STATSGOSoilsInputError):
        fetch_statsgo_soils(bbox=(-150.0, 60.0, -149.0, 61.0))


def test_statsgo_rejects_unknown_field() -> None:
    with pytest.raises(STATSGOSoilsInputError):
        fetch_statsgo_soils(bbox=_FORT_MYERS_BBOX, field="NOPE")  # type: ignore[arg-type]


def test_statsgo_absorbs_invented_kwargs() -> None:
    """Per project convention: tools must absorb LLM-invented kwargs."""
    # We only check that the **_extra_ignored mechanism does NOT
    # reject the call before validation runs. Unknown kwargs raised at
    # the cache layer would propagate before reaching the validator —
    # but a malformed bbox is caught first.
    with pytest.raises(STATSGOSoilsInputError):
        fetch_statsgo_soils(
            bbox=(0.0, 0.0, 0.0, 0.0),
            invented_kwarg="ignored",
            another_invented=42,
        )


# ---------------------------------------------------------------------------
# Input validation — NLDI.
# ---------------------------------------------------------------------------


def test_nldi_requires_exactly_one_seed() -> None:
    with pytest.raises(NHDPlusNLDIInputError):
        fetch_nhdplus_nldi_navigate()
    with pytest.raises(NHDPlusNLDIInputError):
        fetch_nhdplus_nldi_navigate(
            seed_point=_FORT_MYERS_POINT, comid=123456, direction="DM",
        )


def test_nldi_rejects_unknown_direction() -> None:
    with pytest.raises(NHDPlusNLDIInputError):
        fetch_nhdplus_nldi_navigate(
            seed_point=_FORT_MYERS_POINT,
            direction="XX",  # type: ignore[arg-type]
        )


def test_nldi_rejects_bad_distance() -> None:
    with pytest.raises(NHDPlusNLDIInputError):
        fetch_nhdplus_nldi_navigate(
            seed_point=_FORT_MYERS_POINT, direction="DM", distance_km=-1.0,
        )
    with pytest.raises(NHDPlusNLDIInputError):
        fetch_nhdplus_nldi_navigate(
            seed_point=_FORT_MYERS_POINT, direction="DM", distance_km=99999.0,
        )


def test_nldi_rejects_seed_outside_conus() -> None:
    with pytest.raises(NHDPlusNLDIInputError):
        fetch_nhdplus_nldi_navigate(
            seed_point=(15.0, 35.0),  # Mediterranean — not CONUS
            direction="DM",
        )


def test_nldi_rejects_non_positive_comid() -> None:
    with pytest.raises(NHDPlusNLDIInputError):
        fetch_nhdplus_nldi_navigate(comid=0, direction="DM")
    with pytest.raises(NHDPlusNLDIInputError):
        fetch_nhdplus_nldi_navigate(comid=-1, direction="DM")


def test_nldi_absorbs_invented_kwargs() -> None:
    with pytest.raises(NHDPlusNLDIInputError):
        fetch_nhdplus_nldi_navigate(
            comid=0, direction="DM", made_up_kwarg="ignored",
        )


# ---------------------------------------------------------------------------
# Input validation — 3DEP extra.
# ---------------------------------------------------------------------------


def test_3dep_supported_resolutions_set() -> None:
    """Supported set matches kickoff: five non-default 3DEP resolutions."""
    assert set(SUPPORTED_RESOLUTIONS) == {
        "1 arc-second",
        "1/9 arc-second",
        "1 meter",
        "2 arc-second",
        "5 meter",
    }
    # The canonical fetch_dem covers 1/3 arc-second — this tool MUST NOT.
    assert "1/3 arc-second" not in SUPPORTED_RESOLUTIONS


def test_3dep_rejects_unsupported_resolution() -> None:
    """1/3 arc-second is fetch_dem's job; this tool refuses it."""
    with pytest.raises(ThreeDEPExtraInputError):
        fetch_3dep_extra(
            bbox=_FORT_MYERS_BBOX,
            resolution="1/3 arc-second",  # type: ignore[arg-type]
        )


def test_3dep_rejects_bad_bbox() -> None:
    with pytest.raises(ThreeDEPExtraInputError):
        fetch_3dep_extra(bbox=(1.0, 2.0))  # type: ignore[arg-type]


def test_3dep_rejects_outside_us_bbox() -> None:
    """3DEP is US-only. A bbox in Europe raises ThreeDEPExtraInputError."""
    with pytest.raises(ThreeDEPExtraInputError):
        fetch_3dep_extra(bbox=(10.0, 45.0, 11.0, 46.0))


def test_3dep_rejects_bad_max_tiles() -> None:
    with pytest.raises(ThreeDEPExtraInputError):
        fetch_3dep_extra(bbox=_FORT_MYERS_BBOX, max_tiles=0)
    with pytest.raises(ThreeDEPExtraInputError):
        fetch_3dep_extra(bbox=_FORT_MYERS_BBOX, max_tiles=10_000)


def test_3dep_absorbs_invented_kwargs() -> None:
    with pytest.raises(ThreeDEPExtraInputError):
        fetch_3dep_extra(
            bbox=(0.0, 0.0, 0.0, 0.0),
            stray_kwarg="ignored",
        )


# ---------------------------------------------------------------------------
# Live smoke tests (gated by env).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _LIVE, reason="set TRID3NT_TEST_LIVE_PFDF_A11=1 to run")
def test_live_statsgo_fetch_kffact_fort_myers() -> None:
    """Real STATSGO KFFACT fetch over a small CONUS bbox returns a COG URI."""
    layer = fetch_statsgo_soils(bbox=_FORT_MYERS_BBOX, field="KFFACT")
    assert layer.layer_type == "raster"
    assert layer.uri.startswith("gs://")
    assert layer.uri.endswith(".tif")
    assert layer.units is None  # KFFACT is dimensionless
    assert "KFFACT" in layer.name


@pytest.mark.skipif(not _LIVE, reason="set TRID3NT_TEST_LIVE_PFDF_A11=1 to run")
def test_live_nldi_navigate_dm_from_caloosahatchee() -> None:
    """Real NLDI navigate DM from Fort Myers point returns a FlatGeobuf URI."""
    layer = fetch_nhdplus_nldi_navigate(
        seed_point=_FORT_MYERS_POINT,
        direction="DM",
        distance_km=20.0,
    )
    assert layer.layer_type == "vector"
    assert layer.uri.startswith("gs://")
    assert layer.uri.endswith(".fgb")
    assert "NLDI" in layer.name


@pytest.mark.skipif(not _LIVE, reason="set TRID3NT_TEST_LIVE_PFDF_A11=1 to run")
def test_live_3dep_extra_one_arc_second_fort_myers() -> None:
    """Real 3DEP 1-arc-second fetch over Fort Myers returns a COG URI."""
    layer = fetch_3dep_extra(
        bbox=_FORT_MYERS_BBOX,
        resolution="1 arc-second",
    )
    assert layer.layer_type == "raster"
    assert layer.uri.startswith("gs://")
    assert layer.uri.endswith(".tif")
    assert layer.units == "meters"
    assert "1 arc-second" in layer.name


# ---------------------------------------------------------------------------
# Direct end-to-end smoke (no cache): exercises the pfdf / NLDI plumbing
# without touching GCS. Gated by env.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _LIVE, reason="set TRID3NT_TEST_LIVE_PFDF_A11=1 to run")
def test_live_statsgo_direct_pfdf_call() -> None:
    """Direct pfdf.statsgo.read works for the same bbox — substrate health."""
    pfdf = pytest.importorskip("pfdf")
    from pfdf.data.usgs import statsgo
    from pfdf.projection import BoundingBox

    bb = BoundingBox(*_FORT_MYERS_BBOX, crs=4326)
    raster = statsgo.read("KFFACT", bb, timeout=60)
    assert raster is not None
    # pfdf rasters expose ``.values`` via .raster or .values, depending on
    # the version; just confirm the object loaded.
    assert hasattr(raster, "save")


@pytest.mark.skipif(not _LIVE, reason="set TRID3NT_TEST_LIVE_PFDF_A11=1 to run")
def test_live_nldi_snap_and_navigate_direct() -> None:
    """Direct NLDI HTTP smoke — confirms upstream is up."""
    from trid3nt_server.tools.fetchers.hydrology.fetch_nhdplus_nldi_navigate import (
        _navigate_flowlines,
        _snap_point_to_comid,
    )

    comid = _snap_point_to_comid(_FORT_MYERS_POINT)
    assert isinstance(comid, int) and comid > 0
    feats = _navigate_flowlines(comid, "DM", 20.0)
    # At least one feature for a coastal-area seed (may be 0 at network
    # terminus — Fort Myers's COMID typically has at least one DM reach).
    assert isinstance(feats, list)
