"""Unit tests for the ``publish_layer`` atomic tool (job-0062).

Coverage:
1. ``test_publish_layer_registered`` — tool appears in TOOL_REGISTRY with
   the correct metadata (cacheable=False, ttl_class="live-no-cache",
   source_class="publish_layer").
2. ``test_publish_layer_returns_wms_url`` — with the Cloud Run Jobs client
   mocked, ``publish_layer`` returns the expected WMS URL.
3. ``test_publish_layer_raises_on_dispatch_failure`` — when
   ``jobs_client.run_job`` raises, ``publish_layer`` raises
   ``PublishLayerError`` with error_code ``WORKER_JOB_DISPATCH_FAILED``.
4. ``test_publish_layer_raises_on_worker_failure`` — when the LRO result
   yields a FAILED execution state, ``publish_layer`` raises
   ``PublishLayerError`` with error_code ``WORKER_JOB_FAILED``.
5. ``test_publish_layer_gs_to_vsigs_conversion`` — ``_gs_to_vsigs`` converts
   ``gs://`` URIs to ``/vsigs/`` correctly.
6. ``test_publish_layer_wms_url_format`` — ``_build_wms_url`` produces the
   MAP= + LAYERS= query string matching the Map.tsx convention.
7. ``test_publish_layer_qgs_key_parsing`` — ``_parse_qgs_key`` extracts the
   correct key from ``gs://bucket/path/to/file.qgs``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from grace2_agent.tools import TOOL_REGISTRY
from grace2_agent.tools.publish_layer import (
    PublishLayerError,
    _build_wms_url,
    derive_layer_id,
    _gs_to_vsigs,
    _parse_qgs_key,
    _validate_and_correct_layer_uri,
    _verify_layer_in_qgs,
    publish_layer,
    set_jobs_client,
    set_qgis_server_url,
    set_default_qgs_uri,
    set_gcp_project,
    set_gcp_location,
    set_pyqgis_worker_job_name,
    set_storage_client,
)

# Test 8 is imported here (job-0071 auto-dispatch shape guard).
# The import of RunJobRequest validates the library is present.
try:
    from google.cloud.run_v2.types import RunJobRequest as _RunJobRequest, EnvVar as _EnvVar
    _RUN_V2_AVAILABLE = True
except Exception:
    _RUN_V2_AVAILABLE = False


@pytest.fixture(autouse=True)
def _force_legacy_gcs_publish_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise the legacy GCP QGIS-worker dispatch path.

    GCP is decommissioned and ``cache.storage_scheme()`` is now hard-wired to
    ``s3`` (the env override is gone); under ``s3`` ``publish_layer``
    short-circuits raster tiling to the AWS TiTiler path (raising "tile
    publishing not configured" when ``GRACE2_TILE_SERVER_BASE`` is unset).
    These tests validate the QGIS-Server worker dispatch — a carve-out kept
    until job-0308 — so they monkeypatch the scheme helper itself to ``"gcs"``
    to reach that still-present (but no-longer-default) branch in the untouched
    ``publish_layer`` module.
    """
    import grace2_agent.tools.cache as _cache_mod

    monkeypatch.setattr(_cache_mod, "storage_scheme", lambda: "gcs")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _make_succeeded_execution() -> MagicMock:
    """Return a mock Cloud Run v2 Execution in SUCCEEDED state."""
    execution = MagicMock()
    execution.reconciling = False
    cond = MagicMock()
    cond.type_ = "Completed"
    cond.state.name = "CONDITION_SUCCEEDED"
    execution.conditions = [cond]
    return execution


def _make_failed_execution() -> MagicMock:
    """Return a mock Cloud Run v2 Execution in FAILED state."""
    execution = MagicMock()
    execution.reconciling = False
    cond = MagicMock()
    cond.type_ = "Completed"
    cond.state.name = "CONDITION_FAILED"
    execution.conditions = [cond]
    return execution


