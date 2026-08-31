"""Unit tests for the 4 data-fetch atomic tools (job-0033, M4 Stage C).

Coverage:
- Each tool's ``@register_tool`` lands a registered entry with the expected
  TTL class + source class + cacheable flag.
- ``round_bbox_to_resolution`` is deterministic and snaps to a stable grid.
- Bbox quantization at a single resolution produces the same canonicalized
  params dict for two callers within the same grid cell.
- ``fetch_dem`` routes through ``read_through`` (mocked GCS + mocked
  py3dep): cache miss invokes the fetcher and returns a ``LayerURI``.
- ``fetch_buildings`` routes through ``read_through`` (mocked GCS + mocked
  Planetary Computer STAC search): no-matching-items raises
  ``UpstreamAPIError`` (no sentinel written).
- ``fetch_population`` routes through ``read_through`` (mocked Census REST
  + mocked GCS): a single-state CONUS bbox yields a FeatureCollection.
- ``geocode_location`` routes through ``read_through`` (mocked Nominatim
  REST + mocked GCS): returns ``{name, bbox, latitude, longitude, source}``
  shape and emits a ``location-resolved``-eligible payload.
- ``BboxInvalidError`` paths (degenerate bbox, out-of-range lat/lon, bbox
  area over guardrail).
- Mocked external-API failures re-raise as ``UpstreamAPIError`` from inside
  ``read_through`` with no sentinel written to the cache.
"""

from __future__ import annotations

import math
import os
from datetime import datetime, timezone
from typing import Any

import pytest
import requests

