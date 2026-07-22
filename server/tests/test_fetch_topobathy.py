"""Unit + (gated) live tests for the SFINCS North Star P1 ``fetch_topobathy``
coastal topo-bathymetry fetcher.

Proves (all with SYNTHETIC small rasters — the remote CUDEM/3DEP fetch is
mocked):

1. Merge precedence — CUDEM (listed LAST) wins in the land/coast overlap,
   3DEP fills where CUDEM is nodata.
2. Datum gate — a non-NAVD88 (MHW/MSL/LMSL) tile raises ``TopobathyDatumError``;
   a documented ``navd88_offset_m`` converts instead of raising; a NAVD88 tile
   passes with a zero offset.
3. Output contract — EPSG:32616, single-band float32, positive-up land /
   NEGATIVE bathymetry (NO sign flip).
4. CUDEM-missing fallback — when no CUDEM tiles intersect (or the manifest is
   unreachable), the tool DEGRADES to 3DEP-land-only and returns
   ``bathymetry_present=False`` + an honest ``fallback_warning`` (never a
   silent dead-end / fabricated bathy).

Plus registry-shape, typed-error-envelope, input-validation, and the
tile-index intersect math.

A SINGLE live smoke fetch of the CI bbox (-85.45, 29.92, -85.38, 29.98) is
attempted but NOT required (gated by TRID3NT_TEST_LIVE_TOPOBATHY=1) — the build
does not block on a live pull.
"""

from __future__ import annotations

import os
import tempfile
from typing import Any

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools import fetch_topobathy as ftb
from trid3nt_server.tools.fetch_topobathy import (
    ETOPO_GLOBAL_ROOT,
    TARGET_CRS,
    TopobathyDatumError,
    TopobathyEmptyError,
    TopobathyError,
    TopobathyInputError,
    TopobathyResult,
    TopobathyUpstreamError,
    _build_merged_topobathy,
    _classify_vertical_datum,
    _etopo_url_for_corner,
    _fetch_topobathy_bytes_and_flags,
    _merge_sources,
    _parse_tile_nw_corner,
    _select_cudem_tiles,
    _select_etopo_tiles,
    _tile_intersects_bbox,
    estimate_payload_mb,
    fetch_topobathy,
)


_LIVE = os.environ.get("TRID3NT_TEST_LIVE_TOPOBATHY") == "1"

# SFINCS North Star demo + CI smoke bboxes.
_DEMO_BBOX = (-85.75, 29.55, -85.25, 30.20)
_SMOKE_BBOX = (-85.45, 29.92, -85.38, 29.98)
# Crescent City, CA — the live GeoClaw tsunami AOI that found 0/930 CUDEM tiles
# (US Pacific coast; CUDEM's hosted 1/9" collection omits it). Drives the global
# ETOPO 2022 fallback.
_CRESCENT_CITY_BBOX = (-124.22, 41.73, -124.14, 41.86)


# ---------------------------------------------------------------------------
# Synthetic raster helpers (EPSG:4326 source, overlapping extents).
# ---------------------------------------------------------------------------


def _write_synth_raster(
    path: str,
    *,
    bbox: tuple[float, float, float, float],
    nx: int,
    ny: int,
    fill: float,
    nodata: float,
    nodata_mask: np.ndarray | None = None,
    crs: str = "EPSG:4326",
) -> None:
    """Write a small single-band float32 GeoTIFF spanning ``bbox`` in EPSG:4326.

    ``nodata_mask`` (bool array, True == set to nodata) lets a test carve a
    hole so the merge precedence is observable.
    """
    west, south, east, north = bbox
    res_x = (east - west) / nx
    res_y = (north - south) / ny
    transform = from_origin(west, north, res_x, res_y)
    arr = np.full((ny, nx), fill, dtype="float32")
    if nodata_mask is not None:
        arr[nodata_mask] = nodata
    profile = {
        "driver": "GTiff",
        "dtype": "float32",
        "count": 1,
        "height": ny,
        "width": nx,
        "crs": crs,
        "transform": transform,
        "nodata": nodata,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(arr, 1)


# ---------------------------------------------------------------------------
# Registry shape.
# ---------------------------------------------------------------------------


def test_topobathy_registered_with_expected_metadata() -> None:
    assert "fetch_topobathy" in TOOL_REGISTRY
    md = TOOL_REGISTRY["fetch_topobathy"].metadata
    assert md.ttl_class == "static-30d"
    assert md.source_class == "topobathy"
    assert md.cacheable is True
    assert getattr(md, "supports_global_query", None) is False
    assert getattr(md, "payload_mb_estimator_name", None) == "estimate_payload_mb"


def test_topobathy_in_coastal_category() -> None:
    from trid3nt_server.categories import PRIMARY_CATEGORY, tools_for_category

    assert PRIMARY_CATEGORY.get("fetch_topobathy") == "coastal"
    assert "fetch_topobathy" in tools_for_category("coastal")


# ---------------------------------------------------------------------------
# Typed-error envelope (FR-AS-11).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cls, code, retryable",
    [
        (TopobathyError, "TOPOBATHY_ERROR", True),
        (TopobathyInputError, "TOPOBATHY_INPUT_INVALID", False),
        (TopobathyUpstreamError, "TOPOBATHY_UPSTREAM_ERROR", True),
        (TopobathyEmptyError, "TOPOBATHY_EMPTY", False),
        (TopobathyDatumError, "TOPOBATHY_DATUM_MISMATCH", False),
    ],
)
def test_typed_error_envelope(cls: type, code: str, retryable: bool) -> None:
    err = cls("boom")
    assert err.error_code == code
    assert err.retryable is retryable
    assert isinstance(err, RuntimeError)


# ---------------------------------------------------------------------------
# Payload estimator.
# ---------------------------------------------------------------------------


def test_estimate_payload_mb_scales_with_bbox() -> None:
    small = estimate_payload_mb(bbox=_SMOKE_BBOX)
    big = estimate_payload_mb(bbox=_DEMO_BBOX)
    assert 0.0 < small < big
    assert estimate_payload_mb(bbox=None) > 0.0


