"""Unit tests for the QGIS discovery atomic tools (job-0034, FR-AS-9 Level 1a).

Coverage:

- ``list_qgis_algorithms`` happy path with a stubbed submitter — parses
  representative ``qgis_process list`` output and returns capped/ranked
  summaries.
- ``describe_qgis_algorithm`` happy path with a stubbed submitter — parses
  ``qgis_process help <id>`` into structured parameter + output dicts.
- Cache-hit replay: a second call with the same params returns the same
  result without re-invoking the submitter (FR-DC-3 / FR-DC-4).
- Worker submission failure re-raises (NFR-R-1 / FR-CE-8 fail-fast).
- The ``set_worker_submitter`` DI binding wires the qgis_process body so it
  no longer raises ``RuntimeError("worker submitter is not bound")``.
- Registry presence: both tools appear in ``TOOL_REGISTRY`` after the
  eager-import wiring in ``trid3nt_server.main._import_tools_registry``.
"""

from __future__ import annotations

import pytest

from trid3nt_server.tools import TOOL_REGISTRY, passthroughs, qgis_discovery
from trid3nt_server.tools.qgis_discovery import (
    CURATED_ALLOWLIST,
    MAX_LIST_RESULTS,
    SOURCE_CLASS,
    _apply_curated_allowlist,
    _parse_qgis_help_output,
    _parse_qgis_list_output,
    curated_allowlist,
    describe_qgis_algorithm,
    list_qgis_algorithms,
)


# ---------------------------------------------------------------------------
# Representative fixtures from real qgis_process output (QGIS 3.40 local).
# ---------------------------------------------------------------------------


_FAKE_LIST_OUTPUT = """Available algorithms

QGIS (3D)
\t3d:tessellate\tTessellate

GDAL
\tgdal:aspect\tAspect
\tgdal:cliprasterbyextent\tClip raster by extent

QGIS (native c++)
\tnative:zonalstatistics\tZonal statistics (in place)
\tnative:reprojectlayer\tReproject layer
\tnative:reclassifybytable\tReclassify by table
"""


# A wider list spanning ALL providers - used to exercise the curated allowlist
# (native/gdal/qgis/3d pass wholesale; GRASS r.watershed passes via the
# explicit-id set; a non-curated GRASS algorithm + a non-curated SAGA algorithm
# are dropped by the curated default).
_FAKE_LIST_OUTPUT_ALL_PROVIDERS = """Available algorithms

QGIS (3D)
\t3d:tessellate\tTessellate

GDAL
\tgdal:aspect\tAspect

QGIS (native c++)
\tnative:zonalstatistics\tZonal statistics (in place)

QGIS
\tqgis:basicstatisticsforfields\tBasic statistics for fields

GRASS
\tgrass:r.watershed\tr.watershed
\tgrass:r.water.outlet\tr.water.outlet
\tgrass:r.sun\tr.sun

SAGA
\tsaga:fillsinkswangliu\tFill sinks (Wang and Liu)
\tsaga:thinplatespline\tThin plate spline
"""


_FAKE_HELP_OUTPUT = """Zonal statistics (in place) (native:zonalstatistics)

----------------
Description
----------------
Calculates statistics for a raster layer's values for each feature of an overlapping polygon vector layer.

----------------
Arguments
----------------

INPUT_RASTER: Raster layer
\tArgument type:\traster
\tAcceptable values:
\t\t- Path to a raster layer
RASTER_BAND: Raster band
\tDefault value:\t1
\tArgument type:\tband
\tAcceptable values:
\t\t- Integer value representing an existing raster band number
INPUT_VECTOR: Vector layer containing zones
\tArgument type:\tvector
\tAcceptable values:
\t\t- Path to a vector layer
COLUMN_PREFIX: Output column prefix
\tDefault value:\t_
\tArgument type:\tstring

----------------
Outputs
----------------

INPUT_VECTOR: Zonal statistics <outputVector>
"""


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


