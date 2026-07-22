"""Unit tests for ``compute_blended_composite`` atomic tool (job-0319).

Coverage:
 1. ``test_compute_blended_composite_registered`` — tool in TOOL_REGISTRY with
    correct metadata (cacheable=True, ttl_class="static-30d",
    source_class="blended").
 2. ``test_blend_resolvable_params_in_allowlist`` — base/overlay layer URIs are
    in RESOLVABLE_URI_PARAMS so the server resolves handles → COG URIs.
 3. ``test_compute_blended_composite_multiply_math`` — blends a 3-band RGB base
    with a 1-band grayscale overlay; asserts output COG has overviews, dims
    match the base, and the multiply math is correct on a sample pixel.
 4. ``test_compute_blended_composite_invalid_mode_raises`` — bad blend_mode →
    typed BlendedCompositeError(error_code="INVALID_BLEND_MODE").
 5. ``test_compute_blended_composite_returns_layer_uri_fields`` — LayerURI
    fields correct (raster, role, rgb units, "Shaded" name for multiply).
 6. ``test_compute_blended_composite_cache_hit_skips_fetch`` — second identical
    call hits the cache (blend not re-run).
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import patch

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds

from grace2_agent.tools import TOOL_REGISTRY
from grace2_agent.tools.compute_blended_composite import (
    BlendedCompositeError,
    compute_blended_composite,
)

PINNED_NOW = datetime(2026, 6, 16, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Synthetic raster helpers (in-memory → temp file)
# ---------------------------------------------------------------------------


def _write_rgb_base(path: str, size: int = 600) -> np.ndarray:
    """Write a 3-band RGB uint8 base raster; return the RGB array (3, H, W)."""
    rng = np.random.default_rng(7)
    rgb = rng.integers(40, 240, size=(3, size, size), dtype=np.uint8)
    transform = from_bounds(0.0, 0.0, size * 10.0, size * 10.0, size, size)
    profile = {
        "driver": "GTiff",
        "dtype": "uint8",
        "width": size,
        "height": size,
        "count": 3,
        "crs": "EPSG:5070",
        "transform": transform,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(rgb)
    return rgb.astype(np.float32)


def _write_gray_overlay(path: str, size: int = 600) -> np.ndarray:
    """Write a 1-band grayscale uint8 overlay on the SAME grid; return (H, W)."""
    rng = np.random.default_rng(13)
    gray = rng.integers(0, 256, size=(size, size), dtype=np.uint8)
    transform = from_bounds(0.0, 0.0, size * 10.0, size * 10.0, size, size)
    profile = {
        "driver": "GTiff",
        "dtype": "uint8",
        "width": size,
        "height": size,
        "count": 1,
        "crs": "EPSG:5070",
        "transform": transform,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(gray, 1)
    return gray.astype(np.float32)


# A tiny NLCD-like palette: index → (R, G, B). These are the colors the
# composite MUST carry through (not a flat gray), the job-0323 fix.
_NLCD_PALETTE = {
    11: (71, 107, 160),    # open water — blue
    41: (104, 171, 95),    # deciduous forest — green
    81: (220, 217, 57),    # pasture/hay — yellow
    24: (171, 0, 0),       # developed high intensity — red
}


def _write_palette_base(
    path: str, size: int = 600
) -> tuple[np.ndarray, dict[int, tuple[int, int, int]]]:
    """Write a single-band palette-INDEX COG with an EMBEDDED color table.

    Mirrors the NLCD land-cover base the agent commonly blends: a uint8 raster
    of class *indices* whose RGB colors live ONLY in an embedded GDAL color
    table (``dst.write_colormap``) — there is no explicit per-pixel RGB. Returns
    ``(index_array (H, W), palette {index: (r, g, b)})``.
    """
    rng = np.random.default_rng(101)
    classes = np.array(sorted(_NLCD_PALETTE.keys()), dtype=np.uint8)
    idx = rng.choice(classes, size=(size, size)).astype(np.uint8)
    transform = from_bounds(0.0, 0.0, size * 10.0, size * 10.0, size, size)
    profile = {
        "driver": "GTiff",
        "dtype": "uint8",
        "width": size,
        "height": size,
        "count": 1,
        "crs": "EPSG:5070",
        "transform": transform,
    }
    # GDAL colormap wants {index: (r, g, b, a)}; make every class opaque.
    colormap = {i: (r, g, b, 255) for i, (r, g, b) in _NLCD_PALETTE.items()}
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(idx, 1)
        dst.write_colormap(1, colormap)
    return idx, dict(_NLCD_PALETTE)


def _write_gray_base(path: str, size: int = 600) -> np.ndarray:
    """Write a single-band grayscale base with NO color table; return (H, W).

    This is a true-grayscale single-band base (e.g. a hillshade used AS the
    base). It must keep the historical R=G=B grayscale-broadcast behavior.
    """
    rng = np.random.default_rng(202)
    gray = rng.integers(20, 235, size=(size, size), dtype=np.uint8)
    transform = from_bounds(0.0, 0.0, size * 10.0, size * 10.0, size, size)
    profile = {
        "driver": "GTiff",
        "dtype": "uint8",
        "width": size,
        "height": size,
        "count": 1,
        "crs": "EPSG:5070",
        "transform": transform,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(gray, 1)
    return gray.astype(np.float32)


# ---------------------------------------------------------------------------
# Fake GCS scaffolding for cache-shim isolation
# ---------------------------------------------------------------------------


class _S3Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class FakeStorageClient:
    """In-memory S3 double (GCP decommissioned). ``store`` keyed by object KEY.

    Returns the per-test active instance installed by the autouse
    ``_route_cache_to_inmemory_s3`` fixture so the tool's real S3 read-through
    (boto3) reads/writes the same store the test inspects.
    """

    _active: "FakeStorageClient | None" = None

    def __new__(cls) -> "FakeStorageClient":
        if cls._active is not None:
            return cls._active
        return super().__new__(cls)

    def __init__(self) -> None:
        if getattr(self, "_init", False):
            return
        self._init = True
        self.store: dict[str, bytes] = {}
        self.last_put: dict | None = None

    def get_object(self, *, Bucket, Key):
        from botocore.exceptions import ClientError

        try:
            data = self.store[Key]
        except KeyError:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "not found"}},
                "GetObject",
            )
        return {"Body": _S3Body(data)}

    def put_object(self, *, Bucket, Key, Body, ContentType=None):
        data = Body.read() if hasattr(Body, "read") else Body
        self.store[Key] = data
        self.last_put = {"Bucket": Bucket, "Key": Key, "ContentType": ContentType}
        return {}


@pytest.fixture(autouse=True)
def _route_cache_to_inmemory_s3(monkeypatch):
    """Route boto3 S3 (the cache shim's only object store) to an in-memory double."""
    import boto3

    FakeStorageClient._active = None
    client = FakeStorageClient()
    FakeStorageClient._active = client

    def _factory(service_name, *a, **k):
        assert service_name == "s3"
        return client

    monkeypatch.setattr(boto3, "client", _factory)
    try:
        yield client
    finally:
        FakeStorageClient._active = None


@pytest.fixture()
def fake_storage():
    return FakeStorageClient()


# ---------------------------------------------------------------------------
# Test 1 — registration / auto-discovery
# ---------------------------------------------------------------------------


def test_compute_blended_composite_registered():
    """compute_blended_composite is in TOOL_REGISTRY with the expected metadata."""
    assert "compute_blended_composite" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["compute_blended_composite"]
    assert entry.metadata.cacheable is True
    assert entry.metadata.ttl_class == "static-30d"
    assert entry.metadata.source_class == "blended"
    # Registered via the @register_tool decorator from the module's own import.
    assert entry.module == "grace2_agent.tools.compute_blended_composite"


# ---------------------------------------------------------------------------
# Test 2 — handle resolution wiring (server resolves base/overlay handles)
# ---------------------------------------------------------------------------


def test_blend_resolvable_params_in_allowlist():
    """base/overlay layer URIs resolve through the session uri-registry."""
    from grace2_agent.uri_registry import RESOLVABLE_URI_PARAMS

    assert "base_layer_uri" in RESOLVABLE_URI_PARAMS
    assert "overlay_layer_uri" in RESOLVABLE_URI_PARAMS


# ---------------------------------------------------------------------------
# Test 3 — multiply math + overviews + dims (the headline correctness test)
# ---------------------------------------------------------------------------


def test_compute_blended_composite_multiply_math(fake_storage):
    """Blend a 3-band RGB base with a 1-band grayscale overlay.

    Asserts: (a) output COG has overviews, (b) output dims == base dims,
    (c) multiply math correct on a sample pixel:
        result_rgb = round(base_rgb * (overlay_gray / 255)).
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        base_path = os.path.join(tmpdir, "base_rgb.tif")
        overlay_path = os.path.join(tmpdir, "overlay_gray.tif")
        # 600px > the COG driver's 512px overview threshold, so overviews are
        # built (the production behavior the agent depends on for fast WMS).
        base_rgb = _write_rgb_base(base_path, size=600)
        overlay_gray = _write_gray_overlay(overlay_path, size=600)

        result = compute_blended_composite(
            base_layer_uri=base_path,
            overlay_layer_uri=overlay_path,
            blend_mode="multiply",
            overlay_opacity=1.0,
            _bucket="test-bucket",
        )

        # Pull the written COG bytes back out of the fake store.
        assert result.uri is not None and result.uri.endswith(".tif")
        (cog_bytes,) = list(fake_storage.store.values())
        out_path = os.path.join(tmpdir, "out.tif")
        with open(out_path, "wb") as f:
            f.write(cog_bytes)

        with rasterio.open(out_path) as src:
            # (a) overviews present (the COG writer built them).
            assert src.overviews(1), (
                f"output COG must carry overviews; got {src.overviews(1)!r}"
            )
            # (b) dims match the base grid.
            assert (src.height, src.width) == base_rgb.shape[1:], (
                f"dims {(src.height, src.width)} != base {base_rgb.shape[1:]}"
            )
            assert src.count == 4, "RGBA composite expected (3 color + 1 alpha)"
            out_rgb = src.read([1, 2, 3]).astype(np.float32)

        # (c) multiply math on a sample interior pixel.
        r, c = 200, 311
        for band in range(3):
            expected = base_rgb[band, r, c] * (overlay_gray[r, c] / 255.0)
            got = out_rgb[band, r, c]
            # COG DEFLATE is lossless; allow a 1-LSB rounding tolerance.
            assert abs(got - expected) <= 1.0, (
                f"band {band} pixel ({r},{c}): got {got}, expected ~{expected:.2f} "
                f"(base={base_rgb[band, r, c]}, gray={overlay_gray[r, c]})"
            )


# ---------------------------------------------------------------------------
# Test 3b — palette-INDEX base (embedded color table) keeps palette colors
# ---------------------------------------------------------------------------


def test_compute_blended_composite_palette_base_keeps_palette_colors(fake_storage):
    """job-0323: a single-band base with an EMBEDDED color table colorizes.

    The NLCD land-cover base is a single-band palette-INDEX raster whose colors
    live ONLY in an embedded GDAL color table. The composite MUST carry the real
    palette color (modulated by the overlay), NOT a flat gray broadcast.

    Asserts, per sampled pixel: the output RGB equals
        palette_rgb[index] * (overlay_gray / 255)
    AND that the three channels are NOT all-equal (i.e. it is a real color, not
    gray) wherever the palette entry is itself non-gray.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        base_path = os.path.join(tmpdir, "nlcd_landcover.tif")
        overlay_path = os.path.join(tmpdir, "hillshade.tif")
        idx, palette = _write_palette_base(base_path, size=600)
        overlay_gray = _write_gray_overlay(overlay_path, size=600)

        result = compute_blended_composite(
            base_layer_uri=base_path,
            overlay_layer_uri=overlay_path,
            blend_mode="multiply",
            overlay_opacity=1.0,
            _bucket="test-bucket",
        )

        assert result.uri is not None and result.uri.endswith(".tif")
        (cog_bytes,) = list(fake_storage.store.values())
        out_path = os.path.join(tmpdir, "out.tif")
        with open(out_path, "wb") as f:
            f.write(cog_bytes)

        with rasterio.open(out_path) as src:
            assert (src.height, src.width) == idx.shape
            assert src.count == 4
            out_rgb = src.read([1, 2, 3]).astype(np.float32)

        # Check several interior pixels: the output must match the PALETTE color
        # times the overlay multiply factor — proving colorization (not gray).
        non_gray_checks = 0
        for (r, c) in ((100, 100), (200, 311), (333, 50), (480, 510), (12, 590)):
            class_idx = int(idx[r, c])
            pr, pg, pb = palette[class_idx]
            factor = overlay_gray[r, c] / 255.0
            for band, pal_val in enumerate((pr, pg, pb)):
                expected = pal_val * factor
                got = out_rgb[band, r, c]
                assert abs(got - expected) <= 1.0, (
                    f"pixel ({r},{c}) band {band}: got {got}, expected "
                    f"~{expected:.2f} (palette idx={class_idx} "
                    f"rgb=({pr},{pg},{pb}), gray={overlay_gray[r, c]})"
                )
            # The palette colors are deliberately non-gray; the multiply factor
            # is a single scalar per pixel, so a non-gray palette stays non-gray.
            if not (pr == pg == pb):
                rr, gg, bb = (out_rgb[0, r, c], out_rgb[1, r, c], out_rgb[2, r, c])
                assert not (rr == gg == bb), (
                    f"pixel ({r},{c}) came out GRAY ({rr},{gg},{bb}) — palette "
                    f"colorization failed (idx={class_idx} rgb=({pr},{pg},{pb}))"
                )
                non_gray_checks += 1
        assert non_gray_checks >= 1, "no non-gray palette pixel was exercised"


# ---------------------------------------------------------------------------
# Test 3c — single-band base with NO color table still broadcasts grayscale
# ---------------------------------------------------------------------------


def test_compute_blended_composite_grayscale_base_no_colormap_stays_gray(fake_storage):
    """A single-band base with NO embedded color table keeps R=G=B grayscale.

    This is the true-grayscale base path (e.g. a hillshade used AS the base):
    no colormap → the historical grayscale-broadcast behavior must be preserved.
    Asserts the three output channels are equal per pixel and equal the
    grayscale base value times the overlay multiply factor.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        base_path = os.path.join(tmpdir, "gray_base.tif")
        overlay_path = os.path.join(tmpdir, "overlay.tif")
        gray_base = _write_gray_base(base_path, size=600)
        overlay_gray = _write_gray_overlay(overlay_path, size=600)

        result = compute_blended_composite(
            base_layer_uri=base_path,
            overlay_layer_uri=overlay_path,
            blend_mode="multiply",
            overlay_opacity=1.0,
            _bucket="test-bucket",
        )

        (cog_bytes,) = list(fake_storage.store.values())
        out_path = os.path.join(tmpdir, "out.tif")
        with open(out_path, "wb") as f:
            f.write(cog_bytes)

        with rasterio.open(out_path) as src:
            assert src.count == 4
            out_rgb = src.read([1, 2, 3]).astype(np.float32)

        for (r, c) in ((150, 150), (200, 311), (400, 90)):
            rr, gg, bb = (out_rgb[0, r, c], out_rgb[1, r, c], out_rgb[2, r, c])
            # Grayscale broadcast: all three channels identical (lossless COG).
            assert abs(rr - gg) <= 1.0 and abs(gg - bb) <= 1.0, (
                f"pixel ({r},{c}) not gray: ({rr},{gg},{bb}) — single-band base "
                f"with no colormap must stay R=G=B"
            )
            expected = gray_base[r, c] * (overlay_gray[r, c] / 255.0)
            assert abs(rr - expected) <= 1.0, (
                f"pixel ({r},{c}): got {rr}, expected ~{expected:.2f} "
                f"(base={gray_base[r, c]}, gray={overlay_gray[r, c]})"
            )


# ---------------------------------------------------------------------------
# Test 4 — invalid blend mode raises typed error
# ---------------------------------------------------------------------------


def test_compute_blended_composite_invalid_mode_raises(fake_storage):
    with pytest.raises(BlendedCompositeError) as exc_info:
        compute_blended_composite(
            base_layer_uri="/tmp/whatever_base.tif",
            overlay_layer_uri="/tmp/whatever_overlay.tif",
            blend_mode="not_a_mode",  # type: ignore[arg-type]
            _bucket="test-bucket",
        )
    assert exc_info.value.error_code == "INVALID_BLEND_MODE"


# ---------------------------------------------------------------------------
# Test 5 — LayerURI fields
# ---------------------------------------------------------------------------


def test_compute_blended_composite_returns_layer_uri_fields(fake_storage):
    with tempfile.TemporaryDirectory() as tmpdir:
        base_path = os.path.join(tmpdir, "landcover.tif")
        overlay_path = os.path.join(tmpdir, "hillshade.tif")
        _write_rgb_base(base_path, size=32)
        _write_gray_overlay(overlay_path, size=32)

        result = compute_blended_composite(
            base_layer_uri=base_path,
            overlay_layer_uri=overlay_path,
            blend_mode="multiply",
            _bucket="test-bucket",
        )

    assert result.layer_type == "raster"
    assert result.role == "context"
    assert result.units == "rgb"
    assert "blended" in result.layer_id
    assert "multiply" in result.layer_id
    assert result.name.startswith("Shaded")


# ---------------------------------------------------------------------------
# Test 6 — cache hit skips re-blend
# ---------------------------------------------------------------------------


def test_compute_blended_composite_cache_hit_skips_fetch(fake_storage):
    with tempfile.TemporaryDirectory() as tmpdir:
        base_path = os.path.join(tmpdir, "base.tif")
        overlay_path = os.path.join(tmpdir, "overlay.tif")
        _write_rgb_base(base_path, size=32)
        _write_gray_overlay(overlay_path, size=32)

        # First call populates the cache.
        first = compute_blended_composite(
            base_layer_uri=base_path,
            overlay_layer_uri=overlay_path,
            _bucket="test-bucket",
        )
        assert len(fake_storage.store) == 1

        # Second identical call must hit the cache: _run_blend not invoked.
        with patch(
            "grace2_agent.tools.compute_blended_composite._run_blend",
            side_effect=AssertionError("_run_blend should not run on cache hit"),
        ):
            second = compute_blended_composite(
                base_layer_uri=base_path,
                overlay_layer_uri=overlay_path,
                _bucket="test-bucket",
            )
        assert second.uri == first.uri


# ---------------------------------------------------------------------------
# job-0324 follow-up — description must tell the agent the BASE may be a
# paletted/categorical raster (NLCD land cover) so it stops substituting
# compute_colored_relief (elevation colors) as the blend base. The "bake NLCD
# land cover into hillshade" demo rendered the wrong colors because the
# description never said land cover could be the base directly.
# ---------------------------------------------------------------------------


def test_blend_description_says_base_may_be_paletted_categorical():
    """The docstring must state the BASE may be a paletted/categorical raster."""
    doc = compute_blended_composite.__doc__ or ""
    flat = " ".join(doc.split())
    assert "PALETTED" in flat or "paletted" in flat
    assert "categorical" in flat or "CATEGORICAL" in flat
    # Names NLCD land cover specifically as a supported base.
    assert "NLCD" in flat
    assert "land cover" in flat or "land-cover" in flat


def test_blend_description_says_embedded_color_table_applied():
    """The docstring must say the tool reads + applies the EMBEDDED color table,
    so blending land cover directly yields the NLCD CLASS colors."""
    doc = compute_blended_composite.__doc__ or ""
    flat = " ".join(doc.split())
    assert "embedded" in flat.lower() and "color table" in flat.lower()
    # The payoff sentence: real NLCD class colors result.
    assert "class colors" in flat.lower() or "CLASS colors" in flat


def test_blend_description_forbids_colored_relief_substitution():
    """The docstring must warn AGAINST substituting compute_colored_relief
    (elevation colors) for the land-cover base — the exact failure mode."""
    doc = compute_blended_composite.__doc__ or ""
    flat = " ".join(doc.split())
    assert "compute_colored_relief" in flat
    # colored_relief is elevation colors, not land-cover classes.
    assert "elevation" in flat.lower() or "ELEVATION" in flat
    # Pass the land cover DIRECTLY as the base.
    assert "DIRECTLY" in flat or "directly" in flat