# ---------------------------------------------------------------------------
# Input validation.
# ---------------------------------------------------------------------------


def test_rejects_bad_bbox_shape() -> None:
    with pytest.raises(TopobathyInputError):
        fetch_topobathy(bbox=(1.0, 2.0))  # type: ignore[arg-type]


def test_rejects_non_finite_bbox() -> None:
    with pytest.raises(TopobathyInputError):
        fetch_topobathy(bbox=(float("nan"), 29.55, -85.25, 30.20))


def test_rejects_degenerate_bbox() -> None:
    with pytest.raises(TopobathyInputError):
        fetch_topobathy(bbox=(-85.5, 29.9, -85.5, 29.9))


def test_rejects_inland_foreign_bbox() -> None:
    """A bbox far from the US coast (Europe) fails fast — CUDEM is US-only."""
    with pytest.raises(TopobathyInputError):
        fetch_topobathy(bbox=(10.0, 45.0, 11.0, 46.0))


def test_rejects_bad_resolution() -> None:
    with pytest.raises(TopobathyInputError):
        fetch_topobathy(bbox=_SMOKE_BBOX, resolution_m=0)
    with pytest.raises(TopobathyInputError):
        fetch_topobathy(bbox=_SMOKE_BBOX, resolution_m=99999)


def test_rejects_non_finite_offset() -> None:
    with pytest.raises(TopobathyInputError):
        fetch_topobathy(bbox=_SMOKE_BBOX, navd88_offset_m=float("inf"))


def test_absorbs_invented_kwargs() -> None:
    """Bad bbox is caught before the **_extra_ignored sink would matter."""
    with pytest.raises(TopobathyInputError):
        fetch_topobathy(bbox=(0.0, 0.0, 0.0, 0.0), stray_kwarg="ignored")


# ---------------------------------------------------------------------------
# Tile-index intersect math.
# ---------------------------------------------------------------------------


def test_parse_tile_nw_corner() -> None:
    """Tile name encodes the NW (upper-left) corner; 'w' is negative lon."""
    assert _parse_tile_nw_corner("ncei19_n30X00_w085X25_2019v1.tif") == (30.0, -85.25)
    assert _parse_tile_nw_corner(
        "https://x/AL_nwFL/ncei19_n29X75_w085X50_2019v1.tif"
    ) == (29.75, -85.5)
    assert _parse_tile_nw_corner("not_a_cudem_tile.tif") is None


def test_tile_intersects_smoke_bbox() -> None:
    """The CI smoke bbox falls in tile n30X00_w085X50 (lon [-85.50,-85.25])."""
    # NW (30.0, -85.50) tile spans lat [29.75,30.00], lon [-85.50,-85.25] — overlaps.
    assert _tile_intersects_bbox(30.0, -85.50, _SMOKE_BBOX) is True
    # NW (30.0, -85.25) tile spans lon [-85.25,-85.00] — east of the smoke bbox.
    assert _tile_intersects_bbox(30.0, -85.25, _SMOKE_BBOX) is False
    # A Tampa-area tile is nowhere near.
    assert _tile_intersects_bbox(27.25, -82.75, _SMOKE_BBOX) is False


def test_select_cudem_tiles_intersect_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """_select_cudem_tiles returns only AOI-overlapping tiles from a mocked
    manifest (no network)."""
    fake_manifest = [
        "https://x/AL_nwFL/ncei19_n30X00_w085X50_2019v1.tif",  # overlaps smoke
        "https://x/AL_nwFL/ncei19_n30X00_w085X25_2019v1.tif",  # east, no overlap
        "https://x/FL/ncei19_n27X25_w082X75_2017v1.tif",       # Tampa, no overlap
        "https://x/ignore_me.txt",                              # non-tif filtered
    ]
    monkeypatch.setattr(ftb, "_fetch_cudem_urllist", lambda *_a, **_k: fake_manifest)
    sel = _select_cudem_tiles(_SMOKE_BBOX, 30.0)
    assert sel == ["https://x/AL_nwFL/ncei19_n30X00_w085X50_2019v1.tif"]


# ---------------------------------------------------------------------------
# GLOBAL ETOPO 2022 fallback tile selection (the 0/930-CUDEM fix).
# ---------------------------------------------------------------------------


def test_etopo_url_for_corner_naming() -> None:
    """The 15-degree ETOPO tile URL encodes its NW corner: N/S lat, W/E lon."""
    url = _etopo_url_for_corner(45.0, -135.0)
    assert url.startswith(ETOPO_GLOBAL_ROOT)
    assert url.endswith("ETOPO_2022_v1_15s_N45W135_surface.tif")
    # Gulf-coast NW corner (Mexico Beach tile).
    assert _etopo_url_for_corner(30.0, -90.0).endswith("N30W090_surface.tif")
    # Southern / eastern hemispheres encode S / E.
    assert _etopo_url_for_corner(-15.0, 15.0).endswith("S15E015_surface.tif")


def test_select_etopo_tiles_crescent_city() -> None:
    """The Crescent City tsunami AOI (US Pacific) — where CUDEM finds 0 tiles —
    selects the single global ETOPO tile N45W135 (lat [30,45], lon [-135,-120]),
    which DOES contain it. This is the fix for the 0/930 land-only zero-inundation
    bug: a Pacific coastal AOI now gets a real global bathymetry source."""
    sel = _select_etopo_tiles(_CRESCENT_CITY_BBOX)
    assert len(sel) == 1
    assert sel[0].endswith("ETOPO_2022_v1_15s_N45W135_surface.tif")


def test_select_etopo_tiles_straddling_bbox() -> None:
    """A bbox straddling a 15-degree tile seam selects BOTH tiles (AABB overlap),
    so no nearshore strip is dropped at a tile boundary."""
    # Straddle the lon=-120 seam (N45W135 spans [-135,-120], N45W120 spans
    # [-120,-105]) and stay within lat [30,45].
    sel = _select_etopo_tiles((-120.5, 40.0, -119.5, 41.0))
    names = sorted(u.rsplit("/", 1)[-1] for u in sel)
    assert names == [
        "ETOPO_2022_v1_15s_N45W120_surface.tif",
        "ETOPO_2022_v1_15s_N45W135_surface.tif",
    ]


