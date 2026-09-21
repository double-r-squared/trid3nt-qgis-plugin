"""Unit tests for the four data-fetch atomic tools.

Each registers with its expected TTL class, source class and cacheable flag; the
bbox quantizer is deterministic, so two callers in one grid cell canonicalize
identically; each tool routes through the cache read-through against mocked
upstreams; and a bad bbox or a failing upstream refuses with NO sentinel."""

from __future__ import annotations

import math
import os
from datetime import datetime, timezone
from typing import Any

import pytest
import requests

from trid3nt_server.tools import TOOL_REGISTRY
# fetch_dem twin DELETED (library_delegate fold); its value-bearing
# tests migrated to test_router_dem.py.
# fetch_buildings twin DELETED (buildings sidecar-write fold); its value-bearing
# tests migrated to test_router_buildings.py.
from trid3nt_server.tools.fetchers.socioeconomic.geocode_location import geocode_location as geo_mod
# fetch_population twin DELETED (WorldPop library_delegate fold; the half-built
# ACS leg dropped); its value-bearing WorldPop tests migrated to test_router_population.py.
# fetch_river_geometry twin DELETED (river fold); its value-bearing tests
# migrated to test_router_river.py.
from trid3nt_server.tools.fetchers.climate.lookup_precip_return_period import lookup_precip_return_period as pfd_mod
from trid3nt_server.tools.fetchers._fetch_common import (
    BboxInvalidError,
    UpstreamAPIError,
    round_bbox_to_resolution,
)
from trid3nt_server.tools.fetchers.socioeconomic.geocode_location.geocode_location import (
    GeocodeNoMatchError,
    GeocodeUnconfirmableError,
    geocode_location,
)



#: Every data_fetch descendant module; ``read_through`` is bound per-module at
#: import time, so cache-shim patches must hit all of them.
_ALL_FETCH_MODS = (geo_mod, pfd_mod)


def _setattr_all_fetch(monkeypatch, name, value):
    for _m in _ALL_FETCH_MODS:
        monkeypatch.setattr(_m, name, value)


# Fort Myers, FL — small bbox for live + mocked path testing.
FORT_MYERS_BBOX = (-81.92, 26.55, -81.80, 26.68)
PINNED_NOW = datetime(2026, 6, 6, 12, 0, 0, tzinfo=timezone.utc)


class _S3Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class FakeStorageClient:
    """In-memory S3 double; ``store`` is keyed by object KEY.

    Returns the per-test instance the autouse fixture installs, so the tool's real
    boto3 read-through reads and writes the store the test inspects."""

    _active: "FakeStorageClient | None" = None

    def __new__(cls) -> "FakeStorageClient":
        if cls._active is not None:
            return cls._active
        return super().__new__(cls)

    def __init__(self) -> None:
        if getattr(self, "_init", False):
            return
        self._init = True
        self.store: dict[str, bytes] = {}
        self.last_put: dict | None = None

    def get_object(self, *, Bucket, Key):
        from botocore.exceptions import ClientError

        try:
            data = self.store[Key]
        except KeyError:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "not found"}},
                "GetObject",
            )
        return {"Body": _S3Body(data)}

    def put_object(self, *, Bucket, Key, Body, ContentType=None):
        data = Body.read() if hasattr(Body, "read") else Body
        self.store[Key] = data
        self.last_put = {"Bucket": Bucket, "Key": Key, "ContentType": ContentType}
        return {}


@pytest.fixture(autouse=True)
def _route_cache_to_inmemory_s3(monkeypatch):
    """Route boto3 S3 (the cache shim's only object store) to an in-memory double."""
    import boto3

    FakeStorageClient._active = None
    client = FakeStorageClient()
    FakeStorageClient._active = client

    def _factory(service_name, *a, **k):
        assert service_name == "s3"
        return client

    monkeypatch.setattr(boto3, "client", _factory)
    try:
        yield client
    finally:
        FakeStorageClient._active = None