from trid3nt_server.tools import TOOL_REGISTRY
# fetch_dem twin DELETED (ADR 0097 library_delegate fold); its value-bearing
# tests migrated to test_router_dem.py.
# fetch_buildings twin DELETED (ADR 0084 buildings sidecar-write fold); its value-bearing
# tests migrated to test_router_buildings.py.
from trid3nt_server.tools.fetchers.socioeconomic.geocode_location import geocode_location as geo_mod
# fetch_population twin DELETED (ADR 0092 WorldPop library_delegate fold; the half-built
# ACS leg dropped); its value-bearing WorldPop tests migrated to test_router_population.py.
# fetch_river_geometry twin DELETED (ADR 0074 river fold); its value-bearing tests
# migrated to test_router_river.py.
from trid3nt_server.tools.fetchers.climate.lookup_precip_return_period import lookup_precip_return_period as pfd_mod
from trid3nt_server.tools.fetchers._fetch_common import (
    BboxInvalidError,
    UpstreamAPIError,
    round_bbox_to_resolution,
)
from trid3nt_server.tools.fetchers.socioeconomic.geocode_location.geocode_location import (
    GeocodeNoMatchError,
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
    """In-memory S3 double (GCP decommissioned). ``store`` keyed by object KEY.

    Returns the per-test active instance installed by the autouse
    ``_route_cache_to_inmemory_s3`` fixture so the tool's real S3 read-through
    (boto3) reads/writes the same store the test inspects.
    """

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


# ---------------------------------------------------------------------------
# Registration: every tool lands with the right metadata.
# ---------------------------------------------------------------------------


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
    """job-0039 acceptance: this job's 3 new fetchers are registered + M4 fetchers.

    Inside the test process, the eager-import surface is whatever the test
    module triggers — ``tools/__init__.py`` (FROZEN) + the explicit
    fetcher-module imports at the top of this test file (which fires this
    job's three new ``@register_tool`` decorators alongside the M4 four).
    The startup-only imports (``solver`` from job-0041) are triggered by
    ``main._import_tools_registry()`` — see the ``--startup-only`` evidence
    below for the live ≥11-tool assertion the kickoff calls out.
    """
    names = set(TOOL_REGISTRY.keys())
    expected_subset = {
        "fetch_dem",
        "fetch_buildings",
        "fetch_population",
        "geocode_location",
        # job-0039 (this job):
        "fetch_landcover",
        "fetch_river_geometry",
        "lookup_precip_return_period",
    }
    assert expected_subset.issubset(names), f"missing: {expected_subset - names}"
    # 4 M4 fetchers + 3 new fetchers = 7 minimum in test context; >= 7
    # tolerates solver / pipeline-emitter imports landing in parallel.
    assert len(names) >= 7


# ---------------------------------------------------------------------------
# round_bbox_to_resolution — engine-side quantization (OQ-32-QUANTIZATION-LOCATION).
# ---------------------------------------------------------------------------


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



# ---------------------------------------------------------------------------
# geocode_location — mocked Nominatim.
# ---------------------------------------------------------------------------


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

    result = geocode_location("Fort Myers, FL")
    assert result["source"] == "nominatim"
    assert result["bbox"] == [-81.93, 26.55, -81.78, 26.71]
    assert "Fort Myers" in result["name"]
    # No s3:// URI leaks into the returned payload (Tier separation).
    assert "s3://" not in str(result)


def test_geocode_location_rejects_empty_query():
    with pytest.raises(BboxInvalidError):
        geocode_location("   ")


# ---------------------------------------------------------------------------
# geocode_location — state-snap fallback (NATE directive 2026-06-17).
#
# A vague/regional query ("south Florida") that geocodes to an arbitrary /
# wrong-state OSM feature must snap to the full state bbox with an honest note,
# while a PRECISE in-state query ("Fort Myers, FL") must pass through unchanged.
# ---------------------------------------------------------------------------


def _bind_geocode_cache(monkeypatch):
    """Wire read_through to a fresh fake-storage client (shared test plumbing)."""
    fake_storage = FakeStorageClient()
    from trid3nt_server.tools import cache as cache_mod

    _setattr_all_fetch(monkeypatch, "read_through",
        lambda *a, **kw: cache_mod.read_through(
            *a, storage_client=fake_storage, now=PINNED_NOW, **kw
        ),
    )


# --- _extract_us_state edge cases ------------------------------------------


@pytest.mark.parametrize(
    "query,expected",
    [
        # Directional / qualifier stripping.
        ("south Florida", "Florida"),
        ("protected areas in south Florida", "Florida"),
        ("central Texas", "Texas"),
        ("upstate New York", "New York"),
        ("greater metro Los Angeles California", "California"),
        # F71: vernacular sub-state regions whose TAIL (after qualifier strip)
        # is a full state name resolve via steps (2)/(2b) — NATE's headline
        # "South Florida" case. (Interior-position matches like "the Florida
        # Panhandle" were intentionally NOT added — see the reverted (2c) note
        # in _extract_us_state; the any-position scan regressed "Kansas City, MO"
        # and "the Washington Monument".)
        ("Southern California", "California"),
        ("Central Texas", "Texas"),
        ("South Florida", "Florida"),
        # Full-name match BEFORE directional strip would eat the prefix.
        ("west virginia", "West Virginia"),
        ("north carolina", "North Carolina"),
        ("new mexico", "New Mexico"),
        ("rhode island", "Rhode Island"),
        # Bare state names.
        ("Kansas", "Kansas"),
        ("california", "California"),
        # USPS abbreviation in the "City, ST" idiom.
        ("Fort Myers, FL", "Florida"),
        ("wildfires near Los Angeles, CA", "California"),
        # County form still detects the state.
        ("Lee County Florida", "Florida"),
        # DC variants.
        ("Washington DC", "District of Columbia"),
        ("district of columbia", "District of Columbia"),
    ],
)
def test_extract_us_state_detects(query, expected):
    assert geo_mod._extract_us_state(query) == expected


@pytest.mark.parametrize(
    "query",
    [
        "",
        "   ",
        "Houston",            # city, not a state
        "Gulf of Mexico",     # marine zone by name
        "in the woods",       # "in" must NOT match Indiana (word-boundary guard)
        "or maybe later",     # "or" must NOT match Oregon
        "Canada",             # not a US state
        "Puerto Rico",        # territory — has no offline bbox row
        # BARE dangerous 2-letter words (whole query) must NOT leak a state via
        # resolve_state_code's unconditional 2-letter fast path (step-4 guard).
        "in",
        "or",
        "ok",
        "hi",
        "me",
        "co",
        "la",
        # ...and queries that REDUCE to a bare dangerous word after the
        # leading-qualifier strip ("the or" -> "or").
        "the or",
        "near or",
        # F71 sliding-window guard: a bare dangerous 2-letter word sitting in an
        # INTERIOR position (not head/tail) must STILL NOT leak a state — the
        # full-name scanner matches FULL state names only, never abbreviations.
        "fly in a plane",     # interior "in" must NOT match Indiana
        "this or that thing",  # interior "or" must NOT match Oregon
        "park me here please",  # interior "me" must NOT match Maine
    ],
)
def test_extract_us_state_rejects(query):
    assert geo_mod._extract_us_state(query) is None


def test_extract_us_state_abbreviation_word_boundary_guard():
    """A dangerous bare English word ('in', 'or') is not a state abbreviation.

    But the SAME letters in the comma idiom ('Bloomington, IN') ARE.
    """
    assert geo_mod._extract_us_state("flooding in the valley") is None
    assert geo_mod._extract_us_state("Bloomington, IN") == "Indiana"
    assert geo_mod._extract_us_state("Portland, OR") == "Oregon"
    # Non-string input never raises.
    assert geo_mod._extract_us_state(None) is None  # type: ignore[arg-type]
    assert geo_mod._extract_us_state(42) is None  # type: ignore[arg-type]


# --- offline backstop table plausibility -----------------------------------


@pytest.mark.parametrize(
    "state,lon_lo,lon_hi,lat_lo,lat_hi",
    [
        # (state, expected min_lon range, expected max_lat range) — generous
        # plausibility bands around known cartographic extents.
        ("Florida", -88.0, -79.0, 24.0, 31.5),
        ("California", -125.0, -113.5, 32.0, 42.5),
        ("Texas", -107.5, -93.0, 25.5, 37.0),
        ("Kansas", -102.5, -94.0, 36.5, 40.5),
        ("New York", -80.5, -71.0, 40.0, 45.5),
    ],
)
def test_us_state_bbox_table_plausible(state, lon_lo, lon_hi, lat_lo, lat_hi):
    bbox = geo_mod._US_STATE_BBOX[state]
    min_lon, min_lat, max_lon, max_lat = bbox
    # Canonical ordering.
    assert min_lon < max_lon and min_lat < max_lat
    # Within plausibility bands.
    assert lon_lo <= min_lon <= lon_hi
    assert lon_lo <= max_lon <= lon_hi
    assert lat_lo <= min_lat <= lat_hi
    assert lat_lo <= max_lat <= lat_hi


def test_us_state_bbox_table_has_50_states_plus_dc():
    assert len(geo_mod._US_STATE_BBOX) == 51
    assert "District of Columbia" in geo_mod._US_STATE_BBOX
    # Every row is a valid WGS84 ordered bbox.
    for name, bbox in geo_mod._US_STATE_BBOX.items():
        min_lon, min_lat, max_lon, max_lat = bbox
        assert -180.0 <= min_lon < max_lon <= 180.0, name
        assert -90.0 <= min_lat < max_lat <= 90.0, name


# --- (a) precise in-state query returns precise bbox unchanged --------------


def test_geocode_precise_in_state_query_not_snapped(monkeypatch):
    """'Fort Myers, FL' resolves precisely; centroid is in FL -> no widening."""
    import json as _json

    precise = {
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
        lambda query: _json.dumps(precise).encode("utf-8"),
    )
    _bind_geocode_cache(monkeypatch)

    result = geocode_location("Fort Myers, FL")
    assert result["source"] == "nominatim"
    assert result["bbox"] == [-81.93, 26.55, -81.78, 26.71]
    assert "fallback_reason" not in result


def test_geocode_precise_county_query_not_snapped(monkeypatch):
    """'Lee County Florida' (a county) stays precise — not widened to state."""
    import json as _json

    precise = {
        "name": "Lee County, Florida, United States",
        "latitude": 26.66,
        "longitude": -81.84,
        "bbox": [-82.27, 26.32, -81.56, 26.79],
        "source": "nominatim",
        "query": "Lee County Florida",
        "osm_type": "relation",
        "osm_id": 222,
        "place_id": 333,
    }
    monkeypatch.setattr(
        geo_mod,
        "_fetch_nominatim_geocode_bytes",
        lambda query: _json.dumps(precise).encode("utf-8"),
    )
    _bind_geocode_cache(monkeypatch)

    result = geocode_location("Lee County Florida")
    assert result["source"] == "nominatim"
    assert result["bbox"] == [-82.27, 26.32, -81.56, 26.79]
    assert "fallback_reason" not in result


# --- (b) wrong-state result snaps to the state with honest note -------------


def test_geocode_south_florida_wrong_state_snaps_to_florida(monkeypatch):
    """'south Florida' resolving to KANSAS snaps to FL via the offline table."""
    import json as _json

    # The pathological observed behavior: Nominatim returns a Kansas feature.
    wrong = {
        "name": "Somewhere, Kansas, United States",
        "latitude": 38.5,
        "longitude": -98.0,
        "bbox": [-98.1, 38.4, -97.9, 38.6],
        "source": "nominatim",
        "query": "south Florida",
        "osm_type": "node",
        "osm_id": 999,
        "place_id": 111,
    }
    monkeypatch.setattr(
        geo_mod,
        "_fetch_nominatim_geocode_bytes",
        lambda query: _json.dumps(wrong).encode("utf-8"),
    )
    _bind_geocode_cache(monkeypatch)
    # Force the offline-table path (no live state lookup) for a deterministic
    # bbox assertion.
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **kw: (_ for _ in ()).throw(
            requests.RequestException("offline")
        ),
    )

    result = geocode_location("south Florida")
    assert result["source"] == "state-bbox-fallback"
    assert result["bbox"] == geo_mod._US_STATE_BBOX["Florida"]
    assert result["state_bbox_source"] == "offline-state-table"
    # Honest narration note present and truthful.
    assert "fallback_reason" in result
    assert "Florida" in result["fallback_reason"]
    assert "south Florida" in result["fallback_reason"]
    # Backward-compatible key shape preserved.
    for key in (
        "name", "bbox", "latitude", "longitude", "source", "query",
        "osm_type", "osm_id", "place_id",
    ):
        assert key in result
    assert result["osm_id"] is None
    # Centroid is inside the Florida bbox.
    fl = geo_mod._US_STATE_BBOX["Florida"]
    assert fl[0] <= result["longitude"] <= fl[2]
    assert fl[1] <= result["latitude"] <= fl[3]