# ---------------------------------------------------------------------------
# Datum gate (Invariant 7) — pure decision function.
# ---------------------------------------------------------------------------


def test_datum_gate_accepts_navd88() -> None:
    assert _classify_vertical_datum("GEOGCRS ... NAVD88 height", None, "t") == 0.0


def test_datum_gate_rejects_tidal_without_offset() -> None:
    for marker in ("MHW", "vertical datum MSL", "LMSL height", "mean low water"):
        with pytest.raises(TopobathyDatumError):
            _classify_vertical_datum(marker, None, "tile")


def test_datum_gate_applies_documented_offset() -> None:
    """A tidal-datum tile WITH a documented NAVD88 offset converts, not raises."""
    assert _classify_vertical_datum("MHW", 0.23, "tile") == pytest.approx(0.23)


def test_datum_gate_absent_signal_defaults_to_navd88() -> None:
    """A bare tile with no vertical-CS tag accepts the CUDEM collection default
    (NAVD88) — only a POSITIVE non-NAVD88 marker trips the gate."""
    assert _classify_vertical_datum("", None, "tile") == 0.0


# ---------------------------------------------------------------------------
# Merge precedence + output contract (SYNTHETIC rasters, no network).
# ---------------------------------------------------------------------------


def test_merge_cudem_wins_on_coast_and_output_contract() -> None:
    """CUDEM (listed last) wins in the overlap; 3DEP fills nodata; output is
    EPSG:32616 single-band float32 with positive-up land + NEGATIVE bathy."""
    tmpdir = tempfile.mkdtemp(prefix="trid3nt_topobathy_test_")
    land_path = os.path.join(tmpdir, "land.tif")
    cudem_path = os.path.join(tmpdir, "cudem.tif")
    try:
        # 3DEP land: a flat +50 m plateau over the whole smoke bbox (land-only;
        # no bathymetry — that's what 3DEP is).
        _write_synth_raster(
            land_path, bbox=_SMOKE_BBOX, nx=40, ny=40, fill=50.0, nodata=-9999.0
        )
        # CUDEM: the WEST half is bathymetry (-8 m, below NAVD88), the EAST half
        # is nodata (so 3DEP land must fill there). CUDEM positive-up: bathy is
        # NEGATIVE, no sign flip.
        col = np.arange(40)[None, :].repeat(40, axis=0)
        east_half = col >= 20  # True == nodata in CUDEM (east), filled by land
        _write_synth_raster(
            cudem_path, bbox=_SMOKE_BBOX, nx=40, ny=40, fill=-8.0,
            nodata=-99999.0, nodata_mask=east_half,
        )

        cog_bytes, bathy_present, count, regional_count = _build_merged_topobathy(
            cudem_vsicurl_paths=[cudem_path],
            land_local_path=land_path,
            datum_offsets=[0.0],
            bbox=_SMOKE_BBOX,
            target_crs=TARGET_CRS,
        )
        assert bathy_present is True
        assert count == 1
        assert len(cog_bytes) > 0

        # Inspect the merged COG.
        out = os.path.join(tmpdir, "out.tif")
        with open(out, "wb") as fh:
            fh.write(cog_bytes)
        with rasterio.open(out) as ds:
            # Output contract.
            assert ds.count == 1, "must be single-band"
            assert str(ds.dtypes[0]) == "float32", "must be float32"
            assert ds.crs.to_epsg() == 32616, "must be EPSG:32616 (UTM 16N)"
            data = ds.read(1, masked=True)

        finite = data.compressed()
        assert finite.size > 0
        # Positive-up preserved: land plateau ~ +50, bathy ~ -8 (NO sign flip).
        assert finite.max() == pytest.approx(50.0, abs=1.5), (
            "land elevation must stay positive-up (~+50 m)"
        )
        assert finite.min() == pytest.approx(-8.0, abs=1.5), (
            "bathymetry must stay NEGATIVE (~-8 m); no sign flip"
        )
        # CUDEM-wins-on-coast: at least some bathy cells survived (the west half
        # where CUDEM had data beat the +50 land), AND land filled the east half.
        assert (finite < 0).any(), "CUDEM bathy must win where it has data (coast)"
        assert (finite > 40).any(), "3DEP land must fill where CUDEM is nodata"
    finally:
        import shutil

        shutil.rmtree(tmpdir, ignore_errors=True)


def test_merge_masks_unflagged_9999_sentinel_and_sets_nodata() -> None:
    """The 9999-nodata leak fix (fetch_topobathy half): a source raster carrying
    an UNFLAGGED 9999 fill (ds.nodata NOT declaring it) must NOT leak into the
    merged COG as a giant +9999 m wall, and the emitted COG MUST carry a real
    nodata flag (NaN) so downstream readers mask it.

    Regression for the live Mexico-Beach bug: off-coverage offshore fill leaked
    as +9999 onto the SFINCS mesh. The merge defensively masks |z| >= 9000.
    """
    tmpdir = tempfile.mkdtemp(prefix="trid3nt_topobathy_sentinel_")
    cudem_path = os.path.join(tmpdir, "cudem.tif")
    try:
        # CUDEM with a 9999 fill patch in the EAST half BUT ds.nodata left at a
        # different value (-99999) so .filled() does NOT catch the 9999 — the
        # exact unflagged-sentinel condition. West half is real bathy (-8 m).
        col = np.arange(40)[None, :].repeat(40, axis=0)
        east_half = col >= 20
        arr_fill = np.full((40, 40), -8.0, dtype="float32")
        arr_fill[east_half] = 9999.0  # unflagged sentinel (nodata is -99999)
        west, south, east, north = _SMOKE_BBOX
        res_x = (east - west) / 40
        res_y = (north - south) / 40
        with rasterio.open(
            cudem_path, "w", driver="GTiff", height=40, width=40, count=1,
            dtype="float32", crs="EPSG:4326",
            transform=from_origin(west, north, res_x, res_y),
            nodata=-99999.0,
        ) as dst:
            dst.write(arr_fill, 1)

        cog_bytes, bathy_present, count, regional_count = _build_merged_topobathy(
            cudem_vsicurl_paths=[cudem_path],
            land_local_path=None,
            datum_offsets=[0.0],
            bbox=_SMOKE_BBOX,
            target_crs=TARGET_CRS,
        )
        assert bathy_present is True and count == 1

        out = os.path.join(tmpdir, "out.tif")
        with open(out, "wb") as fh:
            fh.write(cog_bytes)
        with rasterio.open(out) as ds:
            # COG nodata flag MUST be set (NaN) so downstream masks the holes.
            assert ds.nodata is not None and np.isnan(ds.nodata), (
                "emitted COG must declare a nodata flag (NaN)"
            )
            data = ds.read(1, masked=True)
        finite = data.compressed()
        assert finite.size > 0
        # No +9999 wall survives anywhere in the merged surface.
        assert finite.max() < 9000.0, "unflagged 9999 sentinel leaked into COG"
        # The real west-half bathy band survives intact.
        assert finite.min() == pytest.approx(-8.0, abs=1.5)
    finally:
        import shutil

        shutil.rmtree(tmpdir, ignore_errors=True)


