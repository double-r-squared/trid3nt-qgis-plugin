"""HTTP-route tests for ``GET /api/local-models``.

The browser cannot reach Ollama directly, so the catalog listener proxies its
tags. The route is ABSENT unless the active provider is openai; a 200 carries the
mapped list with the configured default first; an unreachable upstream is an
honest 502 rather than a fabricated success."""

from __future__ import annotations

import asyncio
import json

from trid3nt_server.server.protocol import catalog_http as tool_catalog_http
from trid3nt_server.adapters import model_discovery


class _FakeReader:
    """Feed a single HTTP/1.1 GET request, then EOF."""

    def __init__(self, request: bytes):
        self._lines = request.split(b"\r\n")
        self._buf = [ln + b"\r\n" for ln in self._lines]

    async def readline(self):
        if self._buf:
            return self._buf.pop(0)
        return b""


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


def _request(path: str) -> bytes:
    return (f"GET {path} HTTP/1.1\r\nHost: agent.local\r\n\r\n").encode()


def _run(coro):
    return asyncio.run(coro)


def _status(out: bytes) -> int:
    return int(out.split(b" ", 2)[1])


def _body(out: bytes) -> dict:
    _, _, body = out.partition(b"\r\n\r\n")
    return json.loads(body.decode("utf-8"))


def _dispatch() -> _FakeWriter:
    reader = _FakeReader(_request("/api/local-models"))
    writer = _FakeWriter()
    _run(tool_catalog_http._handle_http(reader, writer))
    return writer


# ---------------------------------------------------------------------------
# Route gating (404 like any unknown path off the openai provider)
# ---------------------------------------------------------------------------


def test_route_absent_when_provider_is_not_openai(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "anthropic")
    writer = _dispatch()
    assert _status(bytes(writer.buffer)) == 404


def test_route_absent_when_provider_is_scripted(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "scripted")
    writer = _dispatch()
    assert _status(bytes(writer.buffer)) == 404


# ---------------------------------------------------------------------------
# Happy path (MODEL_PROVIDER=openai)
# ---------------------------------------------------------------------------


def test_local_models_listed_with_default_first(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "openai")
    monkeypatch.setenv("TRID3NT_OPENAI_MODEL", "qwen3:8b-16k")

    body = json.dumps(
        {
            "models": [
                {"id": "llama3.2:3b", "label": "llama3.2:3b"},
                {"id": "qwen3:8b-16k", "label": "qwen3:8b-16k"},
            ],
            "default": "qwen3:8b-16k",
        },
        separators=(",", ":"),
    ).encode("utf-8")
    monkeypatch.setattr(model_discovery, "_fetch_local_models", lambda: body)

    writer = _dispatch()
    out = bytes(writer.buffer)
    assert _status(out) == 200
    payload = _body(out)
    assert payload["default"] == "qwen3:8b-16k"
    assert [m["id"] for m in payload["models"]] == [
        "llama3.2:3b",
        "qwen3:8b-16k",
    ]


def test_upstream_unreachable_is_typed_502(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "openai")

    def _boom():
        raise model_discovery._LocalModelsUpstreamError("ollama down")

    monkeypatch.setattr(model_discovery, "_fetch_local_models", _boom)
    writer = _dispatch()
    out = bytes(writer.buffer)
    assert _status(out) == 502
    assert "ollama down" in _body(out)["error"]


# ---------------------------------------------------------------------------
# _fetch_local_models parsing (fake httpx client, no network)
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

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url):
        return _FakeResponse(self.payload)


def test_fetch_local_models_maps_ollama_tags(monkeypatch):
    import httpx

    monkeypatch.setenv("TRID3NT_OPENAI_BASE_URL", "http://127.0.0.1:11434/v1")
    monkeypatch.setenv("TRID3NT_OPENAI_MODEL", "qwen3:8b-16k")
    _FakeHttpxClient.payload = {
        "models": [
            {"name": "llama3.2:3b", "size": 1},
            {"name": "qwen3:8b-16k", "size": 2},
            {"nope": True},  # malformed entry skipped
        ]
    }
    monkeypatch.setattr(httpx, "Client", _FakeHttpxClient)

    payload = json.loads(model_discovery._fetch_local_models())
    # Configured default moved first; malformed entry dropped.
    assert payload == {
        "models": [
            {"id": "qwen3:8b-16k", "label": "qwen3:8b-16k"},
            {"id": "llama3.2:3b", "label": "llama3.2:3b"},
        ],
        "default": "qwen3:8b-16k",
    }


def test_fetch_local_models_null_default_when_env_unset(monkeypatch):
    import httpx

    monkeypatch.setenv("TRID3NT_OPENAI_BASE_URL", "http://127.0.0.1:11434/v1")
    monkeypatch.delenv("TRID3NT_OPENAI_MODEL", raising=False)
    _FakeHttpxClient.payload = {"models": [{"name": "llama3.2:3b"}]}
    monkeypatch.setattr(httpx, "Client", _FakeHttpxClient)

    payload = json.loads(model_discovery._fetch_local_models())
    assert payload["default"] is None
    assert payload["models"] == [{"id": "llama3.2:3b", "label": "llama3.2:3b"}]


# ---------------------------------------------------------------------------
# _ollama_tags_url derivation
# ---------------------------------------------------------------------------


def test_tags_url_strips_v1_suffix(monkeypatch):
    monkeypatch.setenv("TRID3NT_OPENAI_BASE_URL", "http://127.0.0.1:11434/v1")
    assert (
        model_discovery._ollama_tags_url()
        == "http://127.0.0.1:11434/api/tags"
    )


def test_tags_url_trailing_slash_and_default(monkeypatch):
    monkeypatch.setenv("TRID3NT_OPENAI_BASE_URL", "http://box:11434/v1/")
    assert model_discovery._ollama_tags_url() == "http://box:11434/api/tags"
    monkeypatch.delenv("TRID3NT_OPENAI_BASE_URL", raising=False)
    assert (
        model_discovery._ollama_tags_url()
        == "http://127.0.0.1:11434/api/tags"
    )
