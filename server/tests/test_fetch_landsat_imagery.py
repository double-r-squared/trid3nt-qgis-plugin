"""Unit tests for the ``fetch_landsat_imagery`` atomic tool.

Coverage:
- Registration in TOOL_REGISTRY with expected metadata (+ payload estimator).
- bbox validation: degenerate / out-of-range / non-finite / too-large -> typed.
- band_combo normalization (aliases) + an unknown combo -> typed (non-retryable).
- Mocked PC STAC scene selection + band reads: synthetic SR / thermal / QA
  bands round-trip to a cached 3-band uint8 RGB COG (multiband passthrough) for
  each of true_color / false_color_nir / thermal, with the right LayerURI shape
  (style preset, role, units).
- QA cloud masking: clouded pixels are emitted as black no-data.
- No-imagery path: an empty STAC search raises LandsatNoImageryError (honest,
  not fabricated) and is NOT retryable; an all-cloud window likewise.
- Cache-key determinism: a cache hit on a second identical call does not
  re-invoke the fetcher; different band_combo -> different cache key.

Network is fully mocked: the pystac Client + ``_pc_stac.sas_sign_href`` + the
per-band window reader are patched so no real Landsat scene is fetched.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools import fetch_landsat_imagery as landsat_mod
from trid3nt_server.tools.fetch_landsat_imagery import (
    _METADATA,
    _STYLE_PRESET,
    LandsatBandComboError,
    LandsatBboxError,
    LandsatNoImageryError,
    estimate_payload_mb,
    fetch_landsat_imagery,
)

_PINNED_NOW = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)

# Phoenix, AZ area  --  small AOI inside the guardrail (matches the prototype).
_AZ_BBOX = (-112.10, 33.40, -111.95, 33.50)


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


# A square geometry covering the whole AOI so _select_scene's coverage test passes.
_FULL_COVER_GEOM = {
    "type": "Polygon",
    "coordinates": [
        [
            [-113.0, 33.0],
            [-111.0, 33.0],
            [-111.0, 34.0],
            [-113.0, 34.0],
            [-113.0, 33.0],
        ]
    ],
}


def _fake_item(scene_id: str = "LC08_fake", cc: float = 0.0):
    """A minimal STAC-like item with every band asset this tool may read."""
    assets = {
        k: SimpleNamespace(href=f"https://blob/{k}.tif")
        for k in ("red", "green", "blue", "nir08", "lwir11", "qa_pixel")
    }
    return SimpleNamespace(
        id=scene_id,
        geometry=_FULL_COVER_GEOM,
        properties={"eo:cloud_cover": cc, "platform": "landsat-8"},
        assets=assets,
    )


def _fake_search_client(items):
    """Patchable pystac Client.open that returns a search yielding ``items``."""
    search = SimpleNamespace(items=lambda: list(items))
    client = SimpleNamespace(search=lambda **kw: search)
    return SimpleNamespace(open=staticmethod(lambda root: client))


# ---------------------------------------------------------------------------
# Registration / metadata.
# ---------------------------------------------------------------------------


def test_tool_is_registered() -> None:
    assert "fetch_landsat_imagery" in TOOL_REGISTRY
    meta = TOOL_REGISTRY["fetch_landsat_imagery"].metadata
    assert meta.name == "fetch_landsat_imagery"
    assert meta.ttl_class == "static-30d"
    assert meta.source_class == "landsat_imagery"
    assert meta.cacheable is True
    assert meta.supports_global_query is False
    assert meta.payload_mb_estimator_name == "estimate_payload_mb"


def test_open_world_hint_is_set() -> None:
    """A fetch_* external-API tool must carry open_world_hint."""
    spec = TOOL_REGISTRY["fetch_landsat_imagery"]
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
    with pytest.raises(LandsatBboxError):
        fetch_landsat_imagery(bbox=(-112.0, 33.0, -112.0, 33.0))


def test_out_of_range_bbox_raises() -> None:
    with pytest.raises(LandsatBboxError, match="lon out of"):
        fetch_landsat_imagery(bbox=(-200.0, 33.0, -111.0, 34.0))


def test_nonfinite_bbox_raises() -> None:
    with pytest.raises(LandsatBboxError, match="non-finite"):
        fetch_landsat_imagery(bbox=(float("nan"), 33.0, -111.0, 34.0))


def test_too_large_bbox_raises() -> None:
    with pytest.raises(LandsatBboxError, match="guardrail"):
        fetch_landsat_imagery(bbox=(-114.0, 31.0, -111.0, 34.0))  # 9 deg^2


def test_bbox_error_not_retryable() -> None:
    try:
        fetch_landsat_imagery(bbox=(-112.0, 33.0, -112.0, 33.0))
    except LandsatBboxError as exc:
        assert exc.retryable is False
    else:
        pytest.fail("expected LandsatBboxError")


# ---------------------------------------------------------------------------
# band_combo normalization / validation.
# ---------------------------------------------------------------------------


def test_unknown_band_combo_raises_non_retryable() -> None:
    with pytest.raises(LandsatBandComboError) as ei:
        fetch_landsat_imagery(bbox=_AZ_BBOX, band_combo="sonar")
    assert ei.value.retryable is False


def test_band_combo_aliases_normalize() -> None:
    assert landsat_mod._normalize_band_combo("rgb") == "true_color"
    assert landsat_mod._normalize_band_combo("CIR") == "false_color_nir"
    assert landsat_mod._normalize_band_combo("LST") == "thermal"
    assert landsat_mod._normalize_band_combo(None) == "true_color"


# ---------------------------------------------------------------------------
# Synthetic band readers.
# ---------------------------------------------------------------------------


def _sr_dn(value: float):
    """A surface-reflectance DN array that decodes to a known reflectance.

    ref = DN*2.75e-05 - 0.2  ->  DN = (ref + 0.2) / 2.75e-05.
    """
    return (value + 0.2) / 2.75e-05


def _make_band_reader(*, cloud_rows: int = 0):
    """Return a fake _read_band_window.

    Emits distinct DN per asset so each band differs (a real RGB), a thermal DN
    that decodes to ~30 C, an all-clear qa_pixel (value 0 == nothing flagged),
    and, when ``cloud_rows`` > 0, flags the top ``cloud_rows`` rows as cloud
    (qa bit 3) so the masking path is exercised.
    """

    def reader(signed_href, bbox, w, h, *, nearest=False):
        href = signed_href
        if "qa_pixel" in href:
            qa = np.zeros((h, w), dtype="float32")
            if cloud_rows > 0:
                qa[:cloud_rows, :] = float(1 << 3)  # cloud bit
            return qa
        if "lwir11" in href:
            # T(K) = DN*0.00341802 + 149.0; want ~303 K (30 C) -> DN ~ 45050.
            dn = (303.15 - 149.0) / 0.00341802
            return np.full((h, w), dn, dtype="float32")
        # Reflectance bands: distinct values so the RGB is not gray.
        ref = {"red": 0.20, "green": 0.15, "blue": 0.10, "nir08": 0.40}
        for k, v in ref.items():
            if k in href:
                return np.full((h, w), _sr_dn(v), dtype="float32")
        return np.full((h, w), _sr_dn(0.12), dtype="float32")

    return reader


def _run_combo(combo: str, fake, *, cloud_rows: int = 0, item=None):
    rt = _make_read_through_injector(fake)
    items = [item or _fake_item()]
    with patch.object(landsat_mod, "read_through", rt), patch.object(
        landsat_mod._pc_stac, "sas_sign_href", side_effect=lambda href, c: href
    ), patch.object(
        landsat_mod, "_read_band_window", _make_band_reader(cloud_rows=cloud_rows)
    ), patch(
        "pystac_client.Client", _fake_search_client(items)
    ):
        return fetch_landsat_imagery(
            bbox=_AZ_BBOX,
            start_date="2023-06-01",
            end_date="2023-09-30",
            band_combo=combo,
        )


def _decode_rgb(cog_bytes):
    from rasterio.io import MemoryFile

    with MemoryFile(cog_bytes) as mem, mem.open() as src:
        assert src.count == 3
        assert str(src.dtypes[0]) == "uint8"
        return src.read()  # (3, H, W) uint8


# ---------------------------------------------------------------------------
# Happy path per band combo.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "combo,expect_role,expect_units",
    [
        ("true_color", "context", None),
        ("false_color_nir", "context", None),
        ("thermal", "primary", "Land-surface temperature (deg C)"),
    ],
)
def test_band_combo_roundtrips_to_rgb_cog(combo, expect_role, expect_units) -> None:
    fake = _FakeStore()
    layer = _run_combo(combo, fake)

    assert layer.layer_type == "raster"
    assert layer.style_preset == _STYLE_PRESET
    assert layer.role == expect_role
    assert layer.units == expect_units
    assert layer.uri.startswith("s3://")
    assert combo in layer.layer_id

    rgb = _decode_rgb(next(iter(fake.store.values())))
    # The image must paint -- some pixel is non-black.
    assert int(rgb.sum()) > 0
    # true_color bands are distinct -> not a uniform gray frame.
    if combo == "true_color":
        assert not (rgb[0] == rgb[2]).all()


def test_thermal_is_multiband_passthrough() -> None:
    """The 3-band RGB COG triggers publish_layer's RGBA/multiband passthrough
    (band count >= 3 -> rendered directly, no single-band rescale)."""
    from trid3nt_server.tools.publish_layer import _is_rgba_or_multiband

    fake = _FakeStore()
    _run_combo("thermal", fake)
    cog = next(iter(fake.store.values()))
    assert _is_rgba_or_multiband(cog) is True


def test_cloud_pixels_are_masked_black() -> None:
    """qa_pixel cloud bits -> those pixels emit as black no-data."""
    fake = _FakeStore()
    _run_combo("true_color", fake, cloud_rows=3)
    rgb = _decode_rgb(next(iter(fake.store.values())))
    # Top 3 rows were flagged cloud -> fully black; a lower row paints.
    assert int(rgb[:, 0, :].sum()) == 0
    assert int(rgb[:, -1, :].sum()) > 0


# ---------------------------------------------------------------------------
# Honest no-imagery / all-cloud paths.
# ---------------------------------------------------------------------------


def test_no_scene_raises_no_imagery_non_retryable() -> None:
    fake = _FakeStore()
    rt = _make_read_through_injector(fake)
    with patch.object(landsat_mod, "read_through", rt), patch(
        "pystac_client.Client", _fake_search_client([])
    ):
        with pytest.raises(LandsatNoImageryError) as ei:
            fetch_landsat_imagery(
                bbox=_AZ_BBOX, start_date="2023-06-01", end_date="2023-09-30"
            )
    assert ei.value.retryable is False
    assert not fake.store  # nothing fabricated / cached


def test_all_cloud_window_raises_no_imagery() -> None:
    """A fully-clouded scene leaves no clear pixel -> honest no-imagery."""
    fake = _FakeStore()
    # cloud_rows large enough to cover the whole frame.
    with pytest.raises(LandsatNoImageryError):
        _run_combo("true_color", fake, cloud_rows=10_000)


# ---------------------------------------------------------------------------
# Cache determinism.
# ---------------------------------------------------------------------------


def test_cache_hit_does_not_refetch() -> None:
    fake = _FakeStore()
    rt = _make_read_through_injector(fake)
    calls = {"n": 0}
    base_reader = _make_band_reader()

    def counting_reader(href, bbox, w, h, *, nearest=False):
        calls["n"] += 1
        return base_reader(href, bbox, w, h, nearest=nearest)

    with patch.object(landsat_mod, "read_through", rt), patch.object(
        landsat_mod._pc_stac, "sas_sign_href", side_effect=lambda href, c: href
    ), patch.object(landsat_mod, "_read_band_window", counting_reader), patch(
        "pystac_client.Client", _fake_search_client([_fake_item()])
    ):
        fetch_landsat_imagery(
            bbox=_AZ_BBOX, start_date="2023-06-01", end_date="2023-09-30"
        )
        first = calls["n"]
        fetch_landsat_imagery(
            bbox=_AZ_BBOX, start_date="2023-06-01", end_date="2023-09-30"
        )
        second = calls["n"]

    assert first > 0
    assert second == first  # second call served from cache, no re-read


def test_distinct_band_combo_distinct_cache_key() -> None:
    fake = _FakeStore()
    _run_combo("true_color", fake)
    _run_combo("thermal", fake)
    # Two combos -> two distinct cached objects.
    assert len(fake.store) == 2
