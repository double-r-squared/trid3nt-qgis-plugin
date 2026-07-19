"""HTTP-route + unit tests for the OpenRouter model-extensibility seam
(design 2026-07-19).

Two features, both offline (no live network, no live agent):

Feature 3 -- POST /api/provider-config:
  - CLOUD posture: route ABSENT (404) unless MODEL_PROVIDER=openai;
  - a well-formed body updates os.environ[GRACE2_OPENAI_*] and returns
    {"ok", "model", "base_url_host"} -- the effect the openai adapter reads at
    the next call (no restart);
  - the num_ctx discovery cache is reset so a same-name model re-discovers;
  - the api_key is NEVER echoed in the response body;
  - a malformed body -> honest 400 that does not leak the body.

Feature 2 -- _filter_openrouter_models (pure) + _fetch_openrouter_models:
  - FREE = pricing 0/0 OR id ":free"; TOOL-CAPABLE = "tools" in
    supported_parameters; a model missing supported_parameters is kept OUT;
  - malformed rows are skipped, never fatal;
  - the fetched list is cached per base_url with a TTL (one round trip).
"""

from __future__ import annotations

import asyncio
import json

from grace2_agent import tool_catalog_http
from grace2_agent import context_budget


# ---------------------------------------------------------------------------
# Minimal HTTP request/response harness (mirrors test_local_models_http_route)
# ---------------------------------------------------------------------------


class _FakeReader:
    def __init__(self, request: bytes):
        self._lines = request.split(b"\r\n")
        self._buf = [ln + b"\r\n" for ln in self._lines]
        self._body = b""
        # Split header block from body (the double CRLF).
        head, _, body = request.partition(b"\r\n\r\n")
        self._body = body
        head_lines = head.split(b"\r\n")
        self._buf = [ln + b"\r\n" for ln in head_lines] + [b"\r\n"]

    async def readline(self):
        if self._buf:
            return self._buf.pop(0)
        return b""

    async def readexactly(self, n: int):
        data = self._body[:n]
        self._body = self._body[n:]
        return data


class _FakeWriter:
    def __init__(self):
        self.buffer = bytearray()
        self.closed = False

    def write(self, data: bytes):
        self.buffer.extend(data)

    async def drain(self):
        return None

    def close(self):
        self.closed = True


def _post(path: str, body: bytes) -> bytes:
    return (
        f"POST {path} HTTP/1.1\r\nHost: agent.local\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n\r\n"
    ).encode() + body


def _run(coro):
    return asyncio.run(coro)


def _status(out: bytes) -> int:
    return int(out.split(b" ", 2)[1])


def _resp_body(out: bytes) -> dict:
    _, _, body = out.partition(b"\r\n\r\n")
    return json.loads(body.decode("utf-8"))


def _dispatch(path: str, body: bytes) -> _FakeWriter:
    reader = _FakeReader(_post(path, body))
    writer = _FakeWriter()
    _run(tool_catalog_http._handle_http(reader, writer))
    return writer


# ---------------------------------------------------------------------------
# Route gating (cloud posture identical: 404 like any unknown path)
# ---------------------------------------------------------------------------


def test_provider_config_absent_when_provider_unset(monkeypatch):
    monkeypatch.delenv("MODEL_PROVIDER", raising=False)
    writer = _dispatch("/api/provider-config", b'{"model":"x"}')
    assert _status(bytes(writer.buffer)) == 404


def test_provider_config_absent_when_provider_bedrock(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "bedrock")
    writer = _dispatch("/api/provider-config", b'{"model":"x"}')
    assert _status(bytes(writer.buffer)) == 404


# ---------------------------------------------------------------------------
# Happy path -- env updated, cache reset, key not echoed
# ---------------------------------------------------------------------------