def test_geocode_capitalized_south_florida_snaps_to_florida_centroid(monkeypatch):
    """F71 headline: 'South Florida' -> KANSAS hit -> snap; centroid inside FL.

    NATE 2026-06-17 confirmed geocode_location('South Florida') resolved to
    Kansas every time (no comma; the bare-word guard skipped comma-less tokens).
    The fix extracts the full state NAME 'Florida' from the comma-less phrase,
    the Kansas centroid fails the in-state sanity check, and the result snaps to
    the Florida bbox via the state-bbox-fallback. We mock Nominatim to return a
    Kansas hit and assert the snapped bbox's centroid lands inside Florida.
    """
    import json as _json

    kansas_hit = {
        "name": "Some Place, Kansas, United States",
        "latitude": 38.5,
        "longitude": -98.0,
        "bbox": [-98.1, 38.4, -97.9, 38.6],
        "source": "nominatim",
        "query": "South Florida",
        "osm_type": "node",
        "osm_id": 4242,
        "place_id": 5353,
    }
    monkeypatch.setattr(
        geo_mod,
        "_fetch_nominatim_geocode_bytes",
        lambda query: _json.dumps(kansas_hit).encode("utf-8"),
    )
    _bind_geocode_cache(monkeypatch)
    # Force the offline-table path so the bbox/centroid are deterministic.
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **kw: (_ for _ in ()).throw(
            requests.RequestException("offline")
        ),
    )

    result = geocode_location("South Florida")

    # The snap fired with the contracted source + an honest fallback note.
    assert result["source"] == "state-bbox-fallback"
    assert "fallback_reason" in result
    assert "Florida" in result["fallback_reason"]

    # The returned bbox's CENTROID is inside the Florida envelope (the whole
    # point of F71 — it is NOT in Kansas).
    min_lon, min_lat, max_lon, max_lat = result["bbox"]
    cx = 0.5 * (min_lon + max_lon)
    cy = 0.5 * (min_lat + max_lat)
    fl = geo_mod._US_STATE_BBOX["Florida"]
    assert fl[0] <= cx <= fl[2]
    assert fl[1] <= cy <= fl[3]
    # And the reported centroid lat/lon (used to snap the map) is also in FL.
    assert fl[0] <= result["longitude"] <= fl[2]
    assert fl[1] <= result["latitude"] <= fl[3]


