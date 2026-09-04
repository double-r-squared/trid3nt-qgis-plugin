"""fetch_dem router-fold tests (ADR 0097) -- migrated from test_data_fetch.py.

fetch_dem folded from a coded twin to a spec-driven ``library_delegate`` router
source (py3dep hooks + the source="copernicus" cross-sibling dispatch). These
carry the 0091 gated-fallback contract's test-pins intact, adapted to the router
seams: the network step is the monkeypatchable ``dem_3dep._fetch_3dep_dem_array``
(returns ``(array, transform, crs)``), and ``fetch_dem`` is the promoted registry
closure (``TOOL_REGISTRY["fetch_dem"].fn``, keyword-only).
"""

from __future__ import annotations

import time as _time
from typing import Any

import numpy as np
import pytest
import rasterio.transform as _rt

from trid3nt_server.tools import TOOL_REGISTRY, RegisteredTool
from trid3nt_server.tools.fetchers._fetch_common import (
    BboxInvalidError,
    UpstreamAPIError,
    round_bbox_to_resolution,
)
from trid3nt_server.tools.fetchers._router.hooks import dem_3dep as dem_mod
from trid3nt_contracts.execution import DemLayerURI, LayerURI

FORT_MYERS_BBOX = (-81.9, 26.55, -81.8, 26.68)
_WA_STATE_BBOX = (-124.837922, 45.543029, -116.914037, 49.003324)

fetch_dem = TOOL_REGISTRY["fetch_dem"].fn


# --------------------------------------------------------------------------- #
# Helpers.
# --------------------------------------------------------------------------- #


def _synth_array(bbox, resolution_m):
    arr = np.ones((8, 8), dtype="float32")
    tr = _rt.from_bounds(bbox[0], bbox[1], bbox[2], bbox[3], 8, 8)
    return arr, tr, "EPSG:5070"


def _install_fake_array(monkeypatch, fn=_synth_array) -> None:
    monkeypatch.setattr(dem_mod, "_fetch_3dep_dem_array", fn)


def _effective_res_from_layer(layer) -> int:
    return int(layer.layer_id.rsplit("-", 1)[-1].rstrip("m"))


def _patch_copernicus_seam(monkeypatch, **mock_kw):
    """Swap the fetch_copernicus_dem registry entry for a MagicMock-carrying one."""
    from unittest.mock import MagicMock

    orig = TOOL_REGISTRY["fetch_copernicus_dem"]
    spy = MagicMock(**mock_kw)
    monkeypatch.setitem(
        TOOL_REGISTRY, "fetch_copernicus_dem",
        RegisteredTool(metadata=orig.metadata, fn=spy, module=orig.module),
    )
    return spy


def _fake_copernicus_layer(bbox=None, **_kw):
    return LayerURI(
        layer_id="copdem-glo30-test",
        name="Copernicus GLO-30 DEM (30m)",
        layer_type="raster",
        uri="s3://trid3nt-cache/cache/static-30d/copernicus_dem/fake.tif",
        role="input",
        units="meters",
        bbox=tuple(bbox),
    )


def _fake_dem_dataarray(bounds, crs="EPSG:4326"):
    """A minimal rioxarray DataArray spanning ``bounds`` (drives the coverage gate)."""
    import rioxarray  # noqa: F401 -- registers the .rio accessor
    import xarray as xr
    from rasterio.transform import from_bounds

    left, bottom, right, top = bounds
    h, w = 8, 8
    da = xr.DataArray(
        np.ones((h, w), dtype="float32"),
        dims=("y", "x"),
        coords={"y": np.linspace(top, bottom, h), "x": np.linspace(left, right, w)},
    )
    da = da.rio.write_crs(crs)
    da.rio.write_transform(from_bounds(left, bottom, right, top, w, h), inplace=True)
    return da


# --------------------------------------------------------------------------- #
# Registration.
# --------------------------------------------------------------------------- #


def test_fetch_dem_is_registered_with_static_30d():
    entry = TOOL_REGISTRY["fetch_dem"]
    assert entry.metadata.ttl_class == "static-30d"
    assert entry.metadata.source_class == "dem"
    assert entry.metadata.cacheable is True
    # INTERMEDIATE raster: opts out of the auto-render (twin metadata flag).


# --------------------------------------------------------------------------- #
# Happy path + cache write-through.
# --------------------------------------------------------------------------- #