def test_fetch_buildings_is_registered_with_static_30d():
    entry = TOOL_REGISTRY["fetch_buildings"]
    assert entry.metadata.ttl_class == "static-30d"
    assert entry.metadata.source_class == "buildings"
    assert entry.metadata.cacheable is True


def test_fetch_population_is_registered_with_static_30d():
    entry = TOOL_REGISTRY["fetch_population"]
    assert entry.metadata.ttl_class == "static-30d"
    assert entry.metadata.source_class == "population"
    assert entry.metadata.cacheable is True


def test_geocode_location_is_registered_with_dynamic_1h():
    entry = TOOL_REGISTRY["geocode_location"]
    assert entry.metadata.ttl_class == "dynamic-1h"
    assert entry.metadata.source_class == "geocode"
    assert entry.metadata.cacheable is True


def test_registry_contains_job_0039_subset_after_eager_import():
    """The fetchers this file imports are registered after the eager import.

    In-process the registry holds only what the package's own imports plus this
    file's explicit ones fire; the startup-only surface is asserted at startup."""
    names = set(TOOL_REGISTRY.keys())
    expected_subset = {
        "fetch_dem",
        "fetch_buildings",
        "fetch_population",
        "geocode_location",
        # Spec-driven surfaces:
        "fetch_landcover",
        "fetch_river_geometry",
        "lookup_precip_return_period",
    }
    assert expected_subset.issubset(names), f"missing: {expected_subset - names}"
    # 4 M4 fetchers + 3 new fetchers = 7 minimum in test context; >= 7
    # tolerates solver / pipeline-emitter imports landing in parallel.
    assert len(names) >= 7




def test_round_bbox_to_resolution_is_deterministic():
    """Two calls with the same bbox + resolution produce identical output."""
    q1 = round_bbox_to_resolution(FORT_MYERS_BBOX, 10)
    q2 = round_bbox_to_resolution(FORT_MYERS_BBOX, 10)
    assert q1 == q2


def test_round_bbox_to_resolution_collapses_floating_point_jitter():
    """Two callers whose bbox edges differ by sub-meter floats hit the same key.

    This is the dedup-via-quantization property: 1e-7 degrees of jitter
    (sub-meter) at 10m resolution should snap to the same grid cell.
    """
    base = (-81.9000001, 26.5500001, -81.8000001, 26.6800001)
    jitter = (-81.9000002, 26.5500002, -81.8000002, 26.6800002)
    qb = round_bbox_to_resolution(base, 10)
    qj = round_bbox_to_resolution(jitter, 10)
    assert qb == qj


def test_round_bbox_to_resolution_envelopes_input():
    """The quantized bbox covers (>=) the input bbox on all sides."""
    q = round_bbox_to_resolution(FORT_MYERS_BBOX, 30)
    assert q[0] <= FORT_MYERS_BBOX[0]
    assert q[1] <= FORT_MYERS_BBOX[1]
    assert q[2] >= FORT_MYERS_BBOX[2]
    assert q[3] >= FORT_MYERS_BBOX[3]


def test_round_bbox_to_resolution_rejects_degenerate_bbox():
    with pytest.raises(BboxInvalidError):
        round_bbox_to_resolution((-81.9, 26.5, -81.9, 26.6), 10)  # min_lon == max_lon


def test_round_bbox_to_resolution_rejects_out_of_range_lat():
    with pytest.raises(BboxInvalidError):
        round_bbox_to_resolution((-81.9, -95.0, -81.8, 26.6), 10)





