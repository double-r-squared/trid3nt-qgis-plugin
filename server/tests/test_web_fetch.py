"""Unit tests for the ``web_fetch`` atomic tool (job-0092).

Coverage:
- Tool registers with ``dynamic-1h`` TTL + ``web_fetch`` source class.
- Each of 4 extract modes (``full_html``, ``main_text``, ``json``, ``metadata``)
  produces the documented shape from synthetic HTML / JSON.
- BeautifulSoup boilerplate strip removes ``<script>``, ``<style>``, ``<nav>``,
  ``<header>``, ``<footer>``, ``<aside>``, ``<noscript>``.
- ``<main>`` is preferred over ``<body>`` for ``main_text``.
- URL canonicalization (lowercase scheme/host, drop default port, trailing slash).
- Cache miss → fetcher invoked + bytes written; cache hit → fetcher skipped.
- Bad URL (no scheme) → ``WebFetchInputError`` (not retryable).
- 5xx response → ``WebFetchUpstreamError`` (retryable), no sentinel written.
- 4xx response → ``WebFetchInputError``.
- Timeout → ``WebFetchUpstreamError``.
- ``extract='json'`` Content-Type mismatch → ``WebFetchInputError``.
- Bad ``timeout_s`` → ``WebFetchInputError``.

Live test (``GRACE2_TEST_LIVE_WEB=1``): fetches ``https://www.weather.gov/`` in
``metadata`` mode and asserts the response carries a non-empty title.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest

from grace2_agent.tools import TOOL_REGISTRY
from grace2_agent.tools import web_fetch as web_fetch_mod
from grace2_agent.tools.web_fetch import (
    WebFetchInputError,
    WebFetchUpstreamError,
    _canonicalize_url,
    _extract_main_text,
    _extract_metadata,
    web_fetch,
)


_LIVE_WEB = os.environ.get("GRACE2_TEST_LIVE_WEB") == "1"


# ---------------------------------------------------------------------------
# Fake GCS plumbing (mirrors the pattern from test_data_fetch.py).
# ---------------------------------------------------------------------------


class FakeBlob:
    def __init__(self, store: dict[str, bytes], path: str) -> None:
        self._store = store
        self._path = path
        self.custom_time: datetime | None = None
        self.cache_control: str | None = None
        self.uploaded: bytes | None = None
        self.upload_content_type: str | None = None

    def exists(self) -> bool:
        return self._path in self._store

    def download_as_bytes(self) -> bytes:
        return self._store[self._path]

    def upload_from_string(self, data: bytes, content_type: str | None = None) -> None:
        self.uploaded = data
        self.upload_content_type = content_type
        self._store[self._path] = data


class FakeBucket:
    def __init__(self, store: dict[str, bytes]) -> None:
        self._store = store
        self.blobs: list[FakeBlob] = []

    def blob(self, path: str) -> FakeBlob:
        b = FakeBlob(self._store, path)
        self.blobs.append(b)
        return b


class FakeStorageClient:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self._bucket = FakeBucket(self.store)

    def bucket(self, name: str) -> FakeBucket:
        return self._bucket


@pytest.fixture
def fake_storage(monkeypatch: pytest.MonkeyPatch) -> FakeStorageClient:
    """Route ``read_through`` through an in-memory S3 store (GCP decommissioned).

    The production cache shim is S3-only via boto3; tests must not touch the
    network. This patches the tool module's ``read_through`` with an in-memory
    implementation that mints ``s3://`` URIs and reads/writes ``fake.store``
    (keyed by object KEY), so the cache hit/miss/write assertions hold.
    """
    fake = FakeStorageClient()
    from grace2_agent.tools.cache import (
        CACHE_BUCKET,
        cache_path,
        compute_cache_key,
        is_cacheable,
        ReadThroughResult,
    )

    def patched_read_through(metadata, params, ext, fetch_fn, **kw):
        bucket = kw.get("bucket") or CACHE_BUCKET
        source_id = kw.get("source_id") or (metadata.source_class or metadata.name)
        now = kw.get("now")
        force_refresh = kw.get("force_refresh", False)
        if not is_cacheable(metadata):
            return ReadThroughResult(uri=None, data=fetch_fn(), hit=False)
        key = compute_cache_key(source_id, params, metadata.ttl_class, now=now)
        path = cache_path(metadata.source_class, metadata.ttl_class, key, ext)
        uri = f"s3://{bucket}/{path}"
        if not force_refresh and path in fake.store:
            return ReadThroughResult(uri=uri, data=fake.store[path], hit=True)
        data = fetch_fn()
        fake.store[path] = data
        return ReadThroughResult(uri=uri, data=data, hit=False)

    monkeypatch.setattr(web_fetch_mod, "read_through", patched_read_through)
    return fake


# ---------------------------------------------------------------------------
# Mock httpx Client / Response.
# ---------------------------------------------------------------------------


class _MockResponse:
    def __init__(
        self,
        text: str,
        status_code: int = 200,
        url: str = "https://example.com/",
        headers: dict[str, str] | None = None,
        json_obj: Any = None,
    ) -> None:
        self.text = text
        self.status_code = status_code
        self.url = url
        self.headers = headers or {"Content-Type": "text/html; charset=utf-8"}
        self.content = text.encode("utf-8")
        self._json_obj = json_obj

    def json(self) -> Any:
        if self._json_obj is not None:
            return self._json_obj
        return json.loads(self.text)


class _MockClient:
    def __init__(
        self, *, response: _MockResponse | None = None, raise_exc: Exception | None = None
    ) -> None:
        self._response = response
        self._raise = raise_exc

    def __enter__(self) -> "_MockClient":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        return None

    def get(self, url: str) -> _MockResponse:
        if self._raise is not None:
            raise self._raise
        assert self._response is not None
        return self._response


def _patch_httpx(monkeypatch: pytest.MonkeyPatch, mock_client: _MockClient) -> None:
    monkeypatch.setattr(
        web_fetch_mod.httpx, "Client", lambda *a, **kw: mock_client
    )


# ---------------------------------------------------------------------------
# Synthetic HTML fixtures.
# ---------------------------------------------------------------------------


_HTML_RICH = """\
<!doctype html>
<html lang="en">
<head>
  <title>Test Article Title</title>
  <meta name="description" content="A test article">
  <meta property="og:title" content="OG Title">
  <meta property="og:image" content="https://example.com/img.png">
  <meta name="twitter:card" content="summary_large_image">