class _FakeBlob:
    """In-memory ``google.cloud.storage`` blob duck-type for cache tests."""

    def __init__(self, store: dict[str, bytes], path: str) -> None:
        self._store = store
        self._path = path
        self.custom_time = None
        self.cache_control = None

    def exists(self) -> bool:
        return self._path in self._store

    def download_as_bytes(self) -> bytes:
        return self._store[self._path]

    def upload_from_string(self, data: bytes | str, content_type: str | None = None) -> None:
        if isinstance(data, str):
            data = data.encode("utf-8")
        self._store[self._path] = data


class _FakeBucket:
    def __init__(self, store: dict[str, bytes]) -> None:
        self._store = store

    def blob(self, path: str) -> _FakeBlob:
        return _FakeBlob(self._store, path)


class _FakeStorageClient:
    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}

    def bucket(self, name: str) -> _FakeBucket:
        return _FakeBucket(self._store)


@pytest.fixture()
def fake_storage(monkeypatch: pytest.MonkeyPatch) -> _FakeStorageClient:
    """Route ``read_through`` through an in-memory S3 store (GCP decommissioned).

    The production cache shim is S3-only via boto3; tests must not touch the
    network. This patches the tool module's ``read_through`` with an in-memory
    implementation that mints ``s3://`` URIs and reads/writes ``fake._store``
    (keyed by object KEY), so the cache hit/miss/write assertions hold.
    """
    from trid3nt_server.tools.cache import (
        CACHE_BUCKET,
        cache_path,
        compute_cache_key,
        is_cacheable,
        ReadThroughResult,
    )

    fake = _FakeStorageClient()

    def wrapped(metadata, params, ext, fetch_fn, **kwargs):
        bucket = kwargs.get("bucket") or CACHE_BUCKET
        source_id = kwargs.get("source_id") or (metadata.source_class or metadata.name)
        now = kwargs.get("now")
        force_refresh = kwargs.get("force_refresh", False)
        if not is_cacheable(metadata):
            return ReadThroughResult(uri=None, data=fetch_fn(), hit=False)
        key = compute_cache_key(source_id, params, metadata.ttl_class, now=now)
        path = cache_path(metadata.source_class, metadata.ttl_class, key, ext)
        uri = f"s3://{bucket}/{path}"
        if not force_refresh and path in fake._store:
            return ReadThroughResult(uri=uri, data=fake._store[path], hit=True)
        data = fetch_fn()
        fake._store[path] = data
        return ReadThroughResult(uri=uri, data=data, hit=False)

    monkeypatch.setattr(qgis_discovery, "read_through", wrapped)
    return fake


@pytest.fixture()
def stubbed_submitter():
    """Bind a programmable stub submitter and restore on teardown.

    Each test sets ``stub.responses["list"]`` / ``stub.responses["help:<id>"]``
    to the dict the submitter should return; ``stub.calls`` records invocation
    args for assertions.
    """

    class _Stub:
        def __init__(self) -> None:
            self.responses: dict[str, dict] = {}
            self.calls: list[tuple[tuple, int]] = []

        def __call__(self, args: list[str], timeout_s: int) -> dict:
            self.calls.append((tuple(args), timeout_s))
            if args[0] == "list":
                return self.responses.get(
                    "list",
                    {"stdout": _FAKE_LIST_OUTPUT, "returncode": 0, "duration_s": 0.1},
                )
            if args[0] == "help":
                key = f"help:{args[1]}"
                return self.responses.get(
                    key,
                    {"stdout": _FAKE_HELP_OUTPUT, "returncode": 0, "duration_s": 0.05},
                )
            raise AssertionError(f"unexpected stub call: {args!r}")

    stub = _Stub()
    saved = passthroughs._WORKER_SUBMITTER
    passthroughs.set_worker_submitter(stub)
    try:
        yield stub
    finally:
        passthroughs._WORKER_SUBMITTER = saved  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Registration / metadata.
# ---------------------------------------------------------------------------


