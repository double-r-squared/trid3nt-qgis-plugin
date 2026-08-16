"""Library-delegate raster fold parity (ADR 0074): fetch_statsgo_soils via the router.

Migrates the value-bearing coverage of the deleted fetch_statsgo_soils twin (the
statsgo block of test_pfdf_unlock_statsgo_nldi_3dep.py) onto the generic
library-delegate mode: the pre-cache CONUS validate hook, the router field enum, the
pfdf delegate hook's array -> COG serialization + all-NaN empty, the payload gate,
and the units/style-by-field LayerURI stamps. The pfdf socket is the ONE sanctioned
impurity (mocked here for a hermetic offline run); the real ScienceBase path is
proven by the live proof recorded in ADR 0074.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest
from affine import Affine
from rasterio.crs import CRS

from trid3nt_server.agent.tools.fetchers._router import router
from trid3nt_server.agent.tools.fetchers._router.errors import (
    RouterEmptyError,
    RouterInputError,
    RouterUpstreamError,
)
from trid3nt_server.agent.tools.fetchers._router.executors import library_delegate, raster_cog
from trid3nt_server.agent.tools.fetchers._router.spec import load_spec_from_path

STATSGO_SPEC = load_spec_from_path(
    Path(__file__).resolve().parents[1]
    / "trid3nt_server/agent/tools/fetchers/soil/fetch_statsgo_soils/source.yaml"
)

_KANSAS = (-95.30, 39.00, -95.20, 39.10)  # small in-CONUS AOI


def _vp(**raw: Any) -> dict[str, Any]:
    return router.validate_params(STATSGO_SPEC, raw)


class _FakeRaster:
    def __init__(self, values: Any, nodata: Any = None):
        self.values = values
        self.affine = Affine(0.05, 0, -95.30, 0, -0.05, 39.10)
        self.crs = CRS.from_epsg(5069)  # pfdf STATSGO native = NAD27 Albers
        self.nodata = nodata


def _patch_statsgo_read(monkeypatch, values: Any, nodata: Any = None):
    import pfdf.data.usgs.statsgo as statsgo_mod

    def fake_read(field, bbox, timeout=None):
        return _FakeRaster(values, nodata)

    monkeypatch.setattr(statsgo_mod, "read", fake_read)


# --------------------------------------------------------------------------- #
# Registration + spec shape.
# --------------------------------------------------------------------------- #


def test_statsgo_promoted_as_library_delegate_spec():
    from trid3nt_server.agent.tools import TOOL_REGISTRY

    entry = TOOL_REGISTRY["fetch_statsgo_soils"]
    assert entry.metadata.source_class == "statsgo_soils"
    assert entry.metadata.ttl_class == "static-30d"
    assert entry.metadata.cacheable is True
    assert STATSGO_SPEC.hooks.delegate == "pfdf_statsgo.read"
    assert STATSGO_SPEC.hooks.delegate_validate == "pfdf_statsgo.validate"
    # Raster delegate routes through raster_cog (its fetch_source_array calls the hook).
    assert router.select_executor(STATSGO_SPEC).__module__.endswith("raster_cog")


def test_statsgo_docstring_carried_verbatim():
    from trid3nt_server.agent.tools import TOOL_REGISTRY

    doc = TOOL_REGISTRY["fetch_statsgo_soils"].fn.__doc__ or ""
    assert "STATSGO" in doc and "K-factor" in doc


# --------------------------------------------------------------------------- #
# Payload estimator (bbox_area) + field enum.
# --------------------------------------------------------------------------- #


def test_statsgo_payload_scales_with_bbox():
    estimate = router.synthesize_payload_estimator(STATSGO_SPEC)
    small = estimate(bbox=(-82.0, 26.4, -81.7, 26.7))
    big = estimate(bbox=(-83.0, 26.0, -81.0, 28.0))
    assert 0.0 < small < big
    assert estimate(bbox=None) > 0.0


def test_statsgo_rejects_unknown_field():
    with pytest.raises(RouterInputError):
        _vp(bbox=list(_KANSAS), field="NOPE")


# --------------------------------------------------------------------------- #
# CONUS pre-cache validate hook (the source-specific input gate).
# --------------------------------------------------------------------------- #


def test_statsgo_rejects_outside_conus_bbox():
    ak = _vp(bbox=[-150.0, 60.0, -149.0, 61.0], field="KFFACT")
    with pytest.raises(RouterInputError) as ei:
        library_delegate.pre_validate(STATSGO_SPEC, ak)
    assert "CONUS" in str(ei.value)


def test_statsgo_conus_bbox_passes_validate():
    library_delegate.pre_validate(STATSGO_SPEC, _vp(bbox=list(_KANSAS), field="KFFACT"))


# --------------------------------------------------------------------------- #
# Delegate array -> COG + honest empty + upstream backstop.
# --------------------------------------------------------------------------- #


def test_statsgo_delegate_array_serializes_to_cog(monkeypatch):
    _patch_statsgo_read(monkeypatch, np.array([[0.31, 0.35], [0.38, np.nan]], dtype="float32"))
    arr, transform, crs = raster_cog.fetch_source_array(STATSGO_SPEC, _vp(bbox=list(_KANSAS), field="KFFACT"))
    assert arr.shape == (2, 2)
    cog = raster_cog.array_to_cog_bytes(arr, transform, crs)
    assert cog[:2] in (b"II", b"MM")  # TIFF magic


def test_statsgo_all_nan_raises_empty(monkeypatch):
    _patch_statsgo_read(monkeypatch, np.array([[np.nan, np.nan]], dtype="float32"))
    with pytest.raises(RouterEmptyError):
        raster_cog.fetch_source_array(STATSGO_SPEC, _vp(bbox=list(_KANSAS), field="KFFACT"))


def test_statsgo_library_error_maps_to_upstream(monkeypatch):
    import pfdf.data.usgs.statsgo as statsgo_mod

    def boom(field, bbox, timeout=None):
        raise ConnectionError("ScienceBase 503")

    monkeypatch.setattr(statsgo_mod, "read", boom)
    with pytest.raises(RouterUpstreamError) as ei:
        raster_cog.fetch_source_array(STATSGO_SPEC, _vp(bbox=list(_KANSAS), field="KFFACT"))
    # Verbatim provider reason surfaced, never a raw traceback leak.
    assert "ScienceBase 503" in str(ei.value)


# --------------------------------------------------------------------------- #
# units / style-by-field LayerURI stamps.
# --------------------------------------------------------------------------- #


def test_statsgo_units_and_style_by_field():
    kf = router.build_layer_uri(STATSGO_SPEC, _vp(bbox=list(_KANSAS), field="KFFACT"), "s3://c/k.tif")
    assert kf.layer_type == "raster" and kf.role == "input"
    assert kf.units is None and kf.style_preset == "statsgo_kffact"
    th = router.build_layer_uri(STATSGO_SPEC, _vp(bbox=list(_KANSAS), field="THICK"), "s3://c/t.tif")
    assert th.units == "centimeters" and th.style_preset == "statsgo_thick"
