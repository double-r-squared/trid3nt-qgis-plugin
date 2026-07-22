"""``web_fetch`` atomic tool — generic web page ingest with extraction modes (job-0092).

This module registers a single atomic tool ``web_fetch`` that fetches an
arbitrary URL and returns a structured dict with one of four extraction modes:
``full_html``, ``main_text``, ``json``, or ``metadata``.

Unlike the layer-producing fetchers in ``data_fetch.py``, this tool returns a
plain ``dict`` — it is intended for the agent's research / discovery loop
(e.g. confirming an article subject before extracting event metadata, pulling
the body of a news article, parsing a small JSON API response). The result is
NOT a ``LayerURI`` and does not feed the map.

Cache class: ``dynamic-1h`` — web pages change. The 1-hour TTL boundary is
the only freshness gate (the cache key does not include time directly; the
TTL-bucket vintage in ``compute_cache_key`` rolls every hour).

Cache key inputs (via ``read_through(params=...)``):
    - ``url`` (canonicalized: scheme lowercased, default-port stripped,
      trailing slash on root)
    - ``extract`` (one of the four modes)
    - ``user_agent`` (so a UA change forces a refetch and stays attributable)

Output shape (returned as dict; also persisted as JSON blob in the cache):
    {
        "url": str (final URL after redirects),
        "status_code": int,
        "fetched_at": ISO-8601 str (UTC),
        "extract_mode": str,
        "content": str | dict | None,
        "title": str | None,
        "lang": str | None,
        "content_length": int,
    }

Robots.txt: NOT honored in v0.1 (surfaced as OQ-0092-WEB-FETCH-ROBOTS for
sprint-13 — a future revision adds a per-host robots cache + allow-check).

Typed errors (FR-AS-11):
    - ``WebFetchInputError(retryable=False)`` — bad URL (no scheme, malformed)
      or unknown extract mode.
    - ``WebFetchUpstreamError(retryable=True)`` — 5xx, timeout, connect error,
      or JSON decode failure on ``extract="json"``.

External-API resilience (NFR-R-1): per-call timeout, single re-raise on
fetch failure (no sentinel writes — see ``read_through``). The agent
FR-AS-11 surface decides retry/clarify/fallback.

FR-TA-3 docstring discipline: the public ``web_fetch`` carries "Use this when"
and "Do NOT use this for" sections so the FunctionTool surface is
self-describing to Gemini.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import urlparse, urlunparse

import httpx

from grace2_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from .cache import read_through

__all__ = [
    "web_fetch",
    "WebFetchError",
    "WebFetchInputError",
    "WebFetchUpstreamError",
]

logger = logging.getLogger("grace2_agent.tools.web_fetch")


# ---------------------------------------------------------------------------
# Typed errors (FR-AS-11).
# ---------------------------------------------------------------------------


class WebFetchError(RuntimeError):
    """Base class for web_fetch failures. ``error_code`` is the A.6 code."""

    error_code: str = "WEB_FETCH_ERROR"
    retryable: bool = True


class WebFetchInputError(WebFetchError):
    """Invalid input to ``web_fetch`` (bad URL, unknown extract mode)."""

    error_code = "WEB_FETCH_INPUT_INVALID"
    retryable = False


class WebFetchUpstreamError(WebFetchError):
    """Upstream HTTP fetch failed or returned an unparseable response."""

    error_code = "WEB_FETCH_UPSTREAM_ERROR"
    retryable = True


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

_DEFAULT_USER_AGENT = "grace2-agent/0.1 (research; contact: grace2-ops@local)"
_ALLOWED_EXTRACT_MODES = ("full_html", "main_text", "json", "metadata")

#: Boilerplate tags stripped before the main-text extraction so the result is
#: readable narrative content rather than navigation chrome.
_BOILERPLATE_TAGS = ("script", "style", "nav", "header", "footer", "aside", "noscript")


# ---------------------------------------------------------------------------
# URL canonicalization for the cache key.
# ---------------------------------------------------------------------------


def _canonicalize_url(url: str) -> str:
    """Return a deterministic canonical form of ``url`` for cache-keying.

    Rules:
        - lowercase scheme + netloc;
        - drop default ports (http:80, https:443);
        - ensure a trailing slash on the root path;
        - keep query string verbatim (order matters to many APIs).

    Raises ``WebFetchInputError`` if the URL has no scheme or host, since the
    underlying ``httpx.get`` would otherwise raise an opaque error.
    """
    if not url or not isinstance(url, str):
        raise WebFetchInputError(f"url must be a non-empty string; got {url!r}")
    parsed = urlparse(url.strip())
    if not parsed.scheme:
        raise WebFetchInputError(
            f"url has no scheme (expected http:// or https://): {url!r}"
        )
    if parsed.scheme not in ("http", "https"):
        raise WebFetchInputError(
            f"unsupported url scheme {parsed.scheme!r}; only http/https are allowed"
        )
    if not parsed.netloc:
        raise WebFetchInputError(f"url has no host: {url!r}")
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    # Strip default ports.
    if scheme == "http" and netloc.endswith(":80"):
        netloc = netloc[: -len(":80")]
    if scheme == "https" and netloc.endswith(":443"):
        netloc = netloc[: -len(":443")]
    path = parsed.path or "/"
    return urlunparse((scheme, netloc, path, parsed.params, parsed.query, ""))


# ---------------------------------------------------------------------------
# HTML extraction helpers.
# ---------------------------------------------------------------------------


def _extract_main_text(html: str) -> tuple[str, str | None, str | None]:
    """Boilerplate-stripped readable text from ``html``.

    Strategy: parse with ``lxml`` via BeautifulSoup, remove all boilerplate
    tags, then preferentially extract from ``<main>`` → ``<article>`` →
    ``<body>``. The fallback is the whole soup if none of those land.

    Returns ``(text, title, lang)`` so the caller can populate the result
    dict's top-level fields. ``title`` and ``lang`` come from the original
    soup (before body extraction) so they survive the strip.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")

    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else None

    html_tag = soup.find("html")
    lang = html_tag.get("lang") if html_tag else None
    if isinstance(lang, list):
        lang = lang[0] if lang else None

    # Strip boilerplate.
    for tag_name in _BOILERPLATE_TAGS:
        for tag in soup.find_all(tag_name):
            tag.decompose()

    # Preferential extraction.
    container = soup.find("main") or soup.find("article") or soup.find("body") or soup
    text = container.get_text(separator="\n", strip=True)
    # Collapse runs of blank lines.
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return ("\n".join(lines), title, lang)


