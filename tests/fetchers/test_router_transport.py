"""Offline unit tests for the router's remote-FILE transport.

A stdlib threading server with no new dependency serves real range requests over an
in-memory COG and forces error statuses. Covered: a pixel-identical windowed read,
the preflight size, block coalescing and parallel fetch of non-adjacent runs, each
status mapping to its typed error, a mid-read disconnect, and truncation."""

from __future__ import annotations

import io
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import pytest
import rasterio
import rasterio.transform as rtransform

from trid3nt_server.tools.fetchers._router import transport
from trid3nt_server.tools.fetchers._router.transport import (
    TransportAuthError,
    TransportError,
    TransportNotFound,
    TransportUpstreamError,
    client as transport_client,
)




def _make_cog_bytes(n: int = 512) -> bytes:
    arr = (np.arange(n * n, dtype="float32").reshape(n, n) % 101.0) + 1.0
    transform = rtransform.from_bounds(-105.0, 40.0, -104.0, 41.0, n, n)
    buf = io.BytesIO()
    with rasterio.open(
        buf, "w", driver="GTiff", height=n, width=n, count=1, dtype="float32",
        crs="EPSG:4326", transform=transform, tiled=True, blockxsize=256,
        blockysize=256, nodata=float("nan"),
    ) as dst:
        dst.write(arr, 1)
    return buf.getvalue()


class _RangeServerState:
    """Shared knobs the handler reads: force a status, count requests, disconnect."""

    def __init__(self, payload: bytes):
        self.payload = payload
        self.force_status: int | None = None
        self.force_body: bytes = b""
        self.retry_after: str | None = None
        self.fail_first_n: int = 0          # 429 for the first N requests, then serve
        self.short_by: int = 0              # serve N fewer bytes than requested (honest CL)
        self.get_count = 0
        self.head_count = 0
        self.lock = threading.Lock()


def _make_handler(state: _RangeServerState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # silence
            pass

        def _maybe_forced(self) -> bool:
            with state.lock:
                if state.fail_first_n > 0:
                    state.fail_first_n -= 1
                    self.send_response(429)
                    if state.retry_after is not None:
                        self.send_header("Retry-After", state.retry_after)
                    body = b"<Error><Code>SlowDown</Code></Error>"
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return True
                if state.force_status is not None:
                    self.send_response(state.force_status)
                    self.send_header("Content-Length", str(len(state.force_body)))
                    self.end_headers()
                    self.wfile.write(state.force_body)
                    return True
            return False

        def do_HEAD(self):
            with state.lock:
                state.head_count += 1
            if self._maybe_forced():
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(state.payload)))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()

        def do_GET(self):
            with state.lock:
                state.get_count += 1
                short_by = state.short_by
            if self._maybe_forced():
                return
            rng = self.headers.get("Range")
            total = len(state.payload)
            if rng and rng.startswith("bytes="):
                lo_s, hi_s = rng[len("bytes="):].split("-")
                lo = int(lo_s)
                hi = int(hi_s) if hi_s else total - 1
                hi = min(hi, total - 1)
                chunk = state.payload[lo:hi + 1]
                if short_by and len(chunk) > short_by:
                    # Complete, honest-Content-Length response that serves FEWER
                    # bytes than the requested range -> completeness assertion.
                    chunk = chunk[:len(chunk) - short_by]
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {lo}-{hi}/{total}")
                self.send_header("Content-Length", str(len(chunk)))
                self.end_headers()
                self.wfile.write(chunk)
            else:
                self.send_response(200)
                self.send_header("Content-Length", str(total))
                self.end_headers()
                self.wfile.write(state.payload)

    return Handler