def test_fetch_dem_happy_path_writes_through_cache(monkeypatch, fake_s3):
    _install_fake_array(monkeypatch)
    layer = fetch_dem(bbox=FORT_MYERS_BBOX, resolution_m=10)
    assert isinstance(layer, DemLayerURI)
    assert layer.layer_type == "raster"
    assert layer.style == {"kind": "continuous", "ramp": "gray", "units": "m", "label": "Elevation"}
    assert layer.uri.startswith("s3://trid3nt-cache/cache/static-30d/dem/")
    assert layer.uri.endswith(".tif")
    assert layer.units == "meters"
    assert layer.role == "input"
    assert layer.name == "USGS 3DEP DEM (10m)"
    q = round_bbox_to_resolution(FORT_MYERS_BBOX, 10)
    assert layer.layer_id == f"dem-{q[0]:.4f}-{q[1]:.4f}-10m"
    assert layer.bbox is not None and tuple(layer.bbox) == q
    # A real COG was written through the shared writer (serialization moved into
    # the router; the twin's opaque FAKE_COG_BYTES seam is gone).
    assert len(fake_s3.store) == 1
    key = next(iter(fake_s3.store))
    assert "/static-30d/dem/" in key and key.endswith(".tif")
    assert len(fake_s3.store[key]) > 0


def test_fetch_dem_rejects_continent_scale_bbox():
    whole_conus = (-125.0, 24.0, -66.0, 50.0)  # ~8,000,000 km^2
    with pytest.raises(BboxInvalidError, match="5,000,000"):
        fetch_dem(bbox=whole_conus, resolution_m=30)


# --------------------------------------------------------------------------- #
# Pixel-budget auto-coarsen.
# --------------------------------------------------------------------------- #


def test_fetch_dem_state_scale_no_hard_fail(monkeypatch, fake_s3):
    _install_fake_array(monkeypatch)
    layer = fetch_dem(bbox=_WA_STATE_BBOX, resolution_m=30)
    effective = _effective_res_from_layer(layer)
    assert 30 < effective <= 900
    assert "coarsened from 30m" in layer.name
    assert "hillshade/overview" in layer.name


def test_fetch_dem_bypass_enforces_pixel_budget(monkeypatch, fake_s3):
    _install_fake_array(monkeypatch)
    layer = fetch_dem(bbox=_WA_STATE_BBOX, resolution_m=10)
    effective = _effective_res_from_layer(layer)
    assert 10 < effective <= 900
    assert "coarsened from 10m" in layer.name


def test_fetch_dem_explicit_coarse_resolution_honored(monkeypatch, fake_s3):
    _install_fake_array(monkeypatch)
    layer = fetch_dem(bbox=FORT_MYERS_BBOX, resolution_m=300)
    assert _effective_res_from_layer(layer) == 300
    assert "coarsened" not in layer.name


def test_fetch_dem_tiny_bbox_native_resolution_untouched(monkeypatch, fake_s3):
    _install_fake_array(monkeypatch)
    tiny_bbox = (-81.9010, 26.5500, -81.9000, 26.5510)  # ~100 m x 110 m
    layer = fetch_dem(bbox=tiny_bbox, resolution_m=1)
    assert _effective_res_from_layer(layer) == 1
    assert "coarsened" not in layer.name


# --------------------------------------------------------------------------- #
# Gated cross-dataset fallback + pins (0091 contract).
# --------------------------------------------------------------------------- #


def test_fetch_dem_pinned_3dep_upstream_failure_reraises(monkeypatch, fake_s3):
    """A pinned-3dep upstream failure surfaces as UpstreamAPIError; no sentinel."""
    def boom(_bbox, _res):
        raise UpstreamAPIError("py3dep is unreachable")

    _install_fake_array(monkeypatch, boom)
    with pytest.raises(UpstreamAPIError):
        fetch_dem(bbox=FORT_MYERS_BBOX, resolution_m=10, source="3dep")
    assert fake_s3.store == {}