def test_geocode_bare_dangerous_word_does_not_snap_to_state(monkeypatch):
    """F71 guard: a bare 'in'/'or' query never resolves to a state (no snap).

    'in' must NOT leak to Indiana and 'or' must NOT leak to Oregon — so when the
    primary geocode of such a token finds no match, the typed GeocodeNoMatchError
    must propagate (no state detected -> no silent snap).
    """

    def _boom(query):
        raise GeocodeNoMatchError(f"Could not locate {query!r}.")

    monkeypatch.setattr(geo_mod, "_fetch_nominatim_geocode_bytes", _boom)
    _bind_geocode_cache(monkeypatch)

    # No state is detected for these bare dangerous words, so the failure is
    # NOT swallowed by a state-snap.
    assert geo_mod._extract_us_state("in") is None
    assert geo_mod._extract_us_state("or") is None
    for q in ("in", "or"):
        with pytest.raises(GeocodeNoMatchError):
            geocode_location(q)


def test_geocode_wrong_state_prefers_live_osm_state_boundary(monkeypatch):
    """When the live state lookup succeeds, the snap uses the OSM admin bbox."""
    import json as _json

    wrong = {
        "name": "Somewhere, Kansas, United States",
        "latitude": 38.5,
        "longitude": -98.0,
        "bbox": [-98.1, 38.4, -97.9, 38.6],
        "source": "nominatim",
        "query": "south Florida",
        "osm_type": "node",
        "osm_id": 999,
        "place_id": 111,
    }
    monkeypatch.setattr(
        geo_mod,
        "_fetch_nominatim_geocode_bytes",
        lambda query: _json.dumps(wrong).encode("utf-8"),
    )
    _bind_geocode_cache(monkeypatch)

    # Nominatim featuretype=state returns the real FL admin boundingbox
    # ([south, north, west, east] strings, per Nominatim convention).
    class _FakeStateResp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return [
                {
                    "boundingbox": ["24.396", "31.001", "-87.635", "-79.974"],
                    "lat": "27.7",
                    "lon": "-83.8",
                }
            ]

    captured = {}

    def _fake_get(url, params=None, headers=None, timeout=None, **_kw):
        captured["params"] = params
        return _FakeStateResp()

    monkeypatch.setattr(requests, "get", _fake_get)

    result = geocode_location("south Florida")
    assert result["source"] == "state-bbox-fallback"
    assert result["state_bbox_source"] == "nominatim-state"
    # bbox normalized to [min_lon, min_lat, max_lon, max_lat].
    assert result["bbox"] == [-87.635, 24.396, -79.974, 31.001]
    # The live state lookup was scoped to the US with featuretype=state.
    assert captured["params"]["countrycodes"] == "us"
    assert captured["params"]["featuretype"] == "state"


