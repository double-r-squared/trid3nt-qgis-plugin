"""``fetch_usgs_model_k`` through the animation-frames shape.

One call fans into one frame per layer of the ONE model grid covering the AOI,
and each frame is built from the real-archive excerpt zips offline: the canary
medians and probe cells, the ONE-download-per-archive inner cache, the outside-
grid refusal naming its shares, a genuinely inactive layer degrading, the typed
EMPTY and the upstream verdict."""

from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Any

import numpy as np
import pytest

import trid3nt_server.tools.cache as cache_mod
from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.fetchers._router import registration as reg
from trid3nt_server.tools.fetchers._router import router
from trid3nt_server.tools.fetchers._router.errors import (
    RouterEmptyError,
    RouterInputError,
    RouterUpstreamError,
)
from trid3nt_server.tools.fetchers._router.executors import animation_frames as EX
from trid3nt_server.tools.fetchers._router.transport import TransportError
from trid3nt_server.tools.fetchers.hydrology.fetch_usgs_model_k import hooks as MK

_SPEC = reg.get_spec("fetch_usgs_model_k")
_FIX = Path(__file__).resolve().parents[1] / "fixtures" / "usgs_model_k"
_CC_BBOX = [-70.62, 41.59, -70.50, 41.68]
_DM_BBOX = [-93.6729, 41.6625, -93.6369, 41.6895]
# Inside the Des Moines quad but OUTSIDE the excerpt's kept window: every layer
# there reads mask 0, the honest all-degraded path.
_EMPTY_BBOX = [-93.84, 41.60, -93.80, 41.63]
_OCEAN_BBOX = [-122.7, 45.4, -122.5, 45.6]
_CC_PROBE = (-70.56, 41.635)
_DM_PROBE = (-93.655, 41.676)


def _layer_no(layer_id: str) -> int:
    return int(re.search(r"-l(\d+)-", layer_id).group(1))


class _R:
    def __init__(self, uri: str, data: bytes = b""):
        self.uri = uri
        self.data = data


def _wire_fixtures(monkeypatch) -> dict[str, Any]:
    """Route both cache layers to in-memory stores and both archive reads to
    the excerpt zips, counting live GETs - the whole call runs offline."""
    jbcc = (_FIX / "jbcc_ring_excerpt.zip").read_bytes()
    dm = (_FIX / "dm_ring_excerpt.zip").read_bytes()
    seen: dict[str, Any] = {"gets": 0, "inner": [], "uris": {}}

    def _get_bytes(client, url, *, headers=None, params=None):
        seen["gets"] += 1
        body = jbcc if "631405ded34e36012efa3476" in url else dm
        return body, "application/zip", url

    inner: dict[str, bytes] = {}

    def _inner_rt(metadata, params, ext, fetch_fn, **kw):
        key = str(params)
        if key not in inner:
            inner[key] = fetch_fn()
            seen["inner"].append(key)
        return _R(uri=f"s3://cache-in/{len(seen['inner'])}", data=inner[key])

    def _outer_rt(metadata, params, ext, fetch_fn, **kw):
        data = fetch_fn()
        uri = f"s3://cache/{params['archive']}-l{params['layer']}.tif"
        seen["uris"][uri] = data
        return _R(uri=uri, data=data)

    monkeypatch.setattr(MK, "get_bytes", _get_bytes)
    monkeypatch.setattr(cache_mod, "read_through", _inner_rt)
    monkeypatch.setattr(EX, "read_through", _outer_rt)
    return seen


def _band_stats(cog: bytes, probe: tuple[float, float] | None = None):
    """The frame's whole-band median and, asked for, the value of the cell
    whose CENTER is nearest the probe."""
    import rasterio

    with rasterio.open(io.BytesIO(cog)) as ds:
        assert ds.count == 1 and ds.crs is not None and ds.crs.to_epsg() == 4326
        arr = ds.read(1, masked=True)
        med = float(np.ma.median(arr))
        hit = None
        if probe is not None:
            xs = ds.transform.c + (np.arange(ds.width) + 0.5) * ds.transform.a
            ys = ds.transform.f + (np.arange(ds.height) + 0.5) * ds.transform.e
            c = int(np.argmin(np.abs(xs - probe[0])))
            r = int(np.argmin(np.abs(ys - probe[1])))
            hit = float(arr[r, c])
    return med, hit


