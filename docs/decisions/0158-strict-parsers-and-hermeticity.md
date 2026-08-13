# 0158: Strict worker spec parsers + offline-suite hermeticity (Atlas-14)

Date: 2026-08-06
Status: landed

## Context

Two standing hygiene debts, both NATE-approved off `docs/IDEAS.md`:

- **ITEM 1 (the ADR 0148 lesson).** ADR 0148 found that a stale `geoclaw`
  image SILENTLY DROPPED unknown `build_spec` fields (`.get()`-based lenient
  parsing), so two registered knob templates (`manning_coefficients`,
  `amr_regions`) ran as no-ops for a full sprint before anyone noticed. That
  failure class is not GeoClaw-specific: every worker with a JSON/dict spec
  entry point was exposed to the same silent-drop risk, and TELEMAC's
  `_reach_config` had it BY DESIGN (its own docstring: "unknown keys are
  dropped with a warning ... so a stray manifest key never crashes the
  worker" -- a WARNING-level log line nobody reads in practice).

- **ITEM 2 (the NOAA outage lesson).** 9 offline-suite tests
  (`test_urban_flood_publish_offloop.py` x7 + `test_swmm_two_card_sim_observability.py`
  x2) exercised the LIVE NOAA Atlas-14 PFDS lookup via `model_swmm_urban_flood`
  Step 3 (`_atlas14_total_depth_mm`), because none of them supplied an explicit
  `total_rain_depth_mm` on `SWMMRunArgs`. A NOAA outage would flake all 9
  without any code being wrong. Separately, the `SWMM_PRECIP_LOOKUP_FAILED`
  honest gate (ADR 0091 gated-fallback pattern -- STOP rather than fabricate a
  baked rainfall depth) had zero dedicated test coverage.

## Decision

**ITEM 1 -- strict parsers, per worker, with an explicit allowlist:**

- `services/workers/geoclaw/setrun_builder.py::parse_build_spec` -- unknown
  top-level `build_spec` keys raise `GeoClawDeckError("GEOCLAW_SPEC_UNKNOWN_FIELDS")`.
- `services/workers/swan/deck_builder.py::parse_build_spec` -- raises
  `SwanDeckError("SWAN_SPEC_UNKNOWN_FIELDS")`.
- `services/workers/_sfincs_build/{spec.py,deck.py}` -- three strict layers:
  `validate_job_spec` (top-level job_spec), `forcing_spec_from_dict`, and
  `build_options_from_dict`. The `options` allowlist keeps `quadtree` and
  `return_period_yr` as documented pass-through keys (`deck_quadtree.py` reads
  them directly off `spec["options"]`, not through `build_options_from_dict`) --
  the CAUTION case the mission called out: a legitimate non-consuming key must
  not be rejected.
- `services/workers/_modflow_build/spec.py::validate_job_spec` -- strict at
  the TOP LEVEL only (`schema_version`/`engine`/`spec_id`/`run_args`/`options`).
  `run_args` internals stay deliberately open per the module's own docstring
  ("passed through verbatim to `build_modflow_deck`") -- and are ALREADY
  effectively strict by construction: `build_deck_kwargs_from_spec` unpacks
  `run_args` as `**kwargs` against `build_modflow_deck`'s fully-typed, no-`**kwargs`
  signature, so an unknown/typo'd optional knob already raises a loud
  `TypeError` at the call site. No silent-drop path existed there; added a
  test pinning that this is real, not accidental.
- `services/workers/elmfire/deck_builder.py::validate_deck_spec` -- unknown
  top-level deck-spec keys raise `ElmfireSpecUnknownFieldsError`. The
  `simulator_extra`/`outputs_extra`/`inputs_extra` namelist-knob surface is a
  SEPARATE, fully-typed kwarg path (`run_elmfire.py` calls `render_namelist`
  directly) and is intentionally excluded from this dict-spec's allowlist.
- `services/workers/schism/entrypoint.py` -- `manifest.json` (variant/
  ncompute/nscribe/timeout_s/run_id) strict-checked; raises
  `SchismManifestUnknownFieldsError`. SCHISM's actual deck (param.nml etc.) is
  authored server-side via typed Python calls (`agent/workflows/schism/
  deck_authoring.py`), out of `services/workers/*` scope.
- `services/workers/telemac/entrypoint.py::_reach_config` -- converted from
  "unknown keys dropped with a `LOG.warning`" to raising
  `TelemacManifestUnknownFieldsError`. Uses `dataclasses.fields(ReachConfig)`
  dynamically (not a hand-maintained list), so it can never drift from the
  dataclass it validates against.
- `services/workers/hecras/entrypoint.py::run` -- `manifest.json` (both
  shapes: the M3 gate `plan_hdf`/`geom_suffix`/`run_geompre`, and the
  engine-landing `archetype`/`breach_enabled`/`flow_scale`/`target_peak_cfs`)
  strict-checked; raises `HecrasError`.

Every check is a duplicated ~10-20 line per-worker helper (consistency over
DRY across container boundaries -- workers are self-contained images) naming
a `_PARSER_VERSION` string in the error message so a stale image is
distinguishable from a genuinely malformed caller.

**Deploy contract (ADR 0148 law, reaffirmed):** every worker whose parser
changed got its image REBUILT + a live smoke THROUGH the rebuilt image
proving (a) the happy path still solves and (b) an unknown field now errors
loudly. See Consequence for per-worker results.

**ITEM 2 -- hermeticity:**

- `test_urban_flood_publish_offloop.py` / `test_swmm_two_card_sim_observability.py`:
  both installer helpers (`_install_pyswmm_free_chain` / `_install_offbox_chain`)
  now monkeypatch `urban_flood._atlas14_total_depth_mm` to a fixed 120.0 mm
  depth (mirrors the existing pattern already used in
  `test_run_swmm_local_chain.py`), so Step 3 of the composer never reaches
  the network.
- Added `test_atlas14_lookup_failure_raises_precip_lookup_failed_gate` --
  the previously-missing dedicated coverage that `SWMM_PRECIP_LOOKUP_FAILED`
  actually fires (lookup mocked to return `None`) and that
  `build_and_stage_swmm_deck` is never reached after the miss.
- `lookup_precip_return_period.py::_ATLAS14_PFDS_URL` -- NWS HDSC retired
  `/cgi-bin/hdsc/new/` in favor of `/cgi-bin/new/`. Live-verified 2026-08-06:
  the old path still 301-redirects to the new one (so nothing was actually
  broken), but the constant now points at the final URL directly to save the
  round trip. `requests.get`'s default `allow_redirects=True` is untouched, so
  a FUTURE HDSC path change degrades to one extra hop, never a hard break.
- Swept the rest of `server/tests/` for other live-endpoint dependencies
  (grep for `requests.`/`httpx.`/`urlopen` across all test files, then read
  each hit for whether it exercises a REAL vs mocked transport). Findings in
  Consequence -- no other gap found; the fetcher architecture (one generic
  router + `read_through`, ADR 0112) means nearly every fetcher's tests mock
  at the `http_json._get_raw` / `read_through` boundary already. Atlas-14 was
  exposed only because it is a hand-written bespoke fetcher (documented
  permanent-bespoke, per the fold doctrine) sitting outside the router.

## Consequence

**Per-worker parser status (offline, all green):**

| worker | parser | new tests | image rebuilt | live smoke |
|---|---|---|---|---|
| geoclaw | `setrun_builder.parse_build_spec` | 1 | yes (`trid3nt-local/geoclaw:latest`) | pass |
| swan | `deck_builder.parse_build_spec` | 1 | yes, via a same-binary Python-layer overlay on `trid3nt-local/swan:latest` (see below) | pass |
| sfincs | `spec.validate_job_spec` + `deck.forcing_spec_from_dict` + `deck.build_options_from_dict` | 8 | NO (blocked, pre-existing, unrelated -- see below) | offline tests only |
| modflow | `_modflow_build/spec.py::validate_job_spec` + `build_modflow_deck` typed-kwarg strictness | 2 | yes (`trid3nt-local/modflow:latest`, new tag -- Cloud Run Job image, not the live local-docker dispatch, which runs `mf6` natively) | pass |
| elmfire | `deck_builder.validate_deck_spec` | 1 | yes (`trid3nt/elmfire:dev`) | pass |
| schism | `entrypoint.py` manifest.json | 2 | yes (`trid3nt-local/schism:latest`) | pass |
| telemac | `entrypoint.py::_reach_config` | 2 (1 renamed) | yes (`trid3nt-local/telemac:latest`) -- Dockerfile's own build-time smoke also updated (it asserted the OLD lenient behavior) | pass |
| hecras | `entrypoint.py::run` manifest.json | 1 | yes (`trid3nt-local/hecras:latest`) | pass |

**SWAN image note:** a from-scratch rebuild failed at the upstream SWAN
source fetch (`swanmodel.sourceforge.io`) -- the downloaded tarball's SHA-256
did not match the Dockerfile's pin, a PRE-EXISTING environmental issue
unrelated to this change (no Fortran/pin edits were made). Per the standing
"no pin changes" rule, the pin was not touched. Worked around by building a
thin overlay `FROM trid3nt-local/swan:latest` (the pre-existing image,
compiled binary intact) that only re-`COPY`s `services/workers/swan/` and
re-runs the same build-time smoke -- this is the same deploy contract (image
now bakes the strict parser) without needing a source recompile.

**SFINCS build-mode image not rebuilt.** `services/workers/_sfincs_build/`
(the `--build-spec-uri` quadtree path) has never had a successfully-built
local image in this environment: a from-scratch build fails installing
`cht_sfincs==1.0.0` -- its `grid_v2.py` imports `matplotlib`, which is not in
the Dockerfile's pinned dependency list (a pre-existing, unrelated
third-party gap; not touched, per "no pin changes"). Separately, this
build-mode path is not currently reachable from the live local-docker
dispatch (`solver.py::_sfincs_local_spec` runs the bare upstream
`deltares/sfincs-cpu:latest` image directly against a pre-built deck --
`services/workers/sfincs/entrypoint.py`'s `--build-spec-uri` branch is a
Cloud-Run/Batch-era surface). The 8 offline tests
(`services/workers/_sfincs_build/test_spec_strict_fields.py`) are therefore
the full evidence for this worker: happy path + unknown-field rejection at
all three levels (job_spec, forcing, options), including the
quadtree-passthrough non-rejection case.