# --- (c) no-result + state detected snaps to state --------------------------


def test_geocode_no_result_with_state_detected_snaps(monkeypatch):
    """Nominatim returns nothing, but 'south Florida' has a detectable state.

    GeocodeNoMatchError subclasses UpstreamAPIError, so the state-snap fallback
    STILL fires when a US state is recognized in the query.
    """

    def _boom(query):
        raise GeocodeNoMatchError(f"Could not locate {query!r}.")

    monkeypatch.setattr(geo_mod, "_fetch_nominatim_geocode_bytes", _boom)
    _bind_geocode_cache(monkeypatch)
    # Offline path for deterministic bbox.
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **kw: (_ for _ in ()).throw(
            requests.RequestException("offline")
        ),
    )

    result = geocode_location("protected areas in south Florida")
    assert result["source"] == "state-bbox-fallback"
    assert result["bbox"] == geo_mod._US_STATE_BBOX["Florida"]
    assert "Florida" in result["fallback_reason"]


# --- (d) no state + no result still raises (no silent swallow) --------------


def test_geocode_no_result_no_state_still_raises(monkeypatch):
    """A genuine no-match with NO detectable state propagates GeocodeNoMatchError."""

    def _boom(query):
        raise GeocodeNoMatchError(f"Could not locate {query!r}.")

    monkeypatch.setattr(geo_mod, "_fetch_nominatim_geocode_bytes", _boom)
    _bind_geocode_cache(monkeypatch)

    with pytest.raises(GeocodeNoMatchError):
        geocode_location("Atlantis")


# --- typed GEOCODE_NO_MATCH from the real Nominatim fetch branches -----------


class _FakeGeocodeResp:
    """Minimal requests.Response stand-in returning a fixed JSON body."""

    status_code = 200

    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


def test_geocode_empty_body_raises_typed_no_match(monkeypatch):
    """An empty Nominatim result body for an unknown non-US place raises
    GeocodeNoMatchError with the non-retryable GEOCODE_NO_MATCH code.

    Drives the real ``_fetch_nominatim_geocode_bytes`` empty-result branch
    (no _fetch_nominatim_geocode_bytes monkeypatch) so the typed-error contract
    is locked end-to-end through ``geocode_location``.
    """
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **kw: _FakeGeocodeResp([]),
    )
    _bind_geocode_cache(monkeypatch)

    # "Atlantis" has no detectable US state, so the no-match error propagates
    # instead of being swallowed by a state-snap.
    assert geo_mod._extract_us_state("Atlantis") is None
    with pytest.raises(GeocodeNoMatchError) as excinfo:
        geocode_location("Atlantis")
    assert excinfo.value.error_code == "GEOCODE_NO_MATCH"
    assert excinfo.value.retryable is False