def test_geocode_location_happy_path(monkeypatch):
    fake_storage = FakeStorageClient()
    from trid3nt_server.tools import cache as cache_mod
    import json as _json

    fake_payload = {
        "name": "Fort Myers, Lee County, Florida, United States",
        "latitude": 26.6406,
        "longitude": -81.8723,
        "bbox": [-81.93, 26.55, -81.78, 26.71],
        "source": "nominatim",
        "query": "Fort Myers, FL",
        "osm_type": "relation",
        "osm_id": 12345,
        "place_id": 67890,
    }
    monkeypatch.setattr(
        geo_mod,
        "_fetch_nominatim_geocode_bytes",
        lambda query: _json.dumps(fake_payload).encode("utf-8"),
    )
    _setattr_all_fetch(monkeypatch, "read_through",
        lambda *a, **kw: cache_mod.read_through(
            *a, storage_client=fake_storage, now=PINNED_NOW, **kw
        ),
    )

    monkeypatch.setattr(
        "trid3nt_server.render.pipeline_emitter.current_emitter", lambda: object()
    )

    result = geocode_location("Fort Myers, FL")
    assert result["source"] == "nominatim"
    assert result["bbox"] == [-81.93, 26.55, -81.78, 26.71]
    assert "Fort Myers" in result["name"]
    # No s3:// URI leaks into the returned payload (Tier separation).
    assert "s3://" not in str(result)


def test_geocode_location_rejects_empty_query():
    with pytest.raises(BboxInvalidError):
        geocode_location("   ")


# geocode_location -- STRICT (R31): the query travels as written, the answer
# is exactly what the service gives back. No state table, no snap, no
# qualifier stripping, no result-class reorder, no AOI-floor expansion.


def _bind_session(monkeypatch):
    """Bind a stand-in session: the geocoder refuses with nobody to confirm."""
    monkeypatch.setattr(
        "trid3nt_server.render.pipeline_emitter.current_emitter", lambda: object()
    )


def _bind_geocode_cache(monkeypatch):
    """Wire read_through to a fresh fake-storage client (shared test plumbing)."""
    fake_storage = FakeStorageClient()
    from trid3nt_server.tools import cache as cache_mod

    _setattr_all_fetch(monkeypatch, "read_through",
        lambda *a, **kw: cache_mod.read_through(
            *a, storage_client=fake_storage, now=PINNED_NOW, **kw
        ),
    )


class _FakeLocation:
    def __init__(self, raw, latitude, longitude):
        self.raw = raw
        self.latitude = latitude
        self.longitude = longitude


def _geocode_stub(monkeypatch, location):
    """Stub geo_mod._client().geocode to return ``location`` (or None) and
    capture the exact query it was called with."""
    calls: list[str] = []

    class _Client:
        def geocode(self, query, **kw):
            calls.append(query)
            return location

    monkeypatch.setattr(geo_mod, "_client", lambda: _Client())
    return calls


def test_geocode_sends_the_query_verbatim_no_stripping_or_detection(monkeypatch):
    """"south Florida" reaches geopy UNCHANGED -- no directional-qualifier
    strip, no state table lookup, no rewrite of any kind."""
    _bind_session(monkeypatch)
    _bind_geocode_cache(monkeypatch)
    calls = _geocode_stub(monkeypatch, _FakeLocation(
        {"display_name": "South Florida", "boundingbox": ["24.4", "27.0", "-82.0", "-80.0"],
         "osm_type": "relation", "osm_id": 1, "place_id": 1},
        25.7, -81.0,
    ))
    geocode_location("south Florida")
    assert calls == ["south Florida"]


def test_geocode_returns_the_services_own_answer_unmodified(monkeypatch):
    _bind_session(monkeypatch)
    _bind_geocode_cache(monkeypatch)
    _geocode_stub(monkeypatch, _FakeLocation(
        {"display_name": "Kansas, United States",
         "boundingbox": ["37.0", "40.0", "-102.0", "-94.6"],
         "osm_type": "relation", "osm_id": 99, "place_id": 99},
        38.5, -98.0,
    ))
    # Whatever geopy answers is returned as-is -- even a surprising match --
    # because this tool states no opinion of its own about the query.
    result = geocode_location("south Florida")
    assert result["name"] == "Kansas, United States"
    assert result["bbox"] == [-102.0, 37.0, -94.6, 40.0]
    assert result["source"] == "nominatim"


def test_geocode_refuses_when_the_service_finds_nothing(monkeypatch):
    _bind_session(monkeypatch)
    _bind_geocode_cache(monkeypatch)
    _geocode_stub(monkeypatch, None)
    with pytest.raises(GeocodeNoMatchError):
        geocode_location("Atlantis")


