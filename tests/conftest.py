"""Shared pytest fixtures for the agent-service test suite.

The agent-service tests are import-light: every test that needs the tool
registry imports ``trid3nt_server.data`` directly. The registry is a
module-level singleton, so tests that mutate it use the
``clear_registry_for_tests`` helper inside a fixture rather than relying on
import ordering.
"""

from __future__ import annotations

from typing import Any

import pytest

from trid3nt_server import data as agent_tools


# ---------------------------------------------------------------------------
# Shared in-memory S3 double (GCP decommissioned — cache shim is S3-only).
#
# The cache read-through (``trid3nt_server.data.cache``) and every tool
# download-helper build their boto3 S3 client lazily via ``boto3.client``.
# Tests that exercise the cache miss/hit/write paths monkeypatch that factory
# to this in-memory double so no AWS credentials / network are needed and the
# old injected ``google.cloud.storage`` client seam is fully retired.
# ---------------------------------------------------------------------------


class _S3Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class InMemoryS3Client:
    """Minimal in-memory boto3 S3 client double.

    ``store`` is keyed by the object KEY (path) only — agent tests run against
    a single cache bucket, so the bucket name is recorded but not part of the
    lookup key. This keeps the historical ``fake.store[path] = b"..."``
    seeding ergonomics from the pre-S3 GCS doubles.
    """

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.last_put: dict[str, Any] | None = None

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        from botocore.exceptions import ClientError

        try:
            data = self.store[Key]
        except KeyError:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "not found"}},
                "GetObject",
            )
        return {"Body": _S3Body(data)}

    def put_object(
        self, *, Bucket: str, Key: str, Body: bytes, ContentType: str | None = None
    ) -> dict[str, Any]:
        self.store[Key] = Body
        self.last_put = {"Bucket": Bucket, "Key": Key, "ContentType": ContentType}
        return {}


def make_read_through_s3_injector(store: dict[str, bytes]):
    """Return a drop-in ``read_through`` replacement backed by an in-memory store.

    Many fetcher tests patch the tool module's ``read_through`` with a wrapper
    that used to inject a duck-typed ``google.cloud.storage`` client. GCP is
    decommissioned (S3-only read-through), so this helper provides an in-memory
    S3 read-through: it mints ``s3://`` URIs, honors cache hit/miss/write
    semantics against ``store`` (keyed by object KEY), and short-circuits
    ``live-no-cache`` exactly like the real shim. ``store`` is the same dict the
    test inspects after the call (``next(iter(store.values()))`` etc.).
    """
    from trid3nt_server.data.cache import (
        CACHE_BUCKET,
        cache_path,
        compute_cache_key,
        is_cacheable,
        ReadThroughResult,
    )

    def _patched(metadata, params, ext, fetch_fn, **kw):
        bucket = kw.get("bucket") or CACHE_BUCKET
        source_id = kw.get("source_id") or (metadata.source_class or metadata.name)
        now = kw.get("now")
        force_refresh = kw.get("force_refresh", False)
        if not is_cacheable(metadata):
            return ReadThroughResult(uri=None, data=fetch_fn(), hit=False)
        key = compute_cache_key(source_id, params, metadata.ttl_class, now=now)
        path = cache_path(metadata.source_class, metadata.ttl_class, key, ext)
        uri = f"s3://{bucket}/{path}"
        if not force_refresh and path in store:
            return ReadThroughResult(uri=uri, data=store[path], hit=True)
        data = fetch_fn()
        store[path] = data
        return ReadThroughResult(uri=uri, data=data, hit=False)

    return _patched


@pytest.fixture()
def fake_s3(monkeypatch: pytest.MonkeyPatch) -> InMemoryS3Client:
    """Monkeypatch ``boto3.client('s3', ...)`` to a shared in-memory double.

    Returns the client so tests can pre-seed ``fake_s3.store[path]`` for a
    cache hit and inspect ``fake_s3.store`` / ``fake_s3.last_put`` after a
    write. Every ``boto3.client('s3', ...)`` call in the process under test
    resolves to the same instance for the duration of the test.
    """
    import boto3

    client = InMemoryS3Client()

    def _factory(service_name: str, *args: Any, **kwargs: Any) -> InMemoryS3Client:
        assert service_name == "s3", f"unexpected boto3 service {service_name!r}"
        return client

    monkeypatch.setattr(boto3, "client", _factory)
    return client