def _make_jobs_client(execution: Any) -> MagicMock:
    """Return a mock JobsClient whose run_job().result() yields ``execution``."""
    client = MagicMock()
    operation = MagicMock()
    operation.result.return_value = execution
    client.run_job.return_value = operation
    return client


# --------------------------------------------------------------------------- #
# Fake GCS storage client (job-0257 — layer_uri validation + .qgs verification)
# --------------------------------------------------------------------------- #


class _FakeBlob:
    def __init__(self, exists: bool, data: bytes = b"") -> None:
        self._exists = exists
        self._data = data

    def exists(self) -> bool:
        return self._exists

    def download_as_bytes(self) -> bytes:
        if not self._exists:
            raise FileNotFoundError("blob does not exist")
        return self._data


class _FakeBucket:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self._objects = objects

    def blob(self, key: str) -> _FakeBlob:
        if key in self._objects:
            return _FakeBlob(True, self._objects[key])
        return _FakeBlob(False)


class _FakeBlobRef:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeStorageClient:
    """Mimics the minimal google.cloud.storage.Client surface publish_layer uses.

    ``objects`` maps ``bucket_name -> {object_key: bytes}``.
    """

    def __init__(self, objects: dict[str, dict[str, bytes]]) -> None:
        self._objects = objects

    def bucket(self, name: str) -> _FakeBucket:
        return _FakeBucket(self._objects.get(name, {}))

    def list_blobs(self, bucket_name: str, prefix: str | None = None) -> list[_FakeBlobRef]:
        keys = self._objects.get(bucket_name, {})
        return [
            _FakeBlobRef(k) for k in sorted(keys) if prefix is None or k.startswith(prefix)
        ]


def _qgs_bytes_with_layers(*layer_ids: str) -> bytes:
    """Minimal .qgs-shaped XML carrying <layername> entries."""
    names = "".join(f"<layername>{lid}</layername>" for lid in layer_ids)
    return f"<!DOCTYPE qgis><qgis>{names}</qgis>".encode("utf-8")


def _default_fake_storage(
    layer_key: str = "run-abc/flood_depth_peak.tif",
    layer_bucket: str = "grace-2-hazard-prod-runs",
    qgs_layers: tuple[str, ...] = ("flood-depth-peak-run-abc", "flood-depth-peak-run-xyz"),
) -> _FakeStorageClient:
    """Storage fixture where the layer object exists and the .qgs contains the layer."""
    return _FakeStorageClient(
        {
            layer_bucket: {
                layer_key: b"GTIFF",
                "run-xyz/flood_depth_peak.tif": b"GTIFF",
            },
            "runs": {"run-abc/flood_depth_peak.tif": b"GTIFF"},
            "test-qgs-bucket": {"grace2-sample.qgs": _qgs_bytes_with_layers(*qgs_layers)},
        }
    )


# --------------------------------------------------------------------------- #
# Test 1 — tool registration
# --------------------------------------------------------------------------- #


def test_publish_layer_registered() -> None:
    """publish_layer is in TOOL_REGISTRY with correct metadata."""
    # Import the module to trigger registration (mirrors _import_tools_registry).
    import grace2_agent.tools.publish_layer  # noqa: F401

    assert "publish_layer" in TOOL_REGISTRY, (
        f"publish_layer not found in TOOL_REGISTRY; keys={sorted(TOOL_REGISTRY)}"
    )
    entry = TOOL_REGISTRY["publish_layer"]
    assert entry.metadata.cacheable is False
    assert entry.metadata.ttl_class == "live-no-cache"
    assert entry.metadata.source_class == "publish_layer"
    assert entry.fn is publish_layer


# --------------------------------------------------------------------------- #
# Test 2 — happy path: returns WMS URL
# --------------------------------------------------------------------------- #