def test_geocode_malformed_boundingbox_raises_typed_no_match(monkeypatch):
    """A top hit whose boundingbox is the wrong length raises the typed
    GeocodeNoMatchError (non-retryable GEOCODE_NO_MATCH) from the real fetch.
    """
    malformed = [
        {
            "display_name": "Somewhere",
            "lat": "10.0",
            "lon": "20.0",
            # Only two values -> len(bb) != 4 -> malformed-boundingbox branch.
            "boundingbox": ["10.0", "11.0"],
        }
    ]
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **kw: _FakeGeocodeResp(malformed),
    )
    _bind_geocode_cache(monkeypatch)

    assert geo_mod._extract_us_state("Atlantis") is None
    with pytest.raises(GeocodeNoMatchError) as excinfo:
        geocode_location("Atlantis")
    assert excinfo.value.error_code == "GEOCODE_NO_MATCH"
    assert excinfo.value.retryable is False


# --- _resolve_state_bbox falls back to offline table on live failure --------


def test_resolve_state_bbox_falls_back_to_table(monkeypatch):
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **kw: (_ for _ in ()).throw(
            requests.RequestException("down")
        ),
    )
    bbox, lat, lon, source = geo_mod._resolve_state_bbox("Texas")
    assert source == "offline-state-table"
    assert bbox == geo_mod._US_STATE_BBOX["Texas"]
    # Centroid inside the bbox.
    assert bbox[0] <= lon <= bbox[2]
    assert bbox[1] <= lat <= bbox[3]


# ---------------------------------------------------------------------------
# OPEN-10 — "downtown Tampa" (and similar sub-locality phrasings) resolving
# to a single building/POI footprint instead of a usable case AOI.
#
# Live-confirmed root cause (2026-07-11): Nominatim's ONLY match for
# "downtown Tampa" is a category=railway/type=tram_stop node literally named
# "Downtown Tampa" (a streetcar stop), bbox ~11 m across. Two fixes in
# ``_fetch_nominatim_geocode_bytes``: (a) prefer a place-class candidate over
# a point-scale top hit for area-intent queries, (b) floor any surviving
# sub-1km bbox to a 2 km square with an honest ``expansion_note``.
# ---------------------------------------------------------------------------


def test_geocode_open10_downtown_tampa_live_captured_regression(monkeypatch):
    """Golden regression: the EXACT live Nominatim payload for 'downtown Tampa'
    (captured 2026-07-11) must floor-expand rather than return an 11 m bbox.
    """
    tampa_tram_stop = [
        {
            "place_id": 305080868,
            "osm_type": "node",
            "osm_id": 5949810209,
            "lat": "27.9452787",
            "lon": "-82.4567888",
            "category": "railway",
            "type": "tram_stop",
            "display_name": (
                "Downtown Tampa, South Franklin Street, Riverside, Harbour "
                "Island, Tampa, Hillsborough County, Florida, 33601, "
                "United States"
            ),
            "boundingbox": [
                "27.9452287", "27.9453287", "-82.4568388", "-82.4567388",
            ],
        }
    ]
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **kw: _FakeGeocodeResp(tampa_tram_stop),
    )
    _bind_geocode_cache(monkeypatch)

    assert geo_mod._extract_us_state("downtown Tampa") is None

    result = geocode_location("downtown Tampa")

    west, south, east, north = result["bbox"]
    # The raw tram-stop bbox is ~11 m across -- old behavior. The floored
    # bbox must be a real, visible AOI: at least ~1 km on both axes.
    height_km = abs(north - south) * 111.32
    width_km = (
        abs(east - west) * 111.32 * math.cos(math.radians(result["latitude"]))
    )
    assert height_km >= 1.0, height_km
    assert width_km >= 1.0, width_km
    assert "expansion_note" in result
    assert "downtown Tampa" in result["expansion_note"]
    assert "Downtown Tampa" in result["name"]