def test_merge_raises_empty_when_no_sources() -> None:
    with pytest.raises(TopobathyEmptyError):
        _build_merged_topobathy(
            cudem_vsicurl_paths=[],
            land_local_path=None,
            datum_offsets=[],
            bbox=_SMOKE_BBOX,
            target_crs=TARGET_CRS,
        )


def test_merge_land_only_is_land_only(tmp_path: Any) -> None:
    """With CUDEM absent, the merge is 3DEP-land-only (bathymetry_present False)."""
    land_path = str(tmp_path / "land.tif")
    _write_synth_raster(
        land_path, bbox=_SMOKE_BBOX, nx=30, ny=30, fill=12.0, nodata=-9999.0
    )
    cog_bytes, bathy_present, count, regional_count = _build_merged_topobathy(
        cudem_vsicurl_paths=[],
        land_local_path=land_path,
        datum_offsets=[],
        bbox=_SMOKE_BBOX,
        target_crs=TARGET_CRS,
    )
    assert bathy_present is False
    assert count == 0
    out = str(tmp_path / "out.tif")
    with open(out, "wb") as fh:
        fh.write(cog_bytes)
    with rasterio.open(out) as ds:
        assert ds.count == 1
        assert str(ds.dtypes[0]) == "float32"
        assert ds.crs.to_epsg() == 32616
        finite = ds.read(1, masked=True).compressed()
    # Land-only: all positive (no bathy).
    assert finite.size > 0
    assert (finite > 0).all()


def test_merge_etopo_global_fallback_supplies_bathy(tmp_path: Any) -> None:
    """CUDEM absent but the GLOBAL ETOPO fallback present: the merge reports
    bathymetry_present=True (real below-waterline bed) with cudem_tile_count=0,
    3DEP land (higher res) wins on land, ETOPO fills the offshore where 3DEP is
    nodata. This is the core of the Crescent City 0/930 fix."""
    etopo_path = str(tmp_path / "etopo.tif")
    land_path = str(tmp_path / "land.tif")
    # ETOPO base: WEST half is sea floor (-15 m, NEGATIVE/positive-up), EAST half
    # is coastal land (+30 m). It spans the whole AOI (global coverage).
    col = np.arange(40)[None, :].repeat(40, axis=0)
    west_half = col < 20
    arr = np.full((40, 40), 30.0, dtype="float32")
    arr[west_half] = -15.0
    west, south, east, north = _SMOKE_BBOX
    res_x = (east - west) / 40
    res_y = (north - south) / 40
    with rasterio.open(
        etopo_path, "w", driver="GTiff", height=40, width=40, count=1,
        dtype="float32", crs="EPSG:4326",
        transform=from_origin(west, north, res_x, res_y), nodata=-99999.0,
    ) as dst:
        dst.write(arr, 1)
    # 3DEP land: high +50 m plateau over the EAST (land) half only; WEST half
    # (offshore) is nodata so ETOPO bathy must fill it.
    land_arr = np.full((40, 40), 50.0, dtype="float32")
    land_arr[west_half] = -9999.0
    with rasterio.open(
        land_path, "w", driver="GTiff", height=40, width=40, count=1,
        dtype="float32", crs="EPSG:4326",
        transform=from_origin(west, north, res_x, res_y), nodata=-9999.0,
    ) as dst:
        dst.write(land_arr, 1)

    cog_bytes, bathy_present, count, regional_count = _build_merged_topobathy(
        cudem_vsicurl_paths=[],
        land_local_path=land_path,
        datum_offsets=[],
        bbox=_SMOKE_BBOX,
        target_crs=TARGET_CRS,
        etopo_paths=[etopo_path],
    )
    assert bathy_present is True, "ETOPO global fallback must count as bathymetry"
    assert count == 0, "cudem_tile_count is CUDEM-specific (0 on the ETOPO path)"

    out = str(tmp_path / "out.tif")
    with open(out, "wb") as fh:
        fh.write(cog_bytes)
    with rasterio.open(out) as ds:
        assert ds.count == 1 and str(ds.dtypes[0]) == "float32"
        assert ds.crs.to_epsg() == 32616
        finite = ds.read(1, masked=True).compressed()
    assert finite.size > 0
    # Real below-waterline bed from ETOPO (NEGATIVE, positive-up; no sign flip).
    assert (finite < 0).any(), "ETOPO must supply a real below-waterline bed"
    assert finite.min() == pytest.approx(-15.0, abs=2.0)
    # 3DEP land (higher res) wins on land.
    assert (finite > 40).any(), "3DEP land must win on land over coarse ETOPO"