def test_publish_layer_returns_wms_url() -> None:
    """With a mocked Jobs client, publish_layer returns the expected WMS URL."""
    execution = _make_succeeded_execution()
    mock_client = _make_jobs_client(execution)

    set_jobs_client(mock_client)
    set_qgis_server_url("https://qgis.test.example.com/ogc/wms")
    set_default_qgs_uri("gs://test-qgs-bucket/grace2-sample.qgs")
    set_gcp_project("test-project")
    set_gcp_location("us-central1")
    set_pyqgis_worker_job_name("grace-2-pyqgis-worker")
    set_storage_client(_default_fake_storage())

    try:
        result = publish_layer(
            layer_uri="gs://grace-2-hazard-prod-runs/run-abc/flood_depth_peak.tif",
            layer_id="flood-depth-peak-run-abc",
            style_preset="continuous_flood_depth",
        )
    finally:
        # Tear down DI bindings.
        set_jobs_client(None)
        set_qgis_server_url(None)
        set_default_qgs_uri(None)
        set_gcp_project(None)
        set_gcp_location(None)
        set_pyqgis_worker_job_name(None)
        set_storage_client(None)

    assert result == (
        "https://qgis.test.example.com/ogc/wms"
        "?MAP=/mnt/qgs/grace2-sample.qgs"
        "&LAYERS=flood-depth-peak-run-abc"
    ), f"unexpected WMS URL: {result}"

    # Verify run_job was called once with request=RunJobRequest(...) (job-0071 fix).
    mock_client.run_job.assert_called_once()
    call_kwargs = mock_client.run_job.call_args
    # After the job-0071 auto-dispatch fix, run_job is called with request=RunJobRequest(...)
    # not name=/overrides= as direct kwargs.
    assert "request" in call_kwargs.kwargs, (
        f"job-0071: run_job must be called with request=RunJobRequest(...); "
        f"got kwargs={list(call_kwargs.kwargs)}"
    )
    req = call_kwargs.kwargs["request"]
    # The job name must appear in the RunJobRequest.name field.
    assert "grace-2-pyqgis-worker" in req.name, (
        f"job-0071: RunJobRequest.name must include the job name; got {req.name!r}"
    )


# --------------------------------------------------------------------------- #
# Test 3 — dispatch failure → PublishLayerError
# --------------------------------------------------------------------------- #


def test_publish_layer_raises_on_dispatch_failure() -> None:
    """When run_job raises, publish_layer raises PublishLayerError(WORKER_JOB_DISPATCH_FAILED)."""
    mock_client = MagicMock()
    mock_client.run_job.side_effect = RuntimeError("quota exceeded")

    set_jobs_client(mock_client)
    set_default_qgs_uri("gs://test-qgs-bucket/grace2-sample.qgs")
    set_gcp_project("test-project")
    set_storage_client(_default_fake_storage())

    try:
        with pytest.raises(PublishLayerError) as exc_info:
            publish_layer(
                layer_uri="gs://runs/run-abc/flood_depth_peak.tif",
                layer_id="flood-depth-peak-run-abc",
            )
    finally:
        set_jobs_client(None)
        set_default_qgs_uri(None)
        set_gcp_project(None)
        set_storage_client(None)

    assert exc_info.value.error_code == "WORKER_JOB_DISPATCH_FAILED"
    assert "quota exceeded" in str(exc_info.value)


# --------------------------------------------------------------------------- #
# Test 4 — worker execution fails → PublishLayerError
# --------------------------------------------------------------------------- #


def test_publish_layer_raises_on_worker_failure() -> None:
    """When the execution reaches FAILED state, publish_layer raises PublishLayerError."""
    execution = _make_failed_execution()
    mock_client = _make_jobs_client(execution)

    set_jobs_client(mock_client)
    set_default_qgs_uri("gs://test-qgs-bucket/grace2-sample.qgs")
    set_gcp_project("test-project")
    set_storage_client(_default_fake_storage())

    try:
        with pytest.raises(PublishLayerError) as exc_info:
            publish_layer(
                layer_uri="gs://runs/run-abc/flood_depth_peak.tif",
                layer_id="flood-depth-peak-run-abc",
            )
    finally:
        set_jobs_client(None)
        set_default_qgs_uri(None)
        set_gcp_project(None)
        set_storage_client(None)

    assert exc_info.value.error_code == "WORKER_JOB_FAILED"