def test_usgs_model_k_registered_and_served():
    entry = TOOL_REGISTRY["fetch_usgs_model_k"]
    assert entry.metadata.source_class == "usgs_model_k"
    assert entry.metadata.ttl_class == "static-30d"
    assert entry.metadata.supports_global_query is False
    assert _SPEC is not None and _SPEC.shape == "animation_frames"
    assert _SPEC.error_code_prefix == "USGS_MODEL_K"
    assert entry.fn.__annotations__["return"] is list


def test_usgs_model_k_signature_is_bbox_only():
    import inspect

    p = inspect.signature(TOOL_REGISTRY["fetch_usgs_model_k"].fn).parameters
    assert list(p) == ["bbox", "_extra_ignored"]


def test_usgs_model_k_spec_identity():
    row = _SPEC.coverage[0]
    assert row.data_class == "aquifer property"
    assert row.kind == "modelled"
    assert _SPEC.normalize.quantity == "aquifer_hydraulic_conductivity"
    assert _SPEC.normalize.units == "m/day"
    assert row.value_column == "hydraulic_conductivity"
    assert row.units["hydraulic_conductivity"] == "m/day"
    assert "modelled" in (_SPEC.output.display_name or "").lower()


def test_usgs_model_k_is_found_through_the_aquifer_property_class(
    class_routes_to_the_match,
):
    """A covered fetcher carries no corpus: its class is the door."""
    class_routes_to_the_match("aquifer property", "fetch_usgs_model_k")


def test_coverage_rings_are_the_native_grid_corners():
    """The row's declared rings and the hooks' runtime quads are two statements
    of ONE fact; a drift between them would refuse a covered AOI or admit an
    uncovered one."""
    for key, ring in zip(("jbcc", "des_moines"), _SPEC.coverage[0].extent.rings):
        runtime = MK._ring_lonlat(MK._ARCHIVES[key])
        assert len(runtime) == len(ring)
        for (rlon, rlat), (ylon, ylat) in zip(runtime, ring):
            assert rlon == pytest.approx(ylon, abs=1e-4)
            assert rlat == pytest.approx(ylat, abs=1e-4)


def test_cape_cod_fans_into_one_frame_per_model_layer():
    plans = MK.frames_plan(_SPEC, {"bbox": _CC_BBOX})
    assert len(plans) == MK._ARCHIVES["jbcc"]["layers"] == 32
    assert [_layer_no(p.layer_id) for p in plans] == list(range(1, 33))
    first = plans[0]
    assert first.cache_params == {
        "bbox": _CC_BBOX,
        "archive": "jbcc",
        "layer": 1,
        "tool": "fetch_usgs_model_k",
    }
    assert first.name.startswith("Cape Cod JBCC MODFLOW K layer 1")
    assert len({p.layer_id for p in plans}) == 32
    # A static overlay: layers, not timestamps - no frame claims a window.
    assert all(p.valid_from is None and p.valid_to is None for p in plans)
    assert all(tuple(p.bbox) == tuple(_CC_BBOX) for p in plans)


def test_des_moines_fans_into_six_layers():
    plans = MK.frames_plan(_SPEC, {"bbox": _DM_BBOX})
    assert len(plans) == MK._ARCHIVES["des_moines"]["layers"] == 6
    assert {p.fetch_context["archive"] for p in plans} == {"des_moines"}
    assert plans[0].layer_id.startswith("model-k-des_moines-l1-")


def test_an_aoi_outside_both_grids_refuses_naming_them():
    with pytest.raises(RouterInputError) as ei:
        MK.frames_plan(_SPEC, {"bbox": _OCEAN_BBOX})
    assert ei.value.error_code == "USGS_MODEL_K_INPUT_INVALID"
    msg = str(ei.value)
    assert "outside the published USGS MODFLOW model grids" in msg
    assert "Cape Cod JBCC MODFLOW covers 0% of the AOI" in msg
    assert "Des Moines MF6 MODFLOW covers 0% of the AOI" in msg


def test_the_refusal_reaches_the_model_through_the_registry():
    with pytest.raises(RouterInputError) as ei:
        TOOL_REGISTRY["fetch_usgs_model_k"].fn(bbox=_OCEAN_BBOX)
    assert ei.value.error_code == "USGS_MODEL_K_INPUT_INVALID"