def test_fetch_dem_service_down_raises_gated_error_naming_copernicus(monkeypatch, fake_s3):
    """A 3DEP outage on the AUTO path does NOT silently swap; it raises the gate
    naming source="copernicus" + the tradeoff and NEVER touches the copernicus seam."""
    def boom(_bbox, _res):
        raise UpstreamAPIError("py3dep.get_dem failed: Service is currently not available")

    _install_fake_array(monkeypatch, boom)
    spy = _patch_copernicus_seam(monkeypatch, side_effect=_fake_copernicus_layer)
    with pytest.raises(dem_mod.DemAutoFallbackGateError) as exc_info:
        fetch_dem(bbox=FORT_MYERS_BBOX, resolution_m=10)

    spy.assert_not_called()
    err = exc_info.value
    assert err.error_code == "DEM_FALLBACK_GATE"
    assert err.retryable is True
    msg = str(err)
    assert "3DEP" in msg
    assert 'source="copernicus"' in msg
    assert "lidar" in msg.lower() and "radar" in msg.lower()
    suggestions = getattr(err, "suggestions", None)
    assert suggestions and any("copernicus" in s for s in suggestions)
    assert fake_s3.store == {}


def test_fetch_dem_hang_times_out_within_budget_then_gates(monkeypatch, fake_s3):
    """A hung py3dep grind is cut at the wall-clock budget -> gated error, bounded."""
    monkeypatch.setenv("TRID3NT_DEM_PRIMARY_TIMEOUT_S", "0.2")

    def hang(_bbox, _res):
        _time.sleep(8.0)
        return _synth_array(_bbox, _res)

    _install_fake_array(monkeypatch, hang)
    spy = _patch_copernicus_seam(monkeypatch, side_effect=_fake_copernicus_layer)
    start = _time.monotonic()
    with pytest.raises(dem_mod.DemAutoFallbackGateError):
        fetch_dem(bbox=FORT_MYERS_BBOX, resolution_m=30)
    elapsed = _time.monotonic() - start
    assert elapsed < 5.0, f"timeout did not bound the attempt (took {elapsed:.1f}s)"
    spy.assert_not_called()
    assert fake_s3.store == {}


def test_fetch_dem_timeout_error_is_typed_service_failure():
    assert issubclass(dem_mod.DemPrimaryTimeoutError, UpstreamAPIError)
    assert dem_mod.DemPrimaryTimeoutError.error_code == "DEM_PRIMARY_TIMEOUT"


def test_fetch_dem_out_of_coverage_distinct_from_outage(monkeypatch):
    """A clearly NON-US AOI on the auto path fails FAST with a DISTINCT typed
    DemOutOfCoverageError (naming copernicus) -- no 3DEP attempt is even made."""
    called = {"n": 0}

    def spy_3dep(_bbox, _res):
        called["n"] += 1
        return _synth_array(_bbox, _res)

    _install_fake_array(monkeypatch, spy_3dep)
    cop_spy = _patch_copernicus_seam(monkeypatch, side_effect=_fake_copernicus_layer)

    alps_bbox = (7.0, 45.0, 8.0, 46.0)
    with pytest.raises(dem_mod.DemOutOfCoverageError) as exc_info:
        fetch_dem(bbox=alps_bbox, resolution_m=10)

    assert called["n"] == 0, "3DEP must not be attempted for a non-US AOI"
    cop_spy.assert_not_called()
    err = exc_info.value
    assert err.error_code == "DEM_OUT_OF_COVERAGE"
    msg = str(err)
    assert "no coverage" in msg.lower()
    assert 'source="copernicus"' in msg
    assert "is back" not in msg


def test_fetch_dem_us_bbox_not_flagged_out_of_coverage(monkeypatch, fake_s3):
    _install_fake_array(monkeypatch)
    layer = fetch_dem(bbox=FORT_MYERS_BBOX, resolution_m=10)
    assert layer.name.startswith("USGS 3DEP DEM")


def test_fetch_dem_pinned_3dep_no_fallback_suggests_copernicus(monkeypatch, fake_s3):
    """explicit source='3dep' never falls back; error suggests copernicus."""
    def boom(_bbox, _res):
        raise UpstreamAPIError("3DEP: Service is currently not available")

    _install_fake_array(monkeypatch, boom)
    spy = _patch_copernicus_seam(monkeypatch)
    with pytest.raises(UpstreamAPIError) as exc_info:
        fetch_dem(bbox=FORT_MYERS_BBOX, resolution_m=10, source="3dep")

    spy.assert_not_called()
    msg = str(exc_info.value)
    assert "source='copernicus'" in msg
    assert "no cross-source fallback" in msg
    suggestions = getattr(exc_info.value, "suggestions", None)
    assert suggestions and any("copernicus" in s for s in suggestions)