# --------------------------------------------------------------------------- #
# Test 5 — _gs_to_vsigs conversion
# --------------------------------------------------------------------------- #


def test_gs_to_vsigs_conversion() -> None:
    """_gs_to_vsigs converts gs:// URIs to /vsigs/ and passes through others."""
    assert _gs_to_vsigs("gs://bucket/path/to/file.tif") == "/vsigs/bucket/path/to/file.tif"
    assert _gs_to_vsigs("/vsigs/bucket/path/to/file.tif") == "/vsigs/bucket/path/to/file.tif"
    assert _gs_to_vsigs("/local/path/file.tif") == "/local/path/file.tif"


# --------------------------------------------------------------------------- #
# Test 6 — _build_wms_url format
# --------------------------------------------------------------------------- #


def test_build_wms_url_format() -> None:
    """_build_wms_url produces the MAP= + LAYERS= query string."""
    set_qgis_server_url("https://qgis.example.com/ogc/wms")
    try:
        url = _build_wms_url("grace2-sample.qgs", "flood-depth-peak-01")
    finally:
        set_qgis_server_url(None)

    assert url == (
        "https://qgis.example.com/ogc/wms"
        "?MAP=/mnt/qgs/grace2-sample.qgs"
        "&LAYERS=flood-depth-peak-01"
    )


# --------------------------------------------------------------------------- #
# Test 7 — _parse_qgs_key
# --------------------------------------------------------------------------- #


def test_parse_qgs_key() -> None:
    """_parse_qgs_key extracts the GCS object key from a gs:// URI."""
    assert _parse_qgs_key("gs://grace-2-hazard-prod-qgs/grace2-sample.qgs") == "grace2-sample.qgs"
    assert _parse_qgs_key("gs://bucket/subdir/project.qgs") == "subdir/project.qgs"

    with pytest.raises(PublishLayerError) as exc_info:
        _parse_qgs_key("/vsigs/bucket/file.qgs")
    assert exc_info.value.error_code == "QGS_URI_PARSE_ERROR"

    with pytest.raises(PublishLayerError) as exc_info:
        _parse_qgs_key("gs://no-key-here/")
    assert exc_info.value.error_code == "QGS_URI_PARSE_ERROR"


# --------------------------------------------------------------------------- #
# Test 8 — publish_layer auto-dispatch fix (job-0071, OQ-70-AUTO-PUBLISH-DISPATCH)
#
# Pre-fix: publish_layer called
#   jobs_client.run_job(name=..., overrides={...})
# which raises TypeError because JobsClient.run_job() does NOT accept
# ``name`` and ``overrides`` as separate kwargs — it expects a ``request``
# positional arg (or ``request=`` kwarg) of type RunJobRequest.
#
# Post-fix: the code constructs a RunJobRequest proto with the env overrides
# and passes it as ``jobs_client.run_job(request=request)``.
#
# This test asserts:
# 1. The mock client's run_job is called with ``request=`` (not ``name=``,
#    ``overrides=``).
# 2. The ``request`` is a RunJobRequest (or dict-shaped equivalent with the
#    correct structure).
# 3. The env overrides list contains the expected WORKER_OP and QGS_URI keys.
# --------------------------------------------------------------------------- #