def test_geocode_open10_building_first_place_class_wins(monkeypatch):
    """Building-class top hit + a place-class candidate for the same locality
    -> the place-class candidate is promoted, unchanged bbox, no floor note.
    """
    candidates = [
        {
            "osm_type": "way",
            "osm_id": 1,
            "lat": "27.95",
            "lon": "-82.46",
            "category": "building",
            "type": "yes",
            "display_name": "123 Some Building, Tampa, Florida, United States",
            "boundingbox": ["27.9495", "27.9505", "-82.4605", "-82.4595"],
        },
        {
            "osm_type": "node",
            "osm_id": 2,
            "lat": "27.95",
            "lon": "-82.46",
            "category": "place",
            "type": "neighbourhood",
            "display_name": "Downtown, Tampa, Florida, United States",
            "boundingbox": ["27.94", "27.96", "-82.47", "-82.45"],
        },
    ]
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **kw: _FakeGeocodeResp(candidates),
    )
    _bind_geocode_cache(monkeypatch)

    result = geocode_location("downtown Tampa")

    assert "Downtown, Tampa" in result["name"]
    assert result["bbox"] == [-82.47, 27.94, -82.45, 27.96]
    assert "expansion_note" not in result


def test_geocode_open10_building_only_floor_expansion(monkeypatch):
    """No place-class alternate exists -> the point-scale building hit is
    floor-expanded to a 2 km square with an honest ``expansion_note``.
    """
    candidates = [
        {
            "osm_type": "way",
            "osm_id": 1,
            "lat": "27.95",
            "lon": "-82.46",
            "category": "building",
            "type": "yes",
            "display_name": "123 Some Building, Tampa, Florida, United States",
            "boundingbox": ["27.9495", "27.9505", "-82.4605", "-82.4595"],
        },
    ]
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **kw: _FakeGeocodeResp(candidates),
    )
    _bind_geocode_cache(monkeypatch)

    result = geocode_location("downtown Tampa")

    assert "123 Some Building" in result["name"]
    assert "expansion_note" in result
    assert "expanded to a 2 km area" in result["expansion_note"]
    west, south, east, north = result["bbox"]
    height_km = abs(north - south) * 111.32
    width_km = (
        abs(east - west) * 111.32 * math.cos(math.radians(result["latitude"]))
    )
    assert 1.9 <= height_km <= 2.1, height_km
    assert 1.9 <= width_km <= 2.1, width_km


def test_geocode_open10_genuine_poi_query_unchanged(monkeypatch):
    """A query that clearly names a POI (contains 'airport') is NOT redirected
    to a place-class candidate, even when the top hit is building-class and a
    place-class alternate exists for the same locality.
    """
    candidates = [
        {
            "osm_type": "way",
            "osm_id": 3,
            "lat": "27.9772",
            "lon": "-82.5311",
            "category": "aeroway",
            "type": "aerodrome",
            "display_name": (
                "Tampa International Airport, Tampa, Florida, United States"
            ),
            "boundingbox": ["27.955", "28.000", "-82.555", "-82.505"],
        },
        {
            "osm_type": "relation",
            "osm_id": 4,
            "lat": "27.95",
            "lon": "-82.46",
            "category": "boundary",
            "type": "administrative",
            "display_name": "Tampa, Hillsborough County, Florida, United States",
            "boundingbox": ["27.87", "28.06", "-82.58", "-82.35"],
        },
    ]
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **kw: _FakeGeocodeResp(candidates),
    )
    _bind_geocode_cache(monkeypatch)

    result = geocode_location("Tampa International Airport")

    assert "Airport" in result["name"]
    assert result["bbox"] == [-82.555, 27.955, -82.505, 28.0]
    assert "expansion_note" not in result


def test_geocode_open10_street_address_query_unchanged(monkeypatch):
    """A street address (leading house number) is a point lookup, not an
    area-intent query -- the class-preference reorder must not fire even
    though the top hit is building-class and a place candidate exists.
    """
    candidates = [
        {
            "osm_type": "way",
            "osm_id": 5,
            "lat": "27.9",
            "lon": "-82.46",
            "category": "building",
            "type": "yes",
            "display_name": "123 Main St, Tampa, Florida, United States",
            "boundingbox": ["27.8995", "27.9005", "-82.4605", "-82.4595"],
        },
        {
            "osm_type": "relation",
            "osm_id": 6,
            "lat": "27.95",
            "lon": "-82.46",
            "category": "boundary",
            "type": "administrative",
            "display_name": "Tampa, Hillsborough County, Florida, United States",
            "boundingbox": ["27.87", "28.06", "-82.58", "-82.35"],
        },
    ]
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **kw: _FakeGeocodeResp(candidates),
    )
    _bind_geocode_cache(monkeypatch)

    result = geocode_location("123 Main St, Tampa, FL")

    assert "123 Main St" in result["name"]
    # Still building-scale -- the AOI floor (part b) still applies even
    # though the class-preference reorder (part a) correctly stayed off.
    assert "expansion_note" in result


