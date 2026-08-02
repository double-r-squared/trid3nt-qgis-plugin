# Ingest-transport decision - the router's remote-FILE reader

Status: PROPOSAL, awaiting NATE sign-off. Scope: the server-side transport that
reads remote **FILE** artifacts (COG/FGB/GeoTIFF) into the router executors. The
**API** transport is already settled on `httpx` (GDAL cannot see HTTP-200-wrapped
error bodies - proven). This doc decides only the file path, ranks 5 candidates
on a real separate-client link, and names the migration cost + tripwire.

Authority context: `docs/decisions/0036-fetcher-fold-router-core.md` (the router
core is the single seam), `docs/specs/router-pilot-contract.md` (executor set +
shared A.6 error frame). The hard rule this decision must preserve: **upstream
errors surface VERBATIM + typed (status, body, retryable class); never silent.**

## 0. Two independent links - do not conflate

Every deployment shape (co-located monolith, remote-server/local-QGIS tailnet,
pluggable-LLM offline) has the SAME two links:

- **Link A - server <-> upstream** (USGS/AWS/Copernicus). This is OUR ingest
  transport - the a/b/c/d decision below. It is **WAN in the dominant case** in
  every shape (ingest always runs server-side; it never moves to the client).
- **Link B - client <-> MinIO** (QGIS reads published artifacts). Served by the
  client's OWN bundled GDAL `/vsicurl/`, which we cannot swap. Not in scope.

Link B is isolated from this decision entirely by the **published-artifact
contract**: `publish_layer` emits a plain `s3://` COG/FGB; `layer_uri_emit` gates
only renderable-vs-not, carries zero transport specifics. So a/b/c/d is
**server-internal with zero client surface** - the QGIS edge is unaffected either
way, provided artifacts stay stock-GDAL-readable (keep internal tiling +
overviews for link-B perf). This is a SHIPPED invariant, not aspirational.

## 1. Candidates

- **(a) tuned `/vsicurl/`** - GDAL transport. In production TODAY for the raster
  `direct_window` sub-mode.
- **(b) httpx coalescing opener** - our own `httpx.Client` + 1MiB block cache.
  In production TODAY for vector_fgb + station_timeseries + transforms/join.
- **(c) obstore** - Rust object-store PyO3 binding (Development Seed).
- **(d) fsspec HTTPFileSystem / s3fs** - blockcache over aiohttp.

## 2. Head-to-head bench (real separate-client link)

Real remote COG `USGS_13_n40w105.tif` (375MB), home -> S3 us-west-2, **NOT
co-located**: warm RTT ~102ms/req, cold-TLS ~0.30s, eff BW ~1.7MB/s. Cold-per-run
(fresh subprocess = worst-case cold TLS every run). Median wall (s) / requests /
MB moved. **All 5 methods are sha256-pixel-identical to reference at every
window** - correctness is not in question.

| window | a naive vsicurl | a/b tuned vsicurl | b httpx | d fsspec | c obstore |
|--------|-----------------|-------------------|---------|----------|-----------|
| 32px   | 0.93 / 2 / 0.62 | 2.35 / 2 / 3.15   | 1.96 / 3 / 3.15 | 5.73 / 5 / 3.15 | 2.26 / 3 / 3.15 |
| 1024px | 4.70 / 4 / 5.31 | 5.21 / 4 / 6.34   | 4.47 / 9 / 9.44 | 5.38 / 11 / 9.44 | 5.27 / 9 / 9.44 |
| 4096px | 35.4 / 31 / 59.4| 34.4 / 21 / 59.4  | 34.9 / 51 / 58.7| 37.4 / 58 / 58.7 | 35.8 / 51 / 58.7 |

Regime read: **32px is header-bound** (vsicurl wins - exact byte ranges, 0.62MB
vs 3.15MB); **1024px is RTT/request-bound** (httpx fastest, all within ~1s);
**4096px is BANDWIDTH-bound** (~35s, all tied within ~3s - transport choice is
irrelevant once throughput-limited, everyone moves ~59MB).

Request count is the real high-latency differentiator: vsicurl reads exact
ranges and (tuned) MERGES consecutive ones (21 vs naive 31 GETs at 4096); the
coalescing openers c/b round to 1MiB blocks and issue **serial** GETs under the
mandatory `GDAL_NUM_THREADS=1`. c (obstore) and b (httpx) have identical request
patterns by construction - **obstore's native-Rust HTTP edge does NOT
materialize through a rasterio opener**; the Python per-read boundary dominates.

## 3. Error-fidelity (the hard rule is decided here)

Forced 404 (missing key, NoSuchKey) + 403 (requester-pays anon GET, AccessDenied),
verbatim exceptions graded typed/parseable/absent + body present:

