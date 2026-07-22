"""Thin streaming WMS proxy for QGIS Server (job-0255, sprint-13.5 Stage 2).

Background
----------
Sprint-13.5 Stage 2 flips QGIS Server's Cloud Run service from a public
``allUsers`` invoker binding to **invoker-only** (the agent-runtime SA is the
sole principal — see ``infra/qgis-server.tf``). Once that flip is applied, a
direct browser → QGIS Server WMS request returns ``403``. Tier B must still
reach the browser only through QGIS Server (Invariant 4/5), so the agent
service — which holds the invoker grant — fronts QGIS Server with a thin proxy:

    browser  →  GET <agent-http>/qgis-proxy?<WMS params>
             →  agent attaches a Google-signed OIDC identity token
             →  QGIS Server (invoker-only)  →  PNG tile bytes
             →  streamed back to the browser

Design constraints (manifest job-0255 + kickoff):

* **Streaming** — the response body is relayed in chunks; whole tiles are never
  buffered in agent memory (contract lens). The blocking ``requests`` read
  (already an agent dep) runs in a thread executor with a bounded chunk size;
  each chunk is handed to an async writer callback as it arrives.
* **Credential stripping** — EVERY inbound user credential / session header
  (Authorization, Cookie, X-Firebase-*, etc.) is dropped before forwarding.
  QGIS Server must never see a user identity (no UID leak). Only WMS-relevant
  query params transit; only a curated set of response headers come back.
* **No open proxy** — the upstream host is ALWAYS the configured QGIS base URL.
  Any inbound attempt to redirect the upstream (a ``MAP=`` pointing off-box, an
  absolute-URL param, etc.) is ignored: we forward the query string to the
  fixed base and nothing else.
* **OIDC token** — on Cloud Run, ``google.auth`` fetches an ID token for the
  QGIS Cloud Run audience via the metadata server. In dev with no credentials
  (and a still-public QGIS), the proxy forwards WITHOUT a token and never
  crashes (graceful degrade).
* **Env gate** — ``QGIS_PROXY_ENABLED`` defaults to ``"false"``. When off, the
  route is treated as absent (the catalog HTTP server 404s it); NOTHING about
  today's behavior changes. job-0257 flips it on in prod.

This module is server-side agent code that runs IN the agent process on its
existing :8766 HTTP listener (``tool_catalog_http.py``). It lives in the agent
package — not ``services/workers/`` — because the agent must import it at
runtime and ``services/workers/`` is not on the agent's import path. See the
job-0255 report's Open Questions for that path deviation.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Awaitable, Callable, Iterable

logger = logging.getLogger("grace2_agent.qgis_proxy")

__all__ = [
    "qgis_proxy_enabled",
    "qgis_server_base_url",
    "STRIPPED_REQUEST_HEADERS",
    "PASSTHROUGH_RESPONSE_HEADERS",
    "ProxyResult",
    "fetch_oidc_token",
    "stream_qgis_response",
    "DEFAULT_CHUNK_SIZE",
    "DEFAULT_UPSTREAM_TIMEOUT_S",
]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

#: Default QGIS Server WMS base URL. Mirrors ``publish_layer.DEFAULT_QGIS_SERVER_URL``
#: and ``web/src/Map.tsx`` so the proxy forwards to the same upstream the rest
#: of the system already targets. Overridable via ``QGIS_SERVER_URL``.
DEFAULT_QGIS_SERVER_URL: str = (
    "https://grace-2-qgis-server-425352658356.us-central1.run.app/ogc/wms"
)

#: Chunk size for the streamed relay (bytes). 64 KiB keeps per-chunk memory
#: tiny while staying well above a single TLS record so throughput is fine.
DEFAULT_CHUNK_SIZE: int = 64 * 1024

#: Upstream request timeout (seconds). A WMS GetMap should be sub-second on a
#: warm QGIS instance; allow generous headroom for a cold start.
DEFAULT_UPSTREAM_TIMEOUT_S: float = 30.0


def qgis_proxy_enabled() -> bool:
    """Return whether the ``/qgis-proxy`` route is enabled.

    Gated on ``QGIS_PROXY_ENABLED``; defaults to ``False`` (route absent /
    404). Truthy values: ``1``, ``true``, ``yes``, ``on`` (case-insensitive).
    Anything else — including unset — is False, so TODAY'S behavior (no proxy
    route) is unchanged until job-0257 sets the flag in prod.
    """
    raw = os.environ.get("QGIS_PROXY_ENABLED", "false").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def qgis_server_base_url() -> str:
    """Return the fixed upstream QGIS Server base URL (no trailing slash).

    This is the ONLY host the proxy ever forwards to (no-open-proxy guarantee).
    Reads ``QGIS_SERVER_URL`` — the same env var ``publish_layer`` and the
    PyQGIS worker already consume — so all three agree on the upstream.
    """
    return os.environ.get("QGIS_SERVER_URL", DEFAULT_QGIS_SERVER_URL).rstrip("/")


# ---------------------------------------------------------------------------
# Header policy (credential stripping)
# ---------------------------------------------------------------------------
#
# QGIS Server must NEVER see a user identity. We do NOT forward inbound request
# headers verbatim; instead we forward ONLY a fixed, minimal set we construct
# ourselves (the OIDC Authorization, when present, plus a UA). Everything else
# the browser sent — credentials, cookies, Firebase session bits — is dropped
# by construction. ``STRIPPED_REQUEST_HEADERS`` documents the credential-class
# headers explicitly for the report's header-stripping table and for the test
# that asserts none of them reach the upstream request.

#: Inbound headers that carry (or could carry) user identity. Listed lowercase.
#: The proxy never copies inbound headers, so these are stripped by the
#: forward-nothing policy; the set exists for documentation + the assertion
#: test (the forwarded request must contain none of these from the client).
STRIPPED_REQUEST_HEADERS: frozenset[str] = frozenset(
    {
        "authorization",
        "cookie",
        "set-cookie",
        "x-firebase-appcheck",
        "x-firebase-auth",
        "x-forwarded-authorization",
        "x-goog-authenticated-user-id",
        "x-goog-authenticated-user-email",
        "x-goog-iap-jwt-assertion",
        "proxy-authorization",
        "x-grace2-session",
        "x-grace2-user",
    }
)

#: Response headers we relay back to the browser. WMS tiles are images; the
#: browser only needs the content type, length/encoding, and cache hints.
#: Everything else (server identity, set-cookie, upstream auth echoes) is
#: dropped — the response is image bytes, not credentialed data.
PASSTHROUGH_RESPONSE_HEADERS: frozenset[str] = frozenset(
    {
        "content-type",
        "content-length",
        "content-encoding",
        "cache-control",
        "expires",
        "last-modified",
        "etag",
    }
)


# ---------------------------------------------------------------------------
# OIDC identity token (Cloud Run → invoker-only QGIS Server)
# ---------------------------------------------------------------------------


def _oidc_audience(base_url: str) -> str:
    """Return the OIDC audience for the QGIS Cloud Run service.

    Cloud Run validates the ID token's ``aud`` against the service's ROOT URL
    (scheme + host), not the full WMS path. Derive ``https://<host>`` from the
    configured base so the token validates regardless of the ``/ogc/wms`` path.
    """
    from urllib.parse import urlsplit

    parts = urlsplit(base_url)
    if parts.scheme and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"
    # Defensive: if the base is malformed, fall back to the base itself.
    return base_url


def fetch_oidc_token(base_url: str) -> str | None:
    """Fetch a Google-signed OIDC identity token for the QGIS Cloud Run audience.

    Returns the bearer token string on Cloud Run (or any environment where
    Application Default Credentials can mint an ID token), or ``None`` in dev
    when no credentials are available. NEVER raises — a missing/failed token
    degrades to an unauthenticated forward (which still works against a public
    QGIS in dev). The caller attaches the token only when non-None.
    """
    audience = _oidc_audience(base_url)
    try:
        import google.auth.transport.requests as greq
        from google.oauth2 import id_token as goidc

        request = greq.Request()
        token = goidc.fetch_id_token(request, audience)
        if token:
            return token
        logger.info(
            "qgis-proxy: OIDC token fetch returned empty for aud=%s; "
            "forwarding unauthenticated",
            audience,
        )
        return None
    except Exception as exc:  # noqa: BLE001 — degrade gracefully in dev
        logger.info(
            "qgis-proxy: no OIDC token (aud=%s, %s: %s); forwarding "
            "unauthenticated (dev path / public QGIS)",
            audience,
            type(exc).__name__,
            exc,
        )
        return None


# ---------------------------------------------------------------------------
# Streaming relay
# ---------------------------------------------------------------------------


class ProxyResult:
    """Outcome of a proxied upstream request (status + relayable headers).

    The body is NOT held here — it is streamed chunk-by-chunk via the
    ``write_chunk`` callback passed to ``stream_qgis_response``. This class
    only carries what the HTTP responder needs to write the status line and
    headers before the first body byte.
    """

    __slots__ = ("status", "headers")

    def __init__(self, status: int, headers: dict[str, str]) -> None:
        self.status = status
        self.headers = headers

    def __repr__(self) -> str:  # pragma: no cover — debug aid
        return f"ProxyResult(status={self.status}, headers={self.headers!r})"


def _filter_response_headers(raw_headers: Iterable[tuple[str, str]]) -> dict[str, str]:
    """Keep only the allowlisted response headers (lowercased keys)."""
    out: dict[str, str] = {}
    for k, v in raw_headers:
        if k.lower() in PASSTHROUGH_RESPONSE_HEADERS:
            out[k] = v
    return out


async def stream_qgis_response(
    query_string: str,
    write_status_and_headers: Callable[[ProxyResult], Awaitable[None]],
    write_chunk: Callable[[bytes], Awaitable[None]],
    *,
    base_url: str | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    timeout_s: float = DEFAULT_UPSTREAM_TIMEOUT_S,
) -> ProxyResult:
    """Forward ``query_string`` to QGIS Server and STREAM the response.

    Mechanism (streaming, bounded memory):

    1. Build the upstream URL as ``<fixed base>?<query_string>`` — the base is
       the configured ``QGIS_SERVER_URL`` and is NEVER taken from the inbound
       request (no-open-proxy). The client's query string transits verbatim;
       inbound request headers do NOT (credential stripping by construction).
    2. Mint an OIDC token for the QGIS audience (None in dev) and set the only
       request headers the proxy ever sends: ``Authorization: Bearer <token>``
       (when present) and a static ``User-Agent``.
    3. Open the upstream with ``requests`` ``stream=True`` in a worker thread.
       Read fixed-size chunks (``iter_content``) and hand each to the async
       ``write_chunk`` callback via a thread→loop bridge — the whole tile is
       never materialised in agent memory.

    The status + filtered headers are delivered to ``write_status_and_headers``
    BEFORE the first body chunk, so the responder can emit a chunked / streamed
    HTTP response. Upstream 4xx/5xx are relayed honestly (status + body), never
    masked as success.

    Returns the ``ProxyResult`` (status + relayed headers) for the caller's
    logging/telemetry. Raises only on a genuine transport failure to reach the
    upstream (the caller maps that to a 502).
    """
    import asyncio

    import requests

    base = (base_url or qgis_server_base_url()).rstrip("/")
    qs = query_string.lstrip("?")
    upstream_url = f"{base}?{qs}" if qs else base

    # Construct the ONLY request headers we forward — never the inbound ones.
    out_headers: dict[str, str] = {"User-Agent": "grace-2-agent-qgis-proxy/0.1"}
    token = fetch_oidc_token(base)
    if token:
        out_headers["Authorization"] = f"Bearer {token}"

    loop = asyncio.get_running_loop()

    # ``requests`` is blocking; run the whole open+stream in a worker thread and
    # bridge each chunk back to the event loop. We hand the response object out
    # of the thread (after headers arrive) so headers are written on the loop,
    # then pump body chunks. A bounded queue keeps producer/consumer in step so
    # memory stays O(chunk_size), not O(tile).
    queue: "asyncio.Queue[bytes | None | BaseException]" = asyncio.Queue(maxsize=4)
    result_holder: dict[str, ProxyResult] = {}
    open_error: dict[str, BaseException] = {}
    headers_ready = threading.Event()

    def _pump() -> None:
        try:
            resp = requests.get(
                upstream_url,
                headers=out_headers,
                stream=True,
                timeout=timeout_s,
            )
        except BaseException as exc:  # noqa: BLE001 — surfaced to caller as 502
            open_error["err"] = exc
            headers_ready.set()
            return
        try:
            result_holder["result"] = ProxyResult(
                status=resp.status_code,
                headers=_filter_response_headers(resp.headers.items()),
            )
            headers_ready.set()
            for chunk in resp.iter_content(chunk_size=chunk_size):
                if not chunk:
                    continue
                # Block the worker thread until the loop has drained enough —
                # bounded queue => bounded memory.
                fut = asyncio.run_coroutine_threadsafe(queue.put(chunk), loop)
                fut.result()
            asyncio.run_coroutine_threadsafe(queue.put(None), loop).result()
        except BaseException as exc:  # noqa: BLE001
            asyncio.run_coroutine_threadsafe(queue.put(exc), loop).result()
        finally:
            resp.close()

    pump_thread = threading.Thread(
        target=_pump, name="qgis-proxy-pump", daemon=True
    )
    pump_thread.start()

    # Wait (off the loop) for headers to be ready.
    await loop.run_in_executor(None, headers_ready.wait)
    if "err" in open_error:
        # Could not reach the upstream at all — caller maps to 502.
        raise open_error["err"]

    result = result_holder["result"]
    await write_status_and_headers(result)

    # Pump body chunks to the writer.
    while True:
        item = await queue.get()
        if item is None:
            break
        if isinstance(item, BaseException):
            # Mid-stream upstream error after headers were already sent. The
            # status line is out; we can only stop writing. Log honestly.
            logger.warning(
                "qgis-proxy: upstream stream error mid-body url=%s err=%s",
                upstream_url,
                item,
            )
            break
        await write_chunk(item)

    logger.info(
        "qgis-proxy: relayed status=%d bytes_streamed=chunked url=%s authed=%s",
        result.status,
        upstream_url,
        bool(token),
    )
    return result