def _extract_metadata(html: str) -> tuple[dict[str, Any], str | None, str | None]:
    """Open Graph + meta-tag dictionary from ``html``.

    Returns ``(metadata_dict, title, lang)``. ``metadata_dict`` includes
    every ``<meta name=*>`` / ``<meta property=*>`` keyed by the attribute
    value, with the meta's ``content`` as the dict value. ``<title>`` and
    ``<html lang>`` are still surfaced separately so the result dict's
    top-level ``title`` / ``lang`` fields are uniformly populated across
    modes.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")

    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else None

    html_tag = soup.find("html")
    lang = html_tag.get("lang") if html_tag else None
    if isinstance(lang, list):
        lang = lang[0] if lang else None

    metadata: dict[str, Any] = {}
    if title is not None:
        metadata["title"] = title
    if lang is not None:
        metadata["lang"] = lang

    for meta in soup.find_all("meta"):
        # property= is OG/Twitter; name= is everything else (description, etc.).
        key = meta.get("property") or meta.get("name")
        if not key:
            continue
        content = meta.get("content")
        if content is None:
            continue
        metadata[str(key).lower()] = content

    return (metadata, title, lang)


# ---------------------------------------------------------------------------
# Fetch + extract — the body the cache shim calls on miss.
# ---------------------------------------------------------------------------


def _fetch_and_extract_bytes(
    url: str,
    extract: str,
    timeout_s: float,
    user_agent: str,
) -> bytes:
    """Perform the HTTP GET + extraction and return the result dict as JSON bytes.

    The cache shim writes the bytes to GCS verbatim; the tool function then
    decodes them back to a dict before returning to the caller. This keeps
    the cache miss/hit paths symmetric (both return JSON-decodable bytes).
    """
    headers = {"User-Agent": user_agent, "Accept": "*/*"}
    try:
        with httpx.Client(
            timeout=timeout_s, follow_redirects=True, headers=headers
        ) as client:
            response = client.get(url)
    except httpx.TimeoutException as exc:
        raise WebFetchUpstreamError(
            f"web_fetch timed out after {timeout_s}s for url={url!r}: {exc}"
        ) from exc
    except httpx.HTTPError as exc:
        raise WebFetchUpstreamError(
            f"web_fetch HTTP error for url={url!r}: {exc}"
        ) from exc

    status = response.status_code
    if status >= 500:
        raise WebFetchUpstreamError(
            f"web_fetch upstream {status} for url={url!r}"
        )
    if status >= 400:
        # 4xx is a client-input problem (404, 401, etc.) — non-retryable at
        # the same URL. Surface as input error so the agent doesn't retry-loop.
        raise WebFetchInputError(
            f"web_fetch client error {status} for url={url!r}"
        )

    final_url = str(response.url)
    fetched_at = datetime.now(timezone.utc).isoformat()
    body_text = response.text
    content_length = len(response.content)

    content: str | dict[str, Any] | None
    title: str | None = None
    lang: str | None = None

    if extract == "full_html":
        content = body_text
    elif extract == "main_text":
        content, title, lang = _extract_main_text(body_text)
    elif extract == "metadata":
        meta_dict, title, lang = _extract_metadata(body_text)
        content = meta_dict
    elif extract == "json":
        content_type = response.headers.get("Content-Type", "").lower()
        if "json" not in content_type and not content_type.startswith(
            ("application/", "text/")
        ):
            # Strict: refuse to parse non-JSON as JSON.
            raise WebFetchInputError(
                f"web_fetch extract='json' but Content-Type={content_type!r} is not json-ish "
                f"for url={url!r}"
            )
        if "json" not in content_type:
            # text/* or application/* — only proceed if it actually parses.
            pass
        try:
            content = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise WebFetchUpstreamError(
                f"web_fetch could not decode JSON from {url!r}: {exc}"
            ) from exc
    else:
        # Should be filtered upstream; defensive.
        raise WebFetchInputError(
            f"unknown extract mode {extract!r}; allowed: {_ALLOWED_EXTRACT_MODES}"
        )

    result: dict[str, Any] = {
        "url": final_url,
        "status_code": status,
        "fetched_at": fetched_at,
        "extract_mode": extract,
        "content": content,
        "title": title,
        "lang": lang,
        "content_length": content_length,
    }
    return json.dumps(result).encode("utf-8")


# ---------------------------------------------------------------------------
# Registration + public entry point.
# ---------------------------------------------------------------------------


_WEB_FETCH_METADATA = AtomicToolMetadata(
    name="web_fetch",
    ttl_class="dynamic-1h",
    source_class="web_fetch",
    cacheable=True,
)


@register_tool(
    _WEB_FETCH_METADATA,
    # Annotations: readOnlyHint=True (HTTP GET only; no state mutation),
    # openWorldHint=True (fetches arbitrary public URLs; fully open-world),
    # destructiveHint=False, idempotentHint=True (cache shim + TTL deduplicates).
    open_world_hint=True,
)
def web_fetch(
    url: str,
    extract: Literal["full_html", "main_text", "json", "metadata"] = "main_text",
    timeout_s: float = 30.0,
    user_agent: str = _DEFAULT_USER_AGENT,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Generic web-page ingest with content extraction modes.

    Fetches an http/https URL with configurable extraction: stripped article
    text, full HTML, JSON body, or metadata-only. Results are cached for 1 hour.
    Returns a structured dict with the extracted content, HTTP status, and
    provenance fields consumed by downstream claim aggregation.

    When to use:
        - Fetching the body of a news article or incident report URL for Case 2
          event ingest (``run_model_news_event_ingest`` calls this via the
          registry for "url" sources).
        - Confirming a page's subject before deeper extraction (use
          ``extract="metadata"`` — cheapest mode, reads only ``<meta>`` tags).
        - Pulling a small public-data JSON API response that has no dedicated
          fetcher tool.
        - Research or citation checks for a specific URL.

    When NOT to use:
        - Large file downloads (no streaming surface — use a dedicated fetcher).
        - Pages requiring JavaScript rendering (server-rendered HTML only; SPA
          shells with empty bodies will return empty ``content``).
        - Authenticated endpoints (no credential injection).
        - Anything a domain-specific atomic tool already covers (``fetch_dem``,
          ``fetch_landcover``, ``geocode_location``, ``fetch_administrative_boundaries``
          are always preferred over a raw ``web_fetch`` call to the same upstream).

    Params:
        url: the absolute http/https URL to fetch. Schemes other than http/https
            are rejected with ``WebFetchInputError``.
        extract: one of:
            - ``"full_html"`` — entire response body as a string in ``content``;
            - ``"main_text"`` — boilerplate-stripped readable text via
              BeautifulSoup + lxml (strips ``<script>``, ``<style>``,
              ``<nav>``, ``<header>``, ``<footer>``, ``<aside>``,
              ``<noscript>`` and prefers ``<main>``/``<article>`` over
              ``<body>``); the default — best for article bodies;
            - ``"json"`` — ``response.json()`` after a Content-Type check
              (refuses to parse manifestly non-JSON bodies);
            - ``"metadata"`` — Open Graph + ``<meta>`` tags + ``<title>``
              only; cheapest mode, no body extraction.
        timeout_s: per-request timeout in seconds. Default 30.0.
        user_agent: User-Agent header sent with the request. Defaults to
            ``"grace2-agent/0.1 (research; contact: grace2-ops@local)"``; the
            UA is part of the cache key so a change forces a refetch.

    Returns:
        A dict with the shape::

            {
              "url": str,           # final URL after redirects
              "status_code": int,   # HTTP status (always 2xx here; 4xx/5xx raise)
              "fetched_at": str,    # ISO-8601 UTC timestamp of the fetch
              "extract_mode": str,  # echoes the ``extract`` argument
              "content": str | dict | None,
              "title": str | None,  # extracted ``<title>`` if present
              "lang": str | None,   # extracted ``<html lang=...>`` if present
              "content_length": int,
            }

    Caching: the result is cached in GCS at
    ``s3://trid3nt-cache/cache/dynamic-1h/web_fetch/<hash>.json``
    with the 1-hour TTL window as the only freshness boundary. The cache key
    inputs are ``(canonicalized url, extract, user_agent)``.

    Typed errors (FR-AS-11):
        - ``WebFetchInputError`` (not retryable) — bad URL, unsupported scheme,
          unknown extract mode, 4xx HTTP response, Content-Type mismatch on
          ``extract="json"``;
        - ``WebFetchUpstreamError`` (retryable) — timeout, connection error,
          5xx response, JSON decode failure.

    Robots.txt: NOT honored in v0.1 (acceptable for research). Surfaced as
    OQ-0092-WEB-FETCH-ROBOTS for sprint-13 revisit — a future version reads
    and respects ``robots.txt`` per host before fetching.

    Cross-tool dependencies:
        Upstream (consumes):
        - No tool dependencies — takes a raw URL from the agent or user.
        Downstream (feeds):
        - ``aggregate_claims_across_sources`` — the returned dict (with ``url``,
          ``content``, ``fetched_at``) is passed as an element of the ``sources``
          list for cross-source claim extraction.
        - ``run_model_news_event_ingest`` — calls this via the tool registry for
          each "url"-type source in the ``sources`` input list.
    """
    if extract not in _ALLOWED_EXTRACT_MODES:
        raise WebFetchInputError(
            f"unknown extract mode {extract!r}; allowed: {_ALLOWED_EXTRACT_MODES}"
        )
    if not isinstance(timeout_s, (int, float)) or timeout_s <= 0:
        raise WebFetchInputError(
            f"timeout_s must be a positive number; got {timeout_s!r}"
        )

    canonical_url = _canonicalize_url(url)

    params = {
        "url": canonical_url,
        "extract": extract,
        "user_agent": user_agent,
    }
    result = read_through(
        metadata=_WEB_FETCH_METADATA,
        params=params,
        ext="json",
        fetch_fn=lambda: _fetch_and_extract_bytes(
            canonical_url, extract, timeout_s, user_agent
        ),
    )
    try:
        decoded = json.loads(result.data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        # Cache corruption — should not happen, but surface as upstream so
        # the agent can decide to retry (which will force-refresh on next call).
        raise WebFetchUpstreamError(
            f"web_fetch cache entry could not be decoded as JSON: {exc}"
        ) from exc
    if not isinstance(decoded, dict):
        raise WebFetchUpstreamError(
            f"web_fetch cache entry decoded to non-dict ({type(decoded).__name__}); "
            "cache corruption?"
        )
    return decoded