@pytest.fixture(autouse=True)
def _default_scripted_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default the model provider to ``scripted`` for the agent test suite.

    GCP/Vertex is decommissioned and its generate path is removed; the RUNTIME
    default is ``bedrock`` (``bedrock_adapter.model_provider``). The agent-loop
    tests fake model turns through the scripted fake-provider seam (the
    ``fake_llm`` fixture installs a call-sequenced turn source and pins
    ``MODEL_PROVIDER=scripted`` itself; this autouse default covers the tests
    that patch ``stream_events_with_contents`` directly or otherwise never reach
    the provider dispatch). Any test that needs a specific provider sets
    ``MODEL_PROVIDER`` itself (monkeypatch wins inside the test body).
    """
    monkeypatch.setenv("MODEL_PROVIDER", "scripted")


@pytest.fixture(autouse=True)
def _reset_fake_llm_harness():
    """Reset the scripted-adapter test harness around every test.

    Prevents an installed fake-turn source (``fake_llm``) from leaking across
    tests. Cheap no-op when the harness was never installed.
    """
    from trid3nt_server.adapters import scripted_adapter as _sa

    _sa.reset_harness()
    yield
    _sa.reset_harness()


@pytest.fixture()
def fake_llm(monkeypatch: pytest.MonkeyPatch):
    """Fake model provider for the agent-loop tests (the single replacement for
    the retired ``patch build_client + feed fake generate_content_stream chunks``
    harness).

    Pins ``MODEL_PROVIDER=scripted`` so the REAL server dispatch
    (``stream_events_with_contents``) routes to the scripted adapter, and hands
    back a handle that installs a call-sequenced fake-turn source and exposes the
    ``contents`` the server built between turns:

      * ``fake_llm.script([turn, ...])``  -- a fixed list of fake turns.
      * ``fake_llm.on_call(fn)``          -- a dynamic ``(call_index, contents) ->
                                             turn`` source (external-counter tests).
      * ``fake_llm.calls``                -- recorded calls; ``.calls[i]["contents"]``
                                             replaces the ``_capture_and_stream`` snapshot.
      * turn builders: ``fake_llm.text(...)``, ``.call(name, args, call_id=...,
        thought_signature=...)``, ``.parallel(call, call, ...)``, ``.raise_(exc)``
        (or author the turn dicts inline -- see ``scripted_adapter``).
    """
    from trid3nt_server.adapters import scripted_adapter as sa

    monkeypatch.setenv("MODEL_PROVIDER", "scripted")
    sa.reset_harness()

    class _FakeLLM:
        def script(self, turns):
            sa.install_harness(list(turns))
            return self

        def on_call(self, fn):
            sa.install_harness(fn)
            return self

        @property
        def calls(self):
            return sa.harness_calls()

        text = staticmethod(sa.text_turn)
        call = staticmethod(sa.call_turn)
        parallel = staticmethod(sa.calls_turn)
        raise_ = staticmethod(sa.raise_turn)

    return _FakeLLM()


@pytest.fixture()
def empty_registry():
    """Yield a context where ``TOOL_REGISTRY`` is empty; restore on teardown.

    Tests of the ``@register_tool`` decorator and duplicate-name fail-fast
    behavior need a clean slate so the eager passthroughs imports don't
    collide with a test's fixture-registered tool.
    """
    saved = dict(agent_tools.TOOL_REGISTRY)
    agent_tools.clear_registry_for_tests()
    try:
        yield agent_tools.TOOL_REGISTRY
    finally:
        agent_tools.clear_registry_for_tests()
        agent_tools.TOOL_REGISTRY.update(saved)