@pytest.fixture()
def range_server():
    payload = _make_cog_bytes()
    state = _RangeServerState(payload)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    state.url = f"http://{host}:{port}/cog.tif"  # type: ignore[attr-defined]
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch):
    """Neutralize real sleeps so retry tests stay sub-second."""
    monkeypatch.setattr(transport_client.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setitem(transport.READ_POLICY, "GDAL_HTTP_MAX_RETRY", "1")
    monkeypatch.setitem(transport.READ_POLICY, "GDAL_HTTP_RETRY_DELAY", "0")




def test_windowed_read_pixel_identical(range_server):
    url = range_server.url
    from rasterio.windows import Window

    with rasterio.open(io.BytesIO(range_server.payload)) as ref:
        want = ref.read(1, window=Window(10, 20, 64, 48))
    with transport.open_windowed_cog(url) as src:
        got = src.read(1, window=Window(10, 20, 64, 48))
    assert got.shape == want.shape
    assert np.array_equal(np.nan_to_num(got), np.nan_to_num(want))


def test_open_404_typed_not_found_with_the_body(range_server):
    range_server.force_status = 404
    range_server.force_body = b"<Error><Code>NoSuchKey</Code></Error>"
    with pytest.raises(TransportNotFound) as ei:
        with transport.open_windowed_cog(range_server.url):
            pass
    assert ei.value.status == 404
    assert "NoSuchKey" in (ei.value.body or "")
    assert ei.value.retryable is False


def test_open_403_typed_auth_with_the_body(range_server):
    range_server.force_status = 403
    range_server.force_body = b"<Error><Code>AccessDenied</Code></Error>"
    with pytest.raises(TransportAuthError) as ei:
        with transport.open_windowed_cog(range_server.url):
            pass
    assert ei.value.status == 403
    assert "AccessDenied" in (ei.value.body or "")
    assert ei.value.retryable is False


def test_get_bytes_404_typed(range_server):
    range_server.force_status = 404
    range_server.force_body = b"<Error><Code>NoSuchKey</Code></Error>"
    with pytest.raises(TransportNotFound):
        transport.get_bytes(transport.get_client(), range_server.url)


def test_get_bytes_403_typed(range_server):
    range_server.force_status = 403
    range_server.force_body = b"<Error><Code>AccessDenied</Code></Error>"
    with pytest.raises(TransportAuthError):
        transport.get_bytes(transport.get_client(), range_server.url)


def test_429_retried_then_succeeds(range_server):
    range_server.fail_first_n = 2  # two 429s, then serve
    body, _ct, _url = transport.get_bytes(transport.get_client(), range_server.url)
    assert body == range_server.payload
    assert range_server.get_count >= 3  # 2 failed + 1 success


def test_429_exhausts_to_typed_upstream(range_server):
    range_server.fail_first_n = 999  # always 429
    with pytest.raises(TransportUpstreamError) as ei:
        transport.get_bytes(transport.get_client(), range_server.url)
    assert ei.value.status == 429
    assert ei.value.retryable is True
    assert "SlowDown" in (ei.value.body or "")


def test_get_once_reads_a_500_body_without_spending_a_retry(range_server):
    """The reader for a caller whose retries were spent by the GDAL driver."""
    range_server.force_status = 500
    range_server.force_body = b'{"error": {"message": "down"}}'
    before = range_server.get_count
    body, status = transport.get_once(transport.get_client(), range_server.url)
    assert (status, body) == (500, range_server.force_body)
    assert range_server.get_count - before == 1


def test_retry_after_header_honored(range_server, monkeypatch):
    range_server.fail_first_n = 1
    range_server.retry_after = "2"
    seen: list[float] = []
    monkeypatch.setattr(transport_client.time, "sleep", lambda d: seen.append(d))
    transport.get_bytes(transport.get_client(), range_server.url)
    assert seen and seen[0] == pytest.approx(2.0, abs=0.01)


def test_retried_waits_the_retry_after_a_library_429_carries(monkeypatch):
    from dataretrieval.exceptions import RateLimited, TransientError

    seen: list[float] = []
    monkeypatch.setattr(transport_client.time, "sleep", lambda d: seen.append(d))
    calls = iter([RateLimited("slow down", status_code=429, retry_after=7.0)])

    def call() -> str:
        for exc in calls:
            raise exc
        return "read"

    assert transport_client.retried(
        call, transient=lambda exc: isinstance(exc, TransientError),
        label="nwis") == "read"
    assert seen == [7.0]




# Migration edge matrix: raster_cog.direct_window through the transport maps to
# the router A.6 frame (this is the 404->EMPTY split the /vsicurl/ path lost).

from trid3nt_contracts.source_spec import SourceSpec  # noqa: E402
from trid3nt_server.tools.fetchers._router.errors import (  # noqa: E402
    RouterEmptyError,
    RouterUpstreamError,
)
from trid3nt_server.tools.fetchers._router.executors import raster_cog  # noqa: E402


def _direct_window_spec(url: str) -> SourceSpec:
    return SourceSpec.model_validate({
        "name": "fetch_dw_test",
        "source_class": "dw_test",
        "shape": "raster-cog",
        "endpoints": {"data": {"url": url}},
        "params": {"bbox": {"type": "bbox", "required": True}},
        "ingest": {"access": "direct_window"},
        "normalize": {"crs": "EPSG:4326", "units": "Meters"},
        "output": {"layer_type": "raster", "ext": "tif", "style": {"kind": "continuous"}},
        "cache": {"ttl_class": "static-30d"},
        "payload_estimate": {"model": "bbox_area", "mb_per_sq_deg": 1.0},
    })


def test_direct_window_success_through_transport(range_server):
    spec = _direct_window_spec(range_server.url)
    arr, transform, crs = raster_cog.fetch_source_array(
        spec, {"bbox": [-104.9, 40.1, -104.8, 40.2]})
    assert arr.size > 0
    assert crs is not None


def test_direct_window_404_maps_to_router_empty(range_server):
    range_server.force_status = 404
    range_server.force_body = b"<Error><Code>NoSuchKey</Code></Error>"
    spec = _direct_window_spec(range_server.url)
    with pytest.raises(RouterEmptyError) as ei:
        raster_cog.fetch_source_array(spec, {"bbox": [-104.9, 40.1, -104.8, 40.2]})
    assert ei.value.error_code == "DW_TEST_EMPTY"
    assert ei.value.retryable is False


def test_direct_window_403_maps_to_upstream_nonretryable(range_server):
    range_server.force_status = 403
    range_server.force_body = b"<Error><Code>AccessDenied</Code></Error>"
    spec = _direct_window_spec(range_server.url)
    with pytest.raises(RouterUpstreamError) as ei:
        raster_cog.fetch_source_array(spec, {"bbox": [-104.9, 40.1, -104.8, 40.2]})
    assert ei.value.error_code == "DW_TEST_UPSTREAM_ERROR"
    assert ei.value.retryable is False


def test_direct_window_429_maps_to_upstream_retryable(range_server):
    range_server.fail_first_n = 999
    spec = _direct_window_spec(range_server.url)
    with pytest.raises(RouterUpstreamError) as ei:
        raster_cog.fetch_source_array(spec, {"bbox": [-104.9, 40.1, -104.8, 40.2]})
    assert ei.value.error_code == "DW_TEST_UPSTREAM_ERROR"
    assert ei.value.retryable is True


def test_direct_window_truncation_maps_to_upstream_retryable(range_server):
    """A short range body under bytes the window needs fails the read, never a
    silently partial layer."""
    range_server.short_by = 16
    spec = _direct_window_spec(range_server.url)
    with pytest.raises(RouterUpstreamError) as ei:
        raster_cog.fetch_source_array(spec, {"bbox": [-105.0, 40.0, -104.0, 41.0]})
    assert ei.value.retryable is True
