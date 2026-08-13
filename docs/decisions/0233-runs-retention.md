# ADR 0233 - runs-prefix retention: reap raw solver scratch after a successful postprocess

Status: Accepted
Date: 2026-08-12

## Context

`XMinioStorageFull` fired three times against the local-docker MinIO runs
bucket. Root cause: GeoClaw persists ~7.7 GB of raw AMR `fort.q*`/`fort.t*`
frames per run in `s3://trid3nt-runs/<run_id>/_output/` -- the entrypoint
uploads them as part of the manifest `outputs` glob sweep (postprocess reads
them to rasterize the depth COGs) and nothing ever deletes them afterward. The
orchestrator hand-reaped 18.7 GB across old runs to clear the immediate outage.
NATE approved a standing policy instead of repeat manual reaps.

## Decision

After a run's postprocess completes **successfully** and its publishable
artifacts (COGs/charts/json) are written to the run prefix, the worker deletes
that run's raw solver scratch from the runs bucket. Reap is gated on
postprocess `status == "ok"` ONLY -- a failed run keeps its full scratch for
debugging (never reaped on error).

**Shared helper** (`services/workers/_raster_postprocess/retention.py`,
worker-local, no agent import):

- `match_scratch_keys(relative_keys, patterns, keep_patterns=())` -- pure
  fnmatch filter, no I/O. `keep_patterns` always wins over a reap pattern.
- `reap_run_scratch(delete_fn, run_prefix, relative_keys, patterns,
  keep_patterns=())` -- deletes matched keys via a caller-supplied
  `delete_fn(relative_key)`. Never raises: a delete failure is logged and
  returned in `errors`, the reap continues. Operates ONLY over the run's
  already-known uploaded relative keys (the entrypoint's own upload-sweep
  list) -- no S3 `LIST` call, so reap can never touch an object this run
  didn't itself just write.

Each engine owns its own pattern list, co-located with its postprocess module
(e.g. `services/workers/_geoclaw_postprocess/postprocess.py`) so the patterns
travel with the parser that defines what "raw scratch" means for that solver.

**Call site** (`services/workers/geoclaw/entrypoint.py::main`): after
`run_geoclaw_postprocess` returns `status == "ok"` and the publish manifest is
written, the entrypoint builds a scheme-aware `_delete_scratch(rel)` closure
(s3 `delete_object` / gs `blob.delete()`, mirroring the existing
`_upload`/`_download` scheme dispatch) and calls `reap_run_scratch` over the
relative keys it already uploaded this run. Reaped URIs are pruned from
`output_uris` before `completion.json` is written, so the terminal manifest
never advertises a URI that no longer exists (honesty floor).

**Also fixed** (load-bearing for this feature to ever run): the GeoClaw
Dockerfile only `COPY`'d `services/workers/geoclaw/`, never
`_geoclaw_postprocess/` or `_raster_postprocess/` -- unlike the SFINCS/MODFLOW
Dockerfiles, which already copy `_raster_postprocess/`. The entrypoint's
postprocess import sits behind a non-fatal `try/except`, so in the built image
this failed silently: no `publish_manifest.json`, no reap, ever. Added the two
missing `COPY` lines. **This requires an image rebuild to take effect** -- not
done here per the no-rebuild-during-the-live-solve constraint on this job.

## Per-engine scratch table

| engine | candidate scratch | typical size (measured on disk) | verdict |
| --- | --- | --- | --- |
| geoclaw | `_output/fort.q*` (AMR depth/momentum frames), `fort.t*` (frame time headers), `fort.b*`, `fort.a*`, `*.data`/`*.txt` (fgmax grids + point output) | ~7.7 GB/run (the proven offender; already hand-reaped once) | **REAP** on postprocess success. `gauge*.txt` excluded (the gauge time-series tool reads it later); no pattern can match a `.tif`/`.json`/`.fgb`/`.png` artifact |
| telemac | `river.slf` (geometry), `r2d_river.slf` (result mesh), `gaia_river.slf` (sediment) | 17-106 MB across sampled runs (`data/minio/trid3nt-runs/*/r2d_river.slf`) | SKIP -- not proven bulky (under 1 GB by a wide margin); postprocess reads these directly and a future re-postprocess would need them |
| schism | `outputs/*.nc` (schout), `sflux/*.nc` (forcing) | sflux inputs sampled at 2-4 MB; schout not yet observed at scale locally | SKIP -- no evidence of a bulk-scratch class under this job's local-run sample; revisit with measured schout sizes before adding a pattern |
| sfincs, swmm, modflow, landlab | small text/binary decks + results | small (existing local-docker corpus) | SKIP -- conservative, no evidence of a bulk offender |