def test_publish_layer_dispatch_uses_run_job_request_not_kwargs() -> None:
    """Auto-dispatch fix (job-0071): run_job is called with request=RunJobRequest.

    Regression guard for OQ-70-AUTO-PUBLISH-DISPATCH: the pre-fix code called
    ``jobs_client.run_job(name=..., overrides=...)`` which raises TypeError in
    the installed google-cloud-run version.  The fix uses:
        ``jobs_client.run_job(request=RunJobRequest(...))``
    """
    if not _RUN_V2_AVAILABLE:
        pytest.skip("google-cloud-run not installed; cannot validate RunJobRequest shape")

    import inspect

    execution = _make_succeeded_execution()
    mock_client = _make_jobs_client(execution)

    set_jobs_client(mock_client)
    set_qgis_server_url("https://qgis.test.example.com/ogc/wms")
    set_default_qgs_uri("gs://test-qgs-bucket/grace2-sample.qgs")
    set_gcp_project("test-project")
    set_gcp_location("us-central1")
    set_pyqgis_worker_job_name("grace-2-pyqgis-worker")
    set_storage_client(_default_fake_storage())

    try:
        publish_layer(
            layer_uri="gs://grace-2-hazard-prod-runs/run-xyz/flood_depth_peak.tif",
            layer_id="flood-depth-peak-run-xyz",
            style_preset="continuous_flood_depth",
        )
    finally:
        set_jobs_client(None)
        set_qgis_server_url(None)
        set_default_qgs_uri(None)
        set_gcp_project(None)
        set_gcp_location(None)
        set_pyqgis_worker_job_name(None)
        set_storage_client(None)

    # Assert run_job was called exactly once.
    mock_client.run_job.assert_called_once()
    call_args = mock_client.run_job.call_args

    # Critical: must NOT have 'overrides' as a direct kwarg (the pre-fix bug).
    assert "overrides" not in call_args.kwargs, (
        "job-0071 auto-dispatch fix regression: run_job was called with "
        "'overrides=' as a direct kwarg. This raises TypeError in the installed "
        "google-cloud-run version. Use request=RunJobRequest(...) instead."
    )

    # Must be called with ``request=`` keyword arg (the fixed shape).
    assert "request" in call_args.kwargs, (
        f"job-0071: run_job must be called with 'request=RunJobRequest(...)'; "
        f"got kwargs={list(call_args.kwargs)}"
    )

    req = call_args.kwargs["request"]

    # The request must be a RunJobRequest instance (proto-plus message).
    assert isinstance(req, _RunJobRequest), (
        f"job-0071: request must be a RunJobRequest instance; got {type(req).__name__!r}"
    )

    # The request must carry the correct job name.
    assert "grace-2-pyqgis-worker" in req.name, (
        f"job-0071: RunJobRequest.name must include the job name; got {req.name!r}"
    )

    # The overrides must include at least one ContainerOverride with env vars.
    container_overrides = list(req.overrides.container_overrides)
    assert container_overrides, (
        "job-0071: RunJobRequest.overrides.container_overrides must be non-empty"
    )
    env_list = list(container_overrides[0].env)
    assert env_list, (
        "job-0071: ContainerOverride.env must be non-empty"
    )
    env_names = {e.name for e in env_list}
    assert "WORKER_OP" in env_names, (
        f"job-0071: env overrides must include WORKER_OP; got {env_names}"
    )
    assert "QGS_URI" in env_names, (
        f"job-0071: env overrides must include QGS_URI; got {env_names}"
    )
    assert "RASTER_URI" in env_names, (
        f"job-0071: env overrides must include RASTER_URI; got {env_names}"
    )


# --------------------------------------------------------------------------- #
# job-0257 — hillshade no-render root-cause fixes
#
# Live evidence (2026-06-10 demo session, /tmp/agent_demo_ready.log):
# compute_hillshade cached gs://...hillshade/090a4ff8d9a083f67c0b355caf40241a.tif
# but Gemini called publish_layer with .../090a4ff8d9a083b28499252309d12999.tif
# (hash tail hallucinated, 3/3 occurrences). The worker raised WorkerError
# internally, returned a status=error envelope, and exited 0 — so publish_layer
# saw CONDITION_SUCCEEDED and reported false success; the map stayed empty.
# --------------------------------------------------------------------------- #