def test_geocode_refuses_a_match_with_no_bounding_box(monkeypatch):
    _bind_session(monkeypatch)
    _bind_geocode_cache(monkeypatch)
    _geocode_stub(monkeypatch, _FakeLocation(
        {"display_name": "Nowhere", "boundingbox": []}, 0.0, 0.0,
    ))
    with pytest.raises(GeocodeNoMatchError):
        geocode_location("Atlantis")


def test_geocode_upstream_failure_raises_upstream_error(monkeypatch):
    from geopy.exc import GeocoderServiceError

    _bind_session(monkeypatch)
    _bind_geocode_cache(monkeypatch)

    class _Client:
        def geocode(self, query, **kw):
            raise GeocoderServiceError("timed out")

    monkeypatch.setattr(geo_mod, "_client", lambda: _Client())
    with pytest.raises(UpstreamAPIError):
        geocode_location("Fort Myers, FL")


def test_geocode_refuses_with_no_session_to_confirm_the_match(monkeypatch):
    """Headless, the top match has nobody to look at it, so it is not accepted."""
    _bind_geocode_cache(monkeypatch)
    calls = _geocode_stub(monkeypatch, _FakeLocation(
        {"display_name": "Kansas", "boundingbox": ["36.9", "40.0", "-102.1", "-94.6"]},
        38.5, -98.4,
    ))
    monkeypatch.setattr(
        "trid3nt_server.render.pipeline_emitter.current_emitter", lambda: None
    )
    with pytest.raises(GeocodeUnconfirmableError, match="Kansas"):
        geocode_location("Kansas")
    assert calls == []


def test_geocode_auto_mode_labels_the_match_auto_accepted(monkeypatch):
    _bind_session(monkeypatch)
    _bind_geocode_cache(monkeypatch)
    _geocode_stub(monkeypatch, _FakeLocation(
        {"display_name": "Fort Myers, FL", "boundingbox": ["26.55", "26.71", "-81.93", "-81.78"]},
        26.64, -81.87,
    ))
    result = geocode_location("Fort Myers, FL", input_mode="auto")
    assert result["match_mode"] == "auto-accepted"


def test_geocode_user_gated_labels_pending_confirm(monkeypatch):
    """The tool always returns at once; the case-AOI-commit step is what
    shows the confirm gate on this label, not this call."""
    _bind_session(monkeypatch)
    _bind_geocode_cache(monkeypatch)
    _geocode_stub(monkeypatch, _FakeLocation(
        {"display_name": "Fort Myers, FL", "boundingbox": ["26.55", "26.71", "-81.93", "-81.78"]},
        26.64, -81.87,
    ))
    result = geocode_location("Fort Myers, FL", input_mode="user_gated")
    assert result["match_mode"] == "pending-confirm"




from trid3nt_server.tools.fetchers.climate.lookup_precip_return_period.lookup_precip_return_period import (  # noqa: E402 — after main test surface
    lookup_precip_return_period,
)
# fetch_landcover FOLDED to a spec-driven surface: the twin + its
# twin-internal tests (_fetch_nlcd_landcover_bytes / _landcover_bytes_to_cog /
# _fix_nlcd_background_transparency / _clip_raster_bytes_to_bbox / cache-version
# salt / overview generation) DELETED with the twin. Their value moved to
# tests/fetchers/test_router_landcover.py (the wcs_getcoverage mode + pre_resolve
# + the sidecar envelope, incl. a twin-value-parity gate). The metadata + docstring
# surface (network-free) stays here:


def test_fetch_landcover_is_registered_with_static_30d():
    """Registration assertion: fetch_landcover (spec-driven) keeps its metadata."""
    entry = TOOL_REGISTRY["fetch_landcover"]
    assert entry.metadata.ttl_class == "static-30d"
    assert entry.metadata.source_class == "landcover"
    assert entry.metadata.cacheable is True


def test_fetch_landcover_docstring_records_access_tier():
    """Section F.1.1 docstring discipline: the access tier is named (carried verbatim)."""
    doc = TOOL_REGISTRY["fetch_landcover"].fn.__doc__ or ""
    assert "Access pattern:" in doc
    assert "Tier" in doc

