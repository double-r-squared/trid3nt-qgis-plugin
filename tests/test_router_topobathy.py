"""Router-fold + provenance-channel tests for ``fetch_topobathy`` (ADR 0110).

Migrated from ``test_fetch_topobathy.py`` when the coded twin was folded onto the
router (a ``library_delegate`` raster spec + the ``topobathy.*`` hooks + the
fetch-time provenance channel). Proves, all offline against SYNTHETIC rasters:

- registry shape + typed-error envelope + payload estimator (indistinguishability);
- input validation stamps TOPOBATHY_INPUT_INVALID (router + delegate_validate);
- merge precedence + datum gate + the ETOPO / land-only degrades (the merge helpers);
- END-TO-END via the promoted router closure: TopobathyResult fields populated;
- the CHANNEL: a cache-hit REPLAYS the four provenance fields identically (no
  re-fetch), and the LABELED ``land_absent`` loud-degrade names the failed land leg.
"""

from __future__ import annotations

import os
import tempfile
from typing import Any

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from trid3nt_contracts.execution import TopobathyResult
from trid3nt_server.data import TOOL_REGISTRY
from trid3nt_server.data.fetchers._fetch_common import FetchError
from trid3nt_server.data.fetchers._router.hooks import topobathy as tb
from trid3nt_server.data.fetchers._router.hooks.topobathy import (
    ETOPO_GLOBAL_ROOT,
    TARGET_CRS,
    TopobathyDatumError,
    TopobathyEmptyError,
    TopobathyInputError,
    TopobathyUpstreamError,
    _build_merged_topobathy,
    _classify_vertical_datum,
    _etopo_url_for_corner,
    _parse_tile_nw_corner,
    _select_etopo_tiles,
    _tile_intersects_bbox,
    estimate_payload_mb,
)

_DEMO_BBOX = (-85.75, 29.55, -85.25, 30.20)
_SMOKE_BBOX = (-85.45, 29.92, -85.38, 29.98)
_CRESCENT_CITY_BBOX = (-124.22, 41.73, -124.14, 41.86)


def _fetch_topobathy(**kw: Any):
    return TOOL_REGISTRY["fetch_topobathy"].fn(**kw)


def _write_synth_raster(
    path: str, *, bbox, nx, ny, fill, nodata, nodata_mask=None, crs="EPSG:4326"
) -> None:
    west, south, east, north = bbox
    transform = from_origin(west, north, (east - west) / nx, (north - south) / ny)
    arr = np.full((ny, nx), fill, dtype="float32")
    if nodata_mask is not None:
        arr[nodata_mask] = nodata
    with rasterio.open(
        path, "w", driver="GTiff", dtype="float32", count=1, height=ny, width=nx,
        crs=crs, transform=transform, nodata=nodata,
    ) as dst:
        dst.write(arr, 1)


# --------------------------------------------------------------------------- #
# Registry shape + category.
# --------------------------------------------------------------------------- #


def test_topobathy_registered_with_expected_metadata() -> None:
    assert "fetch_topobathy" in TOOL_REGISTRY
    md = TOOL_REGISTRY["fetch_topobathy"].metadata
    assert md.ttl_class == "static-30d"
    assert md.source_class == "topobathy"
    assert md.cacheable is True
    assert getattr(md, "supports_global_query", None) is False
    assert getattr(md, "payload_mb_estimator_name", None) == "estimate_payload_mb"
    assert getattr(md, "auto_publish", None) is False