_REAL_KEY = "cache/static-30d/hillshade/090a4ff8d9a083f67c0b355caf40241a.tif"
_HALLUCINATED = (
    "gs://grace-2-hazard-prod-cache/cache/static-30d/hillshade/"
    "090a4ff8d9a083b28499252309d12999.tif"
)


def test_validate_layer_uri_passes_through_existing_object() -> None:
    """An existing gs:// object validates unchanged (gs://-normalized)."""
    fake = _FakeStorageClient({"grace-2-hazard-prod-cache": {_REAL_KEY: b"GTIFF"}})
    set_storage_client(fake)
    try:
        uri = f"gs://grace-2-hazard-prod-cache/{_REAL_KEY}"
        assert _validate_and_correct_layer_uri(uri) == uri
        # /vsigs/ input normalizes to gs:// after validation.
        assert (
            _validate_and_correct_layer_uri(f"/vsigs/grace-2-hazard-prod-cache/{_REAL_KEY}")
            == uri
        )
    finally:
        set_storage_client(None)


def test_validate_layer_uri_autocorrects_hallucinated_hash_tail() -> None:
    """The exact URI Gemini hallucinated in the demo is corrected to the real key."""
    fake = _FakeStorageClient(
        {
            "grace-2-hazard-prod-cache": {
                _REAL_KEY: b"GTIFF",
                # An unrelated hillshade key that shares no meaningful prefix.
                "cache/static-30d/hillshade/4007d642cb157d11f5db275a50286ae5.tif": b"GTIFF",
            }
        }
    )
    set_storage_client(fake)
    try:
        corrected = _validate_and_correct_layer_uri(_HALLUCINATED)
    finally:
        set_storage_client(None)
    assert corrected == f"gs://grace-2-hazard-prod-cache/{_REAL_KEY}", (
        f"hallucinated hash tail must auto-correct to the real cache key; got {corrected}"
    )


def test_validate_layer_uri_raises_retryable_when_no_match() -> None:
    """No unambiguous correction → LAYER_URI_NOT_FOUND, retryable, lists real objects."""
    fake = _FakeStorageClient(
        {
            "grace-2-hazard-prod-cache": {
                "cache/static-30d/hillshade/aaaa1111bbbb2222cccc3333dddd4444.tif": b"GTIFF",
            }
        }
    )
    set_storage_client(fake)
    try:
        with pytest.raises(PublishLayerError) as exc_info:
            _validate_and_correct_layer_uri(_HALLUCINATED)
    finally:
        set_storage_client(None)
    assert exc_info.value.error_code == "LAYER_URI_NOT_FOUND"
    assert exc_info.value.retryable is True
    # The message must surface the REAL object basenames as the correction hint.
    assert "aaaa1111bbbb2222cccc3333dddd4444.tif" in str(exc_info.value)


def test_validate_layer_uri_ambiguous_prefix_is_not_corrected() -> None:
    """Two candidates with the same shared prefix → ambiguous → typed error."""
    fake = _FakeStorageClient(
        {
            "b": {
                "dir/090a4ff8d9a083aaaaaaaaaaaaaaaaaa.tif": b"x",
                "dir/090a4ff8d9a083bbbbbbbbbbbbbbbbbb.tif": b"x",
            }
        }
    )
    set_storage_client(fake)
    try:
        with pytest.raises(PublishLayerError) as exc_info:
            # Shares 14 chars with BOTH candidates equally → must not guess.
            _validate_and_correct_layer_uri("gs://b/dir/090a4ff8d9a083cccccccccccccccccc.tif")
    finally:
        set_storage_client(None)
    assert exc_info.value.error_code == "LAYER_URI_NOT_FOUND"


def test_validate_layer_uri_fail_open_without_storage_client() -> None:
    """No storage client (CI / no ADC) → legacy pass-through behavior."""
    set_storage_client(None)
    with patch(
        "grace2_agent.tools.publish_layer._get_storage_client", return_value=None
    ):
        assert _validate_and_correct_layer_uri(_HALLUCINATED) == _HALLUCINATED