# lookup_precip_return_period (NOAA Atlas 14 PFDS).


def test_lookup_precip_return_period_is_registered_with_static_30d():
    entry = TOOL_REGISTRY["lookup_precip_return_period"]
    assert entry.metadata.ttl_class == "static-30d"
    assert entry.metadata.source_class == "precip_return_period"
    assert entry.metadata.cacheable is True


def test_lookup_precip_return_period_docstring_records_tier_3():
    """Docstring discipline: Tier 3 (direct HTTPS point query)."""
    doc = lookup_precip_return_period.__doc__ or ""
    assert "Access pattern:" in doc
    assert "Tier 3" in doc


# Verbatim Atlas 14 PFDS response for the Fort Myers center.
_ATLAS14_FORT_MYERS_FIXTURE = b"""Point precipitation frequency estimates (inches)
NOAA Atlas 14 Volume 9 Version 2
Data type: Precipitation depth
Time series type: Partial duration
Project area: Southeastern States
Location name (ESRI Maps): None
Station Name: None
Latitude: 26.6 Degree
Longitude: -81.9 Degree
Elevation (USGS): None None


PRECIPITATION FREQUENCY ESTIMATES
by duration for ARI (years):, 1,2,5,10,25,50,100,200,500,1000
5-min:, 0.553,0.620,0.731,0.822,0.950,1.05,1.15,1.25,1.38,1.48
10-min:, 0.810,0.908,1.07,1.20,1.39,1.54,1.68,1.83,2.02,2.17
15-min:, 0.988,1.11,1.30,1.47,1.70,1.87,2.05,2.23,2.47,2.65
30-min:, 1.60,1.79,2.11,2.37,2.74,3.02,3.31,3.60,3.99,4.28
60-min:, 2.14,2.38,2.79,3.13,3.62,4.00,4.38,4.78,5.32,5.74
2-hr:, 2.69,2.98,3.47,3.90,4.49,4.97,5.46,5.97,6.66,7.20
3-hr:, 2.92,3.25,3.81,4.30,4.99,5.54,6.11,6.71,7.53,8.17
6-hr:, 3.23,3.70,4.50,5.18,6.16,6.94,7.75,8.60,9.76,10.7
12-hr:, 3.49,4.18,5.35,6.36,7.79,8.94,10.1,11.3,13.0,14.3
24-hr:, 4.01,4.76,6.09,7.28,9.05,10.5,12.1,13.7,16.1,18.0
2-day:, 4.94,5.57,6.77,7.94,9.80,11.4,13.3,15.3,18.2,20.7
3-day:, 5.43,6.22,7.68,9.02,11.1,12.9,14.8,16.9,19.8,22.3
4-day:, 5.83,6.78,8.43,9.92,12.1,14.0,15.9,18.0,20.9,23.3
7-day:, 7.08,8.10,9.87,11.4,13.7,15.5,17.5,19.5,22.4,24.6
10-day:, 8.28,9.30,11.0,12.6,14.8,16.6,18.5,20.4,23.2,25.4
20-day:, 11.7,12.9,14.8,16.4,18.7,20.4,22.1,23.8,26.1,27.8
30-day:, 14.5,15.9,18.2,20.0,22.4,24.2,25.9,27.5,29.5,30.9
45-day:, 18.0,19.9,22.7,24.9,27.7,29.6,31.4,33.0,34.9,36.2
60-day:, 21.0,23.3,26.6,29.2,32.4,34.6,36.6,38.3,40.3,41.5

Date/time (GMT):  Sun Jun  7 07:54:20 2026
"""


