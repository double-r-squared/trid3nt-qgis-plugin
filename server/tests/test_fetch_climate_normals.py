"""Unit tests for the ``fetch_climate_normals`` atomic tool.

Coverage:
- Tool is registered in TOOL_REGISTRY with expected metadata + payload estimator.
- Inventory parsing: fixed-width GHCN-Daily-style rows -> station dicts.
- Station discovery: stations inside the bbox returned; out-of-bbox excluded.
- Per-station CSV parse: annual temp + precip normals extracted; NCEI -9999
  sentinel mapped to None; precip-only stations kept with temp=None.
- FlatGeobuf serialization with a synthetic multi-station sample; round-trips.
- Honest-empty: no stations in bbox -> ClimateNormalsEmptyError(retryable=False);
  zero records after fetch -> ClimateNormalsEmptyError.
- Input validation: bad bbox shapes, degenerate bbox, out-of-range coords.
- Upstream error mapping: HTTP 5xx -> ClimateNormalsUpstreamError(retryable=True).
- Cache miss -> fetch_fn invoked; cache hit -> fetch_fn skipped.
- LayerURI shape: layer_type="vector", role="context", units="mixed", s3:// uri.
- Live (env TRID3NT_TEST_LIVE_NORMALS=1): real NCEI returns >=1 station with
  annual normals for the Tampa Bay area; FGB round-trips; coords in US envelope.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.fetchers.climate.fetch_climate_normals import (
    ClimateNormalsEmptyError,
    ClimateNormalsInputError,
    ClimateNormalsUpstreamError,
    _discover_stations_in_bbox,
    _fetch_station_normals,
    _parse_inventory,
    _parse_station_csv,
    _records_to_fgb,
    estimate_payload_mb,
    fetch_climate_normals,
)

_MOD = "trid3nt_server.tools.fetchers.climate.fetch_climate_normals"

_PINNED_NOW = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)

_LIVE = os.environ.get("TRID3NT_TEST_LIVE_NORMALS") == "1"

# Tampa Bay area bbox — has both first-order (temp+precip) and precip-only stations.
_TAMPA_BBOX = (-82.7, 27.7, -82.2, 28.2)


# ---------------------------------------------------------------------------
# In-memory read-through injector (S3-only; GCP decommissioned).
# ---------------------------------------------------------------------------


class _FakeStore:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}


def _make_read_through_injector(fake):
    from trid3nt_server.tools.cache import (
        CACHE_BUCKET,
        ReadThroughResult,
        cache_path,
        compute_cache_key,
        is_cacheable,
    )

    store = fake.store

    def patched(metadata, params, ext, fetch_fn, **kw):
        bucket = kw.get("bucket") or CACHE_BUCKET
        source_id = kw.get("source_id") or (metadata.source_class or metadata.name)
        force_refresh = kw.get("force_refresh", False)
        if not is_cacheable(metadata):
            return ReadThroughResult(uri=None, data=fetch_fn(), hit=False)
        key = compute_cache_key(source_id, params, metadata.ttl_class, now=_PINNED_NOW)
        path = cache_path(metadata.source_class, metadata.ttl_class, key, ext)
        uri = f"s3://{bucket}/{path}"
        if not force_refresh and path in store:
            return ReadThroughResult(uri=uri, data=store[path], hit=True)
        data = fetch_fn()
        store[path] = data
        return ReadThroughResult(uri=uri, data=data, hit=False)

    return patched


# ---------------------------------------------------------------------------
# Synthetic NCEI fixtures.
# ---------------------------------------------------------------------------

# Fixed-width inventory rows (GHCN-Daily layout the tool slices):
#   id[0:11] lat[12:20] lon[21:30] elev[30:37] state[38:40] name[41:71]
# Build them column-exact so _parse_inventory's slices line up.
def _inv_row(sid: str, lat: float, lon: float, elev: float, state: str, name: str) -> str:
    return (
        f"{sid:<11} "
        f"{lat:>8.4f} "
        f"{lon:>9.4f} "
        f"{elev:>6.1f} "
        f"{state:<2} "
        f"{name:<30}"
    )


# Three stations inside the Tampa bbox, one far outside (Alaska).
_SYNTHETIC_INVENTORY = "\n".join([
    _inv_row("USW00012842", 27.9620, -82.5400, 5.8, "FL", "TAMPA INTL AP"),
    _inv_row("USC00087886", 27.7700, -82.6300, 3.4, "FL", "ST PETERSBURG"),
    _inv_row("US1FLHB0010", 27.8860, -82.4900, 12.0, "FL", "TAMPA 5.1 S"),
    _inv_row("USW00026451", 61.1700, -150.0200, 35.0, "AK", "ANCHORAGE INTL AP"),
]).encode("utf-8")


def _station_csv(
    sid: str,
    lat: float,
    lon: float,
    elev: float,
    name: str,
    tavg: float | str,
    tmin: float | str,
    tmax: float | str,
    prcp: float | str,
) -> bytes:
    header = (
        '"STATION","LATITUDE","LONGITUDE","ELEVATION","NAME",'
        '"ANN-TAVG-NORMAL","ANN-TMIN-NORMAL","ANN-TMAX-NORMAL","ANN-PRCP-NORMAL"'
    )
    # NAME is quoted in the real NCEI CSVs (it contains a comma, e.g.
    # "TAMPA INTL AP, FL US"); quote it here so the CSV columns align.
    row = f'"{sid}",{lat},{lon},{elev},"{name}",{tavg},{tmin},{tmax},{prcp}'
    return (header + "\n" + row + "\n").encode("utf-8")


# Per-station CSV bytes keyed by station id. ST PETERSBURG is precip-only
# (temp = -9999 sentinel). The Alaska station is never requested (out of bbox).
_SYNTHETIC_STATION_CSVS = {
    "USW00012842": _station_csv(
        "USW00012842", 27.9620, -82.5400, 5.8, "TAMPA INTL AP, FL US",
        74.5, 65.0, 84.0, 49.48,
    ),
    "USC00087886": _station_csv(
        "USC00087886", 27.7700, -82.6300, 3.4, "ST PETERSBURG, FL US",
        -9999, -9999, -9999, 52.48,
    ),
    "US1FLHB0010": _station_csv(
        "US1FLHB0010", 27.8860, -82.4900, 12.0, "TAMPA 5.1 S, FL US",
        -9999, -9999, -9999, 56.92,
    ),
}


def _fake_http_get(url: str, timeout: float) -> bytes:  # noqa: ARG001
    """Stand-in for ``_http_get``: serves inventory + per-station CSVs."""
    if url.endswith("inventory_30yr.txt"):
        return _SYNTHETIC_INVENTORY
    for sid, body in _SYNTHETIC_STATION_CSVS.items():
        if url.endswith(f"/{sid}.csv"):
            return body
    raise ClimateNormalsUpstreamError(f"NCEI returned HTTP 404 for {url}: Not Found")


# ---------------------------------------------------------------------------
# Registration / metadata.
# ---------------------------------------------------------------------------


def test_tool_is_registered():
    assert "fetch_climate_normals" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["fetch_climate_normals"]
    meta = entry.metadata if hasattr(entry, "metadata") else entry[1]
    assert meta.name == "fetch_climate_normals"
    assert meta.ttl_class == "static-30d"
    assert meta.source_class == "climate_normals"
    assert meta.cacheable is True


def test_payload_estimator_positive_float():
    val = estimate_payload_mb(bbox=_TAMPA_BBOX)
    assert isinstance(val, float)
    assert val > 0.0


def test_payload_estimator_none_bbox():
    val = estimate_payload_mb(bbox=None)
    assert isinstance(val, float)
    assert val > 0.0


def test_payload_larger_bbox_is_larger():
    small = estimate_payload_mb(bbox=(-82.5, 27.8, -82.4, 27.9))
    big = estimate_payload_mb(bbox=(-90.0, 25.0, -80.0, 35.0))
    assert big > small


# ---------------------------------------------------------------------------
# Inventory parsing + station discovery.
# ---------------------------------------------------------------------------


def test_parse_inventory_yields_all_rows():
    stations = _parse_inventory(_SYNTHETIC_INVENTORY)
    assert len(stations) == 4
    by_id = {s["sid"]: s for s in stations}
    assert "USW00012842" in by_id
    tampa = by_id["USW00012842"]
    assert tampa["lat"] == pytest.approx(27.9620, abs=1e-3)
    assert tampa["lon"] == pytest.approx(-82.5400, abs=1e-3)
    assert tampa["state"] == "FL"
    assert "TAMPA" in tampa["name"]


def test_discover_filters_to_bbox():
    stations = _discover_stations_in_bbox(_TAMPA_BBOX, inv_bytes=_SYNTHETIC_INVENTORY)
    ids = {s["sid"] for s in stations}
    # Three FL stations inside; Alaska excluded.
    assert ids == {"USW00012842", "USC00087886", "US1FLHB0010"}


def test_discover_empty_when_ocean_bbox():
    stations = _discover_stations_in_bbox(
        (-40.0, -10.0, -39.0, -9.0), inv_bytes=_SYNTHETIC_INVENTORY
    )
    assert stations == []


def test_discover_empty_inventory_raises_upstream():
    with pytest.raises(ClimateNormalsUpstreamError):
        _discover_stations_in_bbox(_TAMPA_BBOX, inv_bytes=b"")


# ---------------------------------------------------------------------------
# Per-station CSV parse.
# ---------------------------------------------------------------------------


def test_parse_station_csv_extracts_normals():
    norm = _parse_station_csv(_SYNTHETIC_STATION_CSVS["USW00012842"])
    assert norm is not None
    assert norm["tavg"] == pytest.approx(74.5)
    assert norm["prcp"] == pytest.approx(49.48)
    assert norm["tmin"] == pytest.approx(65.0)
    assert norm["tmax"] == pytest.approx(84.0)
    assert "TAMPA" in norm["name"]


def test_parse_station_csv_sentinel_maps_to_none():
    norm = _parse_station_csv(_SYNTHETIC_STATION_CSVS["USC00087886"])
    assert norm is not None
    # Temp normals are the -9999 sentinel -> None; precip present.
    assert norm["tavg"] is None
    assert norm["tmin"] is None
    assert norm["tmax"] is None
    assert norm["prcp"] == pytest.approx(52.48)


def test_parse_station_csv_empty_returns_none():
    assert _parse_station_csv(b"") is None
    assert _parse_station_csv(b"   \n") is None


# ---------------------------------------------------------------------------
# Fetch records + FlatGeobuf serialization.
# ---------------------------------------------------------------------------


def test_fetch_station_normals_builds_records():
    with patch(f"{_MOD}._http_get", side_effect=_fake_http_get):
        stations = _discover_stations_in_bbox(
            _TAMPA_BBOX, inv_bytes=_SYNTHETIC_INVENTORY
        )
        records = _fetch_station_normals(stations)
    assert len(records) == 3
    by_id = {r["station_id"]: r for r in records}
    # First-order station has temp + precip.
    assert by_id["USW00012842"]["normal_temp_f"] == pytest.approx(74.5)
    assert by_id["USW00012842"]["normal_precip_in"] == pytest.approx(49.48)
    # Precip-only station kept with temp None.
    assert by_id["USC00087886"]["normal_temp_f"] is None
    assert by_id["USC00087886"]["normal_precip_in"] == pytest.approx(52.48)


def test_records_to_fgb_roundtrips():
    import geopandas as gpd

    with patch(f"{_MOD}._http_get", side_effect=_fake_http_get):
        stations = _discover_stations_in_bbox(
            _TAMPA_BBOX, inv_bytes=_SYNTHETIC_INVENTORY
        )
        records = _fetch_station_normals(stations)
    fgb = _records_to_fgb(records)
    assert isinstance(fgb, bytes)
    assert len(fgb) > 0

    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".fgb") as f:
        f.write(fgb)
        f.flush()
        gdf = gpd.read_file(f.name)
    assert len(gdf) == 3
    assert gdf.crs is not None and gdf.crs.to_epsg() == 4326
    for col in ("station_id", "name", "normal_temp_f", "normal_precip_in"):
        assert col in gdf.columns
    assert (gdf.geometry.geom_type == "Point").all()


def test_records_to_fgb_empty_raises_empty():
    with pytest.raises(ClimateNormalsEmptyError):
        _records_to_fgb([])


def test_fetch_skips_stations_without_normals():
    """A station whose access file 404s (or is all-sentinel) is dropped."""
    inv = "\n".join([
        _inv_row("USW00012842", 27.9620, -82.5400, 5.8, "FL", "TAMPA INTL AP"),
        _inv_row("US1FLZZ9999", 27.8000, -82.5000, 1.0, "FL", "NO ACCESS FILE"),
    ]).encode("utf-8")
    with patch(f"{_MOD}._http_get", side_effect=_fake_http_get):
        stations = _discover_stations_in_bbox(_TAMPA_BBOX, inv_bytes=inv)
        records = _fetch_station_normals(stations)
    # ZZ9999 has no CSV in the fixture (-> 404) and is skipped.
    assert {r["station_id"] for r in records} == {"USW00012842"}


# ---------------------------------------------------------------------------
# Input validation.
# ---------------------------------------------------------------------------


def test_bbox_wrong_length_raises():
    with pytest.raises(ClimateNormalsInputError):
        fetch_climate_normals((1.0, 2.0, 3.0))  # type: ignore[arg-type]


def test_degenerate_bbox_raises():
    with pytest.raises(ClimateNormalsInputError):
        fetch_climate_normals((-82.0, 27.0, -82.0, 28.0))  # min_lon == max_lon


def test_inverted_bbox_raises():
    with pytest.raises(ClimateNormalsInputError):
        fetch_climate_normals((-81.0, 28.0, -82.0, 27.0))  # min > max


def test_out_of_range_lat_raises():
    with pytest.raises(ClimateNormalsInputError):
        fetch_climate_normals((-82.0, 27.0, -81.0, 95.0))  # lat > 90


def test_nonfinite_bbox_raises():
    with pytest.raises(ClimateNormalsInputError):
        fetch_climate_normals((-82.0, 27.0, float("nan"), 28.0))


# ---------------------------------------------------------------------------
# Typed-error retryability contract.
# ---------------------------------------------------------------------------


def test_input_error_not_retryable():
    assert ClimateNormalsInputError("x").retryable is False


def test_upstream_error_retryable():
    assert ClimateNormalsUpstreamError("x").retryable is True


def test_empty_error_not_retryable():
    assert ClimateNormalsEmptyError("x").retryable is False


# ---------------------------------------------------------------------------
# Honest-empty end-to-end (no stations in bbox).
# ---------------------------------------------------------------------------


def test_ocean_bbox_raises_empty_error():
    fake = _FakeStore()
    with patch(f"{_MOD}._http_get", side_effect=_fake_http_get), patch(
        f"{_MOD}.read_through",
        side_effect=_make_read_through_injector(fake),
    ):
        with pytest.raises(ClimateNormalsEmptyError):
            fetch_climate_normals((-40.0, -10.0, -39.0, -9.0))


# ---------------------------------------------------------------------------
# Mocked end-to-end + LayerURI shape + cache hit/miss.
# ---------------------------------------------------------------------------


def test_end_to_end_writes_fgb_and_layer_uri_shape():
    fake = _FakeStore()
    with patch(f"{_MOD}._http_get", side_effect=_fake_http_get), patch(
        f"{_MOD}.read_through",
        side_effect=_make_read_through_injector(fake),
    ):
        layer = fetch_climate_normals(_TAMPA_BBOX)

    assert layer.layer_type == "vector"
    assert layer.role == "context"
    assert layer.units == "mixed"
    assert layer.uri.startswith("s3://")
    assert layer.uri.endswith(".fgb")
    assert "climate_normals" in layer.uri
    assert layer.style_preset == "climate_normals"
    assert layer.layer_id.startswith("climate-normals-")
    assert layer.bbox is not None
    # One object written to the in-memory cache.
    assert len(fake.store) == 1


def test_cache_miss_then_hit_skips_fetch():
    fake = _FakeStore()
    calls = {"n": 0}

    real_fetch = _fetch_station_normals

    def counting_fetch(stations):
        calls["n"] += 1
        return real_fetch(stations)

    with patch(f"{_MOD}._http_get", side_effect=_fake_http_get), patch(
        f"{_MOD}._fetch_station_normals", side_effect=counting_fetch
    ), patch(
        f"{_MOD}.read_through",
        side_effect=_make_read_through_injector(fake),
    ):
        fetch_climate_normals(_TAMPA_BBOX)
        assert calls["n"] == 1
        # Second identical call hits the cache; fetch_fn not re-invoked.
        fetch_climate_normals(_TAMPA_BBOX)
        assert calls["n"] == 1


# ---------------------------------------------------------------------------
# Live test (opt-in).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _LIVE, reason="set TRID3NT_TEST_LIVE_NORMALS=1 to run live")
def test_live_ncei_tampa():
    import tempfile

    import geopandas as gpd

    stations = _discover_stations_in_bbox(_TAMPA_BBOX)
    assert len(stations) >= 1
    records = _fetch_station_normals(stations)
    assert len(records) >= 1
    # At least one station should carry a temperature normal.
    assert any(r["normal_temp_f"] is not None for r in records)
    fgb = _records_to_fgb(records)
    with tempfile.NamedTemporaryFile(suffix=".fgb") as f:
        f.write(fgb)
        f.flush()
        gdf = gpd.read_file(f.name)
    assert len(gdf) >= 1
    # Coordinates inside the Tampa envelope.
    assert gdf.geometry.x.between(-82.7, -82.2).all()
    assert gdf.geometry.y.between(27.7, 28.2).all()
