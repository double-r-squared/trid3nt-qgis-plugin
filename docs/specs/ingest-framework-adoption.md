# Ingest-framework adoption - can an external declarative-ingest lib absorb our router?

Status: RESEARCH FINDING for NATE. Read-only lanes, live in-process probes, no server
suite. Question: can an EXISTING declarative-ingest framework (Airbyte CDK / dlt / Intake)
be adopted as a LIBRARY to absorb our router's request/paging/auth/error layer - the
`dataretrieval`-delegate precedent at framework scale - so the ~60 remaining coded fetchers
become manifests + our geo tail?

Authority context: `docs/specs/ingest-transport-decision.md` (API transport is SHIPPED on
`httpx` - GDAL cannot see HTTP-200-wrapped error bodies, proven); `docs/specs/router-pilot-contract.md`
(shared A.6 error frame); `docs/decisions/0036-fetcher-fold-router-core.md` (router core =
single seam). 27 sources already folded through `_router/` at time of writing.

## Contract that MUST survive any adoption (non-negotiable)

Typed errors: **status as a structured int** + **verbatim response body** + **Retry-After
honored w/ backoff** + **200-envelope detection** (HTTP-200 bodies that wrap `status:error`).
Never-silent. Shape classification (valid/empty/error-envelope/garbage). Our geo tail
(FGB/COG conversion, CRS/units stamps, payload/granularity gates, cache, publish) wraps the
result UNCHANGED. Transport is httpx (settled, client-facing correctness invariant).

## Bottom line