</head>
<body>
  <header><nav><a href="/">Home</a></nav></header>
  <aside>Advertisement junk here.</aside>
  <main>
    <article>
      <h1>Main Headline</h1>
      <p>The Mississippi River crested at 45 feet today.</p>
      <p>Severe flooding reported across three counties.</p>
    </article>
  </main>
  <footer>Copyright bla bla bla</footer>
  <script>tracking();</script>
  <style>.hidden { display: none; }</style>
  <noscript>Enable JavaScript</noscript>
</body>
</html>
"""

_HTML_NO_MAIN = """\
<!doctype html>
<html><head><title>Body Fallback</title></head>
<body>
  <p>This is plain body text without main or article.</p>
</body></html>
"""


# ---------------------------------------------------------------------------
# Registration.
# ---------------------------------------------------------------------------


def test_web_fetch_is_registered_with_dynamic_1h() -> None:
    entry = TOOL_REGISTRY["web_fetch"]
    assert entry.metadata.ttl_class == "dynamic-1h"
    assert entry.metadata.source_class == "web_fetch"
    assert entry.metadata.cacheable is True


# ---------------------------------------------------------------------------
# URL canonicalization.
# ---------------------------------------------------------------------------


def test_canonicalize_url_lowercases_scheme_and_host() -> None:
    canon = _canonicalize_url("HTTPS://Example.COM/path")
    assert canon == "https://example.com/path"


def test_canonicalize_url_strips_default_port() -> None:
    assert _canonicalize_url("https://example.com:443/x") == "https://example.com/x"
    assert _canonicalize_url("http://example.com:80/y") == "http://example.com/y"


def test_canonicalize_url_root_gets_trailing_slash() -> None:
    assert _canonicalize_url("https://example.com") == "https://example.com/"


def test_canonicalize_url_rejects_missing_scheme() -> None:
    with pytest.raises(WebFetchInputError):
        _canonicalize_url("example.com/x")


def test_canonicalize_url_rejects_ftp_scheme() -> None:
    with pytest.raises(WebFetchInputError):
        _canonicalize_url("ftp://example.com/")


def test_canonicalize_url_rejects_empty() -> None:
    with pytest.raises(WebFetchInputError):
        _canonicalize_url("")


# ---------------------------------------------------------------------------
# main_text + metadata extraction.
# ---------------------------------------------------------------------------


def test_extract_main_text_strips_boilerplate_and_picks_main() -> None:
    text, title, lang = _extract_main_text(_HTML_RICH)
    assert title == "Test Article Title"
    assert lang == "en"
    # main content present
    assert "Main Headline" in text
    assert "Mississippi River crested at 45 feet" in text
    # boilerplate stripped
    assert "Home" not in text
    assert "Advertisement junk" not in text
    assert "Copyright bla" not in text
    assert "tracking()" not in text
    assert ".hidden" not in text
    assert "Enable JavaScript" not in text


def test_extract_main_text_falls_back_to_body() -> None:
    text, title, _lang = _extract_main_text(_HTML_NO_MAIN)
    assert title == "Body Fallback"
    assert "plain body text" in text


def test_extract_metadata_returns_og_and_meta_tags() -> None:
    md, title, lang = _extract_metadata(_HTML_RICH)
    assert title == "Test Article Title"
    assert lang == "en"
    assert md.get("og:title") == "OG Title"
    assert md.get("og:image") == "https://example.com/img.png"
    assert md.get("description") == "A test article"
    assert md.get("twitter:card") == "summary_large_image"


# ---------------------------------------------------------------------------
# Cache miss → fetch + write; cache hit → no refetch.
# ---------------------------------------------------------------------------


def test_web_fetch_full_html_miss_writes_through_cache(
    monkeypatch: pytest.MonkeyPatch, fake_storage: FakeStorageClient
) -> None:
    _patch_httpx(
        monkeypatch,
        _MockClient(response=_MockResponse(_HTML_RICH, url="https://example.com/x")),
    )
    result = web_fetch("https://example.com/x", extract="full_html")
    assert result["extract_mode"] == "full_html"
    assert result["status_code"] == 200
    assert "Main Headline" in result["content"]
    assert result["title"] is None  # full_html does not populate top-level title
    # GCS write happened.
    assert len(fake_storage.store) == 1
    [(path, blob_bytes)] = fake_storage.store.items()
    assert path.startswith("cache/dynamic-1h/web_fetch/")
    assert path.endswith(".json")
    cached = json.loads(blob_bytes.decode("utf-8"))
    assert cached["extract_mode"] == "full_html"


def test_web_fetch_main_text_extract_mode(
    monkeypatch: pytest.MonkeyPatch, fake_storage: FakeStorageClient
) -> None:
    _patch_httpx(
        monkeypatch,
        _MockClient(response=_MockResponse(_HTML_RICH, url="https://example.com/x")),
    )
    result = web_fetch("https://example.com/x", extract="main_text")
    assert result["extract_mode"] == "main_text"
    assert "Main Headline" in result["content"]
    assert "tracking()" not in result["content"]
    assert result["title"] == "Test Article Title"
    assert result["lang"] == "en"


def test_web_fetch_metadata_extract_mode(
    monkeypatch: pytest.MonkeyPatch, fake_storage: FakeStorageClient
) -> None:
    _patch_httpx(
        monkeypatch,
        _MockClient(response=_MockResponse(_HTML_RICH, url="https://example.com/x")),
    )
    result = web_fetch("https://example.com/x", extract="metadata")
    assert result["extract_mode"] == "metadata"
    assert isinstance(result["content"], dict)
    assert result["content"]["og:title"] == "OG Title"
    assert result["title"] == "Test Article Title"


def test_web_fetch_json_extract_mode(
    monkeypatch: pytest.MonkeyPatch, fake_storage: FakeStorageClient
) -> None:
    body = '{"hits": 3, "items": [{"id": 1}, {"id": 2}, {"id": 3}]}'
    _patch_httpx(
        monkeypatch,
        _MockClient(
            response=_MockResponse(
                body,
                url="https://api.example.com/v1/items",
                headers={"Content-Type": "application/json"},
            )
        ),
    )
    result = web_fetch("https://api.example.com/v1/items", extract="json")
    assert result["extract_mode"] == "json"
    assert result["content"] == {"hits": 3, "items": [{"id": 1}, {"id": 2}, {"id": 3}]}


def test_web_fetch_cache_hit_skips_fetcher(
    monkeypatch: pytest.MonkeyPatch, fake_storage: FakeStorageClient
) -> None:
    # First call — writes the cache.
    _patch_httpx(
        monkeypatch,
        _MockClient(response=_MockResponse(_HTML_RICH, url="https://example.com/x")),
    )
    first = web_fetch("https://example.com/x", extract="full_html")
    assert first["extract_mode"] == "full_html"

    # Swap httpx to raise — proves the second call hits the cache, not the wire.
    _patch_httpx(
        monkeypatch,
        _MockClient(raise_exc=RuntimeError("should NOT be called on cache hit")),
    )
    second = web_fetch("https://example.com/x", extract="full_html")
    assert second["url"] == first["url"]
    assert second["content"] == first["content"]


# ---------------------------------------------------------------------------
# Error paths.
# ---------------------------------------------------------------------------


def test_web_fetch_bad_url_raises_input_error(
    fake_storage: FakeStorageClient,
) -> None:
    with pytest.raises(WebFetchInputError):
        web_fetch("not-a-url", extract="main_text")
    # No cache write happened.
    assert fake_storage.store == {}


def test_web_fetch_5xx_raises_upstream_error(
    monkeypatch: pytest.MonkeyPatch, fake_storage: FakeStorageClient
) -> None:
    _patch_httpx(
        monkeypatch,
        _MockClient(
            response=_MockResponse(
                "<html>boom</html>", status_code=503, url="https://example.com/down"
            )
        ),
    )
    with pytest.raises(WebFetchUpstreamError):
        web_fetch("https://example.com/down", extract="main_text")
    # No sentinel write.
    assert fake_storage.store == {}


def test_web_fetch_4xx_raises_input_error(
    monkeypatch: pytest.MonkeyPatch, fake_storage: FakeStorageClient
) -> None:
    _patch_httpx(
        monkeypatch,
        _MockClient(
            response=_MockResponse(
                "<html>not found</html>",
                status_code=404,
                url="https://example.com/missing",
            )
        ),
    )
    with pytest.raises(WebFetchInputError):
        web_fetch("https://example.com/missing", extract="main_text")
    assert fake_storage.store == {}


def test_web_fetch_timeout_raises_upstream_error(
    monkeypatch: pytest.MonkeyPatch, fake_storage: FakeStorageClient
) -> None:
    _patch_httpx(
        monkeypatch,
        _MockClient(raise_exc=httpx.TimeoutException("slow")),
    )
    with pytest.raises(WebFetchUpstreamError):
        web_fetch("https://example.com/slow", extract="main_text")
    assert fake_storage.store == {}


def test_web_fetch_json_on_non_json_content_type_raises_input_error(
    monkeypatch: pytest.MonkeyPatch, fake_storage: FakeStorageClient
) -> None:
    _patch_httpx(
        monkeypatch,
        _MockClient(
            response=_MockResponse(
                "<html>not json</html>",
                url="https://example.com/page",
                headers={"Content-Type": "image/png"},
            )
        ),
    )
    with pytest.raises(WebFetchInputError):
        web_fetch("https://example.com/page", extract="json")


def test_web_fetch_bad_timeout_raises_input_error(
    fake_storage: FakeStorageClient,
) -> None:
    with pytest.raises(WebFetchInputError):
        web_fetch("https://example.com/", extract="main_text", timeout_s=0)
    with pytest.raises(WebFetchInputError):
        web_fetch("https://example.com/", extract="main_text", timeout_s=-5)


def test_web_fetch_unknown_extract_mode_raises_input_error(
    fake_storage: FakeStorageClient,
) -> None:
    with pytest.raises(WebFetchInputError):
        web_fetch("https://example.com/", extract="garbage")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Live verification (env-guarded).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _LIVE_WEB, reason="set GRACE2_TEST_LIVE_WEB=1 to run live network tests")
def test_web_fetch_live_weather_gov_metadata() -> None:
    """Live fetch: ``https://www.weather.gov/`` in metadata mode.

    Asserts the result carries a non-empty title (or Open Graph title) and the
    fetched URL matches the canonical form. This bypasses GCS by NOT using
    the fake_storage fixture — the real ``read_through`` writes to the
    production cache bucket if ADC is available. If GCS is unreachable, the
    test still validates the fetch shape via the upstream attempt.
    """
    result = web_fetch("https://www.weather.gov/", extract="metadata")
    assert result["status_code"] == 200
    assert result["extract_mode"] == "metadata"
    # weather.gov has both <title> and og:title; at least one populated.
    md = result["content"]
    assert isinstance(md, dict)
    has_title = bool(result.get("title")) or bool(md.get("og:title"))
    assert has_title, f"no title found in metadata: keys={list(md.keys())}"