def test_discovery_tools_register_with_expected_metadata() -> None:
    """Both tools land in ``TOOL_REGISTRY`` with static-30d/qgis_algorithms_catalog."""
    for tool_name in ("list_qgis_algorithms", "describe_qgis_algorithm"):
        assert tool_name in TOOL_REGISTRY, f"{tool_name} not registered"
        entry = TOOL_REGISTRY[tool_name]
        assert entry.metadata.ttl_class == "static-30d"
        assert entry.metadata.source_class == SOURCE_CLASS
        assert entry.metadata.cacheable is True


# ---------------------------------------------------------------------------
# Parser unit tests (no submitter, no cache — pure functions).
# ---------------------------------------------------------------------------


def test_parse_list_extracts_provider_and_id() -> None:
    summaries = _parse_qgis_list_output(_FAKE_LIST_OUTPUT)
    ids = [s["algorithm_id"] for s in summaries]
    assert "3d:tessellate" in ids
    assert "gdal:aspect" in ids
    assert "native:zonalstatistics" in ids
    # The provider header for `native:zonalstatistics` is "QGIS (native c++)".
    zs = next(s for s in summaries if s["algorithm_id"] == "native:zonalstatistics")
    assert zs["provider"] == "QGIS (native c++)"
    assert zs["name"] == "Zonal statistics (in place)"


def test_parse_help_extracts_parameters_and_outputs() -> None:
    desc = _parse_qgis_help_output(_FAKE_HELP_OUTPUT, "native:zonalstatistics")
    assert desc["algorithm_id"] == "native:zonalstatistics"
    assert desc["name"] == "Zonal statistics (in place)"
    assert "Calculates statistics" in desc["description"]
    param_names = [p["name"] for p in desc["parameters"]]
    assert param_names == [
        "INPUT_RASTER",
        "RASTER_BAND",
        "INPUT_VECTOR",
        "COLUMN_PREFIX",
    ]
    raster_band = next(p for p in desc["parameters"] if p["name"] == "RASTER_BAND")
    assert raster_band["type"] == "band"
    assert raster_band["default"] == "1"
    # Outputs are parsed too.
    out_names = [o["name"] for o in desc["outputs"]]
    assert "INPUT_VECTOR" in out_names
    # Raw help is preserved for tolerant agents.
    assert "Zonal statistics" in desc["raw_help"]


# ---------------------------------------------------------------------------
# Tool happy paths via the stubbed submitter + fake cache.
# ---------------------------------------------------------------------------


def test_list_qgis_algorithms_happy_path(
    stubbed_submitter, fake_storage: _FakeStorageClient
) -> None:
    """Stubbed submitter + fake cache → tool returns parsed summaries."""
    result = list_qgis_algorithms()
    assert isinstance(result, list)
    assert result, "expected at least one summary from the fake list output"
    # Capped to MAX_LIST_RESULTS.
    assert len(result) <= MAX_LIST_RESULTS
    # The fake fixture has 6 algorithms — well under the cap.
    assert len(result) == 6
    # Submitter called exactly once on the first call.
    assert len(stubbed_submitter.calls) == 1
    assert stubbed_submitter.calls[0][0] == ("list",)


def test_describe_qgis_algorithm_happy_path(
    stubbed_submitter, fake_storage: _FakeStorageClient
) -> None:
    desc = describe_qgis_algorithm("native:zonalstatistics")
    assert desc["algorithm_id"] == "native:zonalstatistics"
    assert any(p["name"] == "INPUT_RASTER" for p in desc["parameters"])
    # Submitter called exactly once with the help args.
    assert stubbed_submitter.calls == [(("help", "native:zonalstatistics"), 60)]


def test_list_qgis_algorithms_category_filter(
    stubbed_submitter, fake_storage: _FakeStorageClient
) -> None:
    result = list_qgis_algorithms(category_filter="gdal")
    assert all("gdal" in s["provider"].lower() for s in result)
    assert all(s["algorithm_id"].startswith("gdal:") for s in result)


def test_list_qgis_algorithms_search_terms_ranks_matches_first(
    stubbed_submitter, fake_storage: _FakeStorageClient
) -> None:
    result = list_qgis_algorithms(search_terms="zonal")
    # The matching algorithm is first.
    assert result[0]["algorithm_id"] == "native:zonalstatistics"


