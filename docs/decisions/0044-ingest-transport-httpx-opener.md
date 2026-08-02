# 0044 - ingest transport: httpx coalescing opener for remote-FILE reads

Context: the router's raster `direct_window` sub-mode read remote COGs through GDAL
`/vsicurl/`. GDAL's C-API boundary only ever exposes `CPLGetLastErrorMsg` TEXT --
no structured HTTP status, no verbatim body, no `Retry-After` -- and it is blind to
HTTP-200-wrapped error bodies (proven). That strips the router's ability to
classify retryability, which the hard rule forbids (upstream errors must surface
VERBATIM + typed). The head-to-head bench on a real separate-client link
(`docs/specs/ingest-transport-decision.md`, USGS_13_n40w105.tif 375MB, home -> S3
us-west-2) ranked 5 candidates: only an httpx opener meets the verbatim+typed hard
rule, and it is performance-competitive (tied at 1024/4096px, 2x slower only at the
trivial 32px header read). The one real edge for vsicurl -- fewer requests via
exact ranges + range-merge on high-latency links -- is fixable inside our own
opener (parallel range fetch); vsicurl's error opacity is not fixable at all.
Decision doc is NATE-signed.

Decision: adopt httpx as the unified router remote-FILE transport, in one module
`_router/transport/` that owns every socket and every error for remote reads.
- Pooled process-wide `httpx.Client` (connection reuse across reads + parallel
  range frames; amortizes the ~0.30s cold-TLS the bench measured).
- `CoalescedRangeFile` + rasterio `opener=`: 1 MiB block cache, adjacent-block
  merge into one range GET, and PARALLEL range fetches (bounded 8) for
  non-adjacent missing runs in a single read -- the bench's named work item that
  closes vsicurl's request-count edge. Every fetched run is length-asserted against
  its requested span, so a truncated block is a typed error, never silent
  corruption. GDAL's native ReadMultiRange hook is deliberately NOT wired: at
  rasterio 1.5 / this GDAL it HANGS through a Python opener (the same "native edge
  vanishes through a rasterio opener" limitation the decision doc found for
  obstore), so the coalescing sequential path -- which measures 4 requests / ~5 MB
  on a real 256px 3DEP window, matching vsicurl -- is the transport.
- Pre-flight HEAD before GDAL is ever invoked -> typed EARLY error with status INT
  + verbatim body; because S3 HEAD carries no body, a non-2xx HEAD triggers a tiny
  range GET that recovers the verbatim `<Code>NoSuchKey</Code>` /
  `<Code>AccessDenied</Code>` XML to distinguish 404 from 403.
- Exception bridge: a transport error hit inside GDAL's C read frame (which
  swallows Python exceptions) is RECORDED on the file object; the opener wrapper
  inspects the recorded error when rasterio raises `RasterioIOError` and re-raises
  the typed original.
- ONE retry authority (the transport): backoff + `Retry-After` honored on
  429/5xx/timeout at block granularity, per the upstream-provider norm (log
  verbatim, retry, honest surface on exhaustion). GDAL-side retries stay off
  everywhere -- reads never touch `/vsicurl/`.
- Typed mapping into the router's A.6 classes: 404/NoSuchKey -> `router_empty_error`
  (EMPTY, non-retryable) -- this SUBSUMES the `direct_window` defect where a missing
  object read as UPSTREAM_ERROR because GDAL discarded the status; 403/AccessDenied
  -> upstream error stamped non-retryable (auth class); 429/5xx/timeout ->
  retryable upstream.
- `/vsicurl/` remains ONLY as the documented decision-doc fallback (tripwire: a raw
  high-latency host where request count is the binding cost and the parallel-range
  lever cannot land). No dead vsicurl code path is left in the executor tree; the
  STAC tile sub-mode's own `/vsicurl/` is a separate seam, out of this scope.

Consequence: `raster_cog._direct_window_to_array` opens via
`transport.open_windowed_cog(url)` instead of `rasterio.open('/vsicurl/'+url)`;
one executor, one seam changed, the published-artifact/client (link B) edge is
untouched. Remote-FILE error fidelity now matches the hand-written twins (404->EMPTY
split restored) and the router classifies retryability from real HTTP status. The
transport is offline-unit-tested against a stdlib range-serving HTTP server
(windowed correctness, coalesce/parallel request counts, forced 404/403/429,
Retry-After, truncation via the C-frame bridge) with zero new dependencies.