| method | 404 / 403 surface | status | body | grade |
|--------|-------------------|--------|------|-------|
| **b httpx** | `HTTPStatusError` .response.status_code==404/403 + verbatim S3 XML `<Code>NoSuchKey/AccessDenied</Code>` | **INT** | yes | **TYPED - retryable-class trivially derived. Uniquely meets the hard rule.** |
| a/b vsicurl | `RasterioIOError('HTTP response code: 404')` | text-only, regex, GDAL-version-fragile | no | PARSEABLE - GDAL discards body; **blind to HTTP-200-wrapped errors (proven)** |
| c obstore | 403->typed `PermissionDeniedError`; 404->downgrades to builtin `FileNotFoundError`; status+body in MESSAGE TEXT only | text (naive regex hit `418.8ms` as false status) | yes | PARSEABLE + typed-class + **silent ~180s Rust retry hazard** |
| d fsspec | BOTH 404 AND 403 -> bare `FileNotFoundError(url)`, **403 indistinguishable from 404** | absent | no | **ABSENT - violates verbatim+typed outright** |

Error-fidelity ranking: **b >> c > a >> d.** Only b surfaces a structured status
INT + verbatim body + trivial retryable class. This is unfixable for vsicurl at
the GDAL C-API boundary: Python only ever sees `CPLGetLastErrorMsg` TEXT
(`CPLE_HttpResponseError` is a class, never a structured status/body/Retry-After).

## 4. Separate-client (high-latency) analysis

- **R1's "3.07x/3.27x httpx-slower" was a CO-LOCATED artifact.** On this real
  separate-client link the penalty collapses: httpx is 2.1x slower ONLY at the
  trivial 32px header read, FASTER at 1024 (0.95x), TIED at 4096 (0.99x). The
  separate-client requirement NEUTRALIZES the co-located httpx penalty.
- **Latency sensitivity is MODELED, not measured** (tc/netem unavailable in
  sandbox - no CAP_NET_ADMIN). Grounded on measured base. Added wall at +50/+100ms
  per req: 1024px b(9req) +0.45/+0.90 vs a(4) +0.20/+0.40; 4096px b(51) +2.55/+5.10
  vs tuned-a(21) +1.05/+2.10. Implication: **on higher-latency links request
  count dominates, and here vsicurl's exact-range + range-merge genuinely pulls
  ahead of the coalescing openers.** This is the one real point against b - but it
  is fixable IN our opener (sec 5c improvements); vsicurl's error opacity is not.
- fsspec (d) DEADLOCKS under GDAL default multithreading -> forced serial; its
  documented obstore-via-fsspec path (c) HANGS on COG random-access (>45s
  confirmed) - so c's "real" numbers already require a hand-written get_range
  opener = reimplementing b with a Rust dep + worse error observability.

## 5. RECOMMENDATION

**Adopt (b) the httpx coalescing opener as the unified router remote-FILE
transport.** Three legs: (1) ERROR FIDELITY - the only candidate meeting the
verbatim+typed hard rule; (2) PERFORMANCE - competitive on the separate-client
link that now matters (tied at 1024/4096, 2x slower only at the trivial 32px
read); (3) OWNERSHIP - we control it and can fix its inefficiencies, whereas
vsicurl's error opacity is unfixable at the C-API boundary.

**Runner-up: (a) tuned vsicurl** - keep documented as fallback. It is the most
byte- and request-efficient (merge 31->21 at 4096, exact ranges) and needs no new
code, but carries the fatal, unfixable error-observability gap + the proven
HTTP-200-wrapped blind spot.

