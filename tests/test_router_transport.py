"""Offline unit tests for the router remote-FILE transport (ADR-0044).

A stdlib threading HTTP server (no new deps) serves real HTTP Range requests over
an in-memory COG and forces error statuses. Coverage:
  - windowed read correctness through the transport opener (pixel-identical);
  - preflight HEAD size + typed early 404/403;
  - block coalescing (adjacent merge = one GET) + PARALLEL fetch of non-adjacent
    runs (request-count + parallel-batch assertions);
  - forced 404 -> TransportNotFound, 403 -> TransportAuthError, 429 -> retried
    then TransportUpstreamError, Retry-After honored;
  - mid-read disconnect -> typed error via the C-frame recorded-error bridge;
  - block-completeness (truncation) assertion.
"""

from __future__ import annotations

import io
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import pytest
import rasterio
import rasterio.transform as rtransform

from trid3nt_server.data.fetchers._router import transport
from trid3nt_server.data.fetchers._router.transport import (
    CoalescedRangeFile,
    TransportAuthError,
    TransportError,
    TransportNotFound,
    TransportTruncatedError,
    TransportUpstreamError,
    client as transport_client,
)


# --------------------------------------------------------------------------- #
# Fixtures: an in-memory COG + a controllable range-serving HTTP server.
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# Correctness + preflight
# --------------------------------------------------------------------------- #


def test_windowed_read_pixel_identical(range_server):
    url = range_server.url
    from rasterio.windows import Window

    with rasterio.open(io.BytesIO(range_server.payload)) as ref:
        want = ref.read(1, window=Window(10, 20, 64, 48))
    with transport.open_windowed_cog(url) as src:
        got = src.read(1, window=Window(10, 20, 64, 48))
    assert got.shape == want.shape
    assert np.array_equal(np.nan_to_num(got), np.nan_to_num(want))


def test_preflight_sizes_via_head(range_server):
    c = transport.get_client()
    size = transport.preflight(range_server.url, c)
    assert size == len(range_server.payload)
    assert range_server.head_count >= 1


def test_preflight_404_typed_not_found(range_server):
    range_server.force_status = 404
    range_server.force_body = b"<Error><Code>NoSuchKey</Code></Error>"
    c = transport.get_client()
    with pytest.raises(TransportNotFound) as ei:
        transport.preflight(range_server.url, c)
    assert ei.value.status == 404
    assert "NoSuchKey" in (ei.value.body or "")
    assert ei.value.retryable is False


def test_preflight_403_typed_auth(range_server):
    range_server.force_status = 403
    range_server.force_body = b"<Error><Code>AccessDenied</Code></Error>"
    c = transport.get_client()
    with pytest.raises(TransportAuthError) as ei:
        transport.preflight(range_server.url, c)
    assert ei.value.status == 403
    assert "AccessDenied" in (ei.value.body or "")
    assert ei.value.retryable is False


# --------------------------------------------------------------------------- #
# Coalescing + parallel fetch (request-count assertions)
# --------------------------------------------------------------------------- #


def test_adjacent_blocks_merge_single_get(range_server):
    c = transport.get_client()
    size = transport.preflight(range_server.url, c)
    f = CoalescedRangeFile(range_server.url, c, size, block=64 * 1024)
    f.seek(0)
    f.read(200 * 1024)  # spans 4 adjacent 64 KiB blocks -> one merged GET
    assert f.requests_made == 1
    assert f.parallel_batches == 0


def test_nonadjacent_runs_fetch_in_parallel(range_server):
    c = transport.get_client()
    size = transport.preflight(range_server.url, c)
    block = 64 * 1024
    f = CoalescedRangeFile(range_server.url, c, size, block=block)
    # Pre-populate block 1 so blocks 0 and 2 are non-adjacent missing runs.
    f.blocks[1] = b"\x00" * block
    before = range_server.get_count
    f.seek(0)
    f.read(3 * block)  # touches blocks 0,1,2 -> two runs fetched in parallel
    assert f.parallel_batches == 1
    assert f.requests_made == 2
    assert range_server.get_count - before == 2


# --------------------------------------------------------------------------- #
# Forced errors + retry authority
# --------------------------------------------------------------------------- #


def test_range_get_404_typed(range_server):
    range_server.force_status = 404
    range_server.force_body = b"<Error><Code>NoSuchKey</Code></Error>"
    c = transport.get_client()
    with pytest.raises(TransportNotFound):
        transport.range_get(c, range_server.url, 0, 10)


def test_range_get_403_typed(range_server):
    range_server.force_status = 403
    range_server.force_body = b"<Error><Code>AccessDenied</Code></Error>"
    c = transport.get_client()
    with pytest.raises(TransportAuthError):
        transport.range_get(c, range_server.url, 0, 10)


def test_429_retried_then_succeeds(range_server):
    range_server.fail_first_n = 2  # two 429s, then serve
    c = transport.get_client()
    data = transport.range_get(c, range_server.url, 0, 1023)
    assert len(data) == 1024
    assert range_server.get_count >= 3  # 2 failed + 1 success


def test_429_exhausts_to_typed_upstream(range_server):
    range_server.fail_first_n = 999  # always 429
    c = transport.get_client()
    with pytest.raises(TransportUpstreamError) as ei:
        transport.range_get(c, range_server.url, 0, 1023)
    assert ei.value.status == 429
    assert ei.value.retryable is True


