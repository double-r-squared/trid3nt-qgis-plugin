"""Unit tests for the shared cog_io COG write / reproject / upload helpers (STEP 1).

Pins the PARAMETER PRESERVATION that makes the five-engine dedupe byte-identical:
the mask predicate, the reproject on/off path, the CRS round-trip guard, the
scheme-aware upload (s3 ContentType / gs fsspec-vs-gcs_client / file:// fallback),
and the generic CogIoError stage tokens the engine shims map onto their codes.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("rasterio")
pytest.importorskip("numpy")

import numpy as np  # noqa: E402
import rasterio  # noqa: E402
from rasterio.transform import from_bounds  # noqa: E402
from rasterio.warp import Resampling  # noqa: E402

from trid3nt_server.workflows import cog_io  # noqa: E402
from trid3nt_server.workflows.cog_io import CogIoError  # noqa: E402


# --------------------------------------------------------------------------- #
# write_cog_4326_from_grid: already-4326 direct-write path (no warp).
# --------------------------------------------------------------------------- #
def _bbox_transform(bbox, w, h):
    return from_bounds(bbox[0], bbox[1], bbox[2], bbox[3], w, h)


def test_direct_write_4326_no_reproject_preserves_grid(tmp_path: Path) -> None:
    grid = np.array([[1.0, 2.0], [3.0, 4.0]], dtype="float32")
    bbox = (-100.0, 40.0, -99.0, 41.0)
    cog = cog_io.write_cog_4326_from_grid(
        grid,
        src_crs="EPSG:4326",
        src_transform=_bbox_transform(bbox, 2, 2),
        reproject=False,
        crs_roundtrip_guard=True,
    )
    try:
        with rasterio.open(cog) as ds:
            assert ds.crs.to_epsg() == 4326
            assert ds.read(1).tolist() == [[1.0, 2.0], [3.0, 4.0]]
        # cog_bbox helper reads it back.
        bb = cog_io.cog_bbox_4326(cog)
        assert bb is not None and bb[0] == pytest.approx(-100.0)
    finally:
        cog_io.safe_unlink(cog)


def test_mask_callable_is_applied_before_write(tmp_path: Path) -> None:
    grid = np.array([[0.0, 0.5], [1.0, 2.0]], dtype="float32")
    bbox = (-100.0, 40.0, -99.0, 41.0)

    def _mask(a):
        # mask-below-1.0 to NaN (the plume/openquake floor pattern, declared param)
        return np.where(a >= 1.0, a, np.nan).astype("float32")

    cog = cog_io.write_cog_4326_from_grid(
        grid,
        src_crs="EPSG:4326",
        src_transform=_bbox_transform(bbox, 2, 2),
        reproject=False,
        mask=_mask,
    )
    try:
        with rasterio.open(cog) as ds:
            out = ds.read(1)
        # below-floor cells masked to NaN; >=1 preserved.
        assert np.isnan(out[0, 0]) and np.isnan(out[0, 1])
        assert out[1, 0] == pytest.approx(1.0) and out[1, 1] == pytest.approx(2.0)
    finally:
        cog_io.safe_unlink(cog)


def test_crs_guard_off_skips_roundtrip(tmp_path: Path) -> None:
    # With the guard off, a write still succeeds (MODFLOW/OpenQuake behavior).
    grid = np.ones((3, 3), dtype="float32")
    bbox = (-100.0, 40.0, -99.0, 41.0)
    cog = cog_io.write_cog_4326_from_grid(
        grid,
        src_crs="EPSG:4326",
        src_transform=_bbox_transform(bbox, 3, 3),
        reproject=False,
        crs_roundtrip_guard=False,
    )
    try:
        assert cog.exists()
    finally:
        cog_io.safe_unlink(cog)


# --------------------------------------------------------------------------- #
# write_cog_4326_from_grid: projected -> 4326 warp path (resampling declared).
# --------------------------------------------------------------------------- #
def test_reproject_path_warps_to_4326(tmp_path: Path) -> None:
    # A small UTM-17N grid; warp to 4326 and assert the tag round-trips.
    grid = np.array([[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]], dtype="float32")
    # UTM metres transform (origin NW, 30 m cells).
    src_transform = rasterio.transform.from_origin(500000.0, 2900000.0, 30.0, 30.0)
    cog = cog_io.write_cog_4326_from_grid(
        grid,
        src_crs="EPSG:32617",
        src_transform=src_transform,
        reproject=True,
        resampling=Resampling.nearest,
        crs_roundtrip_guard=True,
    )
    try:
        with rasterio.open(cog) as ds:
            assert ds.crs.to_epsg() == 4326
            # bounds are geographic now (|lon| < 360).
            assert abs(ds.bounds.left) < 360
    finally:
        cog_io.safe_unlink(cog)


def test_crs_guard_rejects_mistagged_geographic(tmp_path: Path) -> None:
    # A grid tagged EPSG:4326 but with PROJECTED-metre bounds (|x| > 360) must
    # trip the guard (the mistagged-raster bug). Build it directly to force the
    # guard's geographic-magnitude leg.
    grid = np.ones((2, 2), dtype="float32")
    # from_bounds with metre-scale bounds but a 4326 tag.
    bad_transform = from_bounds(500000.0, 2900000.0, 500060.0, 2900060.0, 2, 2)
    with pytest.raises(CogIoError) as ei:
        cog_io.write_cog_4326_from_grid(
            grid,
            src_crs="EPSG:4326",
            src_transform=bad_transform,
            reproject=False,
            crs_roundtrip_guard=True,
        )
    assert ei.value.stage == "CRS_MISMATCH"


# --------------------------------------------------------------------------- #
# reproject_cog_file_to_4326 (Landlab worker-field path).
# --------------------------------------------------------------------------- #
def test_reproject_file_to_4326_returns_bbox(tmp_path: Path) -> None:
    src = tmp_path / "field_utm.tif"
    grid = np.array([[0.2, 0.8], [0.9, 0.1]], dtype="float32")
    src_transform = rasterio.transform.from_origin(500000.0, 2900000.0, 30.0, 30.0)
    with rasterio.open(
        src, "w", driver="GTiff", width=2, height=2, count=1, dtype="float32",
        crs="EPSG:32617", transform=src_transform, nodata=float("nan"),
    ) as dst:
        dst.write(grid, 1)

    out_cog, bbox = cog_io.reproject_cog_file_to_4326(src, crs_roundtrip_guard=True)
    try:
        assert bbox is not None and abs(bbox[0]) < 360
        with rasterio.open(out_cog) as ds:
            assert ds.crs.to_epsg() == 4326
    finally:
        cog_io.safe_unlink(out_cog)


def test_reproject_file_missing_raises_read(tmp_path: Path) -> None:
    with pytest.raises(CogIoError) as ei:
        cog_io.reproject_cog_file_to_4326(tmp_path / "nope.tif")
    assert ei.value.stage == "READ"


def test_reproject_file_no_crs_raises_read(tmp_path: Path) -> None:
    src = tmp_path / "nocrs.tif"
    with rasterio.open(
        src, "w", driver="GTiff", width=2, height=2, count=1, dtype="float32",
        transform=from_bounds(0, 0, 1, 1, 2, 2), nodata=float("nan"),
    ) as dst:
        dst.write(np.ones((2, 2), dtype="float32"), 1)
    with pytest.raises(CogIoError) as ei:
        cog_io.reproject_cog_file_to_4326(src)
    assert ei.value.stage == "READ"


# --------------------------------------------------------------------------- #
# upload_cog: scheme-aware, ContentType, gs backend, file:// fallback.
# --------------------------------------------------------------------------- #
def test_upload_s3_uses_content_type_when_set(tmp_path: Path) -> None:
    cog = tmp_path / "x.tif"
    cog.write_bytes(b"tiff")
    fake_client = MagicMock()
    with (
        patch("trid3nt_server.tools.cache.storage_scheme", return_value="s3"),
        patch("trid3nt_server.tools.solver._get_s3_client", return_value=fake_client),
    ):
        uri = cog_io.upload_cog(
            cog, "run1", "bkt", dest_filename="d.tif", content_type="image/tiff"
        )
    assert uri == "s3://bkt/run1/d.tif"
    kwargs = fake_client.put_object.call_args.kwargs
    assert kwargs["Bucket"] == "bkt" and kwargs["Key"] == "run1/d.tif"
    assert kwargs["ContentType"] == "image/tiff"


def test_upload_s3_omits_content_type_when_none(tmp_path: Path) -> None:
    cog = tmp_path / "x.tif"
    cog.write_bytes(b"tiff")
    fake_client = MagicMock()
    with (
        patch("trid3nt_server.tools.cache.storage_scheme", return_value="s3"),
        patch("trid3nt_server.tools.solver._get_s3_client", return_value=fake_client),
    ):
        cog_io.upload_cog(
            cog, "run1", "bkt", dest_filename="d.tif", content_type=None
        )
    kwargs = fake_client.put_object.call_args.kwargs
    assert "ContentType" not in kwargs  # OpenQuake byte-identical: header omitted


def test_upload_s3_missing_bucket_raises_upload(tmp_path: Path) -> None:
    cog = tmp_path / "x.tif"
    cog.write_bytes(b"tiff")
    with (
        patch("trid3nt_server.tools.cache.storage_scheme", return_value="s3"),
        patch.dict("os.environ", {}, clear=False),
    ):
        # ensure no env bucket leaks in
        with patch.dict("os.environ", {"TRID3NT_RUNS_BUCKET": ""}, clear=False):
            with pytest.raises(CogIoError) as ei:
                cog_io.upload_cog(cog, "run1", None, dest_filename="d.tif")
    assert ei.value.stage == "UPLOAD"


def test_upload_s3_put_failure_raises_upload(tmp_path: Path) -> None:
    cog = tmp_path / "x.tif"
    cog.write_bytes(b"tiff")
    fake_client = MagicMock()
    fake_client.put_object.side_effect = RuntimeError("boom")
    with (
        patch("trid3nt_server.tools.cache.storage_scheme", return_value="s3"),
        patch("trid3nt_server.tools.solver._get_s3_client", return_value=fake_client),
    ):
        with pytest.raises(CogIoError) as ei:
            cog_io.upload_cog(cog, "r", "bkt", dest_filename="d.tif")
    assert ei.value.stage == "UPLOAD"


def test_upload_gs_no_bucket_with_fallback_returns_file_uri(tmp_path: Path) -> None:
    cog = tmp_path / "x.tif"
    cog.write_bytes(b"tiff")
    with (
        patch("trid3nt_server.tools.cache.storage_scheme", return_value="gs"),
        patch.dict("os.environ", {"TRID3NT_RUNS_BUCKET": ""}, clear=False),
    ):
        uri = cog_io.upload_cog(
            cog, "r", None, dest_filename="d.tif",
            gs_backend="gcs_client", gs_fallback_to_file=True,
            runs_bucket_default=None,
        )
    assert uri == f"file://{cog}"  # OpenQuake no-bucket short-circuit


def test_upload_gs_fsspec_failure_no_fallback_raises(tmp_path: Path) -> None:
    cog = tmp_path / "x.tif"
    cog.write_bytes(b"tiff")
    fake_fsspec = MagicMock()
    fake_fsspec.filesystem.return_value.put.side_effect = RuntimeError("gcs down")
    with (
        patch("trid3nt_server.tools.cache.storage_scheme", return_value="gs"),
        patch.dict("sys.modules", {"fsspec": fake_fsspec}),
    ):
        with pytest.raises(CogIoError) as ei:
            cog_io.upload_cog(
                cog, "r", "bkt", dest_filename="d.tif",
                gs_backend="fsspec", gs_fallback_to_file=False,
            )
    assert ei.value.stage == "UPLOAD"


def test_upload_gs_fsspec_failure_with_fallback_returns_file(tmp_path: Path) -> None:
    cog = tmp_path / "x.tif"
    cog.write_bytes(b"tiff")
    fake_fsspec = MagicMock()
    fake_fsspec.filesystem.return_value.put.side_effect = RuntimeError("gcs down")
    with (
        patch("trid3nt_server.tools.cache.storage_scheme", return_value="gs"),
        patch.dict("sys.modules", {"fsspec": fake_fsspec}),
    ):
        uri = cog_io.upload_cog(
            cog, "r", "bkt", dest_filename="d.tif",
            gs_backend="fsspec", gs_fallback_to_file=True,
        )
    assert uri == f"file://{cog}"  # MODFLOW gs best-effort fallback


def test_cog_bbox_4326_degrades_to_none_on_bad_path(tmp_path: Path) -> None:
    assert cog_io.cog_bbox_4326(tmp_path / "missing.tif") is None