def test_a_partial_overlap_is_empty_naming_its_share():
    with pytest.raises(RouterEmptyError) as ei:
        MK.frames_plan(_SPEC, {"bbox": [-70.62, 41.59, -70.0, 41.68]})
    msg = str(ei.value)
    share = re.search(r"Cape Cod JBCC MODFLOW covers (\d+)% of the AOI", msg)
    assert share is not None and int(share.group(1)) < 100


def test_cape_cod_call_returns_the_canary_median_and_downloads_once(monkeypatch):
    seen = _wire_fixtures(monkeypatch)
    layers = TOOL_REGISTRY["fetch_usgs_model_k"].fn(bbox=_CC_BBOX)
    # The REAL archive's ib32 has no active cell over the canary ring (331
    # active cells grid-wide, none here), so its frame degrades honestly.
    assert {_layer_no(l.layer_id) for l in layers} == set(range(1, 32))
    assert len(layers) == 31
    assert seen["gets"] == 1, "the whole multi-frame call shares ONE download"
    assert layers[0].layer_type == "raster" and layers[0].role == "primary"
    assert layers[0].units == "m/day"
    assert all(l.valid_from is None for l in layers)
    med, hit = _band_stats(seen["uris"][layers[0].uri], probe=_CC_PROBE)
    assert med == pytest.approx(70.808, abs=0.01)
    assert hit == pytest.approx(64.271, abs=0.01)


def test_des_moines_call_returns_layer_medians_with_layer3_degraded(monkeypatch):
    seen = _wire_fixtures(monkeypatch)
    layers = TOOL_REGISTRY["fetch_usgs_model_k"].fn(bbox=_DM_BBOX)
    # idomain has no active cell over the ring in layer 3; the rest emit.
    assert {_layer_no(l.layer_id) for l in layers} == {1, 2, 4, 5, 6}
    assert seen["gets"] == 1
    meds = {}
    for layer in layers:
        med, hit = _band_stats(
            seen["uris"][layer.uri],
            probe=_DM_PROBE if _layer_no(layer.layer_id) == 1 else None,
        )
        meds[_layer_no(layer.layer_id)] = (med, hit)
    assert meds[1][0] == pytest.approx(25.037, abs=0.01)
    assert meds[1][1] == pytest.approx(28.973, abs=0.01)
    assert meds[2][0] == pytest.approx(10.028, abs=0.01)
    assert meds[5][0] == pytest.approx(51.718, abs=0.01)


def test_two_archives_two_calls_share_exactly_two_downloads(monkeypatch):
    seen = _wire_fixtures(monkeypatch)
    cc = TOOL_REGISTRY["fetch_usgs_model_k"].fn(bbox=_CC_BBOX)
    dm = TOOL_REGISTRY["fetch_usgs_model_k"].fn(bbox=_DM_BBOX)
    assert len(cc) + len(dm) == 36  # 31 + 5 emitted, 37 frames planned, 2 degraded
    assert seen["gets"] == 2, "36 frames across two archives are two live GETs"
    assert len(seen["inner"]) == 2, "one inner cache key per archive"


def test_an_aoi_with_no_active_cell_raises_the_typed_empty(monkeypatch):
    _wire_fixtures(monkeypatch)
    with pytest.raises(RouterEmptyError) as ei:
        TOOL_REGISTRY["fetch_usgs_model_k"].fn(bbox=_EMPTY_BBOX)
    assert ei.value.error_code == "USGS_MODEL_K_EMPTY"
    assert "every one of 6 frames" in str(ei.value)


def test_a_failed_archive_download_is_a_typed_upstream_error(monkeypatch):
    _wire_fixtures(monkeypatch)

    def _down(client, url, *, headers=None, params=None):
        raise TransportError("simulated outage", retryable=False)

    monkeypatch.setattr(MK, "get_bytes", _down)
    with pytest.raises(RouterUpstreamError) as ei:
        TOOL_REGISTRY["fetch_usgs_model_k"].fn(bbox=_CC_BBOX)
    assert ei.value.error_code == "USGS_MODEL_K_UPSTREAM_ERROR"
    assert ei.value.retryable is False
    assert "simulated outage" in str(ei.value)


def test_payload_estimator_stays_under_the_warn_threshold():
    estimate = router.synthesize_payload_estimator(_SPEC)
    mb = estimate(bbox=tuple(_CC_BBOX))
    assert 0.0 < mb < 25.0