# --------------------------------------------------------------------------- #
# Typed-error envelope.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "cls, code, retryable",
    [
        (tb.TopobathyError, "TOPOBATHY_ERROR", True),
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
    assert isinstance(err, FetchError)  # so library_delegate.invoke passes the code through


def test_estimate_payload_mb_scales_with_bbox() -> None:
    small = estimate_payload_mb(bbox=_SMOKE_BBOX)
    big = estimate_payload_mb(bbox=_DEMO_BBOX)
    assert 0.0 < small < big
    assert estimate_payload_mb(bbox=None) > 0.0


# --------------------------------------------------------------------------- #
# Input validation -> TOPOBATHY_INPUT_INVALID (router bbox gate OR delegate_validate).
# --------------------------------------------------------------------------- #


def _assert_input_invalid(**kw: Any) -> None:
    with pytest.raises(FetchError) as ei:
        _fetch_topobathy(**kw)
    assert ei.value.error_code == "TOPOBATHY_INPUT_INVALID"


def test_rejects_bad_bbox_shape() -> None:
    _assert_input_invalid(bbox=(1.0, 2.0))


def test_rejects_non_finite_bbox() -> None:
    _assert_input_invalid(bbox=(float("nan"), 29.55, -85.25, 30.20))


def test_rejects_degenerate_bbox() -> None:
    _assert_input_invalid(bbox=(-85.5, 29.9, -85.5, 29.9))


def test_rejects_inland_foreign_bbox() -> None:
    """Europe -- misses the US coastal envelope (delegate_validate)."""
    _assert_input_invalid(bbox=(10.0, 45.0, 11.0, 46.0))


def test_rejects_bad_resolution() -> None:
    _assert_input_invalid(bbox=_SMOKE_BBOX, resolution_m=0)
    _assert_input_invalid(bbox=_SMOKE_BBOX, resolution_m=99999)


def test_rejects_non_finite_offset() -> None:
    _assert_input_invalid(bbox=_SMOKE_BBOX, navd88_offset_m=float("inf"))


# --------------------------------------------------------------------------- #
# Tile-index intersect math + ETOPO fallback selection + datum gate.
# --------------------------------------------------------------------------- #


def test_parse_tile_nw_corner() -> None:
    assert _parse_tile_nw_corner("ncei19_n30X00_w085X25_2019v1.tif") == (30.0, -85.25)
    assert _parse_tile_nw_corner("https://x/AL_nwFL/ncei19_n29X75_w085X50_2019v1.tif") == (29.75, -85.5)
    assert _parse_tile_nw_corner("not_a_cudem_tile.tif") is None


def test_tile_intersects_smoke_bbox() -> None:
    assert _tile_intersects_bbox(30.0, -85.50, _SMOKE_BBOX) is True
    assert _tile_intersects_bbox(30.0, -85.25, _SMOKE_BBOX) is False
    assert _tile_intersects_bbox(27.25, -82.75, _SMOKE_BBOX) is False


def test_etopo_url_for_corner_naming() -> None:
    url = _etopo_url_for_corner(45.0, -135.0)
    assert url.startswith(ETOPO_GLOBAL_ROOT)
    assert url.endswith("ETOPO_2022_v1_15s_N45W135_surface.tif")
    assert _etopo_url_for_corner(30.0, -90.0).endswith("N30W090_surface.tif")
    assert _etopo_url_for_corner(-15.0, 15.0).endswith("S15E015_surface.tif")


def test_select_etopo_tiles_crescent_city() -> None:
    sel = _select_etopo_tiles(_CRESCENT_CITY_BBOX)
    assert len(sel) == 1
    assert sel[0].endswith("ETOPO_2022_v1_15s_N45W135_surface.tif")


def test_datum_gate_accepts_navd88() -> None:
    assert _classify_vertical_datum("GEOGCRS ... NAVD88 height", None, "t") == 0.0


def test_datum_gate_rejects_tidal_without_offset() -> None:
    for marker in ("MHW", "vertical datum MSL", "LMSL height", "mean low water"):
        with pytest.raises(TopobathyDatumError):
            _classify_vertical_datum(marker, None, "tile")


def test_datum_gate_applies_documented_offset() -> None:
    assert _classify_vertical_datum("MHW", 0.23, "tile") == pytest.approx(0.23)


def test_datum_gate_absent_signal_defaults_to_navd88() -> None:
    assert _classify_vertical_datum("", None, "tile") == 0.0


# --------------------------------------------------------------------------- #
# Merge precedence + output contract (the merge helpers, unchanged).
# --------------------------------------------------------------------------- #


def test_merge_cudem_wins_on_coast_and_output_contract(tmp_path: Any) -> None:
    land_path = str(tmp_path / "land.tif")
    cudem_path = str(tmp_path / "cudem.tif")
    _write_synth_raster(land_path, bbox=_SMOKE_BBOX, nx=40, ny=40, fill=50.0, nodata=-9999.0)
    col = np.arange(40)[None, :].repeat(40, axis=0)
    _write_synth_raster(cudem_path, bbox=_SMOKE_BBOX, nx=40, ny=40, fill=-8.0,
                        nodata=-99999.0, nodata_mask=(col >= 20))
    cog_bytes, bathy, count, regional = _build_merged_topobathy(
        cudem_vsicurl_paths=[cudem_path], land_local_path=land_path,
        datum_offsets=[0.0], bbox=_SMOKE_BBOX, target_crs=TARGET_CRS,
    )
    assert bathy is True and count == 1 and len(cog_bytes) > 0
    out = str(tmp_path / "out.tif")
    with open(out, "wb") as fh:
        fh.write(cog_bytes)
    with rasterio.open(out) as ds:
        assert ds.count == 1 and str(ds.dtypes[0]) == "float32"
        assert ds.crs.to_epsg() == 32616
        finite = ds.read(1, masked=True).compressed()
    assert finite.max() == pytest.approx(50.0, abs=1.5)
    assert finite.min() == pytest.approx(-8.0, abs=1.5)
    assert (finite < 0).any() and (finite > 40).any()


def test_merge_masks_unflagged_9999_sentinel_and_sets_nodata(tmp_path: Any) -> None:
    cudem_path = str(tmp_path / "cudem.tif")
    col = np.arange(40)[None, :].repeat(40, axis=0)
    arr_fill = np.full((40, 40), -8.0, dtype="float32")
    arr_fill[col >= 20] = 9999.0
    west, south, east, north = _SMOKE_BBOX
    with rasterio.open(
        cudem_path, "w", driver="GTiff", height=40, width=40, count=1, dtype="float32",
        crs="EPSG:4326", transform=from_origin(west, north, (east - west) / 40, (north - south) / 40),
        nodata=-99999.0,
    ) as dst:
        dst.write(arr_fill, 1)
    cog_bytes, bathy, count, regional = _build_merged_topobathy(
        cudem_vsicurl_paths=[cudem_path], land_local_path=None,
        datum_offsets=[0.0], bbox=_SMOKE_BBOX, target_crs=TARGET_CRS,
    )
    out = str(tmp_path / "out.tif")
    with open(out, "wb") as fh:
        fh.write(cog_bytes)
    with rasterio.open(out) as ds:
        assert ds.nodata is not None and np.isnan(ds.nodata)
        finite = ds.read(1, masked=True).compressed()
    assert finite.max() < 9000.0
    assert finite.min() == pytest.approx(-8.0, abs=1.5)


def test_merge_raises_empty_when_no_sources() -> None:
    with pytest.raises(TopobathyEmptyError):
        _build_merged_topobathy(cudem_vsicurl_paths=[], land_local_path=None,
                                datum_offsets=[], bbox=_SMOKE_BBOX, target_crs=TARGET_CRS)


def test_merge_etopo_global_fallback_supplies_bathy(tmp_path: Any) -> None:
    etopo_path = str(tmp_path / "etopo.tif")
    land_path = str(tmp_path / "land.tif")
    col = np.arange(40)[None, :].repeat(40, axis=0)
    arr = np.full((40, 40), 30.0, dtype="float32")
    arr[col < 20] = -15.0
    west, south, east, north = _SMOKE_BBOX
    with rasterio.open(
        etopo_path, "w", driver="GTiff", height=40, width=40, count=1, dtype="float32",
        crs="EPSG:4326", transform=from_origin(west, north, (east - west) / 40, (north - south) / 40),
        nodata=-99999.0,
    ) as dst:
        dst.write(arr, 1)
    land_arr = np.full((40, 40), 50.0, dtype="float32")
    land_arr[col < 20] = -9999.0
    with rasterio.open(
        land_path, "w", driver="GTiff", height=40, width=40, count=1, dtype="float32",
        crs="EPSG:4326", transform=from_origin(west, north, (east - west) / 40, (north - south) / 40),
        nodata=-9999.0,
    ) as dst:
        dst.write(land_arr, 1)
    cog_bytes, bathy, count, regional = _build_merged_topobathy(
        cudem_vsicurl_paths=[], land_local_path=land_path, datum_offsets=[],
        bbox=_SMOKE_BBOX, target_crs=TARGET_CRS, etopo_paths=[etopo_path],
    )
    assert bathy is True and count == 0


# --------------------------------------------------------------------------- #
# END-TO-END via the promoted router closure (synthetic rasters + fake_s3).
# --------------------------------------------------------------------------- #


def _patch_delegate_sources(monkeypatch, *, cudem_tiles, land_path, etopo_tiles=None):
    """Patch the topobathy delegate's source-discovery edges to local synthetic
    rasters; strip the delegate's /vsicurl/ prefix so rasterio can open them."""
    monkeypatch.setattr(tb, "_select_cudem_tiles", lambda *_a, **_k: list(cudem_tiles))
    monkeypatch.setattr(tb, "_select_etopo_tiles", lambda *_a, **_k: list(etopo_tiles or []))
    monkeypatch.setattr(tb, "_assert_navd88", lambda *_a, **_k: 0.0)
    monkeypatch.setattr(tb, "_fetch_3dep_land_to_file", lambda *_a, **_k: land_path)
    real = tb._composite_sources_to_array

    def _strip_and_merge(sources, target_crs, bbox, **kw):
        stripped = [s[len("/vsicurl/"):] if s.startswith("/vsicurl/") else s for s in sources]
        return real(stripped, target_crs, bbox, **kw)

    monkeypatch.setattr(tb, "_composite_sources_to_array", _strip_and_merge)


def test_end_to_end_with_bathy(monkeypatch, tmp_path, fake_s3) -> None:
    land_path = str(tmp_path / "land.tif")
    cudem_path = str(tmp_path / "cudem.tif")
    _write_synth_raster(land_path, bbox=_SMOKE_BBOX, nx=30, ny=30, fill=20.0, nodata=-9999.0)
    col = np.arange(30)[None, :].repeat(30, axis=0)
    _write_synth_raster(cudem_path, bbox=_SMOKE_BBOX, nx=30, ny=30, fill=-5.0,
                        nodata=-99999.0, nodata_mask=(col >= 15))
    _patch_delegate_sources(monkeypatch, cudem_tiles=[cudem_path], land_path=land_path)

    res = _fetch_topobathy(bbox=_SMOKE_BBOX)
    assert isinstance(res, TopobathyResult)
    assert res.layer_type == "raster"
    assert res.style_preset == "continuous_dem"
    assert res.units == "meters"
    assert res.role == "input"
    assert res.uri and res.uri.endswith(".tif")
    assert res.layer_id.startswith("topobathy-")
    assert res.bathymetry_present is True
    assert res.fallback_warning is None
    assert res.cudem_tile_count == 1


def test_end_to_end_fallback_to_land_only(monkeypatch, tmp_path, fake_s3) -> None:
    land_path = str(tmp_path / "land.tif")
    _write_synth_raster(land_path, bbox=_SMOKE_BBOX, nx=30, ny=30, fill=15.0, nodata=-9999.0)
    _patch_delegate_sources(monkeypatch, cudem_tiles=[], land_path=land_path)

    res = _fetch_topobathy(bbox=_SMOKE_BBOX)
    assert res.bathymetry_present is False
    assert res.cudem_tile_count == 0
    assert res.fallback_warning is not None and "BATHYMETRY ABSENT" in res.fallback_warning


def test_end_to_end_etopo_global_fallback(monkeypatch, tmp_path, fake_s3) -> None:
    etopo_path = str(tmp_path / "etopo.tif")
    land_path = str(tmp_path / "land.tif")
    col = np.arange(30)[None, :].repeat(30, axis=0)
    arr = np.full((30, 30), 25.0, dtype="float32")
    arr[col < 15] = -12.0
    west, south, east, north = _SMOKE_BBOX
    with rasterio.open(
        etopo_path, "w", driver="GTiff", height=30, width=30, count=1, dtype="float32",
        crs="EPSG:4326", transform=from_origin(west, north, (east - west) / 30, (north - south) / 30),
        nodata=-99999.0,
    ) as dst:
        dst.write(arr, 1)
    _write_synth_raster(land_path, bbox=_SMOKE_BBOX, nx=30, ny=30, fill=20.0, nodata=-9999.0)
    _patch_delegate_sources(monkeypatch, cudem_tiles=[], land_path=land_path, etopo_tiles=[etopo_path])

    res = _fetch_topobathy(bbox=_SMOKE_BBOX)
    assert res.bathymetry_present is True
    assert res.cudem_tile_count == 0
    assert "GLOBAL-FALLBACK BATHYMETRY" in res.fallback_warning
    assert "ETOPO 2022" in res.fallback_warning
    assert "BATHYMETRY ABSENT" not in res.fallback_warning


def test_end_to_end_datum_mismatch_propagates(monkeypatch, tmp_path, fake_s3) -> None:
    land_path = str(tmp_path / "land.tif")
    cudem_path = str(tmp_path / "cudem.tif")
    _write_synth_raster(land_path, bbox=_SMOKE_BBOX, nx=20, ny=20, fill=5.0, nodata=-9999.0)
    _write_synth_raster(cudem_path, bbox=_SMOKE_BBOX, nx=20, ny=20, fill=-3.0, nodata=-99999.0)
    monkeypatch.setattr(tb, "_select_cudem_tiles", lambda *_a, **_k: [cudem_path])
    monkeypatch.setattr(tb, "_select_etopo_tiles", lambda *_a, **_k: [])
    monkeypatch.setattr(tb, "_fetch_3dep_land_to_file", lambda *_a, **_k: land_path)

    def _raise(*_a, **_k):
        raise TopobathyDatumError("tile is MHW, no offset")

    monkeypatch.setattr(tb, "_assert_navd88", _raise)
    with pytest.raises(TopobathyDatumError) as ei:
        _fetch_topobathy(bbox=_SMOKE_BBOX)
    assert ei.value.error_code == "TOPOBATHY_DATUM_MISMATCH"


# --------------------------------------------------------------------------- #
# THE CHANNEL: cache-hit replay + the labeled land_absent loud-degrade.
# --------------------------------------------------------------------------- #


def test_cache_hit_replays_provenance_identically(monkeypatch, tmp_path, fake_s3) -> None:
    """The provenance channel's proof (live-proof #2 offline analog): a second call
    over the same AOI is a CACHE HIT that never re-runs the merge, yet the four
    provenance fields are REPLAYED IDENTICAL from the ``<key>.provenance.json``
    sidecar -- the fact the twin lost (it reverted to defaults on a cache hit)."""
    land_path = str(tmp_path / "land.tif")
    _write_synth_raster(land_path, bbox=_SMOKE_BBOX, nx=25, ny=25, fill=12.0, nodata=-9999.0)
    # CUDEM absent -> land-only degrade (bathymetry_present=False, a warning).
    _patch_delegate_sources(monkeypatch, cudem_tiles=[], land_path=land_path)

    merges = {"n": 0}
    real = tb._composite_sources_to_array

    def _count(sources, *a, **k):
        merges["n"] += 1
        stripped = [s[len("/vsicurl/"):] if s.startswith("/vsicurl/") else s for s in sources]
        return real(stripped, *a, **k)

    monkeypatch.setattr(tb, "_composite_sources_to_array", _count)

    r1 = _fetch_topobathy(bbox=_SMOKE_BBOX)
    assert merges["n"] == 1  # fresh fetch ran the merge once
    fields = (r1.bathymetry_present, r1.fallback_warning, r1.cudem_tile_count, r1.regional_tile_count)
    assert fields[0] is False and "BATHYMETRY ABSENT" in fields[1]

    r2 = _fetch_topobathy(bbox=_SMOKE_BBOX)
    assert merges["n"] == 1, "cache hit must NOT re-run the merge"
    assert (r2.bathymetry_present, r2.fallback_warning, r2.cudem_tile_count, r2.regional_tile_count) == fields


def test_land_absent_labeled_degrade(monkeypatch, tmp_path, fake_s3) -> None:
    """The loud-fallback norm (the 0091 follow-up): when the 3DEP land leg FAILS but
    a bathy source is present, the surface proceeds bathy-only with a LABELED
    ``land_absent`` warning -- never the twin's SILENT land drop."""
    cudem_path = str(tmp_path / "cudem.tif")
    _write_synth_raster(cudem_path, bbox=_SMOKE_BBOX, nx=25, ny=25, fill=-6.0, nodata=-99999.0)
    # land leg fails -> None; CUDEM present.
    _patch_delegate_sources(monkeypatch, cudem_tiles=[cudem_path], land_path=None)

    res = _fetch_topobathy(bbox=_SMOKE_BBOX)
    assert res.bathymetry_present is True
    assert res.cudem_tile_count == 1
    assert res.fallback_warning is not None
    assert "land_absent" in res.fallback_warning
    assert "BATHYMETRY-ONLY" in res.fallback_warning


# --------------------------------------------------------------------------- #
# Deep-water rung (ADR 0229): the 3DEP land leg's flat ocean-fill must not
# clobber the ETOPO full-column bathy on a forced-bathy-base (offshore/tsunami)
# fetch, so a rupture/basin-scale domain keeps a genuine deep column.
# --------------------------------------------------------------------------- #


def _write_ll_raster(path: str, bbox, nx: int, ny: int, arr: np.ndarray) -> None:
    """Write a NaN-nodata EPSG:4326 float32 raster from a full (ny, nx) array."""
    west, south, east, north = bbox
    with rasterio.open(
        path, "w", driver="GTiff", height=ny, width=nx, count=1, dtype="float32",
        crs="EPSG:4326",
        transform=from_origin(west, north, (east - west) / nx, (north - south) / ny),
        nodata=float("nan"),
    ) as dst:
        dst.write(arr.astype("float32"), 1)


def test_mask_land_leg_drops_ocean_fill_keeps_land(tmp_path: Any) -> None:
    """The rung helper: cells at/below the waterline (the 0 m ocean fill + negative
    fringe) become NaN; genuine emergent (positive) land is preserved unchanged."""
    land_path = str(tmp_path / "land.tif")
    _write_ll_raster(
        land_path, _SMOKE_BBOX, 4, 1,
        np.array([[-3.0, 0.0, 12.5, 50.0]], dtype="float32"),
    )
    out = tb._mask_land_leg_ocean_fill(land_path)
    try:
        with rasterio.open(out) as ds:
            r = ds.read(1)
    finally:
        os.unlink(out)
    assert np.isnan(r[0, 0]) and np.isnan(r[0, 1])  # -3 m and 0 m fill dropped
    assert r[0, 2] == pytest.approx(12.5) and r[0, 3] == pytest.approx(50.0)  # land kept


def _deep_rung_rasters(tmp_path):
    """An all-ocean ETOPO deep column (-3000 m) under a 3DEP land leg that is a flat
    0 m ocean fill with a narrow +50 m emergent strip -- the Chignik land-only shape
    in miniature."""
    etopo_path = str(tmp_path / "etopo.tif")
    land_path = str(tmp_path / "land.tif")
    n = 40
    _write_ll_raster(etopo_path, _SMOKE_BBOX, n, n, np.full((n, n), -3000.0, "float32"))
    land = np.zeros((n, n), dtype="float32")  # 0 m ocean fill (the 3DEP over-water fill)
    col = np.arange(n)[None, :].repeat(n, axis=0)
    land[col < 8] = 50.0  # a genuine emergent land strip
    _write_ll_raster(land_path, _SMOKE_BBOX, n, n, land)
    return etopo_path, land_path


def test_deep_rung_restores_deep_column_under_land_fill(monkeypatch, tmp_path: Any) -> None:
    """force_bathy_base=True: the 0 m land fill no longer clobbers the ETOPO deep
    column -- the composite keeps a genuine deep bed AND the emergent land strip."""
    etopo_path, land_path = _deep_rung_rasters(tmp_path)
    _patch_delegate_sources(monkeypatch, cudem_tiles=[], land_path=land_path,
                            etopo_tiles=[etopo_path])
    arr, _tf, _crs, prov = tb._select_and_merge(
        _SMOKE_BBOX, 10, TARGET_CRS, None, 30.0,
        force_bathy_base=True, include_regional_fine=False, min_pixel_m=None,
        skip_cudem=False, skip_land=False,
    )
    a = arr[np.isfinite(arr)]
    assert a.min() < -2500.0                     # deep ETOPO column survived
    assert (a > 40.0).any()                      # +50 m emergent land preserved
    assert float(np.mean(a < -5.0)) > 0.5        # majority genuinely wet
    assert prov["bathymetry_present"] is True


def test_deep_rung_off_leaves_land_fill_clobber(monkeypatch, tmp_path: Any) -> None:
    """force_bathy_base=False (small-box / non-offshore path): behaviour is UNCHANGED
    -- the 3DEP 0 m fill still wins by precedence, so no deep column (the guard is
    scoped strictly to the forced-bathy-base offshore intent)."""
    etopo_path, land_path = _deep_rung_rasters(tmp_path)
    _patch_delegate_sources(monkeypatch, cudem_tiles=[], land_path=land_path,
                            etopo_tiles=[etopo_path])
    arr, _tf, _crs, _prov = tb._select_and_merge(
        _SMOKE_BBOX, 10, TARGET_CRS, None, 30.0,
        force_bathy_base=False, include_regional_fine=False, min_pixel_m=None,
        skip_cudem=False, skip_land=False,
    )
    a = arr[np.isfinite(arr)]
    assert a.min() > -100.0                       # ETOPO deep column clobbered by fill
    assert not (a < -5.0).any()