def test_verify_layer_in_qgs_detects_missing_layer() -> None:
    """.qgs without the layername → False; with it → True; no client → None."""
    qgs = _qgs_bytes_with_layers("flood-depth-peak-x", "elevation-washington")
    fake = _FakeStorageClient({"qgs-bucket": {"grace2-sample.qgs": qgs}})
    set_storage_client(fake)
    try:
        assert _verify_layer_in_qgs("gs://qgs-bucket/grace2-sample.qgs", "chicago-hillshade") is False
        assert _verify_layer_in_qgs("gs://qgs-bucket/grace2-sample.qgs", "elevation-washington") is True
    finally:
        set_storage_client(None)
    with patch(
        "grace2_agent.tools.publish_layer._get_storage_client", return_value=None
    ):
        assert _verify_layer_in_qgs("gs://qgs-bucket/grace2-sample.qgs", "x") is None


def test_publish_layer_raises_when_worker_swallowed_error() -> None:
    """Execution CONDITION_SUCCEEDED but layer absent from .qgs →
    WORKER_PUBLISH_NOT_APPLIED (the silent-failure gap that caused the
    hillshade no-render)."""
    execution = _make_succeeded_execution()
    mock_client = _make_jobs_client(execution)

    # Layer object exists (validation passes) but the .qgs does NOT gain the
    # layer (worker swallowed its error and exited 0).
    fake = _FakeStorageClient(
        {
            "grace-2-hazard-prod-cache": {_REAL_KEY: b"GTIFF"},
            "test-qgs-bucket": {
                "grace2-sample.qgs": _qgs_bytes_with_layers("some-other-layer"),
            },
        }
    )

    set_jobs_client(mock_client)
    set_default_qgs_uri("gs://test-qgs-bucket/grace2-sample.qgs")
    set_gcp_project("test-project")
    set_storage_client(fake)

    try:
        with pytest.raises(PublishLayerError) as exc_info:
            publish_layer(
                layer_uri=f"gs://grace-2-hazard-prod-cache/{_REAL_KEY}",
                layer_id="chicago-hillshade",
            )
    finally:
        set_jobs_client(None)
        set_default_qgs_uri(None)
        set_gcp_project(None)
        set_storage_client(None)

    assert exc_info.value.error_code == "WORKER_PUBLISH_NOT_APPLIED"
    assert exc_info.value.retryable is False


def test_publish_layer_end_to_end_with_hallucinated_uri_corrects_and_dispatches() -> None:
    """Full tool path: hallucinated layer_uri → auto-corrected RASTER_URI in the
    dispatched worker request → .qgs verification passes → WMS URL returned."""
    if not _RUN_V2_AVAILABLE:
        pytest.skip("google-cloud-run not installed; cannot inspect RunJobRequest")

    execution = _make_succeeded_execution()
    mock_client = _make_jobs_client(execution)

    fake = _FakeStorageClient(
        {
            "grace-2-hazard-prod-cache": {_REAL_KEY: b"GTIFF"},
            "test-qgs-bucket": {
                "grace2-sample.qgs": _qgs_bytes_with_layers("chicago-hillshade"),
            },
        }
    )

    set_jobs_client(mock_client)
    set_qgis_server_url("https://qgis.test.example.com/ogc/wms")
    set_default_qgs_uri("gs://test-qgs-bucket/grace2-sample.qgs")
    set_gcp_project("test-project")
    set_storage_client(fake)

    try:
        result = publish_layer(
            layer_uri=_HALLUCINATED,
            layer_id="chicago-hillshade",
            style_preset="grayscale",
        )
    finally:
        set_jobs_client(None)
        set_qgis_server_url(None)
        set_default_qgs_uri(None)
        set_gcp_project(None)
        set_storage_client(None)

    assert result.endswith("LAYERS=chicago-hillshade")

    req = mock_client.run_job.call_args.kwargs["request"]
    env = {e.name: e.value for e in req.overrides.container_overrides[0].env}
    assert env["RASTER_URI"] == f"/vsigs/grace-2-hazard-prod-cache/{_REAL_KEY}", (
        f"worker must receive the CORRECTED raster URI, not the hallucinated one; "
        f"got {env['RASTER_URI']}"
    )


