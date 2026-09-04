"""Library-delegate raster fold parity (ADR 0075): fetch_3dep_extra via the router.

Migrates the value-bearing coverage of the deleted fetch_3dep_extra twin (the 3DEP
block of test_pfdf_unlock_statsgo_nldi_3dep.py) onto the generic library-delegate
mode, plus the two ADR 0075 fast-follow extensions this fold introduced:
    role=input intermediate opts out of the server auto-render);
  * ``payload_estimate.mb_per_sq_deg_by_param`` -> the per-resolution coefficient
    table (5 / 500 / 5000 / 1 / 200 MB/deg^2) the scalar mb_per_sq_deg cannot hold.
The pre-cache US-envelope validate hook, the pfdf TNM delegate hook's array -> COG
serialization + empty/tile-limit/upstream error mapping, and the units/style LayerURI
stamps round out the parity. The pfdf socket is the ONE sanctioned impurity (mocked
here for a hermetic offline run); the real TNM path is proven by the ADR 0075 live proof.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest
from affine import Affine
from rasterio.crs import CRS

from trid3nt_server.tools.fetchers._router import router
from trid3nt_server.tools.fetchers._router.errors import (
    RouterEmptyError,
    RouterInputError,
    RouterUpstreamError,
)
from trid3nt_server.tools.fetchers._router.executors import library_delegate, raster_cog
from trid3nt_server.tools.fetchers._router.spec import load_spec_from_path

SPEC = load_spec_from_path(
    Path(__file__).resolve().parents[1]
    / "trid3nt_server/tools/fetchers/terrain/fetch_3dep_extra/source.yaml"
)

_FORT_MYERS = (-82.0, 26.4, -81.7, 26.7)  # small in-US AOI


def _vp(**raw: Any) -> dict[str, Any]:
    return router.validate_params(SPEC, raw)


class _FakeRaster:
    def __init__(self, values: Any, nodata: Any = None):
        self.values = values
        self.affine = Affine(0.0002, 0, -82.0, 0, -0.0002, 26.7)
        self.crs = CRS.from_epsg(4326)
        self.nodata = nodata


def _patch_tnm_read(monkeypatch, values: Any = None, nodata: Any = None, exc: Exception | None = None):
    import pfdf.data.usgs.tnm.dem as dem_mod

    def fake_read(bbox, resolution="1/3 arc-second", *, max_tiles=10, timeout=None):
        if exc is not None:
            raise exc
        return _FakeRaster(values, nodata)

    monkeypatch.setattr(dem_mod, "read", fake_read)


# --------------------------------------------------------------------------- #
# Registration + spec shape.
# --------------------------------------------------------------------------- #


def test_3dep_promoted_as_library_delegate_spec():
    from trid3nt_server.tools import TOOL_REGISTRY

    entry = TOOL_REGISTRY["fetch_3dep_extra"]
    assert entry.metadata.source_class == "3dep_extra"
    assert entry.metadata.ttl_class == "static-30d"
    assert entry.metadata.cacheable is True
    assert SPEC.hooks.delegate == "pfdf_3dep.read"
    assert SPEC.hooks.delegate_validate == "pfdf_3dep.validate"
    assert router.select_executor(SPEC).__module__.endswith("raster_cog")



def test_3dep_docstring_carried_verbatim():
    from trid3nt_server.tools import TOOL_REGISTRY

    doc = TOOL_REGISTRY["fetch_3dep_extra"].fn.__doc__ or ""
    assert "3DEP" in doc and "arc-second" in doc


# --------------------------------------------------------------------------- #
# Payload estimator: per-resolution coefficient table + bbox scaling.
# --------------------------------------------------------------------------- #


def test_3dep_payload_scales_with_resolution():
    estimate = router.synthesize_payload_estimator(SPEC)
    coarse = estimate(bbox=_FORT_MYERS, resolution="1 arc-second")
    fine = estimate(bbox=_FORT_MYERS, resolution="1/9 arc-second")
    lidar = estimate(bbox=_FORT_MYERS, resolution="1 meter")
    assert 0.0 < coarse < fine < lidar


def test_3dep_payload_scales_with_bbox():
    estimate = router.synthesize_payload_estimator(SPEC)
    small = estimate(bbox=(-82.0, 26.4, -81.9, 26.5), resolution="1 arc-second")
    big = estimate(bbox=(-83.0, 26.0, -81.0, 28.0), resolution="1 arc-second")
    assert 0.0 < small < big


def test_3dep_rejects_unknown_resolution():
    with pytest.raises(RouterInputError):
        _vp(bbox=list(_FORT_MYERS), resolution="1/3 arc-second")


def test_3dep_rejects_bad_max_tiles():
    with pytest.raises(RouterInputError):
        _vp(bbox=list(_FORT_MYERS), max_tiles=0)
    with pytest.raises(RouterInputError):
        _vp(bbox=list(_FORT_MYERS), max_tiles=10_000)


# --------------------------------------------------------------------------- #
# US-envelope pre-cache validate hook.
# --------------------------------------------------------------------------- #


def test_3dep_rejects_outside_us_bbox():
    europe = _vp(bbox=[10.0, 45.0, 11.0, 46.0])
    with pytest.raises(RouterInputError) as ei:
        library_delegate.pre_validate(SPEC, europe)
    assert "US" in str(ei.value)


def test_3dep_us_bbox_passes_validate():
    library_delegate.pre_validate(SPEC, _vp(bbox=list(_FORT_MYERS)))


# --------------------------------------------------------------------------- #
# Delegate array -> COG + empty / tile-limit / upstream mapping.
# --------------------------------------------------------------------------- #


def test_3dep_delegate_array_serializes_to_cog(monkeypatch):
    _patch_tnm_read(monkeypatch, np.array([[10.0, 12.0], [14.0, 16.0]], dtype="float32"))
    arr, transform, crs = raster_cog.fetch_source_array(SPEC, _vp(bbox=list(_FORT_MYERS)))
    assert arr.shape == (2, 2)
    cog = raster_cog.array_to_cog_bytes(arr, transform, crs)
    assert cog[:2] in (b"II", b"MM")


def test_3dep_no_products_maps_to_empty(monkeypatch):
    _patch_tnm_read(monkeypatch, exc=RuntimeError("No TNM products found for bbox"))
    with pytest.raises(RouterEmptyError):
        raster_cog.fetch_source_array(SPEC, _vp(bbox=list(_FORT_MYERS), resolution="1 meter"))


def test_3dep_tile_limit_maps_to_input(monkeypatch):
    _patch_tnm_read(monkeypatch, exc=RuntimeError("too many tiles for this request"))
    with pytest.raises(RouterInputError):
        raster_cog.fetch_source_array(SPEC, _vp(bbox=list(_FORT_MYERS), resolution="1 meter"))


def test_3dep_library_error_maps_to_upstream(monkeypatch):
    _patch_tnm_read(monkeypatch, exc=ConnectionError("TNM 503"))
    with pytest.raises(RouterUpstreamError) as ei:
        raster_cog.fetch_source_array(SPEC, _vp(bbox=list(_FORT_MYERS)))
    assert "TNM 503" in str(ei.value)


# --------------------------------------------------------------------------- #
# LayerURI stamps.
# --------------------------------------------------------------------------- #


def test_3dep_layer_uri_stamps():
    layer = router.build_layer_uri(SPEC, _vp(bbox=list(_FORT_MYERS)), "s3://c/k.tif")
    assert layer.layer_type == "raster" and layer.role == "input"
    assert layer.units == "meters" and layer.style == {"kind": "continuous", "ramp": "gray", "units": "m", "label": "Elevation"}