**DO NOT REBASE. Our `source_spec` already IS the right-sized manifest for our domain.**
The internal question ("can a declarative layer absorb these fetchers") is empirically
answered YES by our OWN router: the folded `usgs_earthquakes` twin dropped 935->257 LOC
(72.5% absorbed), and the mapping lane measures ~70-75% (~35-38k of 50,835 LOC across 69
un-folded packages) absorbable by the router we already own. The live NATE question ("adopt
an external framework INSTEAD") is a net-negative move: none of the three carry our
first-class geo primitives (bbox/quantize/CRS/FGB/COG output-typing/payload-mb gate/
style_preset), all three are `requests`-transport (impedance mismatch vs our settled httpx
error-extraction), and adopting any means re-deriving those geo extensions inside someone
else's ecosystem while adding weight and schema-churn risk. Steal two schema ideas; adopt
no runtime.

## Per-framework verdict

### Airbyte CDK 7.23.8 (MIT) -- STEAL-DESIGN (error schema only), do NOT carry runtime

- Embeddable as in-process library (YES): `ConcurrentDeclarativeSource` + hand-built
  catalog, ~15 lines boilerplate, no platform/protocol server. `ManifestDeclarativeSource`
  is now BROKEN/legacy in v7 - a concrete stability wart.
- License OK (airbyte-cdk itself is MIT, distinct from the ELv2 platform).
- Error model STRONG and the most worth stealing: `DefaultErrorHandler` + `HttpResponseFilter`
  is declarative and expressive - 404->typed fail w/ custom failure_type, verbatim body+headers
  retained, 429 RATE_LIMITED with `WaitTimeFromHeader` backoff (Retry-After scaled + honored),
  200-envelope caught by a body predicate `{{response['status']=='error'}}`. Live probes
  passed (USGS FDSN 147 recs 0.85s; GBIF offset-paged 80 recs/4 pages 2.26s).
- Deciding arithmetic FAILS: 313MB venv, 83 transitive deps (grpcio, google-cloud-secret-manager,
  pandas, numpy, nltk, protobuf - all dead weight for a fetcher lib), 0.94s cold import.
  Transport is requests+requests_cache NOT httpx -> we would rewire our typed-error extraction
  from `httpx.Response` to `requests.Response`, forking the settled transport. Status code is
  NOT a first-class int on the trace error (embedded in a formatted string) -> we'd parse a
  string to keep our int-contract. Schema churns (DSL renames auto-migrated but the schema
  moves under you); concurrent source deadlocks on early break.
- WHAT TO STEAL: the `HttpResponseFilter` predicate + `failure_type` taxonomy + backoff-strategy
  set (`WaitTimeFromHeader`/`WaitUntilTimeFromHeader`/Exponential/Constant) as vocabulary for
  our spec's error stanza. Our A.6 frame already does the substance; their schema names it well.

### dlt 1.29.1 (Apache-2.0) -- STEAL-DESIGN; the ONLY viable ADOPT-AS-MODE candidate IF a future delegate need arises

- Cleanest embed of the three: `rest_api_source()` -> bare for-loop over plain dicts, zero
  pipeline/destination touched. Lightest: 79MB/40 deps bare (Apache-2.0) - under 1/4 of Airbyte.
- Error model satisfies the contract via `ResourceExtractionError.__cause__` -> `requests.HTTPError`
  with verbatim `status_code`+`text`; default RESTClient auto-retries 429/5xx w/ Retry-After
  honored (`respect_retry_after_header=True`); 200-envelope handled by a `response_actions`
  callable that inspects `response.json()` and raises a bespoke type. All 5 live probes passed
  (FDSN single-page, GBIF 45 recs/3 pages, forced 404/429, custom-200 raise).
- Paginator + auth taxonomy matches our router modes (Offset/PageNumber/Cursor/Link/Range/
  SinglePage; ApiKey/Bearer/Basic/OAuth2/JWT).
- Deciding arithmetic STILL NET-NEGATIVE today: transport is requests not httpx (same fork
  cost); auto-paginator/auto-selector silently WARNs and can guess wrong, so we'd pin
  paginator+data_selector per source ANYWAY (same discipline as our source.yaml today = zero
  LOC saved on the discipline that matters); no geo primitives. The LOC it would absorb is
  already absorbed by our own ~6000 LOC router/executor/transform core, amortized across 27
  folds. Adopting it swaps our code for their runtime + a transport fork and buys nothing our
  router lacks.
- WHAT TO STEAL: confirm our paginator mode-set against theirs (parity check, we already match);
  the `response_actions` callable pattern as the reference shape for our 200-envelope hook.
- ESCAPE HATCH: if a future source ever needs a paging/auth shape our router genuinely can't
  express, dlt (lightest, Apache-2.0, cleanest bare-generator embed) is the one to reach for as
  a `dataretrieval`-style delegate mode - not Airbyte. Not warranted by any current source.

### Intake v2 (BSD-3) -- SKIP

- Not embeddable as a request layer. v2 "Readers" are thin declarative wrappers mapping a
  catalog entry to an existing callable (e.g. `pandas:read_csv`) executed via fsspec byte
  access. No REST client, no query-param/paging/auth abstraction, no HTTP error model.
- Overlaps our SPECS (declarative dataset catalog, same spirit) NOT our ENGINE. REST calls
  still need hand-written `Reader._get_partition` loops -> zero net absorption of the ~5k-line
  router. Mirrors the earlier fsspec-delegate finding (403==404 collapse) since its remote
  path is fsspec underneath.
- No adoption. At most a much-later optional discovery/cataloging veneer over our fetchers.

## Signed arithmetic (why external absorption nets negative)

```
absorbable LOC at stake .................... +35,000 to +38,000  (mapping lane, 70-75%)
already absorbed by OUR router (27 folds) .. -35,000 to -38,000  (same LOC; not double-counted)
                                            -----------------------------------------------
net NEW LOC an external framework absorbs .. ~0     (our router already did the absorbing)

remaining transport/paging/error core ...... ~6,000 LOC shared router/executor/transform
  replace-with-framework cost:
    - requests->httpx transport fork ........ HIGH  (forks a SHIPPED correctness invariant)
    - status-int re-extraction (Airbyte) .... MED   (parse int back out of a string)
    - geo-primitive re-derivation ........... HIGH  (bbox/CRS/FGB/COG/payload/style not native)
    - dependency weight (Airbyte 313MB) ..... HIGH  /  dlt 79MB ... MED
    - schema-churn pin+migration-watch ...... MED   (Airbyte legacy-relocation, DSL renames)
                                            -----------------------------------------------
NET: adopting an external runtime = pure cost, ~0 LOC saved. source_spec stays authoritative.
```

## Integration architecture -- what an ADOPT-AS-MODE would have looked like (and why we don't take it)

Had the arithmetic favored adoption, the slot is clean and does NOT fork the spec paradigm:
`source_spec` gains one optional `access: dlt` (default `access: router`, our native path).
For a delegate source the spec carries a `manifest:` block (dlt `rest_api_source` config);
a thin `_router/delegates/dlt_source.py` adapter runs the source in-process, catches
`ResourceExtractionError`, re-projects `__cause__.response` into our A.6 typed frame
(status int + verbatim body + retryable class + Retry-After), and yields plain dicts into the
EXISTING geo tail (vector_fgb/raster_cog/station_timeseries executors, CRS/units stamps,
payload/granularity gates, cache, publish_layer). This is exactly the `dataretrieval`-delegate
shape: framework owns request/paging, we own the error-contract wrapper + geo tail.

We DON'T take it because the adapter must re-extract the error contract anyway (their status/
body live in an exception chain, not our frame), the transport becomes requests (forking httpx),
and the geo tail - the actual value - stays ours regardless. The delegate adapter is roughly
the size of the router hook it would replace, so there is no LOC win, only a new dependency,
a transport fork, and schema-churn exposure.

## What stays OURS regardless of any decision

The geo tail and everything downstream of the record dict: FGB/COG conversion, CRS/units
stamps, shape classification (valid/empty/error-envelope/garbage), payload-mb + granularity
gates, cache, `publish_layer` -> `s3://` + `layer_uri_emit`. The A.6 typed-error frame
(status int + verbatim body + Retry-After + retryable class) as the CONTRACT - a framework
may feed it, never define it. The httpx transport (settled, client-facing correctness). The
`source_spec` schema with first-class geo keys no external manifest carries.

## Recommendation

1. ADOPT nothing wholesale. `source_spec` + `_router` is the right-sized version of these
   manifests for a geo-fetch-to-LayerURI domain; continue folding the remaining ~60 fetchers
   through it (the answer the router already proves).
2. STEAL two schema ideas into our spec's error stanza: Airbyte's `HttpResponseFilter`
   predicate + `failure_type` taxonomy + backoff-strategy names; dlt's `response_actions`
   callable as the 200-envelope hook reference. Vocabulary only - our A.6 frame keeps the
   substance.
3. If a future source ever exceeds our paginator/auth expressiveness, reach for dlt as a
   narrow `dataretrieval`-style delegate MODE (recipe above), executed under the two-gate
   parity check on exemplar sources - never Airbyte (weight), never Intake (not a request layer).