def test_lookup_precip_return_period_happy_path_returns_structured_dict(monkeypatch):
    """100-year 24-hour at Fort Myers center: parsed from the fixture."""
    fake_storage = FakeStorageClient()
    from trid3nt_server.tools import cache as cache_mod

    monkeypatch.setattr(
        pfd_mod,
        "_fetch_atlas14_pfds_bytes",
        lambda lat, lon: _ATLAS14_FORT_MYERS_FIXTURE,
    )
    _setattr_all_fetch(monkeypatch, "read_through",
        lambda *a, **kw: cache_mod.read_through(
            *a, storage_client=fake_storage, now=PINNED_NOW, **kw
        ),
    )

    result = lookup_precip_return_period(
        location=(26.6, -81.9), return_period_years=100, duration_hours=24.0
    )
    assert result["precip_inches"] == pytest.approx(12.1)
    assert result["units"] == "inches"
    assert result["return_period_years"] == 100
    assert result["duration_hours"] == 24.0
    assert "Volume 9" in result["vintage_volume"]
    assert "Southeastern" in result["project_area"]
    assert result["source"] == "noaa-atlas14-pfds"
    # Quantized location echoed back.
    assert len(result["location"]) == 2


def test_lookup_precip_return_period_quantizes_location_to_atlas14_grid(monkeypatch):
    """Per-source quantization (acceptance criterion 3): 1/120 degree native grid.

    Two callers within the same Atlas 14 grid cell hit the same cache entry.
    """
    fake_storage = FakeStorageClient()
    from trid3nt_server.tools import cache as cache_mod

    fetch_calls: list[tuple[float, float]] = []

    def _capturing_fetch(lat, lon):
        fetch_calls.append((lat, lon))
        return _ATLAS14_FORT_MYERS_FIXTURE

    monkeypatch.setattr(pfd_mod, "_fetch_atlas14_pfds_bytes", _capturing_fetch)
    _setattr_all_fetch(monkeypatch, "read_through",
        lambda *a, **kw: cache_mod.read_through(
            *a, storage_client=fake_storage, now=PINNED_NOW, **kw
        ),
    )

    # Two locations within the same 1/120-degree grid cell (~278 m apart at
    # 26.6 latitude — 1/120 degree ≈ 309 m).
    r1 = lookup_precip_return_period(
        location=(26.6, -81.9), return_period_years=100, duration_hours=24.0
    )
    r2 = lookup_precip_return_period(
        location=(26.6005, -81.9005), return_period_years=100, duration_hours=24.0
    )
    assert r1["location"] == r2["location"]
    # Only one cache miss (second call hits the cache).
    assert len(fetch_calls) == 1
    assert len(fake_storage.store) == 1


def test_lookup_precip_return_period_rejects_unsupported_return_period():
    with pytest.raises(BboxInvalidError):
        lookup_precip_return_period(
            location=(26.6, -81.9), return_period_years=300, duration_hours=24.0
        )


def test_lookup_precip_return_period_rejects_unsupported_duration():
    with pytest.raises(BboxInvalidError):
        lookup_precip_return_period(
            location=(26.6, -81.9), return_period_years=100, duration_hours=1.5
        )


def test_lookup_precip_return_period_writes_csv_through_cache(monkeypatch):
    """FR-CE-8: the PFDS CSV is cached under cache/static-30d/precip_return_period/."""
    fake_storage = FakeStorageClient()
    from trid3nt_server.tools import cache as cache_mod

    monkeypatch.setattr(
        pfd_mod,
        "_fetch_atlas14_pfds_bytes",
        lambda lat, lon: _ATLAS14_FORT_MYERS_FIXTURE,
    )
    _setattr_all_fetch(monkeypatch, "read_through",
        lambda *a, **kw: cache_mod.read_through(
            *a, storage_client=fake_storage, now=PINNED_NOW, **kw
        ),
    )

    lookup_precip_return_period(
        location=(26.6, -81.9), return_period_years=100, duration_hours=24.0
    )
    paths = list(fake_storage.store.keys())
    assert len(paths) == 1
    assert paths[0].startswith("cache/static-30d/precip_return_period/")
    assert paths[0].endswith(".csv")
    assert b"NOAA Atlas 14" in fake_storage.store[paths[0]]
    # GCP decommissioned: TTL eviction is an S3 bucket-lifecycle rule (no
    # per-object customTime); assert the boto3 put landed instead.
    assert fake_storage.last_put is not None