def test_merge_mixed_crs_and_orientation_no_mergeerror() -> None:
    """REGRESSION (live Mexico-Beach crash): merging HETEROGENEOUS-CRS sources
    — a 3DEP land DEM in EPSG:5070 (Albers) + a CUDEM tile in EPSG:4269 (NAD83)
    — where one is "upside down" (POSITIVE pixel-height) must NOT raise
    ``rasterio.errors.MergeError``. The per-source warp normalises CRS AND
    orientation; CUDEM (listed last) still wins in the overlap; output is a
    valid EPSG:32616 single-band float32 raster.

    Before the fix, ``_merge_sources`` fed these straight into ``rio_merge``,
    which threw: ``Rasters with negative pixel height ("upside down" rasters)
    cannot be merged.``
    """
    import shutil

    from rasterio.errors import MergeError
    from rasterio.transform import Affine, from_origin
    from rasterio.warp import transform_bounds

    tmpdir = tempfile.mkdtemp(prefix="trid3nt_topobathy_mixedcrs_")
    land_path = os.path.join(tmpdir, "land_5070.tif")
    cudem_path = os.path.join(tmpdir, "cudem_4269.tif")
    try:
        # The two sources overlap over the smoke bbox but live in DIFFERENT CRS.
        west, south, east, north = _SMOKE_BBOX

        # --- 3DEP land in EPSG:5070 (Albers), NORTH-UP (negative pixel-height),
        #     a flat +50 m land plateau over the AOI footprint.
        l_w, l_s, l_e, l_n = transform_bounds(
            "EPSG:4326", "EPSG:5070", west, south, east, north, densify_pts=21
        )
        nx5070, ny5070 = 60, 60
        res_x = (l_e - l_w) / nx5070
        res_y = (l_n - l_s) / ny5070
        land_tx = from_origin(l_w, l_n, res_x, res_y)  # north-up
        land_arr = np.full((ny5070, nx5070), 50.0, dtype="float32")
        with rasterio.open(
            land_path, "w", driver="GTiff", dtype="float32", count=1,
            height=ny5070, width=nx5070, crs="EPSG:5070", transform=land_tx,
            nodata=-9999.0,
        ) as dst:
            dst.write(land_arr, 1)

        # --- CUDEM in EPSG:4269 (NAD83 geographic), written "UPSIDE DOWN":
        #     a POSITIVE pixel-height affine (origin at the SOUTH edge, rows go
        #     north). This is exactly the orientation that tripped rio_merge.
        #     WEST half is bathymetry (-8 m); EAST half is nodata (land fills).
        nx4269, ny4269 = 50, 50
        c_res_x = (east - west) / nx4269
        c_res_y = (north - south) / ny4269
        # Positive e (pixel height) => "upside down" relative to the usual
        # north-up convention; origin at the SW corner.
        cudem_tx = Affine(c_res_x, 0.0, west, 0.0, c_res_y, south)
        cudem_arr = np.full((ny4269, nx4269), -8.0, dtype="float32")
        col = np.arange(nx4269)[None, :].repeat(ny4269, axis=0)
        cudem_arr[col >= nx4269 // 2] = -99999.0  # east half nodata
        with rasterio.open(
            cudem_path, "w", driver="GTiff", dtype="float32", count=1,
            height=ny4269, width=nx4269, crs="EPSG:4269", transform=cudem_tx,
            nodata=-99999.0,
        ) as dst:
            dst.write(cudem_arr, 1)

        # Confirm the CUDEM tile really is "upside down" (positive pixel height),
        # i.e. the exact condition that made the old code raise MergeError.
        with rasterio.open(cudem_path) as ds:
            assert ds.transform.e > 0, "test fixture must be an upside-down raster"

        # Precedence: land (5070) FIRST, CUDEM (4269) LAST -> CUDEM wins coast.
        try:
            merged = _merge_sources(
                [land_path, cudem_path],
                target_crs="EPSG:32616",
                bbox=_SMOKE_BBOX,
            )
        except MergeError as exc:  # pragma: no cover — the bug we are fixing
            pytest.fail(f"_merge_sources raised the upside-down MergeError: {exc}")

        with rasterio.open(merged) as ds:
            assert ds.count == 1, "must be single-band"
            assert str(ds.dtypes[0]) == "float32", "must be float32"
            assert ds.crs.to_epsg() == 32616, "must reproject to EPSG:32616"
            data = ds.read(1, masked=True)
        os.unlink(merged)

        finite = data.compressed()
        assert finite.size > 0, "merge must produce valid cells"
        # CUDEM (last) wins where it has data: bathy ~ -8 survived the overlap.
        assert (finite < -1.0).any(), "CUDEM bathy (last source) must win the coast"
        # 3DEP land fills the east half where CUDEM is nodata.
        assert (finite > 40.0).any(), "3DEP land must fill where CUDEM is nodata"
        # No sentinel nodata leaked through (no -9999 / -99999).
        assert finite.min() > -1000.0, "no sentinel nodata may survive the merge"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# End-to-end fetch_topobathy via mocked fetch (no network, no GCS).
# ---------------------------------------------------------------------------


def _patch_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    *,
    cudem_tiles: list[str],
    land_path: str | None,
    datum_offsets: list[float] | None = None,
    etopo_tiles: list[str] | None = None,
) -> None:
    """Patch the network/GCS edges of fetch_topobathy so the orchestration runs
    against local synthetic rasters and never writes to a cache bucket."""
    # CUDEM tile selection -> provided local paths (already /vsicurl-free).
    monkeypatch.setattr(ftb, "_select_cudem_tiles", lambda *_a, **_k: cudem_tiles)
    # GLOBAL ETOPO fallback selection -> provided local paths (default NONE so a
    # no-CUDEM test degrades to land-only unless the test supplies ETOPO tiles).
    monkeypatch.setattr(
        ftb, "_select_etopo_tiles", lambda *_a, **_k: list(etopo_tiles or [])
    )
    # Datum gate -> return the supplied offsets (default 0.0 == NAVD88).
    offs = datum_offsets if datum_offsets is not None else [0.0] * len(cudem_tiles)
    seq = iter(offs)
    monkeypatch.setattr(ftb, "_assert_navd88", lambda *_a, **_k: next(seq, 0.0))
    # 3DEP land fetch -> the provided local synthetic file (or None).
    monkeypatch.setattr(
        ftb, "_fetch_3dep_land_to_file", lambda *_a, **_k: land_path
    )
    # The /vsicurl/ prefixing in the orchestrator turns local paths into
    # "/vsicurl/<localpath>" which rasterio can't open; patch the merge to
    # strip that prefix back to the local synthetic path.
    real_merge = ftb._build_merged_topobathy

    def _strip(paths):  # type: ignore[no-untyped-def]
        return [
            p[len("/vsicurl/"):] if p.startswith("/vsicurl/") else p
            for p in (paths or [])
        ]

    def _merge_local(cudem_vsicurl_paths, **kw):  # type: ignore[no-untyped-def]
        # The orchestrator /vsicurl-prefixes BOTH CUDEM and the ETOPO fallback
        # paths; strip both back to the local synthetic files for the merge.
        if "etopo_paths" in kw:
            kw["etopo_paths"] = _strip(kw["etopo_paths"])
        return real_merge(cudem_vsicurl_paths=_strip(cudem_vsicurl_paths), **kw)

    monkeypatch.setattr(ftb, "_build_merged_topobathy", _merge_local)
    # read_through -> write bytes to a temp file + return a local file:// URI so
    # no GCS/S3 is touched and the LayerURI assertion (uri not None) holds.
    from trid3nt_server.tools.cache import ReadThroughResult

    def _fake_read_through(metadata, params, ext, fetch_fn, **_kw):  # type: ignore[no-untyped-def]
        data = fetch_fn()
        with tempfile.NamedTemporaryFile(
            suffix=f".{ext}", delete=False, prefix="trid3nt_topobathy_cache_"
        ) as f:
            f.write(data)
            uri = f"gs://test-cache/cache/static-30d/topobathy/{os.path.basename(f.name)}"
        return ReadThroughResult(uri=uri, data=data, hit=False)

    monkeypatch.setattr(ftb, "read_through", _fake_read_through)