def test_geocode_open10_big_city_bbox_untouched(monkeypatch):
    """An ordinary city-scale query is returned exactly as Nominatim reports
    it -- no reorder (already place-class) and no floor (bbox well over 1 km).
    """
    candidates = [
        {
            "osm_type": "relation",
            "osm_id": 7,
            "lat": "27.9506",
            "lon": "-82.4572",
            "category": "boundary",
            "type": "administrative",
            "display_name": "Tampa, Hillsborough County, Florida, United States",
            "boundingbox": ["27.87", "28.06", "-82.58", "-82.35"],
        }
    ]
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **kw: _FakeGeocodeResp(candidates),
    )
    _bind_geocode_cache(monkeypatch)

    result = geocode_location("Tampa, FL")

    assert result["bbox"] == [-82.58, 27.87, -82.35, 28.06]
    assert "expansion_note" not in result


@pytest.mark.parametrize(
    "candidate,expected",
    [
        ({"category": "place", "type": "neighbourhood"}, True),
        ({"category": "place", "type": "city"}, True),
        ({"category": "place", "type": "isolated_dwelling"}, False),
        ({"category": "boundary", "type": "administrative"}, True),
        ({"category": "boundary", "type": "postal_code"}, False),
        ({"category": "building", "type": "yes"}, False),
        ({"category": "railway", "type": "tram_stop"}, False),
        ({"category": "amenity", "type": "restaurant"}, False),
        ({}, False),
    ],
)
def test_is_place_class(candidate, expected):
    assert geo_mod._is_place_class(candidate) is expected


@pytest.mark.parametrize(
    "query,expected",
    [
        ("Tampa International Airport", True),
        ("downtown Tampa", False),
        ("123 Main St, Tampa, FL", True),
        ("Fort Myers, FL", False),
        ("Yankee Stadium", True),
    ],
)
def test_looks_like_poi_query(query, expected):
    assert geo_mod._looks_like_poi_query(query) is expected


def test_bbox_long_axis_km_and_square_km_bbox_roundtrip():
    # A ~0.0001 deg bbox (like the live Tampa tram-stop) is well under 1 km.
    long_axis = geo_mod._bbox_long_axis_km(
        -82.4568388, 27.9452287, -82.4567388, 27.9453287, 27.9452787
    )
    assert long_axis < 0.02  # ~11 m, in km

    west, south, east, north = geo_mod._square_km_bbox(
        27.9452787, -82.4567888, 2.0
    )
    height_km = abs(north - south) * 111.32
    width_km = abs(east - west) * 111.32 * math.cos(math.radians(27.9452787))
    assert 1.9 <= height_km <= 2.1
    assert 1.9 <= width_km <= 2.1
    # Centered on the input point.
    assert south < 27.9452787 < north
    assert west < -82.4567888 < east


# ---------------------------------------------------------------------------
# job-0039 — fetch_landcover (NLCD MRLC WMS).
# ---------------------------------------------------------------------------


from trid3nt_server.tools.fetchers.climate.lookup_precip_return_period.lookup_precip_return_period import (  # noqa: E402 — after main test surface
    lookup_precip_return_period,
)
# fetch_landcover FOLDED to a spec-driven surface (ADR 0082): the twin + its
# twin-internal tests (_fetch_nlcd_landcover_bytes / _landcover_bytes_to_cog /
# _fix_nlcd_background_transparency / _clip_raster_bytes_to_bbox / cache-version
# salt / overview generation) DELETED with the twin. Their value moved to
# tests/test_router_landcover.py (the wcs_getcoverage mode + pre_resolve auto-coarsen
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

# job-0039 — lookup_precip_return_period (NOAA Atlas 14 PFDS).
# ---------------------------------------------------------------------------


def test_lookup_precip_return_period_is_registered_with_static_30d():
    entry = TOOL_REGISTRY["lookup_precip_return_period"]
    assert entry.metadata.ttl_class == "static-30d"
    assert entry.metadata.source_class == "precip_return_period"
    assert entry.metadata.cacheable is True


def test_lookup_precip_return_period_docstring_records_tier_3():
    """§F.1.1 docstring discipline: Tier 3 (direct HTTPS point query)."""
    doc = lookup_precip_return_period.__doc__ or ""
    assert "Access pattern:" in doc
    assert "Tier 3" in doc


# Verbatim Atlas 14 PFDS response for the Fort Myers center captured 2026-06-07.
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