**Tripwire that flips to (a):** if the ingest counterparty becomes a raw
high-latency host where request count is the binding cost AND we cannot land the
sec-5c batch/parallel-range improvement, vsicurl's fewer-GETs + range-merge wins
throughput enough to justify eating the error-fidelity loss (mitigated by a
mandatory `HEAD`/probe-GET-first shim to recover status before the opaque open).
Pin GDAL >=3.13 if so - retry on the parallel `ReadMultiRange` path only landed
in 3.13.0 (#12933/#13898); >=3.11.1 for retry-under-threading (#12426); >=3.10.2
for the silent-truncation fix (#11552). Most deployed base images pin 3.6-3.10
and remain exposed.

**REJECT (c) obstore** - Rust edge vanishes through a rasterio opener (= b), its
documented path hangs, DIY get_range just re-adds b + a Rust dep + the silent
~180s retry hazard. Revisit only its **S3Store** (not HTTPStore) if remote
hosting ever becomes S3-protocol. **REJECT (d) fsspec** - slowest, most requests,
deadlocks, 403==404 (worst error fidelity).

**Concrete b improvements this bench surfaced (do these on adopt):**
(a) DROP the size `HEAD` -> lazy size from the first ranged-GET `Content-Range`
(saves ~1 RTT/read; this is why b lags at 32px); (b) shrink the 1MiB block or
pass GDAL exact ranges (32px read moved 3.15MB vs vsicurl 0.62MB); (c) BATCH +
parallelize range fetches (obstore get_ranges-style) - the single biggest
high-latency lever, closes the request-count gap that is vsicurl's only edge;
(d) reuse a persistent `httpx.Client` across reads to amortize the 0.30s cold-TLS.

## 6. Migration cost per candidate

The file-read primitive to swap lives INSIDE the executors; the client boundary
is fixed and downstream (no client change for ANY server-side swap).

- **(b) httpx - LOW.** Vector/station/join already use it. Only change:
  `raster_cog.py` `_direct_window_to_array` swaps `rasterio.open('/vsicurl/'+url)`
  for a `rasterio.open(url, opener=<httpx opener>)` (or a direct get_range read).
  Then land the 4 sec-5 improvements. One executor, one seam.
- **(a) tuned vsicurl - ZERO code, but a version pin.** Set the GDAL env block
  (`GDAL_DISABLE_READDIR_ON_OPEN`, `MERGE_CONSECUTIVE_RANGES`, `HTTP_MULTIPLEX`,
  `HTTP_VERSION=2`, `CPL_VSIL_CURL_CHUNK/CACHE_SIZE`, `HTTP_MAX_RETRY/RETRY_DELAY/
  RETRY_CODES`) before open, and pin GDAL >=3.13. Does not close the error gap.
- **(c) obstore - MEDIUM-HIGH.** New Rust dep; a hand-written get_range opener
  (the fsspec path hangs); custom status/body regex out of the stringified source
  chain; MUST override `retry_config` (timedelta) or violate never-silent.
- **(d) fsspec - MEDIUM.** New dep; rebuild coalescing + retry from scratch; must
  force `GDAL_NUM_THREADS=1`; still cannot distinguish 404 from 403.

## 7. What stays true regardless of the choice

- **Published-artifact contract** - artifacts stay plain stock-GDAL-readable
  COG/FGB at a stable `s3://` URI; the QGIS client edge reads them with its own
  GDAL, outside our control. Keep internal tiling + overviews for link-B perf.
- **Typed-error taxonomy** - executors terminate in the shared A.6 frame:
  `RouterInputError` (retryable=False), `RouterUpstreamError` (retryable=True),
  `RouterEmptyError`, `RouterNotAvailableError`, stamped `<SOURCE>_<SUFFIX>`.
  The transport feeds this; it does not replace it.
- **Retry authority stays with the router**, not the transport. The `retryable`
  bool is set from the surfaced status class at raise time - which is exactly why
  a transport that hides the status (vsicurl text, fsspec absent, obstore silent
  Rust retry) is disqualified: it strips the router's ability to classify.

## 8. Honest contradictions between lanes (not smoothed)

- **R1 (docs) hedged; the bench resolved it.** R1 could not confirm from docs
  whether obstore's generic HTTPStore maps status codes to typed exceptions -
  flagged it as the one load-bearing open question. The live probe answered it:
  status is text-only-in-message + 404 downgrades to `FileNotFoundError` + a
  silent ~180s retry. obstore's typed-error strength is real for S3/GCS/Azure
  stores, NOT for the arbitrary-HTTP host that is our actual case.
- **R1's co-located penalty vs the separate-client bench** - R1 recommended httpx
  partly despite a measured 3x slowdown; that slowdown was a co-located artifact
  and does not transfer to the separate-client link. The recommendation is
  stronger than R1 could state.
- **R2 (vsicurl) is a decision-support lane, not an adoption path.** Its own
  conclusion: no GDAL version/config combination removes the Q2 error-observability
  ceiling. The bug fixes (#11552/#12426/#12933) close silent-truncation and
  add retry, but the hard rule remains unmeetable.
- **The bench's own pro-vsicurl finding is left standing:** on high-latency links
  request count dominates and vsicurl (exact range + merge) is genuinely more
  efficient than b's 1MiB-coalesce serial GETs. b wins on error fidelity +
  ownership + fixability - NOT on raw request efficiency today. Sec-5c is the work
  that must land to make the performance leg fully true.
- **Latency numbers in sec 4 are MODELED (tc/netem unavailable), not measured.**
  Treat the +50/+100ms-per-req deltas as directional, grounded on the measured
  102ms-RTT / 1.7MB/s base - not as a benchmarked result.
