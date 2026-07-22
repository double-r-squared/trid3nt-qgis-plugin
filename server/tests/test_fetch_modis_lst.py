"""Unit tests for the ``fetch_modis_lst`` atomic tool.

Coverage:
- Registration in TOOL_REGISTRY with expected metadata (+ payload estimator).
- bbox validation: degenerate / out-of-range / non-finite / too-large -> typed.
- product + daynight normalization (aliases) + unknown values -> typed
  (non-retryable).
- Mocked PC STAC item selection + band read: a synthetic uint16 LST DN array
  round-trips to a cached SINGLE-BAND float32 deg-C COG with the right LayerURI
  shape (style preset, role, units) and the correct DN -> deg-C scaling.
- Fill handling: DN == 0 pixels decode to NaN nodata in the emitted COG.
- No-data paths: an empty STAC search raises ModisLstNoDataError (honest, not
  fabricated) and is NOT retryable; an all-fill (DN==0) window likewise.
- Cache-key determinism: a second identical call hits the (in-memory) cache and
  does not re-invoke the fetcher; a different daynight -> a different cache key.

Network is fully mocked: the pystac Client + the per-href SAS signer + the
windowed band reader are patched so no real MODIS tile is fetched.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools import fetch_modis_lst as modis_mod
from trid3nt_server.tools.fetch_modis_lst import (
    _LST_SCALE,
    _KELVIN_C,
    _STYLE_PRESET,
    ModisLstBboxError,
    ModisLstNoDataError,
    ModisLstParamError,
    estimate_payload_mb,
    fetch_modis_lst,
)

_PINNED_NOW = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)

# Phoenix, AZ area  --  small AOI inside the guardrail (matches the prototype).
_AZ_BBOX = (-112.35, 33.25, -111.85, 33.75)


# ---------------------------------------------------------------------------
# In-memory S3 read-through injector (mirrors sibling test pattern).
# ---------------------------------------------------------------------------


class _FakeStore:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.fetch_calls = 0


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
            fake.fetch_calls += 1
            return ReadThroughResult(uri=None, data=fetch_fn(), hit=False)
        key = ck(source_id, params, metadata.ttl_class, now=_PINNED_NOW)
        path = cache_path(metadata.source_class, metadata.ttl_class, key, ext)
        uri = f"s3://{bucket}/{path}"
        if not force_refresh and path in store:
            return ReadThroughResult(uri=uri, data=store[path], hit=True)
        fake.fetch_calls += 1
        data = fetch_fn()
        store[path] = data
        return ReadThroughResult(uri=uri, data=data, hit=False)

    return patched


def _fake_item(item_id: str = "MOD11A2_fake", dt: str = "2023-08-29T00:00:00Z"):
    """A minimal STAC-like item carrying both day/night LST assets (11A2 keys)."""
    assets = {
        k: SimpleNamespace(href=f"https://blob/{k}.tif")
        for k in ("LST_Day_1km", "LST_Night_1km")
    }
    return SimpleNamespace(
        id=item_id,
        geometry=None,
        properties={"datetime": dt},
        assets=assets,
    )


def _fake_search_client(items):
    """Patchable pystac Client.open that returns a search yielding ``items``."""
    search = SimpleNamespace(items=lambda: list(items))
    client = SimpleNamespace(search=lambda **kw: search)
    return SimpleNamespace(open=staticmethod(lambda root: client))


def _dn_for_celsius(celsius: float) -> float:
    """uint16 DN that decodes to ``celsius`` deg C (T_K = DN*0.02; degC = K-273.15)."""
    return (celsius + _KELVIN_C) / _LST_SCALE


def _make_band_reader(*, celsius: float = 45.0, fill_rows: int = 0, all_fill: bool = False):
    """Return a fake _read_lst_window emitting a synthetic uint16-like DN array.

    Decodes to ``celsius`` deg C everywhere except the top ``fill_rows`` rows,
    which are DN==0 (fill -> NaN). ``all_fill=True`` returns an all-zero array to
    exercise the honest no-data path.
    """

    def reader(signed_href, bbox, w, h):
        if all_fill:
            return np.zeros((h, w), dtype="float32")
        arr = np.full((h, w), _dn_for_celsius(celsius), dtype="float32")
        if fill_rows > 0:
            arr[:fill_rows, :] = 0.0
        return arr

    return reader


def _run(fake, *, product="11A2", daynight="day", reader=None, items=None,
         start="2023-07-01", end="2023-08-31"):
    rt = _make_read_through_injector(fake)
    items = items if items is not None else [_fake_item()]
    reader = reader or _make_band_reader()
    with patch.object(modis_mod, "read_through", rt), patch.object(
        modis_mod, "_sign_href", side_effect=lambda href, c: href
    ), patch.object(
        modis_mod, "_read_lst_window", reader
    ), patch(
        "pystac_client.Client", _fake_search_client(items)
    ):
        return fetch_modis_lst(
            bbox=_AZ_BBOX,
            start_date=start,
            end_date=end,
            product=product,
            daynight=daynight,
        )


def _decode_single_band(cog_bytes):
    from rasterio.io import MemoryFile

    with MemoryFile(cog_bytes) as mem, mem.open() as src:
        assert src.count == 1
        assert str(src.dtypes[0]) == "float32"
        assert src.crs is not None and src.crs.to_epsg() == 4326
        return src.read(1)  # (H, W) float32


# ---------------------------------------------------------------------------
# Registration / metadata.
# ---------------------------------------------------------------------------


def test_tool_is_registered() -> None:
    assert "fetch_modis_lst" in TOOL_REGISTRY
    meta = TOOL_REGISTRY["fetch_modis_lst"].metadata
    assert meta.name == "fetch_modis_lst"
    assert meta.ttl_class == "static-30d"
    assert meta.source_class == "modis_lst"
    assert meta.cacheable is True
    assert meta.supports_global_query is False
    assert meta.payload_mb_estimator_name == "estimate_payload_mb"


def test_open_world_hint_is_set() -> None:
    """A fetch_* external-API tool must carry open_world_hint."""
    spec = TOOL_REGISTRY["fetch_modis_lst"]
    annotations = getattr(spec, "annotations", None)
    if annotations is not None:
        assert getattr(annotations, "open_world_hint", None) is True


def test_payload_estimator_scales_with_area() -> None:
    small = estimate_payload_mb(bbox=(-112.0, 33.0, -111.99, 33.01))
    big = estimate_payload_mb(bbox=(-112.0, 33.0, -111.5, 33.5))
    assert big > small
    assert estimate_payload_mb(bbox=None) > 0


# ---------------------------------------------------------------------------
# bbox validation.
# ---------------------------------------------------------------------------


def test_degenerate_bbox_raises() -> None:
    with pytest.raises(ModisLstBboxError):
        fetch_modis_lst(bbox=(-112.0, 33.0, -112.0, 33.0))


def test_out_of_range_bbox_raises() -> None:
    with pytest.raises(ModisLstBboxError, match="lon out of"):
        fetch_modis_lst(bbox=(-200.0, 33.0, -111.0, 34.0))


def test_nonfinite_bbox_raises() -> None:
    with pytest.raises(ModisLstBboxError, match="non-finite"):
        fetch_modis_lst(bbox=(float("nan"), 33.0, -111.0, 34.0))


def test_too_large_bbox_raises() -> None:
    with pytest.raises(ModisLstBboxError, match="guardrail"):
        fetch_modis_lst(bbox=(-118.0, 30.0, -111.0, 34.0))  # 28 deg^2 > 6


def test_bbox_error_not_retryable() -> None:
    try:
        fetch_modis_lst(bbox=(-112.0, 33.0, -112.0, 33.0))
    except ModisLstBboxError as exc:
        assert exc.retryable is False
    else:
        pytest.fail("expected ModisLstBboxError")


# ---------------------------------------------------------------------------
# product / daynight normalization / validation.
# ---------------------------------------------------------------------------


def test_product_aliases_normalize() -> None:
    assert modis_mod._normalize_product(None) == "11A2"
    assert modis_mod._normalize_product("MOD11A2") == "11A2"
    assert modis_mod._normalize_product("myd11a2") == "11A2"
    assert modis_mod._normalize_product("modis-21A2-061") == "21A2"
    assert modis_mod._normalize_product("21a2") == "21A2"


def test_daynight_aliases_normalize() -> None:
    assert modis_mod._normalize_daynight(None) == "day"
    assert modis_mod._normalize_daynight("Daytime") == "day"
    assert modis_mod._normalize_daynight("NIGHT") == "night"
    assert modis_mod._normalize_daynight("n") == "night"


def test_unknown_product_raises_non_retryable() -> None:
    with pytest.raises(ModisLstParamError) as ei:
        fetch_modis_lst(bbox=_AZ_BBOX, product="sonar")
    assert ei.value.retryable is False


def test_unknown_daynight_raises_non_retryable() -> None:
    with pytest.raises(ModisLstParamError) as ei:
        fetch_modis_lst(bbox=_AZ_BBOX, daynight="dusk")
    assert ei.value.retryable is False


# ---------------------------------------------------------------------------
# Happy path: synthetic DN -> single-band float32 deg-C COG.
# ---------------------------------------------------------------------------


def test_day_lst_roundtrips_to_degc_cog() -> None:
    fake = _FakeStore()
    lu = _run(fake, daynight="day", reader=_make_band_reader(celsius=45.0))

    assert lu.layer_type == "raster"
    assert lu.role == "primary"
    assert lu.style_preset == _STYLE_PRESET == "land_surface_temp_c"
    assert lu.units == "Land-surface temperature (deg C)"
    assert lu.uri.startswith("s3://")
    assert "modis-lst-11a2-day" in lu.layer_id

    # The cached COG bytes decode to ~45 deg C everywhere (single-band float32).
    path = lu.uri.split("/", 3)[3]
    band = _decode_single_band(fake.store[path])
    finite = band[np.isfinite(band)]
    assert finite.size > 0
    assert np.allclose(finite, 45.0, atol=0.5)


def test_style_preset_avoids_kelvin_temperature_family() -> None:
    """deg-C LST must NOT be styled by the Kelvin (*temperature*) family rule.

    The publish_layer registry forces any ``*temperature*`` preset to a 250..320
    Kelvin rescale; our deg-C preset must avoid that substring so it resolves to
    a deg-C-appropriate ramp (band-stats / a dedicated registry entry).
    """
    assert "temperature" not in _STYLE_PRESET.lower()
    from trid3nt_server.tools.publish_layer import _registry_style_params

    # Today there is no exact key -> None (falls through to band-stats viridis);
    # crucially it does NOT pick up the Kelvin 250,320 family rescale.
    resolved = _registry_style_params(_STYLE_PRESET)
    assert resolved is None or "250,320" not in resolved


def test_night_lst_uses_night_asset_and_distinct_cache_key() -> None:
    fake = _FakeStore()
    day = _run(fake, daynight="day", reader=_make_band_reader(celsius=45.0))
    night = _run(fake, daynight="night", reader=_make_band_reader(celsius=20.0))
    assert "night" in night.layer_id
    # day vs night must be DIFFERENT cache objects (distinct key).
    assert night.uri != day.uri
    assert fake.fetch_calls == 2


def test_product_21a2_uses_uppercase_km_asset() -> None:
    """21A2 day asset is ``LST_Day_1KM`` (uppercase); 11A2 is ``LST_Day_1km``."""
    fake = _FakeStore()
    item = SimpleNamespace(
        id="MOD21A2_fake",
        geometry=None,
        properties={"datetime": "2023-08-29T00:00:00Z"},
        assets={
            k: SimpleNamespace(href=f"https://blob/{k}.tif")
            for k in ("LST_Day_1KM", "LST_Night_1KM")
        },
    )
    lu = _run(fake, product="21A2", daynight="day", items=[item],
              reader=_make_band_reader(celsius=40.0))
    assert "modis-lst-21a2-day" in lu.layer_id


# ---------------------------------------------------------------------------
# Fill handling: DN==0 -> NaN nodata.
# ---------------------------------------------------------------------------


def test_fill_pixels_decode_to_nan() -> None:
    fake = _FakeStore()
    lu = _run(fake, reader=_make_band_reader(celsius=50.0, fill_rows=3))
    path = lu.uri.split("/", 3)[3]
    band = _decode_single_band(fake.store[path])
    # Some pixels are NaN (the fill rows) and some are the valid 50 deg C.
    assert np.isnan(band).any()
    assert np.isfinite(band).any()
    finite = band[np.isfinite(band)]
    assert np.allclose(finite, 50.0, atol=0.5)


# ---------------------------------------------------------------------------
# Honest no-data paths (data-source fallback norm).
# ---------------------------------------------------------------------------


def test_empty_search_raises_no_data_non_retryable() -> None:
    fake = _FakeStore()
    with pytest.raises(ModisLstNoDataError) as ei:
        _run(fake, items=[])
    assert ei.value.retryable is False


def test_all_fill_window_raises_no_data() -> None:
    fake = _FakeStore()
    with pytest.raises(ModisLstNoDataError):
        _run(fake, reader=_make_band_reader(all_fill=True))


# ---------------------------------------------------------------------------
# Cache determinism.
# ---------------------------------------------------------------------------


def test_identical_call_hits_cache_no_refetch() -> None:
    fake = _FakeStore()
    a = _run(fake, daynight="day", reader=_make_band_reader(celsius=45.0))
    assert fake.fetch_calls == 1
    b = _run(fake, daynight="day", reader=_make_band_reader(celsius=45.0))
    assert fake.fetch_calls == 1  # second call served from cache
    assert a.uri == b.uri