def test_retry_after_header_honored(range_server, monkeypatch):
    range_server.fail_first_n = 1
    range_server.retry_after = "2"
    seen: list[float] = []
    monkeypatch.setattr(transport_client.time, "sleep", lambda d: seen.append(d))
    c = transport.get_client()
    transport.range_get(c, range_server.url, 0, 511)
    assert seen and seen[0] == pytest.approx(2.0, abs=0.01)


# --------------------------------------------------------------------------- #
# Bridge + truncation
# --------------------------------------------------------------------------- #


def test_mid_read_disconnect_bridges_typed_error(range_server):
    # A short-body read makes the completeness assertion fire INSIDE the GDAL C
    # read frame; the opener bridge must re-raise the typed transport error rather
    # than let the opaque RasterioIOError escape.
    range_server.short_by = 16
    with pytest.raises(TransportError) as ei:
        with transport.open_windowed_cog(range_server.url) as src:
            src.read(1)
    assert isinstance(ei.value, TransportTruncatedError)
    assert ei.value.retryable is True


def test_block_completeness_assertion(range_server):
    # The completeness gate lives in the fetch primitive (raises); the GDAL-facing
    # readinto records rather than raises (unguarded-callback safety), so assert on
    # the primitive AND on the recorded error a short read leaves behind.
    range_server.short_by = 8
    c = transport.get_client()
    size = transport.preflight(range_server.url, c)
    f = CoalescedRangeFile(range_server.url, c, size, block=64 * 1024)
    with pytest.raises(TransportTruncatedError):
        f._fetch_span(0, 64 * 1024 - 1)
    g = CoalescedRangeFile(range_server.url, c, size, block=64 * 1024)
    assert g.readinto(bytearray(64 * 1024)) == 0
    assert isinstance(g._error, TransportTruncatedError)


# --------------------------------------------------------------------------- #
# Migration edge matrix: raster_cog.direct_window through the transport maps to
# the router A.6 frame (this is the 404->EMPTY split the /vsicurl/ path lost).
# --------------------------------------------------------------------------- #

from trid3nt_contracts.source_spec import SourceSpec  # noqa: E402
from trid3nt_server.data.fetchers._router.errors import (  # noqa: E402
    RouterEmptyError,
    RouterUpstreamError,
)
from trid3nt_server.data.fetchers._router.executors import raster_cog  # noqa: E402


def _direct_window_spec(url: str) -> SourceSpec:
    return SourceSpec.model_validate({
        "name": "fetch_dw_test",
        "source_class": "dw_test",
        "shape": "raster-cog",
        "endpoints": {"data": {"url": url}},
        "params": {"bbox": {"type": "bbox", "required": True}},
        "ingest": {"access": "direct_window"},
        "normalize": {"crs": "EPSG:4326", "units": "Meters"},
        "output": {"layer_type": "raster", "ext": "tif", "style_preset": "elevation"},
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
    range_server.short_by = 16
    spec = _direct_window_spec(range_server.url)
    with pytest.raises(RouterUpstreamError) as ei:
        raster_cog.fetch_source_array(spec, {"bbox": [-104.9, 40.1, -104.8, 40.2]})
    assert ei.value.retryable is True


# --------------------------------------------------------------------------- #
# STAC-tile seam migration (scope 2): raster_cog._read_tile_window now reads a
# remote https tile href through the transport instead of GDAL /vsicurl/. The
# reproject body is unchanged, so a remote read must be BYTE-IDENTICAL to the
# local read of the same COG bytes, and a transport error maps to the typed
# router upstream frame.
# --------------------------------------------------------------------------- #

_STAC_TILE_BBOX = (-104.9, 40.1, -104.8, 40.2)
_STAC_TILE_WH = 40


def test_stac_tile_read_transport_byte_identical_to_local(range_server, tmp_path):
    spec = _direct_window_spec(range_server.url)
    # Local read of the SAME COG bytes -> the /vsicurl/-free reference path.
    local = tmp_path / "tile.tif"
    local.write_bytes(range_server.payload)
    ref_arr, ref_cmap = raster_cog._read_tile_window(
        spec, str(local), _STAC_TILE_BBOX, _STAC_TILE_WH, _STAC_TILE_WH, 0)
    # Remote read of the identical bytes through the httpx transport.
    got_arr, got_cmap = raster_cog._read_tile_window(
        spec, range_server.url, _STAC_TILE_BBOX, _STAC_TILE_WH, _STAC_TILE_WH, 0)
    assert got_arr.dtype == np.uint8
    assert got_arr.shape == ref_arr.shape
    assert np.array_equal(got_arr, ref_arr)
    assert got_cmap == ref_cmap
    assert range_server.get_count >= 1  # served over the wire, not /vsicurl/


def test_stac_tile_transport_404_maps_to_router_upstream(range_server):
    range_server.force_status = 404
    range_server.force_body = b"<Error><Code>NoSuchKey</Code></Error>"
    spec = _direct_window_spec(range_server.url)
    with pytest.raises(RouterUpstreamError) as ei:
        raster_cog._read_tile_window(
            spec, range_server.url, _STAC_TILE_BBOX, _STAC_TILE_WH, _STAC_TILE_WH, 0)
    assert ei.value.error_code == "DW_TEST_UPSTREAM_ERROR"
