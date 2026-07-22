"""Unit tests for the ``fetch_iucn_red_list_range`` atomic tool (job-0129).

Coverage (the kickoff demands ≥4 unit tests + ≥1 live env-guarded test):

Registration + metadata:
- Tool is registered in TOOL_REGISTRY with the expected metadata
  (ttl_class=static-30d, source_class=iucn_red_list, cacheable, payload
  estimator wired).

Input validation:
- Empty / non-string species name → IUCNInputError.
- Invalid region → IUCNInputError.

Auth gate (the three-path waterfall + fail-closed):
- No api_key, no secret_ref, no env var → IUCNAuthError BEFORE any
  network call (the fetch_fn assertion below confirms no call).
- secret_ref provider mismatch → IUCNAuthError.
- secret_ref without a persistence resolver → IUCNAuthError.
- env-var fallback resolves when TRID3NT_IUCN_RED_LIST_API_KEY is set.

Network-mocked happy path:
- Mocked IUCN /species response → FlatGeobuf with 1 feature carrying the
  documented schema (taxonid, category, etc.) + is_placeholder_geometry=True.
- IUCN returns empty result → FlatGeobuf still emitted (single feature
  with category='DD' sentinel).
- IUCN returns the in-body "Token not valid!" message → IUCNAuthError.

Caching:
- Two identical (species_name, region) calls reuse the cached bytes
  (fetch_fn invoked once). api_key value is NOT part of the cache key.

Live verification (env-gated, skipped by default):
- ``TRID3NT_TEST_LIVE_IUCN_RED_LIST=1`` + a real
  ``TRID3NT_IUCN_RED_LIST_API_KEY`` env var; fetches "Puma concolor" and
  asserts the IUCN category field is one of the documented enum values
  (not an arbitrary string). The codified job-0086 lesson applies:
  the assertion is on semantic content (category in known enum), not just
  bytes-round-trip.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.fetch_iucn_red_list_range import (
    IUCNAuthError,
    IUCNInputError,
    IUCNUpstreamError,
    _resolve_api_key,
    _validate_region,
    _validate_species_name,
    estimate_payload_mb,
    fetch_iucn_red_list_range,
)
from trid3nt_contracts import new_ulid, now_utc
from trid3nt_contracts.secrets import SecretRecord


# ---------------------------------------------------------------------------
# Constants / helpers.
# ---------------------------------------------------------------------------

_PINNED_NOW = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)

_LIVE_IUCN = os.environ.get("TRID3NT_TEST_LIVE_IUCN_RED_LIST") == "1"
_LIVE_KEY = os.environ.get("TRID3NT_IUCN_RED_LIST_API_KEY", "")

# The documented IUCN Red List categories (per
# https://apiv3.iucnredlist.org/api/v3/docs).
_IUCN_CATEGORIES = {
    "EX",   # Extinct
    "EW",   # Extinct in the Wild
    "CR",   # Critically Endangered
    "EN",   # Endangered
    "VU",   # Vulnerable
    "NT",   # Near Threatened
    "LC",   # Least Concern
    "DD",   # Data Deficient
    "NE",   # Not Evaluated
    "LR/lc",
    "LR/nt",
    "LR/cd",
}


# ---------------------------------------------------------------------------
# Fake GCS plumbing (mirrors the WDPA / GBIF test pattern).
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


def _iucn_species_payload(
    *,
    name: str = "Puma concolor",
    category: str = "LC",
    taxonid: int = 18868,
    common: str = "Cougar",
    population_trend: str = "Decreasing",
    empty: bool = False,
) -> dict[str, Any]:
    """Build a fake /species response payload."""
    if empty:
        return {"name": name, "result": []}
    return {
        "name": name,
        "result": [
            {
                "taxonid": taxonid,
                "scientific_name": name,
                "kingdom": "ANIMALIA",
                "phylum": "CHORDATA",
                "class": "MAMMALIA",
                "order_name": "CARNIVORA",
                "family": "FELIDAE",
                "main_common_name": common,
                "category": category,
                "criteria": "",
                "population_trend": population_trend,
                "marine_system": False,
                "freshwater_system": False,
                "terrestrial_system": True,
                "elevation_lower": 0,
                "elevation_upper": 5800,
                "depth_lower": None,
                "depth_upper": None,
                "published_year": 2008,
                "assessment_date": "2008-06-30",
            }
        ],
    }


# ---------------------------------------------------------------------------
# Registration tests.
# ---------------------------------------------------------------------------


def test_tool_is_registered_in_registry():
    """fetch_iucn_red_list_range appears in TOOL_REGISTRY with expected metadata."""
    assert "fetch_iucn_red_list_range" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["fetch_iucn_red_list_range"]
    assert entry.metadata.name == "fetch_iucn_red_list_range"
    assert entry.metadata.ttl_class == "static-30d"
    assert entry.metadata.source_class == "iucn_red_list"
    assert entry.metadata.cacheable is True
    assert entry.metadata.supports_global_query is True
    assert entry.metadata.payload_mb_estimator_name == "estimate_payload_mb"


def test_payload_estimator_returns_kickoff_bound():
    """estimate_payload_mb returns the kickoff-specified ~0.5 MB upper bound."""
    assert estimate_payload_mb(species_name="Puma concolor") == 0.5
    assert estimate_payload_mb() == 0.5


# ---------------------------------------------------------------------------
# Input validation.
# ---------------------------------------------------------------------------


def test_empty_species_name_raises():
    with pytest.raises(IUCNInputError):
        _validate_species_name("")
    with pytest.raises(IUCNInputError):
        _validate_species_name("   ")


def test_non_string_species_name_raises():
    with pytest.raises(IUCNInputError):
        _validate_species_name(123)  # type: ignore[arg-type]


def test_species_name_too_long_raises():
    with pytest.raises(IUCNInputError):
        _validate_species_name("x" * 300)


def test_species_name_normalizes_whitespace():
    assert _validate_species_name("  Puma   concolor  ") == "Puma concolor"


def test_invalid_region_chars_raises():
    with pytest.raises(IUCNInputError):
        _validate_region("../etc/passwd")
    with pytest.raises(IUCNInputError):
        _validate_region("a b")  # space


def test_region_lowercases():
    assert _validate_region("Europe") == "europe"
    assert _validate_region("PAN-AFRICA") == "pan-africa"


def test_degenerate_inputs_raise_through_public_tool():
    """Public-tool surface validates inputs BEFORE any auth/network call."""
    with pytest.raises(IUCNInputError):
        fetch_iucn_red_list_range(species_name="")
    with pytest.raises(IUCNInputError):
        fetch_iucn_red_list_range(species_name="Panthera tigris", region="bad/region")


# ---------------------------------------------------------------------------
# Auth gate tests (three-path waterfall).
# ---------------------------------------------------------------------------


def test_no_credentials_raises_auth_error_before_network_call(monkeypatch):
    """No api_key / secret_ref / env var → IUCNAuthError, no network call."""
    monkeypatch.delenv("TRID3NT_IUCN_RED_LIST_API_KEY", raising=False)
    # Patch read_through to fail loudly if it's reached — the auth gate must
    # raise BEFORE we get there.
    sentinel = RuntimeError("read_through should not be invoked")
    with patch(
        "trid3nt_server.tools.fetch_iucn_red_list_range.read_through",
        side_effect=sentinel,
    ):
        with pytest.raises(IUCNAuthError) as excinfo:
            fetch_iucn_red_list_range(species_name="Puma concolor")
    # Error message should NOT echo any candidate key value.
    assert "TRID3NT_IUCN_RED_LIST_API_KEY" in str(excinfo.value)


def test_explicit_api_key_resolves():
    """Path 1: explicit api_key= is the highest-precedence resolver."""
    assert _resolve_api_key(api_key="my-key", secret_ref=None) == "my-key"
    # Whitespace stripped.
    assert _resolve_api_key(api_key="  my-key  ", secret_ref=None) == "my-key"


def test_explicit_api_key_empty_raises():
    with pytest.raises(IUCNAuthError):
        _resolve_api_key(api_key="", secret_ref=None)
    with pytest.raises(IUCNAuthError):
        _resolve_api_key(api_key="   ", secret_ref=None)


def test_secret_ref_provider_mismatch_raises():
    """secret_ref with provider != 'iucn_red_list' → IUCNAuthError."""
    bad = SecretRecord(
        secret_id=new_ulid(),
        provider="ebird",  # wrong provider
        case_id=new_ulid(),
        vault_ref="projects/p/secrets/s/versions/1",
        added_at=now_utc(),
    )

    class FakePersistence:
        def get_secret_value(self, ref):  # pragma: no cover — should not be called
            return "should-not-reach"

    with pytest.raises(IUCNAuthError) as excinfo:
        _resolve_api_key(api_key=None, secret_ref=bad, persistence=FakePersistence())
    assert "provider mismatch" in str(excinfo.value)


def test_secret_ref_without_persistence_raises():
    good = SecretRecord(
        secret_id=new_ulid(),
        provider="iucn_red_list",
        case_id=new_ulid(),
        vault_ref="projects/p/secrets/s/versions/1",
        added_at=now_utc(),
    )
    with pytest.raises(IUCNAuthError):
        _resolve_api_key(api_key=None, secret_ref=good, persistence=None)


def test_secret_ref_with_sync_persistence_resolves():
    """Path 2: secret_ref + persistence mock → key value."""
    good = SecretRecord(
        secret_id=new_ulid(),
        provider="iucn_red_list",
        case_id=new_ulid(),
        vault_ref="projects/p/secrets/s/versions/1",
        added_at=now_utc(),
    )

    class FakePersistence:
        def get_secret_value(self, ref):
            return "vault-resolved-key"

    assert (
        _resolve_api_key(api_key=None, secret_ref=good, persistence=FakePersistence())
        == "vault-resolved-key"
    )


def test_secret_ref_awaitable_result_resolves_synchronously():
    """An async ``get_secret_value`` (the production Persistence) is resolved
    synchronously by the tool — NOT refused.

    job credential-pipeline-generic: the generic credential flow injects a
    ``secret_ref`` and the agent's real ``Persistence.get_secret_value`` is a
    coroutine. The IUCN tool body is sync, so it must materialize the awaitable
    on a worker-thread loop (mirrors eBird / FIRMS) — otherwise an IUCN vault
    key would dead-end. The previous behavior (refuse awaitables) was the bug.
    """
    good = SecretRecord(
        secret_id=new_ulid(),
        provider="iucn_red_list",
        case_id=new_ulid(),
        vault_ref="projects/p/secrets/s/versions/1",
        added_at=now_utc(),
    )

    # A resolver whose get_secret_value returns a coroutine (the production
    # async Persistence shape).
    class AsyncPersistence:
        def get_secret_value(self, ref):
            async def _inner():
                return "vault-async-key"

            return _inner()

    assert (
        _resolve_api_key(
            api_key=None, secret_ref=good, persistence=AsyncPersistence()
        )
        == "vault-async-key"
    )


def test_secret_ref_resolves_via_module_persistence_seam():
    """When no ``persistence=`` kwarg is passed, the module-level seam (bound
    by the agent at startup) resolves the vault key — the path the server's
    ``_inject_secret_ref`` exercises (it injects secret_ref alone)."""
    import trid3nt_server.tools.fetch_iucn_red_list_range as iucn_mod

    good = SecretRecord(
        secret_id=new_ulid(),
        provider="iucn_red_list",
        case_id=new_ulid(),
        vault_ref="projects/p/secrets/s/versions/1",
        added_at=now_utc(),
    )

    class SeamPersistence:
        def get_secret_value(self, ref):
            return "seam-resolved-key"

    iucn_mod.set_persistence_for_secrets(SeamPersistence())
    try:
        assert (
            _resolve_api_key(api_key=None, secret_ref=good, persistence=None)
            == "seam-resolved-key"
        )
    finally:
        iucn_mod.set_persistence_for_secrets(None)


def test_env_var_fallback_resolves(monkeypatch):
    """Path 3: TRID3NT_IUCN_RED_LIST_API_KEY env var resolves when set."""
    monkeypatch.setenv("TRID3NT_IUCN_RED_LIST_API_KEY", "env-resolved-key")
    assert _resolve_api_key(api_key=None, secret_ref=None) == "env-resolved-key"


# ---------------------------------------------------------------------------
# Mocked-network tests.
# ---------------------------------------------------------------------------


def test_mocked_happy_path_emits_one_feature_flatgeobuf(monkeypatch):
    """A mocked /species response yields a FlatGeobuf with 1 placeholder feature."""
    import geopandas as gpd

    monkeypatch.delenv("TRID3NT_IUCN_RED_LIST_API_KEY", raising=False)
    fake_gcs = FakeStorageClient()

    with patch(
        "trid3nt_server.tools.fetch_iucn_red_list_range._fetch_iucn_species_payload",
        return_value=_iucn_species_payload(),
    ), patch(
        "trid3nt_server.tools.fetch_iucn_red_list_range.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_iucn_red_list_range(
            species_name="Puma concolor",
            api_key="dummy-key",
        )

    assert result.layer_type == "vector"
    assert result.role == "context"
    assert result.units is None
    assert result.style_preset == "iucn_red_list_range"
    assert "Puma concolor" in result.name
    assert result.uri.startswith("s3://")
    assert len(fake_gcs.store) == 1

    # Round-trip the bytes through geopandas to confirm the schema + the
    # placeholder-geometry sentinel.
    fgb_bytes = next(iter(fake_gcs.store.values()))
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as tf:
        path = tf.name
        tf.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        assert len(gdf) == 1
        row = gdf.iloc[0]
        assert row["scientific_name"] == "Puma concolor"
        assert row["category"] == "LC"
        assert row["common_name"] == "Cougar"
        assert bool(row["is_placeholder_geometry"]) is True
        assert row["region"] == "global"
        # CRS is EPSG:4326 (placeholder square).
        assert gdf.crs.to_epsg() == 4326
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def test_mocked_empty_result_returns_data_deficient_sentinel(monkeypatch):
    """IUCN returns an empty 'result' list → still 1 feature with DD sentinel."""
    import geopandas as gpd

    monkeypatch.delenv("TRID3NT_IUCN_RED_LIST_API_KEY", raising=False)
    fake_gcs = FakeStorageClient()

    with patch(
        "trid3nt_server.tools.fetch_iucn_red_list_range._fetch_iucn_species_payload",
        return_value=_iucn_species_payload(empty=True, name="Nonexistent fakeus"),
    ), patch(
        "trid3nt_server.tools.fetch_iucn_red_list_range.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_iucn_red_list_range(
            species_name="Nonexistent fakeus",
            api_key="dummy-key",
        )

    fgb_bytes = next(iter(fake_gcs.store.values()))
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as tf:
        path = tf.name
        tf.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        assert len(gdf) == 1
        row = gdf.iloc[0]
        # Empty result → category falls back to "DD" sentinel.
        assert row["category"] == "DD"
        assert row["scientific_name"] == "Nonexistent fakeus"
        assert bool(row["is_placeholder_geometry"]) is True
        # Region echo preserved.
        assert row["region"] == "global"
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

    # The returned LayerURI still resolves (placeholder geometry but valid uri).
    assert result.uri is not None


def test_mocked_token_invalid_body_raises_auth_error(monkeypatch):
    """IUCN 'Token not valid!' in body (HTTP 200) → IUCNAuthError."""
    import httpx

    monkeypatch.delenv("TRID3NT_IUCN_RED_LIST_API_KEY", raising=False)
    fake_gcs = FakeStorageClient()

    # Use a fake httpx client to simulate the 200 / message body path.
    def _mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": "Token not valid!"})

    transport = httpx.MockTransport(_mock_handler)

    def _make_fake_client():
        return httpx.Client(
            transport=transport,
            timeout=5.0,
            follow_redirects=True,
        )

    from trid3nt_server.tools import fetch_iucn_red_list_range as mod

    real_fetch = mod._fetch_iucn_species_payload

    def patched_fetch(species_name, region, api_key, *, client=None):
        with _make_fake_client() as c:
            return real_fetch(species_name, region, api_key, client=c)

    with patch(
        "trid3nt_server.tools.fetch_iucn_red_list_range._fetch_iucn_species_payload",
        side_effect=patched_fetch,
    ), patch(
        "trid3nt_server.tools.fetch_iucn_red_list_range.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        with pytest.raises(IUCNAuthError):
            fetch_iucn_red_list_range(
                species_name="Puma concolor",
                api_key="bad-key",
            )


def test_mocked_5xx_raises_upstream_error(monkeypatch):
    """IUCN returns HTTP 502 → IUCNUpstreamError (retryable)."""
    import httpx

    monkeypatch.delenv("TRID3NT_IUCN_RED_LIST_API_KEY", raising=False)
    fake_gcs = FakeStorageClient()

    def _mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    transport = httpx.MockTransport(_mock_handler)

    from trid3nt_server.tools import fetch_iucn_red_list_range as mod

    real_fetch = mod._fetch_iucn_species_payload

    def patched_fetch(species_name, region, api_key, *, client=None):
        with httpx.Client(transport=transport, timeout=5.0) as c:
            return real_fetch(species_name, region, api_key, client=c)

    with patch(
        "trid3nt_server.tools.fetch_iucn_red_list_range._fetch_iucn_species_payload",
        side_effect=patched_fetch,
    ), patch(
        "trid3nt_server.tools.fetch_iucn_red_list_range.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        with pytest.raises(IUCNUpstreamError):
            fetch_iucn_red_list_range(
                species_name="Puma concolor",
                api_key="dummy-key",
            )


def test_cache_hit_skips_fetch(monkeypatch):
    """Second call with identical (species, region) hits the cache; fetch_fn invoked once."""
    monkeypatch.delenv("TRID3NT_IUCN_RED_LIST_API_KEY", raising=False)
    fake_gcs = FakeStorageClient()

    call_counter = {"n": 0}

    def _counting_fetch(species_name, region, api_key, *, client=None):
        call_counter["n"] += 1
        return _iucn_species_payload(name=species_name, category="VU")

    with patch(
        "trid3nt_server.tools.fetch_iucn_red_list_range._fetch_iucn_species_payload",
        side_effect=_counting_fetch,
    ), patch(
        "trid3nt_server.tools.fetch_iucn_red_list_range.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        r1 = fetch_iucn_red_list_range(
            species_name="Panthera tigris", api_key="key-A"
        )
        r2 = fetch_iucn_red_list_range(
            species_name="Panthera tigris", api_key="key-B"  # different key, same cache
        )

    assert r1.uri == r2.uri, "identical (species, region) must hit same cache key"
    assert call_counter["n"] == 1, (
        f"fetch_fn should be invoked exactly once on cache HIT; got {call_counter['n']}"
    )


def test_cache_key_does_not_include_api_key(monkeypatch):
    """Two calls with different api_keys but same (species, region) reuse the cache."""
    monkeypatch.delenv("TRID3NT_IUCN_RED_LIST_API_KEY", raising=False)
    fake_gcs = FakeStorageClient()

    with patch(
        "trid3nt_server.tools.fetch_iucn_red_list_range._fetch_iucn_species_payload",
        return_value=_iucn_species_payload(),
    ), patch(
        "trid3nt_server.tools.fetch_iucn_red_list_range.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        r1 = fetch_iucn_red_list_range(species_name="Puma concolor", api_key="k1")
        r2 = fetch_iucn_red_list_range(species_name="Puma concolor", api_key="k2")
        r3 = fetch_iucn_red_list_range(species_name="puma   concolor", api_key="k3")

    # All three URIs equal: cache key is normalized to lower-cased species_name.
    assert r1.uri == r2.uri == r3.uri
    # Only one object written to the fake GCS.
    assert len(fake_gcs.store) == 1


# ---------------------------------------------------------------------------
# Live integration tests (TRID3NT_TEST_LIVE_IUCN_RED_LIST=1 + valid key to run).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not (_LIVE_IUCN and _LIVE_KEY),
    reason=(
        "Set TRID3NT_TEST_LIVE_IUCN_RED_LIST=1 + TRID3NT_IUCN_RED_LIST_API_KEY "
        "to run live IUCN Red List tests"
    ),
)
def test_live_puma_concolor_returns_known_category():
    """Live: fetch Puma concolor and assert IUCN category is a documented enum value.

    The codified job-0086 lesson applies: this asserts SEMANTIC correctness
    (category is one of the documented IUCN enum values), not just
    bytes-round-trip.
    """
    import geopandas as gpd

    fake_gcs = FakeStorageClient()

    with patch(
        "trid3nt_server.tools.fetch_iucn_red_list_range.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_iucn_red_list_range(species_name="Puma concolor")

    assert result.uri.startswith("s3://")
    fgb_bytes = next(iter(fake_gcs.store.values()))
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as tf:
        path = tf.name
        tf.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        assert len(gdf) == 1
        row = gdf.iloc[0]
        cat = str(row["category"])
        assert cat in _IUCN_CATEGORIES, (
            f"IUCN returned an unexpected category {cat!r} for Puma concolor; "
            f"expected one of {sorted(_IUCN_CATEGORIES)}"
        )
        # Geometry is still placeholder at v0.1 (OQ-0129-RANGE-SPATIAL).
        assert bool(row["is_placeholder_geometry"]) is True
        # Scientific name round-trips.
        assert "Puma concolor" in str(row["scientific_name"])
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