# ---------------------------------------------------------------------------
# Cache-hit replay.
# ---------------------------------------------------------------------------


def test_list_qgis_algorithms_cache_hit_replays_without_submitter_call(
    stubbed_submitter, fake_storage: _FakeStorageClient
) -> None:
    """Second call hits the (fake) cache and skips the submitter."""
    first = list_qgis_algorithms()
    assert len(stubbed_submitter.calls) == 1

    # The fake bucket should now hold one blob.
    assert len(fake_storage._store) == 1

    second = list_qgis_algorithms()
    # No additional submitter call.
    assert len(stubbed_submitter.calls) == 1
    # Same parsed result.
    assert [s["algorithm_id"] for s in second] == [s["algorithm_id"] for s in first]


def test_describe_qgis_algorithm_cache_hit_replays_without_submitter_call(
    stubbed_submitter, fake_storage: _FakeStorageClient
) -> None:
    first = describe_qgis_algorithm("native:zonalstatistics")
    assert len(stubbed_submitter.calls) == 1
    second = describe_qgis_algorithm("native:zonalstatistics")
    assert len(stubbed_submitter.calls) == 1
    assert first["parameters"] == second["parameters"]


# ---------------------------------------------------------------------------
# Failure paths.
# ---------------------------------------------------------------------------


def test_submitter_failure_re_raises_no_sentinel_written(
    stubbed_submitter, fake_storage: _FakeStorageClient
) -> None:
    """A failing submitter raises through; the cache stays empty (no poison)."""

    def _boom(args: list[str], timeout_s: int) -> dict:
        raise RuntimeError("simulated worker failure")

    passthroughs.set_worker_submitter(_boom)
    with pytest.raises(RuntimeError, match="simulated worker failure"):
        list_qgis_algorithms()
    # No sentinel persisted on failure.
    assert fake_storage._store == {}


def test_discovery_tool_with_no_submitter_bound_raises(
    fake_storage: _FakeStorageClient,
) -> None:
    """An unbound submitter raises a clear ``RuntimeError`` per FR-CE-8."""
    saved = passthroughs._WORKER_SUBMITTER
    passthroughs._WORKER_SUBMITTER = None  # type: ignore[attr-defined]
    try:
        with pytest.raises(RuntimeError, match="worker submitter is not bound"):
            list_qgis_algorithms()
    finally:
        passthroughs._WORKER_SUBMITTER = saved  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# qgis_process DI binding — the body no longer raises NotImplementedError.
# ---------------------------------------------------------------------------


def test_qgis_process_raises_runtime_error_when_no_backend(monkeypatch) -> None:
    """With no docker image, no docker, and no local qgis_process, the
    qgis_process pass-through raises an actionable RuntimeError.

    job-0308 (Decision Q) rewired qgis_process OFF the old job-0032
    NotImplementedError stub and onto a stage-then-mount docker path
    (``TRID3NT_QGIS_DOCKER_IMAGE`` / the ``grace2-qgis`` image present on the
    EC2 box) with a local-``qgis_process``-on-PATH dev fallback. When NO
    backend is reachable the body raises a RuntimeError telling the operator
    how to provide one. This pins that contract deterministically — we
    monkeypatch ``shutil.which`` to None and clear the image env so the
    result does not depend on whether docker / qgis_process happen to be
    installed on the test host. (The ``_WORKER_SUBMITTER`` binding is NOT
    used by qgis_process anymore — it remains live only for the discovery
    tools, covered by the tests above.)

    Reliability hardening 2026-06-29: on-box ``qgis_process`` RUN is now gated
    OFF by default (it returns an honest "offloaded" result instead of running
    on the shared box). The no-backend RuntimeError contract still holds when
    an operator ENABLES on-box execution, so this test sets
    ``TRID3NT_QGIS_ONBOX_DOCKER=on`` to reach the backend-resolution path.
    """
    import shutil

    from trid3nt_server.tools.passthroughs import qgis_process

    monkeypatch.setenv("TRID3NT_QGIS_ONBOX_DOCKER", "on")
    monkeypatch.delenv("TRID3NT_QGIS_DOCKER_IMAGE", raising=False)
    monkeypatch.setattr(shutil, "which", lambda _name: None)

    with pytest.raises(RuntimeError, match="qgis_process unavailable"):
        qgis_process(algorithm="native:zonalstatistics", params={})