Conservative by design: only GeoClaw gets a reap pattern in this landing. The
`reap_run_scratch` helper is engine-agnostic, so adding a second engine later
is a small, additive per-engine pattern list (mirroring
`GEOCLAW_SCRATCH_PATTERNS`/`GEOCLAW_SCRATCH_KEEP_PATTERNS`) plus one call-site
wire -- no changes to the shared helper.

## Consequences

- GeoClaw runs stop accumulating multi-GB `_output/fort.*` scratch once the
  postprocess success path actually runs in a rebuilt image.
- A GeoClaw run can no longer be re-postprocessed from its raw frames after
  the fact (the COGs + manifest are the durable artifact going forward) --
  accepted trade-off per NATE, matching the proven-offender exception carved
  out for `fort.q`.
- Failed runs are untouched -- full scratch stays for debugging, exactly as
  before.
- The Dockerfile fix is a text-only change; the actual retention behavior does
  not take effect in the deployed image until the next GeoClaw image rebuild.

## Addendum (2026-08-12) - sweep-order fix + missing outputs-glob entry

The rebuilt image's own smoke run (01KZWT7J3T0V95E8HF0E5S8XHF) surfaced a
second, load-bearing defect: `services/workers/geoclaw/entrypoint.py::main`
ran the `_expand_outputs` upload sweep **before**
`run_geoclaw_postprocess` had written the peak/frame depth COGs into scratch.
`services/workers/sfincs/entrypoint.py`'s `_solve_postprocess_sweep` has the
correct order (postprocess writes COGs into `run_dir` BEFORE the sweep glob
runs, per its own in-code comment) -- geoclaw's `main()` did not follow that
pattern. Net effect: `publish_manifest.json` was written with valid
`cog_uris`, but the referenced `.tif` objects were never actually uploaded to
the runs prefix -- a live honesty-floor violation (a manifest that lies about
what exists).

**Fix 1 (ordering, `services/workers/geoclaw/entrypoint.py`)**: reordered
`main()` to `postprocess -> sweep -> reap`, matching the sfincs pattern. The
reap stays gated strictly inside `status == "ok"` and now runs AFTER the
sweep (previously it ran interleaved with a sweep that had already happened),
so a freshly-swept COG can never be reaped. Belt-and-suspenders: verified
`GEOCLAW_SCRATCH_PATTERNS` (`_output/fort.q*`, `fort.t*`, `fort.b*`,
`fort.a*`, `*.data`) can never match a `.tif` regardless of ordering.

**Fix 2 (outputs-glob, `server/src/trid3nt_server/agent/workflows/geoclaw/run_geoclaw.py`)**:
fixing the ordering alone was NOT sufficient -- a live smoke rerun after Fix 1
still shipped zero COGs. Root cause: `GEOCLAW_OUTPUT_GLOBS`, the agent-authored
manifest's `outputs` field, is AUTHORITATIVE and OVERRIDES the worker's
`DEFAULT_OUTPUT_GLOBS` (the same rule already called out in this list's own
comment for the deformation artifacts) -- and it never carried a `"*.tif"`
entry. So even with the ordering fixed, the worker's sweep glob had nothing
telling it to pick up `geoclaw_depth_peak.tif` / `geoclaw_depth_frame_NN.tif`.
Added `"*.tif"` to `GEOCLAW_OUTPUT_GLOBS`.

Both fixes were required together; the ordering fix alone reorders a sweep
over an outputs list that still excluded COGs, and the glob fix alone would
still race the COGs' write against an unordered sweep. Live smoke evidence
(run `01KZWTS275YMV91NDW22TDR60Z`, Crescent City tsunami bbox) after both
fixes: `geoclaw_depth_peak.tif` + 7 `geoclaw_depth_frame_NN.tif` files present
in the run prefix, byte-for-byte matching `publish_manifest.json`'s 8
`cog_uris`; raw `_output/fort.*` scratch absent (reaped); `_output/gauge00001.txt`
kept; `completion.json` `output_uris` lists all 8 COGs + the manifest.