def test_fetch_dem_healthy_3dep_path_unchanged_no_fallback_note(monkeypatch, fake_s3):
    _install_fake_array(monkeypatch)
    spy = _patch_copernicus_seam(monkeypatch)
    layer = fetch_dem(bbox=FORT_MYERS_BBOX, resolution_m=10)
    spy.assert_not_called()
    assert layer.name.startswith("USGS 3DEP DEM (10m)")
    assert layer.fallback_note is None


def test_fetch_dem_partial_coverage_propagates_not_ladder(monkeypatch, fake_s3):
    """DemPartialCoverageError is a DATA signal: no cross-source ladder."""
    def clipped(_bbox, _res):
        raise dem_mod.DemPartialCoverageError("south edge short")

    _install_fake_array(monkeypatch, clipped)
    spy = _patch_copernicus_seam(monkeypatch)
    with pytest.raises(dem_mod.DemPartialCoverageError):
        fetch_dem(bbox=FORT_MYERS_BBOX, resolution_m=10)
    spy.assert_not_called()


# --------------------------------------------------------------------------- #
# source="copernicus" cross-sibling DISPATCH (ADR 0097).
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "alias",
    ["copernicus", "COPERNICUS", "cop-dem-glo-30", "glo-30", "glo30", "copernicus_glo30"],
)
def test_fetch_dem_copernicus_dispatch_verbatim(monkeypatch, fake_s3, alias):
    """source="copernicus" is served VERBATIM by fetch_copernicus_dem via the
    pre-flight dispatch: its layer is returned unchanged, 3DEP is never attempted,
    and NO object is cached under the dem prefix (no double-cache)."""
    called = {"n": 0}

    def spy_3dep(_bbox, _res):
        called["n"] += 1
        return _synth_array(_bbox, _res)

    _install_fake_array(monkeypatch, spy_3dep)
    spy = _patch_copernicus_seam(monkeypatch, side_effect=_fake_copernicus_layer)

    layer = fetch_dem(bbox=FORT_MYERS_BBOX, resolution_m=10, source=alias)

    assert called["n"] == 0, "3DEP must not be attempted for a copernicus dispatch"
    spy.assert_called_once_with(bbox=FORT_MYERS_BBOX)
    assert "Copernicus GLO-30" in layer.name
    assert "copernicus_dem" in layer.uri
    # NO double-cache: the dispatch created no dem-prefix cache object.
    assert not any("/static-30d/dem/" in k for k in fake_s3.store), fake_s3.store


# --------------------------------------------------------------------------- #
# Coverage gate (LANE-C): the DataArray reproject-bounds partial-coverage check.
# --------------------------------------------------------------------------- #


def test_fetch_3dep_full_coverage_passes(monkeypatch):
    """A DEM that fully spans the requested bbox serializes (no raise)."""
    req = (-97.755, 30.26, -97.725, 30.285)
    full = (-97.76, 30.255, -97.72, 30.29)
    import py3dep

    monkeypatch.setattr(py3dep, "get_dem", lambda bbox, resolution: _fake_dem_dataarray(full))
    arr, transform, crs = dem_mod._fetch_3dep_dem_array(req, 10)
    assert arr.shape == (8, 8)


def test_fetch_3dep_south_edge_short_raises_partial_coverage(monkeypatch):
    """A DEM short on the SOUTH edge raises DemPartialCoverageError."""
    req = (-97.755, 30.26, -97.725, 30.285)
    short_south = (-97.755, 30.265, -97.725, 30.285)
    import py3dep

    monkeypatch.setattr(py3dep, "get_dem", lambda bbox, resolution: _fake_dem_dataarray(short_south))
    with pytest.raises(dem_mod.DemPartialCoverageError) as exc:
        dem_mod._fetch_3dep_dem_array(req, 10)
    assert exc.value.error_code == "DEM_PARTIAL_COVERAGE"


def test_dem_partial_coverage_is_upstream_subclass():
    assert issubclass(dem_mod.DemPartialCoverageError, UpstreamAPIError)


def test_bbox_covers_flags_material_shortfall():
    req = (-97.755, 30.26, -97.725, 30.285)
    tol = dem_mod._DEM_COVERAGE_TOL_DEG
    assert dem_mod._bbox_covers((-97.76, 30.25, -97.72, 30.29), req) is True
    assert dem_mod._bbox_covers((-97.755, 30.27, -97.725, 30.285), req) is False
    assert dem_mod._bbox_covers((-97.755, 30.26 + tol * 0.4, -97.725, 30.285), req) is True