# ---------------------------------------------------------------------------
# Curated allowlist (job-0308 Q-discovery lane).
# ---------------------------------------------------------------------------


def test_curated_allowlist_default_keeps_high_value_drops_noise() -> None:
    """The pure post-filter keeps native/gdal/qgis/3d + explicit GRASS picks,
    drops non-curated GRASS / SAGA algorithms."""
    summaries = _parse_qgis_list_output(_FAKE_LIST_OUTPUT_ALL_PROVIDERS)
    kept_ids = {s["algorithm_id"] for s in _apply_curated_allowlist(summaries)}
    # Wholesale provider families.
    assert "native:zonalstatistics" in kept_ids
    assert "gdal:aspect" in kept_ids
    assert "qgis:basicstatisticsforfields" in kept_ids
    assert "3d:tessellate" in kept_ids
    # Explicit GRASS hydrology picks survive.
    assert "grass:r.watershed" in kept_ids
    assert "grass:r.water.outlet" in kept_ids
    # Explicit SAGA pick survives.
    assert "saga:fillsinkswangliu" in kept_ids
    # Non-curated GRASS + SAGA dropped.
    assert "grass:r.sun" not in kept_ids
    assert "saga:thinplatespline" not in kept_ids


def test_curated_allowlist_module_constant_includes_grass_hydrology() -> None:
    """The exported constant enumerates the GRASS hydrology set the roadmap
    leans on."""
    for hydro in (
        "grass:r.watershed",
        "grass:r.water.outlet",
        "grass:r.stream.extract",
        "grass:r.fill.dir",
    ):
        assert hydro in CURATED_ALLOWLIST


def test_curated_allowlist_env_all_disables_curation(monkeypatch) -> None:
    """``TRID3NT_QGIS_ALLOWLIST=all`` -> empty-sets sentinel -> no filtering."""
    monkeypatch.setenv("TRID3NT_QGIS_ALLOWLIST", "all")
    prefixes, explicit = curated_allowlist()
    assert prefixes == frozenset()
    assert explicit == frozenset()
    summaries = _parse_qgis_list_output(_FAKE_LIST_OUTPUT_ALL_PROVIDERS)
    # Sentinel -> the filter returns everything untouched.
    assert _apply_curated_allowlist(summaries) == summaries


def test_curated_allowlist_env_custom_overrides(monkeypatch) -> None:
    """A custom comma list of <provider>:* wildcards + ids replaces the set."""
    monkeypatch.setenv("TRID3NT_QGIS_ALLOWLIST", "gdal:*, grass:r.sun")
    prefixes, explicit = curated_allowlist()
    assert prefixes == frozenset({"gdal"})
    assert explicit == frozenset({"grass:r.sun"})
    summaries = _parse_qgis_list_output(_FAKE_LIST_OUTPUT_ALL_PROVIDERS)
    kept_ids = {s["algorithm_id"] for s in _apply_curated_allowlist(summaries)}
    assert kept_ids == {"gdal:aspect", "grass:r.sun"}