def test_fetch_topobathy_end_to_end_with_bathy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Full tool run with CUDEM present: returns a TopobathyResult LayerURI with
    bathymetry_present=True, continuous_dem style, meters, EPSG:32616 contract."""
    land_path = str(tmp_path / "land.tif")
    cudem_path = str(tmp_path / "cudem.tif")
    _write_synth_raster(
        land_path, bbox=_SMOKE_BBOX, nx=30, ny=30, fill=20.0, nodata=-9999.0
    )
    col = np.arange(30)[None, :].repeat(30, axis=0)
    _write_synth_raster(
        cudem_path, bbox=_SMOKE_BBOX, nx=30, ny=30, fill=-5.0,
        nodata=-99999.0, nodata_mask=(col >= 15),
    )
    _patch_pipeline(monkeypatch, cudem_tiles=[cudem_path], land_path=land_path)

    res = fetch_topobathy(bbox=_SMOKE_BBOX)
    assert isinstance(res, TopobathyResult)
    assert isinstance(res, ftb.LayerURI)  # byte-format compatible w/ fetch_dem
    assert res.layer_type == "raster"
    assert res.style_preset == "continuous_dem"
    assert res.units == "meters"
    assert res.role == "input"
    assert res.uri and res.uri.endswith(".tif")
    assert res.bathymetry_present is True
    assert res.fallback_warning is None
    assert res.cudem_tile_count == 1


def test_fetch_topobathy_fallback_to_land_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """CUDEM missing -> DEGRADE to 3DEP-land-only with an honest warning.
    Never a silent dead-end or fabricated bathymetry (data-source norm)."""
    land_path = str(tmp_path / "land.tif")
    _write_synth_raster(
        land_path, bbox=_SMOKE_BBOX, nx=30, ny=30, fill=15.0, nodata=-9999.0
    )
    # No CUDEM tiles intersect.
    _patch_pipeline(monkeypatch, cudem_tiles=[], land_path=land_path)

    res = fetch_topobathy(bbox=_SMOKE_BBOX)
    assert res.bathymetry_present is False
    assert res.cudem_tile_count == 0
    assert res.fallback_warning is not None
    assert "BATHYMETRY ABSENT" in res.fallback_warning
    # Still a valid layer (land-only DEM) — not a dead-end.
    assert res.uri and res.style_preset == "continuous_dem"


def test_fetch_topobathy_etopo_global_fallback_when_no_cudem(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """CUDEM absent but the GLOBAL ETOPO fallback present (the Crescent City
    case): the tool returns bathymetry_present=True with a REAL below-waterline
    bed and an honest GLOBAL-FALLBACK warning naming the ETOPO source + datum —
    NOT the land-only BATHYMETRY-ABSENT dead-end that produced zero inundation."""
    etopo_path = str(tmp_path / "etopo.tif")
    land_path = str(tmp_path / "land.tif")
    # ETOPO base spanning the AOI: west half sea floor (-12 m), east half land.
    col = np.arange(30)[None, :].repeat(30, axis=0)
    arr = np.full((30, 30), 25.0, dtype="float32")
    arr[col < 15] = -12.0
    west, south, east, north = _SMOKE_BBOX
    with rasterio.open(
        etopo_path, "w", driver="GTiff", height=30, width=30, count=1,
        dtype="float32", crs="EPSG:4326",
        transform=from_origin(west, north, (east - west) / 30, (north - south) / 30),
        nodata=-99999.0,
    ) as dst:
        dst.write(arr, 1)
    _write_synth_raster(
        land_path, bbox=_SMOKE_BBOX, nx=30, ny=30, fill=20.0, nodata=-9999.0
    )
    # No CUDEM; ETOPO fallback supplies the synthetic global tile.
    _patch_pipeline(
        monkeypatch, cudem_tiles=[], land_path=land_path, etopo_tiles=[etopo_path]
    )

    res = fetch_topobathy(bbox=_SMOKE_BBOX)
    assert res.bathymetry_present is True, "ETOPO fallback must yield bathymetry"
    assert res.cudem_tile_count == 0
    assert res.fallback_warning is not None
    assert "GLOBAL-FALLBACK BATHYMETRY" in res.fallback_warning
    assert "ETOPO 2022" in res.fallback_warning
    assert "BATHYMETRY ABSENT" not in res.fallback_warning
    assert res.uri and res.style_preset == "continuous_dem"


def test_force_bathy_base_includes_etopo_even_when_cudem_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """P0.1: force_bathy_base lays ETOPO down as the ALWAYS-ON base even when CUDEM
    has coverage, so the open-ocean portion of the (offshore-extended) domain is
    genuinely-negative bathymetry -- CUDEM/3DEP paint on top. Without the flag
    ETOPO is selected ONLY when CUDEM is absent (the SFINCS default)."""
    land_path = str(tmp_path / "land.tif")
    cudem_path = str(tmp_path / "cudem.tif")
    etopo_path = str(tmp_path / "etopo.tif")
    # CUDEM covers ONLY the east coast columns (>=20); the offshore west is nodata.
    _write_synth_raster(
        land_path, bbox=_SMOKE_BBOX, nx=30, ny=30, fill=20.0, nodata=-9999.0
    )
    col = np.arange(30)[None, :].repeat(30, axis=0)
    _write_synth_raster(
        cudem_path, bbox=_SMOKE_BBOX, nx=30, ny=30, fill=-3.0,
        nodata=-99999.0, nodata_mask=(col < 20),  # nodata over the open ocean
    )
    # ETOPO base: a genuinely-DEEP offshore bed (-500 m) over the whole AOI.
    _write_synth_raster(
        etopo_path, bbox=_SMOKE_BBOX, nx=30, ny=30, fill=-500.0, nodata=-99999.0
    )

    calls: dict[str, int] = {"etopo": 0}
    real_select_etopo = ftb._select_etopo_tiles

    def _spy_etopo(bbox):  # type: ignore[no-untyped-def]
        calls["etopo"] += 1
        return [etopo_path]

    monkeypatch.setattr(ftb, "_select_etopo_tiles", _spy_etopo)
    _patch_pipeline(monkeypatch, cudem_tiles=[cudem_path], land_path=land_path)
    # _patch_pipeline overrode _select_etopo_tiles; re-apply the spy AFTER it.
    monkeypatch.setattr(ftb, "_select_etopo_tiles", _spy_etopo)

    # (a) WITHOUT the flag: CUDEM present -> ETOPO NOT selected (legacy behaviour).
    calls["etopo"] = 0
    res_default = fetch_topobathy(bbox=_SMOKE_BBOX)
    assert calls["etopo"] == 0, "ETOPO must NOT be pulled when CUDEM covers + no flag"
    assert res_default.cudem_tile_count == 1

    # (b) WITH the flag: ETOPO IS selected as the base even though CUDEM is present.
    calls["etopo"] = 0
    res_forced = fetch_topobathy(bbox=_SMOKE_BBOX, force_bathy_base=True)
    assert calls["etopo"] == 1, "force_bathy_base must pull ETOPO as the base"
    assert res_forced.bathymetry_present is True
    assert res_forced.cudem_tile_count == 1
    # The merged COG offshore (west, where CUDEM is nodata) must carry the REAL
    # ETOPO deep bed (~ -500 m), not a flat land/zero fill.
    import rasterio

    with rasterio.open(_local_from_uri(res_forced.uri)) as ds:
        z = ds.read(1)
    assert np.nanmin(z) < -100.0, f"offshore must be genuinely deep, got min={np.nanmin(z)}"


def _local_from_uri(uri: str) -> str:
    """Map the test's gs://test-cache/.../<basename>.tif fake URI back to the temp
    file the fake read_through wrote (same basename in the system temp dir)."""
    base = os.path.basename(uri)
    return os.path.join(tempfile.gettempdir(), base)


def test_fetch_topobathy_manifest_unreachable_degrades(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A CUDEM manifest network failure degrades to land-only (does NOT abort the
    coastal run) — the manifest-unreachable branch of the fallback norm."""
    land_path = str(tmp_path / "land.tif")
    _write_synth_raster(
        land_path, bbox=_SMOKE_BBOX, nx=20, ny=20, fill=8.0, nodata=-9999.0
    )

    def _boom(*_a, **_k):  # type: ignore[no-untyped-def]
        raise TopobathyUpstreamError("manifest 503")

    # Patch _select_cudem_tiles to raise upstream (manifest unreachable).
    monkeypatch.setattr(ftb, "_select_cudem_tiles", _boom)
    # Also force the GLOBAL ETOPO fallback unavailable so this exercises the
    # BOTH-bathy-sources-down -> land-only honest degrade (BATHYMETRY ABSENT).
    monkeypatch.setattr(ftb, "_select_etopo_tiles", lambda *_a, **_k: [])
    monkeypatch.setattr(ftb, "_assert_navd88", lambda *_a, **_k: 0.0)
    monkeypatch.setattr(ftb, "_fetch_3dep_land_to_file", lambda *_a, **_k: land_path)
    from trid3nt_server.tools.cache import ReadThroughResult

    def _fake_rt(metadata, params, ext, fetch_fn, **_kw):  # type: ignore[no-untyped-def]
        data = fetch_fn()
        return ReadThroughResult(uri="gs://t/c.tif", data=data, hit=False)

    monkeypatch.setattr(ftb, "read_through", _fake_rt)

    res = fetch_topobathy(bbox=_SMOKE_BBOX)
    assert res.bathymetry_present is False
    assert res.fallback_warning is not None and "BATHYMETRY ABSENT" in res.fallback_warning