**Environment note (unrelated to the parser work, logged for the next
session):** this machine's Docker Desktop rootless builder, when a `docker
build` is launched from a BACKGROUNDED (`&`) shell job after the harness's
tracked cwd had drifted, resolved the build context against a STALE sibling
checkout (`/home/nate/Documents/GRACE-2`, the pre-rename repo) instead of
`trid3nt-local` -- two rebuilds (elmfire, modflow) silently baked the WRONG
(old, pre-ADR-0158) source on the first attempt. Caught by the live smoke
(`ElmfireSpecUnknownFieldsError` / `validate_job_spec`'s strict-field import
was simply absent from the built image) rather than trusted blindly; fixed by
re-running both `docker build` invocations with fully absolute paths for
both `-f` and the context argument (which are cwd-independent). All 8 image
histories were then grepped for stray `grace2` residue to confirm a clean
bake. Lesson: never rely on a `cd &&` prefix inside a backgrounded shell job
on this host -- pass absolute paths.

**Hermeticity:** the 9 previously-live-touching tests are now fully
network-free; a NOAA outage no longer flakes the offline suite. The
`SWMM_PRECIP_LOOKUP_FAILED` gate now has direct coverage. The Atlas-14 URL
follows the current NWS HDSC path (live-verified 200 OK, matrix parses,
100-yr/24-hr Fort Myers FL = 12.1 in, matching the documented header). No
other live-endpoint gap found in the rest of `server/tests/`.

**Files changed:** see the specialist's closing report for the full list
(worker parsers + tests, two Dockerfiles' build-time smokes, the Atlas-14
fetcher URL + hermeticity fixes in `server/tests/`).