def test_curated_allowlist_trailing_star_id_prefix(monkeypatch) -> None:
    """P0 edge: a ``<provider>:<stem>*`` token (e.g. ``gdal:aspect*``) is an
    id-PREFIX match, not an exact id and not a provider wildcard.

    Pre-fix this token matched NOTHING: it ends in ``*`` but is not exactly
    ``:*`` (so not a provider wildcard) and is not a literal id (so not an exact
    match). It must now keep every algorithm whose id starts with the stem.
    """
    monkeypatch.setenv("TRID3NT_QGIS_ALLOWLIST", "gdal:aspect*")
    prefixes, explicit = curated_allowlist()
    # Not a provider-prefix wildcard.
    assert prefixes == frozenset()
    # Stored as an explicit entry WITH the trailing star preserved so the
    # matcher treats it as an id-prefix.
    assert explicit == frozenset({"gdal:aspect*"})

    summaries = [
        {
            "algorithm_id": "gdal:aspect",
            "name": "Aspect",
            "provider": "GDAL",
            "brief_description": "Aspect",
        },
        {
            "algorithm_id": "gdal:aspectband",
            "name": "Aspect (band)",
            "provider": "GDAL",
            "brief_description": "Aspect (band)",
        },
        {
            "algorithm_id": "gdal:slope",
            "name": "Slope",
            "provider": "GDAL",
            "brief_description": "Slope",
        },
        {
            "algorithm_id": "native:zonalstatistics",
            "name": "Zonal statistics",
            "provider": "QGIS",
            "brief_description": "Zonal statistics",
        },
    ]
    kept_ids = {s["algorithm_id"] for s in _apply_curated_allowlist(summaries)}
    # Both ``gdal:aspect*`` matches kept; ``gdal:slope`` + native dropped.
    assert kept_ids == {"gdal:aspect", "gdal:aspectband"}


def test_curated_allowlist_trailing_star_mixed_with_exact_and_wildcard(
    monkeypatch,
) -> None:
    """A trailing-* id-prefix coexists with exact ids and provider wildcards."""
    monkeypatch.setenv(
        "TRID3NT_QGIS_ALLOWLIST", "native:*, gdal:aspect*, grass:r.watershed"
    )
    prefixes, explicit = curated_allowlist()
    assert prefixes == frozenset({"native"})
    assert explicit == frozenset({"gdal:aspect*", "grass:r.watershed"})

    summaries = [
        {"algorithm_id": "native:buffer", "name": "Buffer", "provider": "QGIS",
         "brief_description": "Buffer"},
        {"algorithm_id": "gdal:aspect", "name": "Aspect", "provider": "GDAL",
         "brief_description": "Aspect"},
        {"algorithm_id": "gdal:slope", "name": "Slope", "provider": "GDAL",
         "brief_description": "Slope"},
        {"algorithm_id": "grass:r.watershed", "name": "r.watershed",
         "provider": "GRASS", "brief_description": "r.watershed"},
        {"algorithm_id": "grass:r.sun", "name": "r.sun", "provider": "GRASS",
         "brief_description": "r.sun"},
    ]
    kept_ids = {s["algorithm_id"] for s in _apply_curated_allowlist(summaries)}
    assert kept_ids == {"native:buffer", "gdal:aspect", "grass:r.watershed"}


def test_list_qgis_algorithms_curated_by_default(
    stubbed_submitter, fake_storage: _FakeStorageClient
) -> None:
    """The tool curates by default: non-curated GRASS/SAGA dropped."""
    stubbed_submitter.responses["list"] = {
        "stdout": _FAKE_LIST_OUTPUT_ALL_PROVIDERS,
        "returncode": 0,
        "duration_s": 0.1,
    }
    result = list_qgis_algorithms()
    ids = {s["algorithm_id"] for s in result}
    assert "native:zonalstatistics" in ids
    assert "grass:r.watershed" in ids  # explicit hydrology pick
    assert "grass:r.sun" not in ids  # non-curated GRASS dropped
    assert "saga:thinplatespline" not in ids  # non-curated SAGA dropped


def test_list_qgis_algorithms_include_all_escape_hatch(
    stubbed_submitter, fake_storage: _FakeStorageClient
) -> None:
    """``include_all=True`` returns the full unfiltered catalog."""
    stubbed_submitter.responses["list"] = {
        "stdout": _FAKE_LIST_OUTPUT_ALL_PROVIDERS,
        "returncode": 0,
        "duration_s": 0.1,
    }
    result = list_qgis_algorithms(include_all=True)
    ids = {s["algorithm_id"] for s in result}
    # The non-curated algorithms ARE present when include_all is set.
    assert "grass:r.sun" in ids
    assert "saga:thinplatespline" in ids
