"""Coalescing range-reading file + rasterio opener for remote COGs.

``CoalescedRangeFile`` presents a remote HTTP object as a seekable read-only file
GDAL reads through rasterio ``opener=``; GDAL itself never networks. Reads round to
1 MiB blocks with an in-memory cache; adjacent missing blocks merge into ONE range
GET; multiple NON-adjacent missing runs in a single read fetch in PARALLEL (bounded
~8) -- the work item the ingest decision named to close vsicurl's request-count
edge. Every fetched span is length-asserted against its request, so a truncated
read is a typed error rather than silent corruption.

``TransportOpener`` is a plain fsspec-shaped opener (rasterio adapts it via its
filesystem container). GDAL's native ReadMultiRange path is deliberately NOT wired:
it hangs GDAL through a Python opener at this rasterio/GDAL version (the same
"native edge vanishes through a rasterio opener" limitation the decision doc found
for obstore), so the coalescing sequential path -- which works and is request-
efficient -- is the transport.

The exception bridge: GDAL's C read callback is unguarded, so a raise out of
``readinto`` corrupts its buffer (proven abort). Instead a transport error hit
mid-read is RECORDED on the file (``recorded_error``) and ``readinto`` returns 0
(a short read); GDAL fails cleanly and the opener wrapper re-raises the recorded
typed original.
"""

from __future__ import annotations

import io
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx

from .client import range_get
from .errors import TransportError, TransportTruncatedError

__all__ = ["CoalescedRangeFile", "TransportOpener", "BLOCK", "MAX_PARALLEL"]

BLOCK = 1024 * 1024  # 1 MiB
MAX_PARALLEL = 8


class CoalescedRangeFile(io.RawIOBase):
    """Lazy 1 MiB-block range reader over the pooled httpx client."""

    def __init__(self, url: str, client: httpx.Client, size: int,
                 *, block: int = BLOCK, max_parallel: int = MAX_PARALLEL):
        self.url = url
        self.client = client
        self.size = size
        self.block = block
        self.max_parallel = max_parallel
        self.pos = 0
        self.blocks: dict[int, bytes] = {}
        self._error: TransportError | None = None
        self._lock = threading.Lock()
        # Observability for the bench + request-count unit assertions.
        self.bytes_fetched = 0
        self.requests_made = 0       # logical range GETs issued
        self.parallel_batches = 0    # reads that fanned out >1 range concurrently

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False

    def tell(self) -> int:
        return self.pos

    def seek(self, off: int, whence: int = 0) -> int:
        base = {0: 0, 1: self.pos, 2: self.size}[whence]
        self.pos = base + off
        return self.pos

    def _fetch_span(self, lo: int, hi: int) -> bytes:
        """One range GET over ``[lo, hi]`` (inclusive) with completeness assertion."""
        expected = hi - lo + 1
        data = range_get(self.client, self.url, lo, hi)
        if len(data) != expected:
            raise TransportTruncatedError(
                f"range GET returned {len(data)} bytes, expected {expected} "
                f"(bytes={lo}-{hi}) url={self.url}")
        with self._lock:
            self.bytes_fetched += len(data)
            self.requests_made += 1
        return data

    def _missing_runs(self, b0: int, b1: int) -> list[tuple[int, int]]:
        """Contiguous runs of not-yet-cached blocks in ``[b0, b1]``."""
        missing = [b for b in range(b0, b1 + 1) if b not in self.blocks]
        runs: list[tuple[int, int]] = []
        i = 0
        while i < len(missing):
            j = i
            while j + 1 < len(missing) and missing[j + 1] == missing[j] + 1:
                j += 1
            runs.append((missing[i], missing[j]))
            i = j + 1
        return runs

    def _ensure(self, b0: int, b1: int) -> None:
        runs = self._missing_runs(b0, b1)
        if not runs:
            return
        spans = [
            (lo_blk * self.block, min((hi_blk + 1) * self.block, self.size) - 1)
            for lo_blk, hi_blk in runs
        ]
        if len(spans) == 1:
            payloads = [self._fetch_span(*spans[0])]
        else:
            # Multiple non-adjacent gaps -> parallel range fetches (bounded).
            self.parallel_batches += 1
            workers = min(self.max_parallel, len(spans))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                payloads = list(pool.map(lambda s: self._fetch_span(*s), spans))
        with self._lock:
            for (lo_blk, hi_blk), data in zip(runs, payloads):
                for k in range(lo_blk, hi_blk + 1):
                    s = (k - lo_blk) * self.block
                    self.blocks[k] = data[s:s + self.block]

    def readinto(self, buf) -> int:  # type: ignore[override]
        # NEVER raise out of a GDAL C read frame: rasterio's read callback is
        # unguarded and a raised exception corrupts its buffer (proven abort).
        # Record the typed error and return 0 (short read) -- GDAL fails cleanly
        # and the opener's recorded_error bridge re-raises the typed original.
        if self._error is not None:
            return 0
        if self.pos >= self.size:
            return 0
        n = min(len(buf), self.size - self.pos)
        b0 = self.pos // self.block
        b1 = (self.pos + n - 1) // self.block
        try:
            self._ensure(b0, b1)
        except TransportError as exc:
            self._error = exc
            return 0
        out = bytearray()
        p = self.pos
        while len(out) < n:
            blk = self.blocks[p // self.block]
            off = p % self.block
            take = min(n - len(out), len(blk) - off)
            if take <= 0:
                break
            out += blk[off:off + take]
            p += take
        got = len(out)
        buf[:got] = out
        self.pos += got
        return got


class TransportOpener:
    """rasterio ``opener=`` adapter (fsspec-shaped) serving ONE remote COG.

    Exposes EXACTLY the minimal filesystem-container method set rasterio's opener
    adaptation needs for a single-file random-access read (open / isfile / isdir /
    mtime / size). Adding ``ls`` / ``exists`` makes GDAL treat the path as a
    listable directory and enumerate, which HANGS the read -- so they are omitted
    by design. Holds the produced file objects so the caller can recover a recorded
    transport error after rasterio raises (the C-frame exception bridge).
    """

    def __init__(self, url: str, client: httpx.Client, size: int):
        self.url = url
        self.client = client
        self._size = size
        self.files: list[CoalescedRangeFile] = []

    def open(self, path, mode: str = "rb", **kwargs) -> CoalescedRangeFile:
        f = CoalescedRangeFile(self.url, self.client, self._size)
        self.files.append(f)
        return f

    def isdir(self, path) -> bool:
        return False

    def isfile(self, path) -> bool:
        return True

    def mtime(self, path) -> int:
        return 0

    def size(self, path) -> int:
        return self._size

    def recorded_error(self) -> TransportError | None:
        """The first typed transport error recorded across produced files, if any."""
        for f in self.files:
            if f._error is not None:
                return f._error
        return None
