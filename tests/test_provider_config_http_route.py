"""HTTP-route + unit tests for the OpenRouter model-extensibility seam
(design 2026-07-19).

Two features, both offline (no live network, no live agent):

Feature 3 -- POST /api/provider-config:
  - CLOUD posture: route ABSENT (404) unless MODEL_PROVIDER=openai;
  - a well-formed body updates os.environ[TRID3NT_OPENAI_*] and returns
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
import os

import pytest

from trid3nt_server.server.protocol import catalog_http as tool_catalog_http
from trid3nt_server.adapters import model_discovery
from trid3nt_server.gates import context_budget

_PROVIDER_ENV = (
    "MODEL_PROVIDER",
    "TRID3NT_OPENAI_BASE_URL",
    "TRID3NT_OPENAI_API_KEY",
    "TRID3NT_OPENAI_MODEL",
    "TRID3NT_OPENAI_NUM_CTX",
)


@pytest.fixture(autouse=True)
def _isolate_provider_env():
    """Snapshot/restore the provider env block around every test.

    The route mutates ``os.environ`` DIRECTLY, and ``monkeypatch.delenv`` on an
    already-absent var records nothing to undo -- so without this a test's write
    outlives it and seeds the next test's coherence gate.
    """
    saved = {name: os.environ.get(name) for name in _PROVIDER_ENV}
    yield
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


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
    monkeypatch.delenv("TRID3NT_OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("TRID3NT_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("TRID3NT_OPENAI_MODEL", raising=False)
    monkeypatch.delenv("TRID3NT_OPENAI_NUM_CTX", raising=False)
    # Seed the window-discovery cache; the update must clear it.
    context_budget._WINDOW_CACHE[("openai", "some-model")] = context_budget.ContextWindow(
        tokens=4096,
        source=context_budget.WINDOW_SOURCE_ENV,
        provider="openai",
        model="some-model",
    )

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

    assert os.environ["TRID3NT_OPENAI_BASE_URL"] == "https://openrouter.ai/api/v1"
    assert os.environ["TRID3NT_OPENAI_API_KEY"] == "sk-or-SECRET-do-not-leak"
    assert os.environ["TRID3NT_OPENAI_MODEL"] == (
        "meta-llama/llama-3.3-70b-instruct:free"
    )
    assert os.environ["TRID3NT_OPENAI_NUM_CTX"] == "32768"
    # Discovery cache cleared so a same-name model re-discovers its window.
    assert context_budget._WINDOW_CACHE == {}

    # The response NEVER carries the api key (raw bytes check).
    assert b"sk-or-SECRET-do-not-leak" not in out


def test_provider_config_partial_body_only_sets_present(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "openai")
    monkeypatch.setenv("TRID3NT_OPENAI_BASE_URL", "http://127.0.0.1:11434/v1")
    monkeypatch.setenv("TRID3NT_OPENAI_API_KEY", "keep-me")
    monkeypatch.delenv("TRID3NT_OPENAI_MODEL", raising=False)

    # Only model present -> base_url + key untouched.
    writer = _dispatch("/api/provider-config", b'{"model":"qwen3:8b-24k"}')
    out = bytes(writer.buffer)
    assert _status(out) == 200
    import os

    assert os.environ["TRID3NT_OPENAI_BASE_URL"] == "http://127.0.0.1:11434/v1"
    assert os.environ["TRID3NT_OPENAI_API_KEY"] == "keep-me"
    assert os.environ["TRID3NT_OPENAI_MODEL"] == "qwen3:8b-24k"
    assert _resp_body(out)["base_url_host"] == "127.0.0.1"


def test_provider_config_empty_values_do_not_clobber(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "openai")
    monkeypatch.setenv("TRID3NT_OPENAI_API_KEY", "existing-key")
    # Empty api_key string must NOT overwrite an existing key.
    writer = _dispatch("/api/provider-config", b'{"api_key":"","model":"m"}')
    assert _status(bytes(writer.buffer)) == 200
    import os

    assert os.environ["TRID3NT_OPENAI_API_KEY"] == "existing-key"


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
# base_url/model provider-coherence gate
#
# A dock Save pushes fields independently, so a base-URL-only push could strand
# the previous provider's model id in place: the daemon then dialled an endpoint
# that does not serve it and was silently un-runnable until restart. The gate
# checks the RESOLVED pair before touching os.environ.
# ---------------------------------------------------------------------------


def _seed_provider_env(monkeypatch, base_url: str, model: str) -> None:
    monkeypatch.setenv("MODEL_PROVIDER", "openai")
    monkeypatch.setenv("TRID3NT_OPENAI_BASE_URL", base_url)
    monkeypatch.setenv("TRID3NT_OPENAI_MODEL", model)
    monkeypatch.setenv("TRID3NT_OPENAI_API_KEY", "seeded-key")


def test_coherent_openrouter_pair_is_applied(monkeypatch):
    """(a) matching base_url + model -> 200 and the env actually moves."""
    import os

    _seed_provider_env(monkeypatch, "http://127.0.0.1:11434/v1", "qwen3:8b-24k")
    body = json.dumps(
        {
            "base_url": "https://openrouter.ai/api/v1",
            "model": "deepseek/deepseek-chat",
        }
    ).encode("utf-8")
    writer = _dispatch("/api/provider-config", body)
    out = bytes(writer.buffer)

    assert _status(out) == 200
    assert _resp_body(out)["base_url_host"] == "openrouter.ai"
    assert os.environ["TRID3NT_OPENAI_BASE_URL"] == "https://openrouter.ai/api/v1"
    assert os.environ["TRID3NT_OPENAI_MODEL"] == "deepseek/deepseek-chat"


def test_coherent_ollama_pair_is_applied(monkeypatch):
    """(a) a plain Ollama tag against an Ollama base URL never probes."""
    import os

    _seed_provider_env(
        monkeypatch, "https://openrouter.ai/api/v1", "deepseek/deepseek-chat"
    )
    body = json.dumps(
        {"base_url": "http://127.0.0.1:11434/v1", "model": "llama3.1:8b"}
    ).encode("utf-8")
    writer = _dispatch("/api/provider-config", body)
    out = bytes(writer.buffer)

    assert _status(out) == 200
    assert os.environ["TRID3NT_OPENAI_BASE_URL"] == "http://127.0.0.1:11434/v1"
    assert os.environ["TRID3NT_OPENAI_MODEL"] == "llama3.1:8b"


def test_ollama_base_url_with_openrouter_model_is_rejected(monkeypatch):
    """(b) THE incident: base_url pushed to Ollama, OpenRouter model left in
    place. Rejected statically (no probe) with both values named; env UNCHANGED.
    """
    import os

    _seed_provider_env(
        monkeypatch,
        "https://openrouter.ai/api/v1",
        "meta-llama/llama-3.3-70b-instruct:free",
    )
    # Only base_url is pushed -- the model rides in from the live env.
    body = b'{"base_url":"http://127.0.0.1:11434/v1"}'
    writer = _dispatch("/api/provider-config", body)
    out = bytes(writer.buffer)

    assert _status(out) == 400
    err = _resp_body(out)["error"]
    assert "127.0.0.1" in err
    assert "meta-llama/llama-3.3-70b-instruct:free" in err
    assert "Ollama" in err
    # No partial mutation: BOTH vars are exactly as seeded.
    assert os.environ["TRID3NT_OPENAI_BASE_URL"] == "https://openrouter.ai/api/v1"
    assert os.environ["TRID3NT_OPENAI_MODEL"] == (
        "meta-llama/llama-3.3-70b-instruct:free"
    )


def test_openrouter_base_url_with_bare_tag_is_rejected(monkeypatch):
    """(b) the reverse direction: an Ollama tag against OpenRouter, which only
    serves namespaced 'vendor/model' ids. Env UNCHANGED, api_key untouched."""
    import os

    _seed_provider_env(monkeypatch, "http://127.0.0.1:11434/v1", "qwen3:8b-24k")
    body = json.dumps(
        {"base_url": "https://openrouter.ai/api/v1", "api_key": "sk-or-NEW-SECRET"}
    ).encode("utf-8")
    writer = _dispatch("/api/provider-config", body)
    out = bytes(writer.buffer)

    assert _status(out) == 400
    err = _resp_body(out)["error"]
    assert "openrouter.ai" in err
    assert "qwen3:8b-24k" in err
    assert "vendor/model" in err
    assert os.environ["TRID3NT_OPENAI_BASE_URL"] == "http://127.0.0.1:11434/v1"
    # The rejected key never landed, and never rode back out in the response.
    assert os.environ["TRID3NT_OPENAI_API_KEY"] == "seeded-key"
    assert b"sk-or-NEW-SECRET" not in out


def test_unknown_provider_endpoint_is_not_gated(monkeypatch):
    """vLLM / LM Studio / llama.cpp serve HF-style ids on loopback; only the
    Ollama PORT identifies Ollama, so these are never second-guessed."""
    import os

    _seed_provider_env(monkeypatch, "http://127.0.0.1:11434/v1", "qwen3:8b-24k")
    body = json.dumps(
        {
            "base_url": "http://127.0.0.1:8000/v1",
            "model": "meta-llama/Llama-3.1-8B-Instruct",
        }
    ).encode("utf-8")
    writer = _dispatch("/api/provider-config", body)

    assert _status(bytes(writer.buffer)) == 200
    assert os.environ["TRID3NT_OPENAI_MODEL"] == "meta-llama/Llama-3.1-8B-Instruct"


# --- the live /api/tags probe (namespaced id vs Ollama only) ----------------


class _TagsClient:
    """httpx.Client stand-in for the /api/tags probe."""

    payload: object = {"models": []}
    boom: Exception | None = None
    calls: int = 0

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, headers=None):
        _TagsClient.calls += 1
        if _TagsClient.boom is not None:
            raise _TagsClient.boom
        return _FakeResponse(_TagsClient.payload)


def _install_tags_probe(monkeypatch, payload=None, boom=None):
    import httpx

    _TagsClient.calls = 0
    _TagsClient.payload = payload if payload is not None else {"models": []}
    _TagsClient.boom = boom
    monkeypatch.setattr(httpx, "Client", _TagsClient)


def test_namespaced_model_absent_from_ollama_is_rejected(monkeypatch):
    """(b) an Ollama base URL accepts 'namespace/model' references of its own,
    so shape alone cannot settle it -- the probe answers, the model is absent."""
    import os

    _seed_provider_env(
        monkeypatch, "https://openrouter.ai/api/v1", "deepseek/deepseek-chat"
    )
    _install_tags_probe(
        monkeypatch,
        payload={"models": [{"name": "qwen3:8b-24k"}, {"name": "llama3.1:8b"}]},
    )
    writer = _dispatch(
        "/api/provider-config", b'{"base_url":"http://127.0.0.1:11434/v1"}'
    )
    out = bytes(writer.buffer)

    assert _TagsClient.calls == 1
    assert _status(out) == 400
    err = _resp_body(out)["error"]
    assert "deepseek/deepseek-chat" in err
    assert "127.0.0.1" in err
    assert os.environ["TRID3NT_OPENAI_BASE_URL"] == "https://openrouter.ai/api/v1"


def test_namespaced_model_installed_in_ollama_is_accepted(monkeypatch):
    """A real Ollama namespaced pull must NOT be rejected."""
    import os

    _seed_provider_env(monkeypatch, "https://openrouter.ai/api/v1", "gpt-4o-mini")
    _install_tags_probe(
        monkeypatch, payload={"models": [{"name": "hf.co/user/some-model:latest"}]}
    )
    body = json.dumps(
        {
            "base_url": "http://127.0.0.1:11434/v1",
            "model": "hf.co/user/some-model",
        }
    ).encode("utf-8")
    writer = _dispatch("/api/provider-config", body)

    assert _status(bytes(writer.buffer)) == 200
    assert os.environ["TRID3NT_OPENAI_MODEL"] == "hf.co/user/some-model"


def test_probe_unreachable_does_not_reject(monkeypatch):
    """(c) a network hiccup is NOT incoherence -- an unreachable probe applies."""
    import os

    _seed_provider_env(
        monkeypatch, "https://openrouter.ai/api/v1", "deepseek/deepseek-chat"
    )
    _install_tags_probe(monkeypatch, boom=RuntimeError("connection refused"))
    writer = _dispatch(
        "/api/provider-config", b'{"base_url":"http://127.0.0.1:11434/v1"}'
    )

    assert _status(bytes(writer.buffer)) == 200
    assert os.environ["TRID3NT_OPENAI_BASE_URL"] == "http://127.0.0.1:11434/v1"
    assert os.environ["TRID3NT_OPENAI_MODEL"] == "deepseek/deepseek-chat"


def test_probe_empty_or_unusable_body_does_not_reject(monkeypatch):
    """(c) an endpoint that answers with nothing usable proves nothing."""
    import os

    _seed_provider_env(
        monkeypatch, "https://openrouter.ai/api/v1", "deepseek/deepseek-chat"
    )
    _install_tags_probe(monkeypatch, payload={"models": []})
    writer = _dispatch(
        "/api/provider-config", b'{"base_url":"http://127.0.0.1:11434/v1"}'
    )
    assert _status(bytes(writer.buffer)) == 200
    assert os.environ["TRID3NT_OPENAI_BASE_URL"] == "http://127.0.0.1:11434/v1"

    _install_tags_probe(monkeypatch, payload="not-a-dict")
    writer = _dispatch(
        "/api/provider-config", b'{"base_url":"http://127.0.0.1:11434/v1"}'
    )
    assert _status(bytes(writer.buffer)) == 200


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
    out = model_discovery._filter_openrouter_models(raw)
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
    assert model_discovery._filter_openrouter_models(None) == []
    assert model_discovery._filter_openrouter_models({"data": "nope"}) == []


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

    monkeypatch.setenv("TRID3NT_OPENAI_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("TRID3NT_OPENAI_API_KEY", "sk-or-live-key")
    monkeypatch.setenv(
        "TRID3NT_OPENAI_MODEL", "qwen/qwen-2.5-72b-instruct:free"
    )
    # Fresh cache so the fetch actually runs.
    model_discovery._OPENROUTER_MODELS_CACHE.clear()
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

    payload = json.loads(model_discovery._fetch_local_models())
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
    model_discovery._fetch_local_models()
    assert _FakeHttpxClient.calls == calls_after_first


def test_fetch_openrouter_upstream_error_is_typed(monkeypatch):
    import httpx

    monkeypatch.setenv("TRID3NT_OPENAI_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.delenv("TRID3NT_OPENAI_API_KEY", raising=False)
    model_discovery._OPENROUTER_MODELS_CACHE.clear()

    class _BoomClient(_FakeHttpxClient):
        def get(self, url, headers=None):
            raise RuntimeError("connection refused")

    monkeypatch.setattr(httpx, "Client", _BoomClient)
    try:
        model_discovery._fetch_local_models()
    except model_discovery._LocalModelsUpstreamError as exc:
        assert "openrouter.ai" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected _LocalModelsUpstreamError")
