"""Unit tests for the ``fetch_sentinel1_sar`` atomic tool.

Coverage:
- Registration in TOOL_REGISTRY with expected metadata (+ payload estimator).
- bbox validation: degenerate / out-of-range / non-finite / too-large -> typed.
- polarization + collection normalization (aliases) + unknowns -> typed
  (non-retryable).
- Mocked PC STAC scene selection + band read: a synthetic linear-gamma0 power
  array round-trips to a cached single-band float32 dB COG with the right
  LayerURI shape (style preset, role, units) and dB = 10*log10(power).
- No-imagery path: an empty STAC search raises Sentinel1NoImageryError (honest,
  not fabricated) and is NOT retryable; an all-nodata window likewise.
- Cache-key determinism: a cache hit on a second identical call does not
  re-invoke the fetcher; a different polarization -> different cache key.

Network is fully mocked: the pystac Client + ``_pc_stac.sas_sign_href`` + the
windowed band reader are patched so no real Sentinel-1 scene is fetched.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest
import rasterio

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools import fetch_sentinel1_sar as s1_mod
from trid3nt_server.tools.fetch_sentinel1_sar import (
    _METADATA,
    _NODATA,
    _STYLE_PRESET,
    Sentinel1BboxError,
    Sentinel1CollectionError,
    Sentinel1NoImageryError,
    Sentinel1PolarizationError,
    estimate_payload_mb,
    fetch_sentinel1_sar,
)

_PINNED_NOW = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)

# Houston, TX flood-prone area -- small AOI inside the guardrail (matches proto).
_TX_BBOX = (-95.45, 29.70, -95.30, 29.82)


# ---------------------------------------------------------------------------
# In-memory S3 read-through injector (mirrors sibling test pattern).
# ---------------------------------------------------------------------------


class _FakeStore:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}


def _make_read_through_injector(fake):
    from trid3nt_server.tools.cache import (
        CACHE_BUCKET,
        cache_path,
        compute_cache_key as ck,
        is_cacheable,
        ReadThroughResult,
    )

    store = fake.store

    def patched(metadata, params, ext, fetch_fn, **kw):
        bucket = kw.get("bucket") or CACHE_BUCKET
        source_id = kw.get("source_id") or (metadata.source_class or metadata.name)
        force_refresh = kw.get("force_refresh", False)
        if not is_cacheable(metadata):
            return ReadThroughResult(uri=None, data=fetch_fn(), hit=False)
        key = ck(source_id, params, metadata.ttl_class, now=_PINNED_NOW)
        path = cache_path(metadata.source_class, metadata.ttl_class, key, ext)
        uri = f"s3://{bucket}/{path}"
        if not force_refresh and path in store:
            return ReadThroughResult(uri=uri, data=store[path], hit=True)
        data = fetch_fn()
        store[path] = data
        return ReadThroughResult(uri=uri, data=data, hit=False)

    return patched


# A square geometry fully covering the AOI so _select_scene's coverage test passes.
_FULL_COVER_GEOM = {
    "type": "Polygon",
    "coordinates": [
        [
            [-96.0, 29.0],
            [-95.0, 29.0],
            [-95.0, 30.0],
            [-96.0, 30.0],
            [-96.0, 29.0],
        ]
    ],
}


def _fake_item(
    scene_id: str = "S1_fake",
    *,
    dt: str = "2023-06-23T00:27:05Z",
    pols=("vv", "vh"),
):
    """A minimal STAC-like item carrying the requested polarization assets."""
    assets = {p: SimpleNamespace(href=f"https://blob/{p}.tif") for p in pols}
    return SimpleNamespace(
        id=scene_id,
        geometry=_FULL_COVER_GEOM,
        properties={"datetime": dt, "sat:orbit_state": "ascending"},
        assets=assets,
    )


def _fake_search_client(items):
    """Patchable pystac Client.open that returns a search yielding ``items``."""
    search = SimpleNamespace(items=lambda: list(items))
    client = SimpleNamespace(search=lambda **kw: search)
    return SimpleNamespace(open=staticmethod(lambda root: client))


# A known linear-power level whose dB is exact: 10*log10(0.1) = -10 dB.
_POWER_LEVEL = 0.1
_EXPECTED_DB = -10.0


def _make_band_reader(*, all_nodata: bool = False):
    """Return a fake _read_band_window emitting a uniform linear-power array.

    When ``all_nodata`` is True every pixel is NaN (no valid backscatter) so the
    honest no-imagery path is exercised.
    """

    def reader(signed_href, bbox, w, h):
        if all_nodata:
            return np.full((h, w), np.nan, dtype="float32")
        return np.full((h, w), _POWER_LEVEL, dtype="float32")

    return reader


def _read_cog_band1(data: bytes):
    """Read band 1 of an in-memory COG to a numpy array (with nodata as NaN)."""
    with rasterio.MemoryFile(data) as mem:
        with mem.open() as src:
            arr = src.read(1).astype("float32")
            nod = src.nodata
            count = src.count
            dtype = src.dtypes[0]
    if nod is not None:
        arr = np.where(arr == nod, np.nan, arr)
    return arr, count, dtype


# ---------------------------------------------------------------------------
# Registration / metadata.
# ---------------------------------------------------------------------------


def test_tool_is_registered() -> None:
    assert "fetch_sentinel1_sar" in TOOL_REGISTRY
    meta = TOOL_REGISTRY["fetch_sentinel1_sar"].metadata
    assert meta.name == "fetch_sentinel1_sar"
    assert meta.ttl_class == "static-30d"
    assert meta.source_class == "sentinel1_sar"
    assert meta.cacheable is True
    assert meta.supports_global_query is False
    assert meta.payload_mb_estimator_name == "estimate_payload_mb"


def test_open_world_hint_is_set() -> None:
    """A fetch_* external-API tool must carry open_world_hint."""
    spec = TOOL_REGISTRY["fetch_sentinel1_sar"]
    annotations = getattr(spec, "annotations", None)
    if annotations is not None:
        assert getattr(annotations, "open_world_hint", None) is True


def test_payload_estimator_scales_with_area() -> None:
    small = estimate_payload_mb(bbox=(-95.45, 29.70, -95.44, 29.71))
    big = estimate_payload_mb(bbox=(-95.45, 29.70, -95.30, 29.82))
    assert big > small
    assert estimate_payload_mb(bbox=None) > 0


# ---------------------------------------------------------------------------
# bbox validation.
# ---------------------------------------------------------------------------


def test_degenerate_bbox_raises() -> None:
    with pytest.raises(Sentinel1BboxError):
        fetch_sentinel1_sar(bbox=(-95.4, 29.7, -95.4, 29.7))


def test_out_of_range_bbox_raises() -> None:
    with pytest.raises(Sentinel1BboxError, match="lon out of"):
        fetch_sentinel1_sar(bbox=(-200.0, 29.7, -95.3, 29.8))


def test_nonfinite_bbox_raises() -> None:
    with pytest.raises(Sentinel1BboxError, match="non-finite"):
        fetch_sentinel1_sar(bbox=(float("nan"), 29.7, -95.3, 29.8))


def test_too_large_bbox_raises() -> None:
    with pytest.raises(Sentinel1BboxError, match="guardrail"):
        fetch_sentinel1_sar(bbox=(-96.0, 28.0, -94.0, 30.0))  # 4 deg^2


def test_bbox_error_not_retryable() -> None:
    try:
        fetch_sentinel1_sar(bbox=(-95.4, 29.7, -95.4, 29.7))
    except Sentinel1BboxError as exc:
        assert exc.retryable is False
    else:
        pytest.fail("expected Sentinel1BboxError")


# ---------------------------------------------------------------------------
# polarization / collection normalization / validation.
# ---------------------------------------------------------------------------


def test_unknown_polarization_raises_non_retryable() -> None:
    with pytest.raises(Sentinel1PolarizationError) as ei:
        fetch_sentinel1_sar(bbox=_TX_BBOX, polarization="hh")
    assert ei.value.retryable is False


def test_polarization_aliases_normalize() -> None:
    assert s1_mod._normalize_polarization("co-pol") == "vv"
    assert s1_mod._normalize_polarization("CROSS_POL") == "vh"
    assert s1_mod._normalize_polarization("VH") == "vh"
    assert s1_mod._normalize_polarization(None) == "vv"


def test_unknown_collection_raises_non_retryable() -> None:
    with pytest.raises(Sentinel1CollectionError) as ei:
        fetch_sentinel1_sar(bbox=_TX_BBOX, collection="sentinel-2-l2a")
    assert ei.value.retryable is False


def test_collection_aliases_normalize() -> None:
    assert s1_mod._normalize_collection("rtc") == "sentinel-1-rtc"
    assert s1_mod._normalize_collection("GRD") == "sentinel-1-grd"
    assert s1_mod._normalize_collection(None) == "sentinel-1-rtc"


# ---------------------------------------------------------------------------
# Happy path: synthetic power -> dB COG.
# ---------------------------------------------------------------------------


def test_power_converts_to_db_and_roundtrips_to_cog() -> None:
    fake = _FakeStore()
    item = _fake_item()
    with (
        patch.object(s1_mod, "read_through", _make_read_through_injector(fake)),
        patch("pystac_client.Client", _fake_search_client([item])),
        patch.object(s1_mod._pc_stac, "sas_sign_href", lambda href, coll: href),
        patch.object(s1_mod, "_read_band_window", _make_band_reader()),
    ):
        layer = fetch_sentinel1_sar(bbox=_TX_BBOX)

    assert layer.layer_type == "raster"
    assert layer.role == "primary"
    assert layer.style_preset == _STYLE_PRESET
    assert layer.units == "VV gamma0 backscatter (dB)"
    assert layer.uri and layer.uri.startswith("s3://")
    assert "vv" in layer.layer_id

    # Stored COG must be single-band float32 with the expected dB value.
    (path, data), = fake.store.items()
    arr, count, dtype = _read_cog_band1(data)
    assert count == 1
    assert dtype == "float32"
    assert math.isclose(float(np.nanmedian(arr)), _EXPECTED_DB, abs_tol=0.01)


def test_vh_polarization_changes_layer_and_cache_key() -> None:
    fake = _FakeStore()
    item = _fake_item()
    with (
        patch.object(s1_mod, "read_through", _make_read_through_injector(fake)),
        patch("pystac_client.Client", _fake_search_client([item])),
        patch.object(s1_mod._pc_stac, "sas_sign_href", lambda href, coll: href),
        patch.object(s1_mod, "_read_band_window", _make_band_reader()),
    ):
        vv = fetch_sentinel1_sar(bbox=_TX_BBOX, polarization="vv")
        vh = fetch_sentinel1_sar(bbox=_TX_BBOX, polarization="vh")

    assert vv.uri != vh.uri  # distinct cache keys
    assert "vh" in vh.layer_id
    assert vh.units == "VH gamma0 backscatter (dB)"
    assert len(fake.store) == 2


# ---------------------------------------------------------------------------
# Cache determinism.
# ---------------------------------------------------------------------------


def test_second_identical_call_is_a_cache_hit() -> None:
    fake = _FakeStore()
    item = _fake_item()
    calls = {"n": 0}

    def counting_reader(signed_href, bbox, w, h):
        calls["n"] += 1
        return np.full((h, w), _POWER_LEVEL, dtype="float32")

    with (
        patch.object(s1_mod, "read_through", _make_read_through_injector(fake)),
        patch("pystac_client.Client", _fake_search_client([item])),
        patch.object(s1_mod._pc_stac, "sas_sign_href", lambda href, coll: href),
        patch.object(s1_mod, "_read_band_window", counting_reader),
    ):
        a = fetch_sentinel1_sar(bbox=_TX_BBOX)
        b = fetch_sentinel1_sar(bbox=_TX_BBOX)

    assert a.uri == b.uri
    assert calls["n"] == 1  # second call served from cache, no re-read


# ---------------------------------------------------------------------------
# Honest no-imagery paths (data-source fallback norm).
# ---------------------------------------------------------------------------


def test_empty_search_raises_no_imagery_non_retryable() -> None:
    fake = _FakeStore()
    with (
        patch.object(s1_mod, "read_through", _make_read_through_injector(fake)),
        patch("pystac_client.Client", _fake_search_client([])),
    ):
        with pytest.raises(Sentinel1NoImageryError) as ei:
            fetch_sentinel1_sar(bbox=_TX_BBOX)
    assert ei.value.retryable is False
    assert len(fake.store) == 0  # nothing fabricated / written


def test_scene_missing_requested_polarization_raises_no_imagery() -> None:
    """A VV-only scene must NOT satisfy a VH request -> honest no-imagery."""
    fake = _FakeStore()
    vv_only = _fake_item(pols=("vv",))
    with (
        patch.object(s1_mod, "read_through", _make_read_through_injector(fake)),
        patch("pystac_client.Client", _fake_search_client([vv_only])),
    ):
        with pytest.raises(Sentinel1NoImageryError):
            fetch_sentinel1_sar(bbox=_TX_BBOX, polarization="vh")
    assert len(fake.store) == 0


def test_all_nodata_window_raises_no_imagery() -> None:
    fake = _FakeStore()
    item = _fake_item()
    with (
        patch.object(s1_mod, "read_through", _make_read_through_injector(fake)),
        patch("pystac_client.Client", _fake_search_client([item])),
        patch.object(s1_mod._pc_stac, "sas_sign_href", lambda href, coll: href),
        patch.object(
            s1_mod, "_read_band_window", _make_band_reader(all_nodata=True)
        ),
    ):
        with pytest.raises(Sentinel1NoImageryError):
            fetch_sentinel1_sar(bbox=_TX_BBOX)
    assert len(fake.store) == 0


def test_nodata_sentinel_value() -> None:
    """The COG nodata sentinel constant is the negative dB sentinel."""
    assert _NODATA == -9999.0
