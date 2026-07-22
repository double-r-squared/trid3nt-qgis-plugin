"""Unit tests for the ``fetch_gbif_occurrences`` atomic tool (job-0087).

Coverage:
- Tool is registered in TOOL_REGISTRY with expected metadata.
- Validation: bad bbox / bad year_range / bad max_records raise typed errors.
- Mocked happy path: a 1-page response (≤300 records) yields a FlatGeobuf with
  the same feature count.
- Pagination: 600 records across 2 pages produces 600 features.
- Species-name resolution: ``species_key="Puma concolor coryi"`` calls the
  ``species/match`` endpoint then proceeds with the resolved taxonKey.
- Empty bbox / no results: returns an empty FlatGeobuf without error.
- Geographic correctness (job-0086 codified lesson): a record with coords
  OUTSIDE the bbox is filtered before serialization.
- Cache hit: a second call with identical params reuses the cached URI.

Live tests (gated by ``TRID3NT_TEST_LIVE_GBIF=1``):
- Florida panther via taxonKey (2435099 = Puma concolor, species-level) over
  Big Cypress / Everglades bbox. Records to evidence/gbif_live.txt.
- Florida panther via scientific-name string ("Puma concolor") over the same
  bbox — exercises the species/match resolution path AND verifies the name
  resolves to the same species-level taxonKey (job-0117 OQ-0087 follow-up).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.fetch_gbif_occurrences import (
    GBIFError,
    GBIFInputError,
    GBIFUpstreamError,
    _records_to_flatgeobuf_bytes,
    _round_bbox_to_6dp,
    _validate_bbox,
    _validate_year_range,
    fetch_gbif_occurrences,
)


# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

_PINNED_NOW = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)

# Big Cypress / Everglades bbox — known Florida panther range.
_EVERGLADES_BBOX = (-81.5, 25.5, -80.5, 26.5)

# Florida panther taxonKey resolution — OQ-0087-PANTHER-TAXON-KEY (job-0117):
#
# job-0087's kickoff specified taxonKey 7193927 ("Puma concolor concolor"),
# but that subspecies has ~310 records globally and NONE in Florida — GBIF
# Florida-panther observations are catalogued under the parent species
# Puma concolor = 2435099 (~250 records in this bbox). The mocked unit tests
# still use 7193927 as an arbitrary int placeholder (no real GBIF call), but
# every LIVE test now hits the species-level key 2435099. The canonical
# common-name → key mapping is in ``_species_reference.FLORIDA_DEMO_SPECIES``.
_PANTHER_TAXON_KEY = 7193927  # mock-only placeholder; never sent to real GBIF
_PANTHER_LIVE_TAXON_KEY = 2435099  # Puma concolor — used by the live tests
_PANTHER_LIVE_SCIENTIFIC_NAME = "Puma concolor"  # name-resolution live path

# Live-test gate.
_LIVE_GBIF = os.environ.get("TRID3NT_TEST_LIVE_GBIF") == "1"


# ---------------------------------------------------------------------------
# Fake GCS plumbing (mirrors test_fetch_administrative_boundaries pattern).
# ---------------------------------------------------------------------------


class FakeBlob:
    def __init__(self, store: dict[str, bytes], path: str) -> None:
        self._store = store
        self._path = path
        self.custom_time: datetime | None = None
        self.cache_control: str | None = None

    def exists(self) -> bool:
        return self._path in self._store

    def download_as_bytes(self) -> bytes:
        return self._store[self._path]

    def upload_from_string(self, data: bytes, content_type: str | None = None) -> None:
        self._store[self._path] = data


class FakeBucket:
    def __init__(self, store: dict[str, bytes]) -> None:
        self._store = store

    def blob(self, path: str) -> FakeBlob:
        return FakeBlob(self._store, path)


class FakeStorageClient:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    def bucket(self, name: str) -> FakeBucket:
        return FakeBucket(self.store)


def _make_read_through_injector(fake_gcs):
    """S3-only in-memory read-through injector (GCP decommissioned).

    Replaces the retired ``google.cloud.storage`` double: drives the tool's
    ``read_through`` off an in-memory S3 store (``fake_gcs.store``, keyed by
    object KEY), minting ``s3://`` URIs and honoring cache hit/miss/write.
    """
    from trid3nt_server.tools.cache import (
        CACHE_BUCKET,
        cache_path,
        compute_cache_key,
        is_cacheable,
        ReadThroughResult,
    )

    store = fake_gcs.store

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


def _make_occurrence_record(
    *,
    gbif_id: int,
    lon: float,
    lat: float,
    species: str = "Puma concolor coryi",
    event_date: str = "2024-05-12",
    uncertainty: float | None = 50.0,
    basis: str = "HUMAN_OBSERVATION",
) -> dict[str, Any]:
    """Build a GBIF-shaped occurrence record."""
    return {
        "gbifID": gbif_id,
        "decimalLongitude": lon,
        "decimalLatitude": lat,
        "species": species,
        "eventDate": event_date,
        "coordinateUncertaintyInMeters": uncertainty,
        "basisOfRecord": basis,
    }


def _make_search_page(records: list[dict[str, Any]], end_of_records: bool) -> dict[str, Any]:
    """Build a GBIF-shaped search response page."""
    return {
        "offset": 0,
        "limit": 300,
        "endOfRecords": end_of_records,
        "count": len(records),
        "results": records,
    }


class _FakeHTTPResponse:
    """Minimal httpx.Response-like object for patching."""

    def __init__(self, status_code: int, payload: dict[str, Any] | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self) -> dict[str, Any]:
        if self._payload is None:
            raise ValueError("no JSON payload")
        return self._payload


# ---------------------------------------------------------------------------
# Registration tests.
# ---------------------------------------------------------------------------


def test_tool_is_registered_in_registry():
    """fetch_gbif_occurrences appears in TOOL_REGISTRY with expected metadata."""
    assert "fetch_gbif_occurrences" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["fetch_gbif_occurrences"]
    assert entry.metadata.name == "fetch_gbif_occurrences"
    assert entry.metadata.ttl_class == "static-30d"
    assert entry.metadata.source_class == "gbif"
    assert entry.metadata.cacheable is True


# ---------------------------------------------------------------------------
# Validation / typed-error tests.
# ---------------------------------------------------------------------------


def test_degenerate_bbox_raises_input_error():
    with pytest.raises(GBIFInputError):
        _validate_bbox((-81.0, 26.0, -81.0, 26.0))


def test_lon_out_of_range_raises_input_error():
    with pytest.raises(GBIFInputError):
        _validate_bbox((-181.0, 25.0, -80.0, 26.0))


def test_lat_out_of_range_raises_input_error():
    with pytest.raises(GBIFInputError):
        _validate_bbox((-81.0, 25.0, -80.0, 91.0))


def test_year_range_inverted_raises_input_error():
    with pytest.raises(GBIFInputError):
        _validate_year_range((2020, 2010))


def test_year_range_out_of_bounds_raises_input_error():
    with pytest.raises(GBIFInputError):
        _validate_year_range((1400, 2025))


def test_year_range_none_is_valid():
    _validate_year_range(None)  # no raise


def test_input_error_is_not_retryable():
    """GBIFInputError carries retryable=False for FR-AS-11 mapping."""
    try:
        fetch_gbif_occurrences(
            species_key=-1,  # invalid taxonKey
            bbox=_EVERGLADES_BBOX,
        )
    except GBIFInputError as exc:
        assert exc.retryable is False
    else:
        pytest.fail("Expected GBIFInputError")


def test_max_records_zero_raises_input_error():
    with pytest.raises(GBIFInputError):
        fetch_gbif_occurrences(
            species_key=_PANTHER_TAXON_KEY,
            bbox=_EVERGLADES_BBOX,
            max_records=0,
        )


def test_max_records_over_cap_raises_input_error():
    with pytest.raises(GBIFInputError):
        fetch_gbif_occurrences(
            species_key=_PANTHER_TAXON_KEY,
            bbox=_EVERGLADES_BBOX,
            max_records=10_000_000,
        )


def test_bad_species_key_type_raises_input_error():
    with pytest.raises(GBIFInputError):
        fetch_gbif_occurrences(
            species_key=3.14,  # type: ignore[arg-type]
            bbox=_EVERGLADES_BBOX,
        )


# ---------------------------------------------------------------------------
# Helper tests.
# ---------------------------------------------------------------------------


def test_round_bbox_to_6dp():
    raw = (-81.123456789, 25.123456789, -80.987654321, 26.987654321)
    rounded = _round_bbox_to_6dp(raw)
    assert rounded == (-81.123457, 25.123457, -80.987654, 26.987654)


# ---------------------------------------------------------------------------
# FlatGeobuf serialization tests.
# ---------------------------------------------------------------------------


def test_records_to_flatgeobuf_serializes_features():
    """A handful of in-bbox records become a non-trivial FlatGeobuf."""
    records = [
        _make_occurrence_record(gbif_id=1, lon=-81.0, lat=26.0),
        _make_occurrence_record(gbif_id=2, lon=-80.8, lat=25.9),
        _make_occurrence_record(gbif_id=3, lon=-81.2, lat=26.1),
    ]
    fgb_bytes = _records_to_flatgeobuf_bytes(records, _EVERGLADES_BBOX)
    assert len(fgb_bytes) > 0
    # FlatGeobuf magic number: "fgb\x03fgb\x01" at the start.
    assert fgb_bytes.startswith(b"fgb"), (
        f"FlatGeobuf magic header missing; got {fgb_bytes[:16]!r}"
    )

    # Round-trip: read it back and assert feature count.
    import tempfile
    import geopandas as gpd  # type: ignore[import-not-found]

    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as tf:
        tf.write(fgb_bytes)
        tf_path = tf.name
    try:
        gdf = gpd.read_file(tf_path, engine="pyogrio")
        assert len(gdf) == 3
        assert set(gdf.columns) >= {
            "gbifID",
            "species",
            "eventDate",
            "coordinateUncertaintyInMeters",
            "basisOfRecord",
            "geometry",
        }
    finally:
        os.unlink(tf_path)


def test_records_outside_bbox_are_filtered_geographic_correctness():
    """job-0086 codified lesson: every emitted point must lie inside the bbox.

    A record outside the bbox must be dropped before serialization, even if
    GBIF returned it.
    """
    in_bbox = _make_occurrence_record(gbif_id=1, lon=-81.0, lat=26.0)
    way_outside = _make_occurrence_record(gbif_id=2, lon=-120.0, lat=40.0)  # California
    just_outside = _make_occurrence_record(gbif_id=3, lon=-80.499, lat=26.0)  # 1m E of east edge
    records = [in_bbox, way_outside, just_outside]

    fgb_bytes = _records_to_flatgeobuf_bytes(records, _EVERGLADES_BBOX)
    import tempfile
    import geopandas as gpd  # type: ignore[import-not-found]

    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as tf:
        tf.write(fgb_bytes)
        tf_path = tf.name
    try:
        gdf = gpd.read_file(tf_path, engine="pyogrio")
        # Only the in-bbox record survives.
        assert len(gdf) == 1
        # The surviving feature MUST geographically be within the bbox.
        for geom in gdf.geometry:
            x, y = geom.x, geom.y
            assert _EVERGLADES_BBOX[0] <= x <= _EVERGLADES_BBOX[2], (
                f"feature lon {x} outside requested bbox"
            )
            assert _EVERGLADES_BBOX[1] <= y <= _EVERGLADES_BBOX[3], (
                f"feature lat {y} outside requested bbox"
            )
    finally:
        os.unlink(tf_path)


def test_records_with_missing_coords_are_skipped():
    """Records without decimalLongitude/Latitude are silently dropped."""
    good = _make_occurrence_record(gbif_id=1, lon=-81.0, lat=26.0)
    no_coords = {
        "gbifID": 2,
        "species": "Foo bar",
        "decimalLongitude": None,
        "decimalLatitude": None,
        "basisOfRecord": "HUMAN_OBSERVATION",
    }
    records = [good, no_coords]
    fgb_bytes = _records_to_flatgeobuf_bytes(records, _EVERGLADES_BBOX)

    import tempfile
    import geopandas as gpd  # type: ignore[import-not-found]

    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as tf:
        tf.write(fgb_bytes)
        tf_path = tf.name
    try:
        gdf = gpd.read_file(tf_path, engine="pyogrio")
        assert len(gdf) == 1
    finally:
        os.unlink(tf_path)


# ---------------------------------------------------------------------------
# Mocked HTTP tests — happy path, pagination, empty, species-name resolution.
# ---------------------------------------------------------------------------


def test_mocked_happy_path_single_page():
    """A 1-page response (50 records, endOfRecords=True) → 50 features."""
    fake_gcs = FakeStorageClient()
    records = [
        _make_occurrence_record(gbif_id=i, lon=-81.0 + (i * 0.001), lat=26.0)
        for i in range(50)
    ]
    page = _make_search_page(records, end_of_records=True)

    with patch(
        "trid3nt_server.tools.fetch_gbif_occurrences.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ), patch(
        "trid3nt_server.tools.fetch_gbif_occurrences.httpx.Client"
    ) as mock_client_cls:
        mock_client = MagicMock()
        mock_client.get.return_value = _FakeHTTPResponse(200, page)
        mock_client_cls.return_value = mock_client

        result = fetch_gbif_occurrences(
            species_key=_PANTHER_TAXON_KEY,
            bbox=_EVERGLADES_BBOX,
        )

    assert result.uri is not None
    assert result.uri.startswith("s3://")
    assert result.layer_type == "vector"
    assert result.role == "context"
    assert result.units is None
    # Tool should only have hit the search endpoint once.
    assert mock_client.get.call_count == 1

    # Verify the saved FlatGeobuf has 50 features.
    [(path, data)] = list(fake_gcs.store.items())
    assert path.startswith("cache/static-30d/gbif/")
    assert path.endswith(".fgb")

    import tempfile
    import geopandas as gpd  # type: ignore[import-not-found]

    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as tf:
        tf.write(data)
        tf_path = tf.name
    try:
        gdf = gpd.read_file(tf_path, engine="pyogrio")
        assert len(gdf) == 50
    finally:
        os.unlink(tf_path)


def test_mocked_pagination_two_pages():
    """600 records across 2 pages (300 + 300, endOfRecords on page 2) → 600 features."""
    fake_gcs = FakeStorageClient()
    page1_records = [
        _make_occurrence_record(gbif_id=i, lon=-81.0 + (i * 0.0001), lat=26.0)
        for i in range(300)
    ]
    page2_records = [
        _make_occurrence_record(gbif_id=300 + i, lon=-81.0 + (i * 0.0001), lat=26.1)
        for i in range(300)
    ]
    page1 = _make_search_page(page1_records, end_of_records=False)
    page2 = _make_search_page(page2_records, end_of_records=True)

    with patch(
        "trid3nt_server.tools.fetch_gbif_occurrences.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ), patch(
        "trid3nt_server.tools.fetch_gbif_occurrences.httpx.Client"
    ) as mock_client_cls:
        mock_client = MagicMock()
        mock_client.get.side_effect = [
            _FakeHTTPResponse(200, page1),
            _FakeHTTPResponse(200, page2),
        ]
        mock_client_cls.return_value = mock_client

        result = fetch_gbif_occurrences(
            species_key=_PANTHER_TAXON_KEY,
            bbox=_EVERGLADES_BBOX,
            max_records=5000,
        )

    assert mock_client.get.call_count == 2
    # Verify pagination offsets.
    call0_params = mock_client.get.call_args_list[0].kwargs["params"]
    call1_params = mock_client.get.call_args_list[1].kwargs["params"]
    assert call0_params["offset"] == 0
    assert call1_params["offset"] == 300

    # Verify saved FlatGeobuf has 600 features.
    [(_, data)] = list(fake_gcs.store.items())
    import tempfile
    import geopandas as gpd  # type: ignore[import-not-found]

    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as tf:
        tf.write(data)
        tf_path = tf.name
    try:
        gdf = gpd.read_file(tf_path, engine="pyogrio")
        assert len(gdf) == 600
    finally:
        os.unlink(tf_path)


def test_mocked_species_name_resolution():
    """species_key=str triggers species/match call before search call."""
    fake_gcs = FakeStorageClient()
    page = _make_search_page(
        [_make_occurrence_record(gbif_id=1, lon=-81.0, lat=26.0)],
        end_of_records=True,
    )
    match_response = {
        "usageKey": _PANTHER_TAXON_KEY,
        "scientificName": "Puma concolor coryi",
        "matchType": "EXACT",
    }

    with patch(
        "trid3nt_server.tools.fetch_gbif_occurrences.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ), patch(
        "trid3nt_server.tools.fetch_gbif_occurrences.httpx.Client"
    ) as mock_client_cls:
        # Two distinct .Client() invocations: one for resolve, one for search.
        # We return the same MagicMock for both so the .get() calls accumulate.
        mock_client = MagicMock()
        mock_client.get.side_effect = [
            _FakeHTTPResponse(200, match_response),  # species/match call
            _FakeHTTPResponse(200, page),  # occurrence/search call
        ]
        mock_client_cls.return_value = mock_client

        result = fetch_gbif_occurrences(
            species_key="Puma concolor coryi",
            bbox=_EVERGLADES_BBOX,
        )

    # First call: species/match. Second call: occurrence/search.
    assert mock_client.get.call_count == 2
    first_call_url = mock_client.get.call_args_list[0].args[0]
    second_call_url = mock_client.get.call_args_list[1].args[0]
    assert "species/match" in first_call_url
    assert "occurrence/search" in second_call_url
    # The search call must use the resolved taxonKey.
    second_params = mock_client.get.call_args_list[1].kwargs["params"]
    assert second_params["taxonKey"] == _PANTHER_TAXON_KEY
    assert result.uri is not None


def test_mocked_unknown_species_name_raises_input_error():
    """A species/match response with usageKey=None raises GBIFInputError."""
    with patch(
        "trid3nt_server.tools.fetch_gbif_occurrences.httpx.Client"
    ) as mock_client_cls:
        mock_client = MagicMock()
        mock_client.get.return_value = _FakeHTTPResponse(
            200, {"matchType": "NONE"}
        )
        mock_client_cls.return_value = mock_client

        with pytest.raises(GBIFInputError, match="could not resolve species name"):
            fetch_gbif_occurrences(
                species_key="Notarealsp notarealspecies",
                bbox=_EVERGLADES_BBOX,
            )


def test_mocked_fuzzy_match_raises_input_error_with_did_you_mean():
    """A species/match FUZZY near-spelling must FAIL LOUD, not silently widen.

    "goes18 vs goes-18" hazard for taxon names: GBIF maps a single typo
    (``"Puma concoler"``) to ``Puma concolor`` with matchType=FUZZY at
    confidence 95. Accepting that usageKey would drive the occurrence search
    off a taxon the caller never asked for. The fix raises GBIFInputError
    naming the fuzzy match so the caller can confirm or correct (verified
    against the live GBIF response shape 2026-06-27).
    """
    fuzzy_response = {
        "usageKey": 2435099,
        "scientificName": "Puma concolor (Linnaeus, 1771)",
        "canonicalName": "Puma concolor",
        "rank": "SPECIES",
        "status": "ACCEPTED",
        "confidence": 95,
        "matchType": "FUZZY",
    }
    with patch(
        "trid3nt_server.tools.fetch_gbif_occurrences.httpx.Client"
    ) as mock_client_cls:
        mock_client = MagicMock()
        mock_client.get.return_value = _FakeHTTPResponse(200, fuzzy_response)
        mock_client_cls.return_value = mock_client

        with pytest.raises(GBIFInputError) as exc_info:
            fetch_gbif_occurrences(
                species_key="Puma concoler",  # single-letter typo
                bbox=_EVERGLADES_BBOX,
            )
    msg = str(exc_info.value)
    assert "FUZZY" in msg, f"error must name the matchType; got: {msg}"
    assert "Puma concolor" in msg, f"error must name the matched taxon; got: {msg}"
    assert "Did you mean" in msg, f"error must be clarifiable; got: {msg}"
    # Never reached the occurrence/search call -- only the species/match resolve.
    assert mock_client.get.call_count == 1
    # Loud + non-retryable (caller must disambiguate, not blindly retry).
    assert exc_info.value.retryable is False


def test_mocked_higherrank_match_raises_input_error():
    """A HIGHERRANK match (typo'd subspecies -> parent species) must fail loud.

    Reproduces the named hazard: ``"Puma concolar coyi"`` (two typos) returns
    matchType=HIGHERRANK with the *species* usageKey 2435099 -- GBIF silently
    discarded the bad subspecies epithet and widened to the parent. The old
    code accepted any usageKey and searched the broader taxon. Now it refuses.
    """
    higherrank_response = {
        "usageKey": 2435099,
        "scientificName": "Puma concolor (Linnaeus, 1771)",
        "canonicalName": "Puma concolor",
        "rank": "SPECIES",
        "status": "ACCEPTED",
        "confidence": 95,
        "matchType": "HIGHERRANK",
    }
    with patch(
        "trid3nt_server.tools.fetch_gbif_occurrences.httpx.Client"
    ) as mock_client_cls:
        mock_client = MagicMock()
        mock_client.get.return_value = _FakeHTTPResponse(200, higherrank_response)
        mock_client_cls.return_value = mock_client

        with pytest.raises(GBIFInputError) as exc_info:
            fetch_gbif_occurrences(
                species_key="Puma concolar coyi",  # two typos in subspecies name
                bbox=_EVERGLADES_BBOX,
            )
    msg = str(exc_info.value)
    assert "HIGHERRANK" in msg, f"error must name the matchType; got: {msg}"
    assert "taxonKey=2435099" in msg, (
        f"error must surface the resolved (wrong) taxonKey; got: {msg}"
    )
    # Did not proceed to search the wrong taxon.
    assert mock_client.get.call_count == 1


def test_mocked_genus_name_exact_match_resolves():
    """An EXACT match (even at genus rank) is accepted -- matchType is the gate.

    A bare genus name like ``"Puma"`` returns matchType=EXACT/rank=GENUS from
    GBIF (verified live). EXACT is unambiguous, so we accept it and proceed to
    search; the resolved rank is surfaced in the resolution log so a deliberate
    higher-taxon query is visible in telemetry rather than silent. This guards
    against over-tightening the gate into rejecting legitimate EXACT matches.
    """
    fake_gcs = FakeStorageClient()
    genus_match = {
        "usageKey": 2435098,
        "scientificName": "Puma Jardine, 1834",
        "canonicalName": "Puma",
        "rank": "GENUS",
        "status": "ACCEPTED",
        "confidence": 94,
        "matchType": "EXACT",
    }
    page = _make_search_page(
        [_make_occurrence_record(gbif_id=1, lon=-81.0, lat=26.0)],
        end_of_records=True,
    )
    with patch(
        "trid3nt_server.tools.fetch_gbif_occurrences.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ), patch(
        "trid3nt_server.tools.fetch_gbif_occurrences.httpx.Client"
    ) as mock_client_cls:
        mock_client = MagicMock()
        mock_client.get.side_effect = [
            _FakeHTTPResponse(200, genus_match),  # species/match
            _FakeHTTPResponse(200, page),  # occurrence/search
        ]
        mock_client_cls.return_value = mock_client

        result = fetch_gbif_occurrences(
            species_key="Puma",
            bbox=_EVERGLADES_BBOX,
        )

    assert result.uri is not None
    # Proceeded to the search using the EXACT-resolved genus usageKey.
    assert mock_client.get.call_count == 2
    second_params = mock_client.get.call_args_list[1].kwargs["params"]
    assert second_params["taxonKey"] == 2435098


def test_mocked_missing_match_type_raises_input_error():
    """A usageKey present but matchType absent/None must fail loud, not guess.

    Defensive: if GBIF (or a proxy) returns a usageKey with no matchType, we
    cannot certify the match as EXACT, so we refuse rather than silently
    trusting an unverifiable resolution.
    """
    no_match_type = {
        "usageKey": 2435099,
        "scientificName": "Puma concolor (Linnaeus, 1771)",
        "canonicalName": "Puma concolor",
        "rank": "SPECIES",
    }
    with patch(
        "trid3nt_server.tools.fetch_gbif_occurrences.httpx.Client"
    ) as mock_client_cls:
        mock_client = MagicMock()
        mock_client.get.return_value = _FakeHTTPResponse(200, no_match_type)
        mock_client_cls.return_value = mock_client

        with pytest.raises(GBIFInputError):
            fetch_gbif_occurrences(
                species_key="Puma concolor",
                bbox=_EVERGLADES_BBOX,
            )


def test_mocked_empty_bbox_returns_empty_flatgeobuf():
    """An endOfRecords=True page with no records → empty FlatGeobuf, no error."""
    fake_gcs = FakeStorageClient()
    page = _make_search_page([], end_of_records=True)

    with patch(
        "trid3nt_server.tools.fetch_gbif_occurrences.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ), patch(
        "trid3nt_server.tools.fetch_gbif_occurrences.httpx.Client"
    ) as mock_client_cls:
        mock_client = MagicMock()
        mock_client.get.return_value = _FakeHTTPResponse(200, page)
        mock_client_cls.return_value = mock_client

        result = fetch_gbif_occurrences(
            species_key=_PANTHER_TAXON_KEY,
            bbox=_EVERGLADES_BBOX,
        )

    assert result.uri is not None
    [(_, data)] = list(fake_gcs.store.items())
    assert len(data) > 0  # empty FlatGeobuf is still a few hundred bytes of header


def test_mocked_5xx_raises_upstream_error_retryable():
    """A 500 from the search endpoint raises a retryable GBIFUpstreamError.

    Uses fake-GCS injection so a cache-miss path doesn't reach for real GCP
    credentials before invoking ``fetch_fn`` (which is where the 503 is raised).
    """
    fake_gcs = FakeStorageClient()
    with patch(
        "trid3nt_server.tools.fetch_gbif_occurrences.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ), patch(
        "trid3nt_server.tools.fetch_gbif_occurrences.httpx.Client"
    ) as mock_client_cls:
        mock_client = MagicMock()
        mock_client.get.return_value = _FakeHTTPResponse(503, text="Service Unavailable")
        mock_client_cls.return_value = mock_client

        with pytest.raises(GBIFUpstreamError) as exc_info:
            fetch_gbif_occurrences(
                species_key=_PANTHER_TAXON_KEY,
                bbox=_EVERGLADES_BBOX,
            )
        assert exc_info.value.retryable is True
    # No artifact should have been written on the 5xx path.
    assert fake_gcs.store == {}


# ---------------------------------------------------------------------------
# Cache-layer tests.
# ---------------------------------------------------------------------------


def test_cache_hit_skips_fetch_fn():
    """Second call with identical params returns the cached URI without re-fetching."""
    fake_gcs = FakeStorageClient()
    page = _make_search_page(
        [_make_occurrence_record(gbif_id=1, lon=-81.0, lat=26.0)],
        end_of_records=True,
    )

    with patch(
        "trid3nt_server.tools.fetch_gbif_occurrences.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ), patch(
        "trid3nt_server.tools.fetch_gbif_occurrences.httpx.Client"
    ) as mock_client_cls:
        mock_client = MagicMock()
        mock_client.get.return_value = _FakeHTTPResponse(200, page)
        mock_client_cls.return_value = mock_client

        r1 = fetch_gbif_occurrences(
            species_key=_PANTHER_TAXON_KEY, bbox=_EVERGLADES_BBOX
        )
        r2 = fetch_gbif_occurrences(
            species_key=_PANTHER_TAXON_KEY, bbox=_EVERGLADES_BBOX
        )

    # Only one search call should have been made (second hit the cache).
    assert mock_client.get.call_count == 1
    assert r1.uri == r2.uri


def test_layer_uri_shape_fields():
    """The returned LayerURI carries the documented fields."""
    fake_gcs = FakeStorageClient()
    page = _make_search_page(
        [_make_occurrence_record(gbif_id=1, lon=-81.0, lat=26.0)],
        end_of_records=True,
    )
    with patch(
        "trid3nt_server.tools.fetch_gbif_occurrences.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ), patch(
        "trid3nt_server.tools.fetch_gbif_occurrences.httpx.Client"
    ) as mock_client_cls:
        mock_client = MagicMock()
        mock_client.get.return_value = _FakeHTTPResponse(200, page)
        mock_client_cls.return_value = mock_client

        result = fetch_gbif_occurrences(
            species_key=_PANTHER_TAXON_KEY,
            bbox=_EVERGLADES_BBOX,
        )

    assert result.layer_type == "vector"
    assert result.role == "context"
    assert result.units is None
    assert result.style_preset == "gbif_occurrences"
    assert str(_PANTHER_TAXON_KEY) in result.layer_id
    assert "GBIF" in result.name


# ---------------------------------------------------------------------------
# Live test — real GBIF API call (env-gated).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _LIVE_GBIF, reason="TRID3NT_TEST_LIVE_GBIF=1 not set")
def test_live_florida_panther_over_big_cypress(tmp_path):
    """LIVE: Florida panther (Puma concolor, taxonKey 2435099) over Everglades bbox.

    Calls the real GBIF API. Captures evidence to evidence/gbif_live.txt.
    Asserts ≥1 feature returned, all within the requested bbox.

    Note: the audit.md kickoff specified taxonKey 7193927 (= subspecies
    Puma concolor concolor), but that subspecies has NO records in Florida —
    Florida-panther occurrences in GBIF are catalogued under the parent
    species (Puma concolor = 2435099, ~250 records in this bbox). See
    OQ-0087-PANTHER-TAXON-KEY.
    """
    fake_gcs = FakeStorageClient()
    with patch(
        "trid3nt_server.tools.fetch_gbif_occurrences.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_gbif_occurrences(
            species_key=_PANTHER_LIVE_TAXON_KEY,
            bbox=_EVERGLADES_BBOX,
            max_records=1000,
        )

    assert result.uri is not None
    [(path, data)] = list(fake_gcs.store.items())
    assert path.startswith("cache/static-30d/gbif/")
    assert path.endswith(".fgb")

    import tempfile
    import geopandas as gpd  # type: ignore[import-not-found]

    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as tf:
        tf.write(data)
        tf_path = tf.name
    try:
        gdf = gpd.read_file(tf_path, engine="pyogrio")
    finally:
        os.unlink(tf_path)

    assert len(gdf) >= 1, "Expected at least one Florida panther occurrence in Big Cypress"

    # Geographic-correctness check: every emitted point lies within the bbox.
    for geom in gdf.geometry:
        x, y = geom.x, geom.y
        assert _EVERGLADES_BBOX[0] <= x <= _EVERGLADES_BBOX[2], (
            f"feature lon {x} outside Big Cypress bbox"
        )
        assert _EVERGLADES_BBOX[1] <= y <= _EVERGLADES_BBOX[3], (
            f"feature lat {y} outside Big Cypress bbox"
        )

    # Capture evidence (sample first 5 records).
    evidence_lines = [
        f"# GBIF live test — Florida panther (Puma concolor, taxonKey {_PANTHER_LIVE_TAXON_KEY})",
        f"# bbox: {_EVERGLADES_BBOX}",
        f"# result.uri: {result.uri}",
        f"# feature count: {len(gdf)}",
        "",
    ]
    for i, row in enumerate(gdf.head(5).itertuples(index=False)):
        evidence_lines.append(f"feature {i}: {row}")
    evidence_text = "\n".join(evidence_lines)
    print("\n" + evidence_text)


@pytest.mark.skipif(not _LIVE_GBIF, reason="TRID3NT_TEST_LIVE_GBIF=1 not set")
def test_live_florida_panther_via_scientific_name_resolves_to_correct_key():
    """LIVE: ``species_key="Puma concolor"`` resolves through species/match
    to taxonKey 2435099 and yields ≥1 in-bbox feature.

    Covers OQ-0087-PANTHER-TAXON-KEY end-to-end: a user (or LLM) supplying
    the scientific name MUST land on the species-level key, not the
    subspecies key. We confirm by inspecting the URI — the cache filename
    is keyed on the RESOLVED taxonKey.
    """
    fake_gcs = FakeStorageClient()
    with patch(
        "trid3nt_server.tools.fetch_gbif_occurrences.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_gbif_occurrences(
            species_key=_PANTHER_LIVE_SCIENTIFIC_NAME,
            bbox=_EVERGLADES_BBOX,
            max_records=1000,
        )

    assert result.uri is not None
    # LayerURI layer_id embeds the RESOLVED taxonKey — confirm the
    # name-resolution path landed on 2435099 (Puma concolor, species).
    assert str(_PANTHER_LIVE_TAXON_KEY) in result.layer_id, (
        f"name-resolution should land on species-level taxonKey "
        f"{_PANTHER_LIVE_TAXON_KEY}; got layer_id={result.layer_id!r}"
    )
    assert f"taxonKey {_PANTHER_LIVE_TAXON_KEY}" in result.name

    [(path, data)] = list(fake_gcs.store.items())
    assert path.startswith("cache/static-30d/gbif/")
    assert path.endswith(".fgb")

    import tempfile
    import geopandas as gpd  # type: ignore[import-not-found]

    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as tf:
        tf.write(data)
        tf_path = tf.name
    try:
        gdf = gpd.read_file(tf_path, engine="pyogrio")
    finally:
        os.unlink(tf_path)

    assert len(gdf) >= 1, (
        f"Expected ≥1 Florida panther occurrence in Big Cypress via name "
        f"resolution; got {len(gdf)}"
    )

    # Geographic-correctness check: every emitted point lies within the bbox.
    for geom in gdf.geometry:
        x, y = geom.x, geom.y
        assert _EVERGLADES_BBOX[0] <= x <= _EVERGLADES_BBOX[2], (
            f"feature lon {x} outside Big Cypress bbox"
        )
        assert _EVERGLADES_BBOX[1] <= y <= _EVERGLADES_BBOX[3], (
            f"feature lat {y} outside Big Cypress bbox"
        )

    print(
        f"\n# GBIF live name-resolution test\n"
        f"# species_key: {_PANTHER_LIVE_SCIENTIFIC_NAME!r}\n"
        f"# resolved taxonKey: {_PANTHER_LIVE_TAXON_KEY}\n"
        f"# bbox: {_EVERGLADES_BBOX}\n"
        f"# result.uri: {result.uri}\n"
        f"# feature count: {len(gdf)}"
    )