# --------------------------------------------------------------------------- #
# 2026-07-08 - layer_id is OPTIONAL (small-model resilience)
#
# Live evidence: local 8B models call publish_layer without layer_id at all
# (TypeError: publish_layer() missing 1 required positional argument:
# 'layer_id'). The arg now defaults to None and is DERIVED - registered
# handle for the resolved layer_uri, else the URI basename stem, else a
# fresh layer-<ulid>.
# --------------------------------------------------------------------------- #


def test_derive_layer_id_prefers_registered_handle() -> None:
    """A layer_uri the registry knows derives the producing tool's layer_id."""
    from grace2_agent.uri_registry import (
        get_uri_registry,
        reset_uri_registries_for_tests,
    )

    reset_uri_registries_for_tests()
    try:
        reg = get_uri_registry("sess-derive-layer-id")
        reg.record(
            "dem-3dep-10m",
            uri="s3://grace2-hazard-cache/cache/static-30d/fetch_dem/abc.tif",
            tool_name="fetch_dem",
        )
        derived = derive_layer_id(
            "s3://grace2-hazard-cache/cache/static-30d/fetch_dem/abc.tif", reg
        )
        assert derived == "dem-3dep-10m"
    finally:
        reset_uri_registries_for_tests()


def test_derive_layer_id_falls_back_to_basename_stem() -> None:
    assert (
        derive_layer_id("s3://bucket/runs/01X/flood_depth_peak.tif")
        == "flood_depth_peak"
    )
    assert derive_layer_id("gs://bucket/dir/continuous-dem-10m.tif") == (
        "continuous-dem-10m"
    )


def test_derive_layer_id_sanitizes_and_never_returns_empty() -> None:
    assert derive_layer_id("s3://bucket/dir/my layer (v2).tif") == "my-layer-v2"
    # No basename at all -> a fresh ULID-suffixed id, never an empty string.
    derived = derive_layer_id("s3://bucket/dir/")
    assert derived.startswith("layer-") and len(derived) > len("layer-")


def test_publish_layer_derives_layer_id_when_omitted() -> None:
    """Omitting layer_id publishes under the basename-stem-derived id."""
    execution = _make_succeeded_execution()
    mock_client = _make_jobs_client(execution)

    set_jobs_client(mock_client)
    set_qgis_server_url("https://qgis.test.example.com/ogc/wms")
    set_default_qgs_uri("gs://test-qgs-bucket/grace2-sample.qgs")
    set_gcp_project("test-project")
    set_gcp_location("us-central1")
    set_pyqgis_worker_job_name("grace-2-pyqgis-worker")
    set_storage_client(_default_fake_storage(qgs_layers=("flood_depth_peak",)))

    try:
        result = publish_layer(
            layer_uri="gs://grace-2-hazard-prod-runs/run-abc/flood_depth_peak.tif",
        )
    finally:
        set_jobs_client(None)
        set_qgis_server_url(None)
        set_default_qgs_uri(None)
        set_gcp_project(None)
        set_gcp_location(None)
        set_pyqgis_worker_job_name(None)
        set_storage_client(None)

    assert result == (
        "https://qgis.test.example.com/ogc/wms"
        "?MAP=/mnt/qgs/grace2-sample.qgs"
        "&LAYERS=flood_depth_peak"
    ), f"unexpected WMS URL: {result}"

    req = mock_client.run_job.call_args.kwargs["request"]
    env = {e.name: e.value for e in req.overrides.container_overrides[0].env}
    assert env["RASTER_LAYER_ID"] == "flood_depth_peak"
