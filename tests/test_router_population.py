"""WorldPop library-delegate raster fold parity (ADR 0092): fetch_population via the router.

Migrates the value-bearing WorldPop coverage of the deleted fetch_population twin (the
worldpop block of test_data_fetch.py) onto the generic library-delegate raster mode:
the pre-cache vintage validate hook, the URL composition, the whole-object-download-
then-window delegate read (WorldPop serves HTTP 200 to range requests, so /vsicurl
cannot window it -> the delegate downloads once + windows), the payload gate, and the
units/style LayerURI stamps.

APPROVED REMOVAL (ADR 0092): the twin's half-built ACS (Census B01003) leg is DROPPED.
An ``acs_*`` dataset now fails the validate gate with the standard typed input error
(fetch_census_acs serves tract population) -- covered explicitly below as the
surface-change contract, NOT a parity break.

The requests/rasterio socket is the ONE sanctioned delegate impurity (mocked here for a
hermetic offline run over a synthetic country GeoTIFF); the real WorldPop path is
proven by the live proof recorded in ADR 0092. ASCII only.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from trid3nt_server.agent.tools.fetchers._router import router
from trid3nt_server.agent.tools.fetchers._router.errors import (
    RouterEmptyError,
    RouterInputError,
    RouterUpstreamError,
)
from trid3nt_server.agent.tools.fetchers._router.executors import library_delegate, raster_cog
from trid3nt_server.agent.tools.fetchers._router.hooks import worldpop
from trid3nt_server.agent.tools.fetchers._router.spec import load_spec_from_path

POP_SPEC = load_spec_from_path(
    Path(__file__).resolve().parents[1]
    / "trid3nt_server/agent/tools/fetchers/socioeconomic/fetch_population/source.yaml"
)

FORT_MYERS_BBOX = (-81.92, 26.55, -81.80, 26.68)  # small in-USA AOI


def _vp(**raw: Any) -> dict[str, Any]:
    return router.validate_params(POP_SPEC, raw)


def _synthetic_country_tif_bytes() -> bytes:
    """A WorldPop-shaped GeoTIFF over the Florida region at ~0.01 deg, value = 5 people/cell.

    Fine enough that a Fort-Myers-class AOI windows to a non-degenerate sub-region (the
    real WorldPop 1km file is ~0.0083 deg/cell); the tif only needs to CONTAIN the bbox.
    """
    import rasterio
    from rasterio.transform import from_bounds

    west, south, east, north = -88.0, 24.0, -79.0, 31.0
    w, h = 900, 700
    arr = np.full((h, w), 5.0, dtype="float32")
    transform = from_bounds(west, south, east, north, w, h)
    fd, path = tempfile.mkstemp(suffix=".tif")
    os.close(fd)
    try:
        with rasterio.open(
            path, "w", driver="GTiff", height=h, width=w, count=1,
            dtype="float32", crs="EPSG:4326", transform=transform, nodata=-99999.0,
        ) as dst:
            dst.write(arr, 1)
        with open(path, "rb") as f:
            return f.read()
    finally:
        os.unlink(path)


class _FakeResp:
    def __init__(self, data: bytes, status: int = 200):
        self._data = data
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"{self.status_code}")

    def iter_content(self, chunk_size: int = 1 << 20):
        for i in range(0, len(self._data), chunk_size):
            yield self._data[i:i + chunk_size]


def _patch_worldpop_download(monkeypatch, status: int = 200, data: bytes | None = None):
    tif = data if data is not None else _synthetic_country_tif_bytes()

    def fake_get(url, **_kw):
        return _FakeResp(tif, status)

    monkeypatch.setattr(worldpop.requests, "get", fake_get)


# --------------------------------------------------------------------------- #
# Registration + spec shape.
# --------------------------------------------------------------------------- #


def test_population_promoted_as_library_delegate_spec():
    from trid3nt_server.agent.tools import TOOL_REGISTRY

    entry = TOOL_REGISTRY["fetch_population"]
    assert entry.metadata.source_class == "population"
    assert entry.metadata.ttl_class == "static-30d"
    assert entry.metadata.cacheable is True
    assert POP_SPEC.shape == "raster-cog"
    assert POP_SPEC.hooks.delegate == "worldpop.read"
    assert POP_SPEC.hooks.delegate_validate == "worldpop.validate"
    assert (POP_SPEC.ingest or {}).get("access") == "library_delegate"
    # Raster delegate routes through raster_cog (its fetch_source_array calls the hook).
    assert router.select_executor(POP_SPEC).__module__.endswith("raster_cog")


def test_population_signature_matches_twin():
    import inspect

    from trid3nt_server.agent.tools import TOOL_REGISTRY

    p = inspect.signature(TOOL_REGISTRY["fetch_population"].fn).parameters
    assert list(p) == ["bbox", "dataset", "target_resolution_m", "_extra_ignored"]
    assert p["dataset"].default == "worldpop_2020"
    assert p["target_resolution_m"].default == 1000


def test_population_docstring_is_worldpop_only():
    from trid3nt_server.agent.tools import TOOL_REGISTRY

    doc = TOOL_REGISTRY["fetch_population"].fn.__doc__ or ""
    assert "WorldPop" in doc
    # The ACS leg is dropped; the doc routes census asks to fetch_census_acs.
    assert "fetch_census_acs" in doc


# --------------------------------------------------------------------------- #
# APPROVED REMOVAL: the ACS leg is gone from the surface.
# --------------------------------------------------------------------------- #


def test_acs_dataset_rejected_as_input_error():
    """dataset='acs_2022' now fails the validate gate (ACS leg dropped, ADR 0092)."""
    with pytest.raises(RouterInputError) as ei:
        worldpop.validate_population(POP_SPEC, _vp(bbox=list(FORT_MYERS_BBOX), dataset="acs_2022"))
    assert "worldpop" in str(ei.value).lower() or "WorldPop" in str(ei.value)


def test_acs_request_never_reaches_network(monkeypatch):
    """A full acs_2022 call raises pre-network (validate runs before read_through)."""
    from trid3nt_server.agent.tools import TOOL_REGISTRY

    def _no_net(*_a, **_kw):  # pragma: no cover -- must not be reached
        raise AssertionError("requests.get must NOT be called for an acs_* dataset")

    monkeypatch.setattr(worldpop.requests, "get", _no_net)
    with pytest.raises(RouterInputError):
        TOOL_REGISTRY["fetch_population"].fn(bbox=list(FORT_MYERS_BBOX), dataset="acs_2022")


# --------------------------------------------------------------------------- #
# Vintage validate hook (normalize-then-validate; pre-cache, offline).
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("year", [2000, 2005, 2010, 2015, 2020])
def test_validate_accepts_in_range_vintages(year):
    worldpop.validate_population(POP_SPEC, _vp(bbox=list(FORT_MYERS_BBOX), dataset=f"worldpop_{year}"))
    assert worldpop._worldpop_year_from_dataset(POP_SPEC, f"worldpop_{year}") == year


@pytest.mark.parametrize("year", [1999, 1850, 2021, 2024, 2030])
def test_validate_rejects_out_of_range_year(year):
    dataset = f"worldpop_{year}"
    with pytest.raises(RouterInputError) as ei:
        worldpop.validate_population(POP_SPEC, _vp(bbox=list(FORT_MYERS_BBOX), dataset=dataset))
    msg = str(ei.value)
    assert dataset in msg and str(year) in msg and "[2000,2020]" in msg


def test_validate_rejects_non_numeric_suffix():
    with pytest.raises(RouterInputError) as ei:
        worldpop.validate_population(POP_SPEC, _vp(bbox=list(FORT_MYERS_BBOX), dataset="worldpop_latest"))
    assert "worldpop_YYYY" in str(ei.value)


# --------------------------------------------------------------------------- #
# URL composition (100m native opt-in vs 1km default).
# --------------------------------------------------------------------------- #


def test_worldpop_url_for_100m_returns_unadj_native_url():
    url = worldpop._worldpop_url_for("USA", 2020, resolution_m=100)
    assert "Global_2000_2020/" in url and "Global_2000_2020_1km" not in url
    assert url.endswith("usa_ppp_2020_UNadj.tif")


def test_worldpop_url_for_default_returns_1km_url():
    assert worldpop._worldpop_url_for("USA", 2020) == worldpop._worldpop_url_for("USA", 2020, resolution_m=1000)
    assert worldpop._worldpop_url_for("USA", 2020) == (
        "https://data.worldpop.org/GIS/Population/Global_2000_2020_1km/2020/"
        "USA/usa_ppp_2020_1km_Aggregated.tif"
    )


# --------------------------------------------------------------------------- #
# Payload estimator + bbox cache-key (target_resolution_m distinct keys).
# --------------------------------------------------------------------------- #


def test_population_payload_scales_with_bbox():
    estimate = router.synthesize_payload_estimator(POP_SPEC)
    small = estimate(bbox=(-82.0, 26.4, -81.7, 26.7))
    big = estimate(bbox=(-84.0, 24.0, -80.0, 30.0))
    assert 0.0 < small < big
    assert estimate(bbox=None) > 0.0


def test_target_resolution_m_enters_cache_params():
    """100m vs 1km validated params differ -> distinct cache keys (distinct products)."""
    p1000 = _vp(bbox=list(FORT_MYERS_BBOX), dataset="worldpop_2020")
    p100 = _vp(bbox=list(FORT_MYERS_BBOX), dataset="worldpop_2020", target_resolution_m=100)
    assert p1000["target_resolution_m"] == 1000
    assert p100["target_resolution_m"] == 100
    assert p1000 != p100


# --------------------------------------------------------------------------- #
# Delegate download-then-window -> array -> COG + honest empty + upstream.
# --------------------------------------------------------------------------- #


def test_delegate_downloads_windows_and_serializes_to_cog(monkeypatch):
    _patch_worldpop_download(monkeypatch)
    arr, transform, crs = raster_cog.fetch_source_array(
        POP_SPEC, _vp(bbox=list(FORT_MYERS_BBOX), dataset="worldpop_2020")
    )
    assert arr.ndim == 2 and arr.size > 0
    # nodata (-99999) masked to NaN; the covered AOI carries the 5 people/cell value.
    assert np.nanmax(arr) == pytest.approx(5.0)
    cog = raster_cog.array_to_cog_bytes(arr, transform, crs)
    assert cog[:2] in (b"II", b"MM")  # TIFF magic


def test_delegate_404_maps_to_empty(monkeypatch):
    _patch_worldpop_download(monkeypatch, status=404)
    with pytest.raises(RouterEmptyError):
        raster_cog.fetch_source_array(POP_SPEC, _vp(bbox=list(FORT_MYERS_BBOX), dataset="worldpop_2020"))


def test_delegate_off_country_bbox_raises_upstream(monkeypatch):
    """A bbox center outside every ISO3 envelope has no country file URL -> upstream."""
    _patch_worldpop_download(monkeypatch)
    # Mid-Pacific bbox matches no envelope.
    with pytest.raises(RouterUpstreamError):
        raster_cog.fetch_source_array(POP_SPEC, _vp(bbox=[-160.0, 0.0, -159.0, 1.0], dataset="worldpop_2020"))


def test_delegate_download_failure_maps_to_upstream(monkeypatch):
    import requests

    def boom(*_a, **_kw):
        raise requests.ConnectionError("WorldPop 503")

    monkeypatch.setattr(worldpop.requests, "get", boom)
    with pytest.raises(RouterUpstreamError) as ei:
        raster_cog.fetch_source_array(POP_SPEC, _vp(bbox=list(FORT_MYERS_BBOX), dataset="worldpop_2020"))
    assert "WorldPop 503" in str(ei.value)


# --------------------------------------------------------------------------- #
# LayerURI stamps.
# --------------------------------------------------------------------------- #


def test_population_units_and_style_stamps():
    layer = router.build_layer_uri(POP_SPEC, _vp(bbox=list(FORT_MYERS_BBOX)), "s3://c/p.tif")
    assert layer.layer_type == "raster" and layer.role == "input"
    assert layer.units == "people"
    assert layer.style_preset == "population_density"
    # bbox stamp is the res_100-quantized request bbox (the twin's grid-snap).
    assert list(layer.bbox) == _vp(bbox=list(FORT_MYERS_BBOX))["bbox"]