def test_provider_config_updates_env_and_returns_host(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "openai")
    monkeypatch.delenv("GRACE2_OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("GRACE2_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GRACE2_OPENAI_MODEL", raising=False)
    monkeypatch.delenv("GRACE2_OPENAI_NUM_CTX", raising=False)
    # Seed the num_ctx cache; the update must clear it.
    context_budget._NUM_CTX_CACHE["some-model"] = 4096

    body = json.dumps(
        {
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "sk-or-SECRET-do-not-leak",
            "model": "meta-llama/llama-3.3-70b-instruct:free",
            "num_ctx": 32768,
        }
    ).encode("utf-8")
    writer = _dispatch("/api/provider-config", body)
    out = bytes(writer.buffer)

    assert _status(out) == 200
    payload = _resp_body(out)
    assert payload["ok"] is True
    assert payload["model"] == "meta-llama/llama-3.3-70b-instruct:free"
    assert payload["base_url_host"] == "openrouter.ai"

    # Env actually mutated -- the adapter reads these at the next call.
    import os

    assert os.environ["GRACE2_OPENAI_BASE_URL"] == "https://openrouter.ai/api/v1"
    assert os.environ["GRACE2_OPENAI_API_KEY"] == "sk-or-SECRET-do-not-leak"
    assert os.environ["GRACE2_OPENAI_MODEL"] == (
        "meta-llama/llama-3.3-70b-instruct:free"
    )
    assert os.environ["GRACE2_OPENAI_NUM_CTX"] == "32768"
    # num_ctx cache cleared so a same-name model re-discovers.
    assert context_budget._NUM_CTX_CACHE == {}

    # The response NEVER carries the api key (raw bytes check).
    assert b"sk-or-SECRET-do-not-leak" not in out


def test_provider_config_partial_body_only_sets_present(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "openai")
    monkeypatch.setenv("GRACE2_OPENAI_BASE_URL", "http://127.0.0.1:11434/v1")
    monkeypatch.setenv("GRACE2_OPENAI_API_KEY", "keep-me")
    monkeypatch.delenv("GRACE2_OPENAI_MODEL", raising=False)

    # Only model present -> base_url + key untouched.
    writer = _dispatch("/api/provider-config", b'{"model":"qwen3:8b-24k"}')
    out = bytes(writer.buffer)
    assert _status(out) == 200
    import os

    assert os.environ["GRACE2_OPENAI_BASE_URL"] == "http://127.0.0.1:11434/v1"
    assert os.environ["GRACE2_OPENAI_API_KEY"] == "keep-me"
    assert os.environ["GRACE2_OPENAI_MODEL"] == "qwen3:8b-24k"
    assert _resp_body(out)["base_url_host"] == "127.0.0.1"


def test_provider_config_empty_values_do_not_clobber(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "openai")
    monkeypatch.setenv("GRACE2_OPENAI_API_KEY", "existing-key")
    # Empty api_key string must NOT overwrite an existing key.
    writer = _dispatch("/api/provider-config", b'{"api_key":"","model":"m"}')
    assert _status(bytes(writer.buffer)) == 200
    import os

    assert os.environ["GRACE2_OPENAI_API_KEY"] == "existing-key"


def test_provider_config_malformed_body_is_honest_400(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "openai")
    writer = _dispatch("/api/provider-config", b"not json at all")
    out = bytes(writer.buffer)
    assert _status(out) == 400
    # Error is generic and does NOT echo the raw body.
    err = _resp_body(out)["error"]
    assert "not json at all" not in err


def test_provider_config_non_object_body_is_400(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "openai")
    writer = _dispatch("/api/provider-config", b'["a","list"]')
    assert _status(bytes(writer.buffer)) == 400


# ---------------------------------------------------------------------------
# Feature 2: _filter_openrouter_models (pure)
# ---------------------------------------------------------------------------


def test_filter_keeps_free_tool_capable_only():
    raw = {
        "data": [
            {  # free (pricing 0/0) + tools -> KEPT
                "id": "meta-llama/llama-3.3-70b-instruct",
                "pricing": {"prompt": "0", "completion": "0"},
                "supported_parameters": ["tools", "temperature"],
            },
            {  # :free suffix + tools -> KEPT (label already ends :free)
                "id": "qwen/qwen-2.5-72b-instruct:free",
                "pricing": {"prompt": "0", "completion": "0"},
                "supported_parameters": ["tools"],
            },
            {  # paid -> dropped
                "id": "deepseek/deepseek-chat",
                "pricing": {"prompt": "0.0000014", "completion": "0.0000028"},
                "supported_parameters": ["tools"],
            },
            {  # free but NO tools -> dropped
                "id": "some/free-no-tools:free",
                "pricing": {"prompt": "0", "completion": "0"},
                "supported_parameters": ["temperature"],
            },
            {  # free but supported_parameters ABSENT -> dropped (safe default)
                "id": "some/free-unknown-params:free",
                "pricing": {"prompt": "0", "completion": "0"},
            },
            "not-a-dict",  # malformed -> skipped, not fatal
            {"pricing": {"prompt": "0", "completion": "0"}},  # no id -> skipped
        ]
    }
    out = tool_catalog_http._filter_openrouter_models(raw)
    ids = [m["id"] for m in out]
    assert ids == [
        "meta-llama/llama-3.3-70b-instruct",
        "qwen/qwen-2.5-72b-instruct:free",
    ]
    labels = {m["id"]: m["label"] for m in out}
    # Non-:free id gets a " (free)" suffix; a :free id is left as-is.
    assert labels["meta-llama/llama-3.3-70b-instruct"] == (
        "meta-llama/llama-3.3-70b-instruct (free)"
    )
    assert labels["qwen/qwen-2.5-72b-instruct:free"] == (
        "qwen/qwen-2.5-72b-instruct:free"
    )


def test_filter_handles_non_dict_payload():
    assert tool_catalog_http._filter_openrouter_models(None) == []
    assert tool_catalog_http._filter_openrouter_models({"data": "nope"}) == []


# ---------------------------------------------------------------------------
# Feature 2: _fetch_local_models routes to OpenRouter + caches (mocked httpx)
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeHttpxClient:
    payload: dict = {}
    calls: int = 0
    last_headers: dict | None = None

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, headers=None):
        _FakeHttpxClient.calls += 1
        _FakeHttpxClient.last_headers = headers
        return _FakeResponse(self.payload)