def test_fetch_topobathy_datum_mismatch_propagates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A non-NAVD88 CUDEM tile with no offset raises TopobathyDatumError through
    the full tool (Invariant 7 — never a silent cross-datum merge)."""
    land_path = str(tmp_path / "land.tif")
    cudem_path = str(tmp_path / "cudem.tif")
    _write_synth_raster(
        land_path, bbox=_SMOKE_BBOX, nx=20, ny=20, fill=5.0, nodata=-9999.0
    )
    _write_synth_raster(
        cudem_path, bbox=_SMOKE_BBOX, nx=20, ny=20, fill=-3.0, nodata=-99999.0
    )
    monkeypatch.setattr(ftb, "_select_cudem_tiles", lambda *_a, **_k: [cudem_path])
    monkeypatch.setattr(ftb, "_fetch_3dep_land_to_file", lambda *_a, **_k: land_path)

    def _datum_raise(*_a, **_k):  # type: ignore[no-untyped-def]
        raise TopobathyDatumError("tile is MHW, no offset")

    monkeypatch.setattr(ftb, "_assert_navd88", _datum_raise)
    from trid3nt_server.tools.cache import ReadThroughResult

    monkeypatch.setattr(
        ftb, "read_through",
        lambda metadata, params, ext, fetch_fn, **_kw: ReadThroughResult(
            uri="gs://t/c.tif", data=fetch_fn(), hit=False
        ),
    )
    with pytest.raises(TopobathyDatumError):
        fetch_topobathy(bbox=_SMOKE_BBOX)


# ---------------------------------------------------------------------------
# Live smoke (gated; the build does NOT block on it).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _LIVE, reason="set TRID3NT_TEST_LIVE_TOPOBATHY=1 to run")
def test_live_etopo_fallback_crescent_city_has_real_bathy() -> None:
    """LIVE: the Crescent City tsunami AOI finds 0 CUDEM tiles (Pacific coast)
    but the GLOBAL ETOPO 2022 fallback returns a tile with REAL below-waterline
    depths over the exact bbox — proving the fix turns a zero-inundation
    land-only run into one with a real submarine bed."""
    import numpy as _np
    import rasterio as _rio
    from rasterio.windows import from_bounds as _from_bounds

    # CUDEM has no coverage here.
    assert _select_cudem_tiles(_CRESCENT_CITY_BBOX, timeout_s=60.0) == []
    # ETOPO fallback selects exactly the covering tile.
    etopo = _select_etopo_tiles(_CRESCENT_CITY_BBOX)
    assert len(etopo) == 1 and etopo[0].endswith("N45W135_surface.tif")

    with _rio.Env(**ftb._VSICURL_ENV_KW):
        with _rio.open("/vsicurl/" + etopo[0]) as ds:
            win = _from_bounds(*_CRESCENT_CITY_BBOX, ds.transform)
            arr = ds.read(1, window=win, masked=True).filled(_np.nan)
    finite = arr[~_np.isnan(arr)]
    assert finite.size > 0, "ETOPO window over Crescent City is empty"
    assert float(_np.nanmin(finite)) < 0.0, (
        "expected real nearshore bathymetry (negative depths) over Crescent City"
    )


@pytest.mark.skipif(not _LIVE, reason="set TRID3NT_TEST_LIVE_TOPOBATHY=1 to run")
def test_live_cudem_manifest_resolves() -> None:
    """The real CUDEM tile-index resolves and at least one tile intersects the
    CI smoke bbox — proves the endpoint is live without a full multi-GB merge."""
    tiles = _select_cudem_tiles(_SMOKE_BBOX, timeout_s=60.0)
    assert len(tiles) >= 1, "expected at least one CUDEM tile over the smoke bbox"
    assert all(t.startswith("https://") and t.endswith(".tif") for t in tiles)


# Mexico Beach demo AOI — the EXACT bbox from the live-prod crash.
_MEXICO_BEACH_BBOX = (-85.47, 29.89, -85.36, 29.98)


@pytest.mark.skipif(not _LIVE, reason="set TRID3NT_TEST_LIVE_TOPOBATHY=1 to run")
def test_live_mexico_beach_merge_no_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """LIVE end-to-end: pull the REAL NOAA NCEI CUDEM tiles + the REAL 3DEP land
    DEM for the Mexico-Beach demo AOI and run the FULL merge -> COG with the
    GDAL CLI forced absent (``_gdal_bin`` -> None), exercising EXACTLY the prod
    (no-CLI) path that crashed with the upside-down ``MergeError``.

    Asserts a valid single-band float32 EPSG:32616 COG with bathymetry present.
    Takes a minute or two (real multi-source downloads).
    """
    # Force the prod path: NO GDAL CLI available anywhere.
    monkeypatch.setattr(ftb, "_gdal_bin", lambda *_a, **_k: None)

    cog_bytes, bathy_present, fallback_warning, cudem_count = (
        _fetch_topobathy_bytes_and_flags(
            bbox=_MEXICO_BEACH_BBOX,
            resolution_m=10,
            target_crs=TARGET_CRS,
            navd88_offset_m=None,
            timeout_s=180.0,
        )
    )

    assert cog_bytes, "no COG bytes produced"
    assert cudem_count >= 1, "expected real CUDEM tiles over Mexico Beach"
    assert bathy_present is True, "Mexico Beach must have CUDEM bathymetry"
    assert fallback_warning is None, "should not fall back to land-only"

    # Validate the COG contract + that it really spans the shoreline.
    with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as f:
        out_path = f.name
    try:
        with open(out_path, "wb") as fh:
            fh.write(cog_bytes)
        with rasterio.open(out_path) as ds:
            assert ds.count == 1, "must be single-band"
            assert str(ds.dtypes[0]) == "float32", "must be float32"
            assert ds.crs.to_epsg() == 32616, "must be EPSG:32616 (UTM 16N)"
            data = ds.read(1, masked=True)
        finite = data.compressed()
        assert finite.size > 0, "merged COG has no valid cells"
        # A real coastal AOI spans the shoreline: both land (positive-up) and
        # nearshore bathymetry (negative) must be present, no sign flip.
        assert (finite > 0).any(), "expected land (positive-up) cells"
        assert (finite < 0).any(), "expected bathymetry (negative) cells"
        print(
            f"\nLIVE Mexico-Beach merge OK: {len(cog_bytes):,} byte COG, "
            f"{cudem_count} CUDEM tile(s), bathymetry_present={bathy_present}, "
            f"elev range [{finite.min():.2f}, {finite.max():.2f}] m NAVD88"
        )
    finally:
        os.unlink(out_path)
