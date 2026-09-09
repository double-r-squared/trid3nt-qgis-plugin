"""Shared pytest fixtures for the agent-service test suite.

The agent-service tests are import-light: every test that needs the tool
registry imports ``trid3nt_server.tools`` directly. The registry is a
module-level singleton, so tests that mutate it use the
``clear_registry_for_tests`` helper inside a fixture rather than relying on
import ordering.
"""

from __future__ import annotations

from typing import Any

import pytest

from trid3nt_server import tools as agent_tools


# ---------------------------------------------------------------------------
# Shared in-memory S3 double (GCP decommissioned — cache shim is S3-only).
#
# The cache read-through (``trid3nt_server.tools.cache``) and every tool
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

    The RUNTIME default is ``openai``
    (``adapters.model_selection.model_provider``). The agent-loop
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
    behavior need a clean slate so the eager package imports don't
    collide with a test's fixture-registered tool.
    """
    saved = dict(agent_tools.TOOL_REGISTRY)
    agent_tools.clear_registry_for_tests()
    try:
        yield agent_tools.TOOL_REGISTRY
    finally:
        agent_tools.clear_registry_for_tests()
        agent_tools.TOOL_REGISTRY.update(saved)