def test_fetch_local_models_routes_to_openrouter(monkeypatch):
    import httpx

    monkeypatch.setenv("GRACE2_OPENAI_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("GRACE2_OPENAI_API_KEY", "sk-or-live-key")
    monkeypatch.setenv(
        "GRACE2_OPENAI_MODEL", "qwen/qwen-2.5-72b-instruct:free"
    )
    # Fresh cache so the fetch actually runs.
    tool_catalog_http._OPENROUTER_MODELS_CACHE.clear()
    _FakeHttpxClient.calls = 0
    _FakeHttpxClient.payload = {
        "data": [
            {
                "id": "meta-llama/llama-3.3-70b-instruct:free",
                "pricing": {"prompt": "0", "completion": "0"},
                "supported_parameters": ["tools"],
            },
            {
                "id": "qwen/qwen-2.5-72b-instruct:free",
                "pricing": {"prompt": "0", "completion": "0"},
                "supported_parameters": ["tools"],
            },
        ]
    }
    monkeypatch.setattr(httpx, "Client", _FakeHttpxClient)

    payload = json.loads(tool_catalog_http._fetch_local_models())
    # Configured default moved first.
    assert payload["default"] == "qwen/qwen-2.5-72b-instruct:free"
    assert payload["models"][0]["id"] == "qwen/qwen-2.5-72b-instruct:free"
    assert {m["id"] for m in payload["models"]} == {
        "meta-llama/llama-3.3-70b-instruct:free",
        "qwen/qwen-2.5-72b-instruct:free",
    }
    # The key rode as a Bearer header (never logged).
    assert _FakeHttpxClient.last_headers == {"Authorization": "Bearer sk-or-live-key"}

    # Second call within the TTL is served from cache (no new round trip).
    calls_after_first = _FakeHttpxClient.calls
    tool_catalog_http._fetch_local_models()
    assert _FakeHttpxClient.calls == calls_after_first


def test_fetch_openrouter_upstream_error_is_typed(monkeypatch):
    import httpx

    monkeypatch.setenv("GRACE2_OPENAI_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.delenv("GRACE2_OPENAI_API_KEY", raising=False)
    tool_catalog_http._OPENROUTER_MODELS_CACHE.clear()

    class _BoomClient(_FakeHttpxClient):
        def get(self, url, headers=None):
            raise RuntimeError("connection refused")

    monkeypatch.setattr(httpx, "Client", _BoomClient)
    try:
        tool_catalog_http._fetch_local_models()
    except tool_catalog_http._LocalModelsUpstreamError as exc:
        assert "openrouter.ai" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected _LocalModelsUpstreamError")
