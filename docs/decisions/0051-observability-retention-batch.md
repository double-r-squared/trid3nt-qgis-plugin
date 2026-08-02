# 0051 - observability/retention batch: rotation, telemetry retention, error actionability, shape classifier

Context: NATE approved a four-item server-side observability/retention batch
(2026-07-30). `logs/agent.log` had grown unbounded (24MB + a manually-rotated
`.bak`); the tool-call telemetry JSONL sink was a single unbounded file with no
retention story; typed errors carried `error_code`/`retryable` but no signal
for WHO/WHAT can act on a failure (the model, the user, or nobody -- ours to
fix); and response-shape classification (valid/empty/error-envelope/garbage)
was duplicated ad hoc across the router's `vector_fgb` executor, the
transport's `classify_status`, and several bespoke fetchers.

Decision:
- **Log rotation** -- Python owns rotation at the logging layer
  (`trid3nt_server.main._configure_logging`, a `RotatingFileHandler` at
  `TRID3NT_AGENT_LOG_FILE` or `<repo>/logs/agent.log`, ~10MB x 3 backups,
  env-overridable via `TRID3NT_AGENT_LOG_MAX_BYTES`/`_BACKUPS`), not shell
  redirection. `scripts/start_agent.sh` no longer pipes routine stdout/stderr
  into `agent.log` (that would double-write and race the handler's own
  rotation); it redirects only to a small, freshly-truncated
  `logs/agent_boot.log` that catches a crash BEFORE Python's logging
  configures. The pre-existing 24MB `agent.log` + `agent.log.bak` are left on
  disk as user artifacts (not touched by this change).
- **Deliberate telemetry retention** -- the tool-call telemetry sink
  (`telemetry.py`) becomes session/boot-segmented:
  `TRID3NT_TELEMETRY_PATH` unset or set to a directory selects
  `<dir>/tool_calls.<boot_id>.jsonl` (default dir `/tmp/trid3nt_telemetry`);
  `main.run()` prunes segments beyond `TRID3NT_TELEMETRY_KEEP` (default 3) at
  every daemon boot (`telemetry.cleanup_telemetry_segments`). Ephemerality is
  POLICY, daemon-enforced, not platform accident. Back-compat: an explicit
  `TRID3NT_TELEMETRY_PATH` ending in `.jsonl` stays an EXACT, unsegmented
  file override (every existing test pins one this way). Readers
  (`load_tool_call_records`, `tool_catalog_http.build_telemetry_summary`)
  default to the current segment; `all_segments=True` reads every retained
  one. `tool_catalog_http._get_telemetry_path` now delegates to `telemetry`'s
  resolver instead of duplicating the env/default logic.
- **Error actionability** -- a closed `{agent, user, operator}` classifier
  (`agent.gates.actionability.classify_actionability`) sits at the
  `summarize_tool_result` chokepoint. `agent` (default, unchanged behavior):
  every typed FR-AS-11 tool exception plus the untyped transient/arg-shape
  primitives `_classify_error` already recognizes -- rich verbatim
  `function_response`. `user`: reuses the EXISTING credential-shape detector
  (`credential_registry.is_credential_shaped_error`) -- the function_response
  carries a concise narration directive (the credential provider's own
  `default_message`, or a generic pointer) instead of raw exception text.
  `operator`: a NARROW, explicit internal-bug family
  (`AssertionError`/`NotImplementedError`/`pydantic.ValidationError`) -- a
  terse `"internal error, logged"` reaches the model; full detail stays in
  the log (`logger.exception` at the dispatch site) and telemetry's
  `error_code`. An untyped, unrecognized exception with NEITHER signal (e.g.
  a bare `RuntimeError`) stays `agent` -- mirrors `_classify_error`'s own
  catch-all so no existing message/behavior regresses. `ToolCircuitBreaker.
  record_failure` exempts operator-class failures from the trip-threshold
  counter (mirrors the existing client/arg-error exemption) -- an internal
  bug must not burn the tool's retry budget. The `contracts` package exports
  the shared `ActionabilityClass`/`ACTIONABILITY_CLASSES` type; `ToolInputError`
  itself is NOT given a wire field -- its pinned 3-key shape
  (`{code, message, retryable}`) is a golden test external consumers type
  against, so actionability for that family is computed dynamically
  (always `"agent"`) rather than added to the wire contract. `RouterError`/
  `FetchError` bases carry `actionability = "agent"`; `TransportError` base
  carries `"agent"`, `TransportAuthError` overrides to `"user"`.
- **Unified shape classifier** -- one `classify_response` component
  (`agent/tools/fetchers/_router/shape_classifier.py`) implements the NATE
  shape principle (valid-shape -> data; valid-empty -> honest empty;
  recognized error envelope [ArcGIS `{"error": ...}`, WMS
  `<ServiceException>`, S3 `<Error><Code>...</Code></Error>`] -> typed error
  with the VERBATIM message; unparseable -> error-shaped with a body
  excerpt). Callers keep composing their OWN exception type/wording from a
  `ShapeVerdict` -- this module never raises and never a picks an exception
  class, so migrating onto it changes WHERE the logic lives, never WHAT a
  caller does with it. Migrated: `executors/vector_fgb._fetch_one_page`
  (the ArcGIS envelope + JSON-parse check); `transport.errors.classify_status`
  (S3 XML `<Code>` extraction, kept ADDITIVE alongside the original substring
  fallback so the migration is provably a superset, never narrower); two
  exemplar bespoke fetchers with clear ArcGIS envelope checks --
  `fetch_wdpa_protected_areas._wdpa_query_one_page` and
  `fetch_usace_dams._fetch_nid_geojson_page` (the latter also proves the
  classifier composes with a fetcher's own structured error payload --
  USACE's ESRI token-gate code extraction reads `ShapeVerdict.error_payload`
  unchanged). The remaining ArcGIS-envelope-shaped bespoke fetchers
  (`fetch_epa_frs_facilities`, `fetch_tsunami_events`, `fetch_usace_levees`,
  `fetch_usgs_earthquakes`, `fetch_wfigs_incident`, `fetch_nwi_wetlands`,
  `fetch_noaa_slr_scenarios`, `fetch_epa_ejscreen`, `fetch_lehd_jobs`,
  `fetch_airnow_air_quality`, `fetch_openaq_measurements`,
  `fetch_storm_tracks`) migrate opportunistically in a future wave -- not
  swept now.

Consequence:
- Byte-identical proof: `server/tests` FAILED set stays exactly the 9-item
  baseline (`test_fetch_resolution_gate` x4, `test_run_river_dye_scenario`
  x5) before AND after this batch; the router/transport/executor +
  `test_fetch_wdpa_protected_areas`/`test_fetch_usace_dams` suites, the
  telemetry suites, the circuit-breaker suites, and `contracts/tests/
  test_errors.py` all pass unchanged. Registry stays 190 (`test_catalog_
  surfacing.py`, `test_spatial_query.py`); zero tool name/docstring/corpus
  edits.
- A future wave that migrates the remaining bespoke fetchers onto
  `classify_response` should keep the same discipline: preserve each
  fetcher's own exception TYPE/wording, verify against its own existing
  test file, never touch a registered tool's name/docstring/metadata.
- The turn-telemetry and solve-telemetry sinks (`telemetry.py`'s OWN
  separate JSONL files) are OUT OF SCOPE for this wave's segmentation --
  only the tool-call telemetry sink (`TRID3NT_TELEMETRY_PATH`) is
  session/boot-segmented. A future wave may extend the same pattern to
  those sinks if warranted.
