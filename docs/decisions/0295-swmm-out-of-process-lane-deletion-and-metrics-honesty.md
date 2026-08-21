# ADR 0295 -- the out-of-process SWMM lane dies; the supervisor stops eating the metrics pointer

Status: LANDED. Date: 2026-08-20. Two independent items ruled together by NATE:
the PARKED fork ADR 0294 left standing, and a pre-existing honesty-floor
violation the 0294 verifier surfaced while proving the legacy-run fallback.

---

## Item 1 -- DELETE the out-of-process SWMM lane

### Context

ADR 0294 parked a fork: `workers/_swmm_postprocess` wrote `publish_manifest.json`
frame entries and NO `outputs.json`, so its frames were not superseded by the
emission campaign and could not be collapsed with the other three docker raster
workers. Two options were offered -- (a) migrate the lane onto the seam, (b)
delete it. NATE picked (b).

The lane was built for AWS Batch. That backend is decommissioned, the
`trid3nt-local/swmm` image does not exist on any box, and `docker images` confirms
it never did. The product path is host-exec: `pyswmm` is a pip dep in the agent
venv and SWMM5 is fully headless, so the composer solves the deck in-process.
The whole out-of-process apparatus was reachable only by setting
`TRID3NT_SWMM_LOCAL=0`, which would have dispatched at a nonexistent image.

### What was deleted

The lane is the unit. The enumerated pieces make the rest unreachable, so
stopping at the enumeration would have left a producer with no consumer and a
consumer with no producer -- delete-don't-disable, taken to zero references.

| Deleted | LOC |
|---|---|
| `workers/_swmm_postprocess/` (the whole package: `__init__.py`, `postprocess.py`, `test_postprocess_wiring.py`) | 615 |
| `workers/swmm/` (`entrypoint.py` the Batch worker, `Dockerfile` the nonexistent image's build, `run_inp.py` the local-exec shim, `__init__.py`) | 681 |
| `tests/test_swmm_two_card_sim_observability.py` (the whole file drove the off-box lane) | 261 |
| `run_swmm.py`: `is_local_mode`, `stage_swmm_manifest`, `swmm_local_spec`, `register_swmm_solver`, the module's lane-3 docstring | 303 |
| `urban_flood.py`: the `if not is_local_mode():` branch (dispatch + two-card sim cards + Batch telemetry + the register-only `_swmm_reg.layers[1:]` frame consumer + the Batch-output download fallback), `_record_swmm_batch_solve_telemetry`, `_BatchSWMMRun`, `_download_batch_swmm_outputs` | 485 |
| `test_run_swmm_local_chain.py` off-box cases, `test_local_subprocess_runner.py` SWMM cases, `test_worker_postprocess_offload.py` SWMM case | ~430 |

`SWMM_SOLVER_NAME` SURVIVES -- the in-process lane still tags its
solve-progress + telemetry rows with it. `'swmm'` was never in solver.py's static
`SOLVER_WORKFLOW_REGISTRY` literal (it self-registered), so its removal leaves no
orphan entry.

### Reference sweep

`TRID3NT_SWMM_LOCAL`, `_swmm_postprocess`, `workers/swmm`, `trid3nt-local/swmm`,
`stage_swmm_manifest`, `swmm_local_spec`, `register_swmm_solver`,
`run_swmm_postprocess`, `_download_batch_swmm_outputs`, `_BatchSWMMRun` are all
at ZERO outside `docs/decisions/` (immutable history) and
`docs/validation/engine-coverage-inventory.md` (a dated audit artifact that
already cites pre-unnesting `services/` paths). Swept: the sibling worker
entrypoints that cited `workers/swmm/entrypoint.py` and `run_inp.py` as their
pattern source (geoclaw, openquake, landlab), `workers/README.md`'s exec-mode
+ cloud-Dockerfile sections, `contracts/swmm_contracts.py` and
`mesh/raster_cell_mesh.py` (both cited a `spike_quasi2d.py` that had ALREADY been
deleted), `pyproject.toml`'s pin rationale, `scripts/run_swmm_direct.py`, and
five design/site docs.

### Coverage after

All 15 registered SWMM tools still route through the host-exec composer only, and
every one is covered. 153 tests across 14 SWMM files pass:
`test_postprocess_swmm.py`, `test_postprocess_swmm_pollutants.py`,
`test_run_swmm_local_chain.py`, `test_set_swmm_parameters.py`,
`test_swmm_deck_runner.py`, `test_swmm_hyetograph.py`,
`test_swmm_mechanism_compare.py`, `test_swmm_mesh_builder.py`,
`test_swmm_network_import.py`, `test_swmm_outputs_seam.py`,
`test_swmm_rdii_rtk.py`, `test_swmm_step3_quantities.py`,
`test_swmm_wq_deck.py`, `test_urban_flood_publish_offloop.py`. No template lost a
test -- the only deleted cases exercised the deleted lane.

---

## Item 2 -- the supervisor was eating `publish_manifest_uri`

### The bug

A quadtree SFINCS solve narrated `max_depth_m=0.0 / mean 0.0 / p95 0.0 /
flooded_cell_count 0` while its own peak COG held 19.99 m of water. Verified on
run `01M0H8FWAX4G4M260VRGR2XZCC`: `publish_manifest.json` sat under the run prefix
carrying the real metrics, and `completion.json` carried `output_uris: []` and NO
`publish_manifest_uri`.

The chain:

1. A self-S3 spec (`sfincs-quadtree`, `geoclaw`, `swan`) runs a container on
   `--network host` with credentials injected. The container does its own S3 I/O:
   it writes `publish_manifest.json`, `outputs.json`, the COGs, AND its own rich
   `completion.json` (with the pointer, the output URIs, and its engine `extra`).
2. When the container exits, `_supervise_local_run` uploads stdout/stderr, globs
   the (empty, because self-S3) rundir, and ALWAYS writes `completion.json` --
   OVERWRITING the worker's. The supervisor knows nothing about the manifest, so
   the pointer dies with the worker's copy.
3. `read_publish_manifest` resolves `completion.json.publish_manifest_uri` and
   NEVER globs -- by design, so the agent has one explicit contract. No pointer,
   `None` returned.
4. `flood.py` takes the SEAM path (`outputs.json` IS readable at its own
   deterministic key, so 146 layers published correctly), reads the metrics
   carrier for its narration scalars, gets `None`, and set `depth_metrics = {}`.
5. `FloodMetrics` was then built with `depth_metrics.get("max_depth_m", 0.0)` --
   four defaults presented as answers.

The GeoClaw / SWAN runs in the ADR 0294 close-out escaped only because their
drivers are short-lived processes: the supervisor is a DAEMON thread, so process
exit killed it before its overwrite landed, leaving the worker's completion.json
intact. Through the long-lived agent daemon the overwrite always wins. That is
also the pre-existing reason ADR 0294's second legacy candidate
(`01KZFRZTEK1Z53DWRXAQ7H4TPD`) returned empty.

### The fix, at both ends

**Root (`solver.py`).** `_write_local_completion` now probes the run prefix for
`<run_id>/publish_manifest.json` and folds the pointer into the payload when the
worker wrote one and the spec did not already supply it
(`_discover_publish_manifest_uri`). Every consumer of the pointer heals at once:
the flood, geoclaw, swan, swmm, landlab and openquake register paths,
`list_run_frames`'s legacy fallback, and `read_run_diagnostics`. A `head_object`
miss returns `None`, so the mounted-rundir specs (regular-grid SFINCS, MODFLOW)
are byte-unchanged.

Narrow by intent: `output_uris` is NOT reconstructed. Nothing on the healed path
reads it -- `outputs.json` and `publish_manifest.json` both resolve at
deterministic keys -- and inventing a URI list from a bucket listing would be a
second, unasked-for guess.

**Honesty floor (`flood.py`).** Law 9: the four narrated depth scalars are
physics-consequential, so an absent metrics carrier REFUSES instead of
defaulting. `missing_depth_metric_keys` (in `run_sfincs.py`, beside the metrics
it guards) names the rule; the composer returns
`_build_failed_envelope(error_code="SFINCS_METRICS_UNAVAILABLE")`, which rides the
documented out-of-band seam (`solver_version="failed:<CODE>"` +
`workflow_name="...:FAILED:<CODE>"`) so the agent surface narrates the failure
rather than the zeros.

The guard keys on key PRESENCE, never truthiness: a genuinely dry solve carries
`max_depth_m: 0.0` and narrates its zeros honestly. Only a MISSING key means "no
data", and only that refuses.

### Live proof

`01M0HDZN5PP5QYQ9YRWEV6A6K2` -- Mexico Beach coastal quadtree through
`model_flood_scenario(quadtree=True, coastal=True)`, dispatched
`sfincs-quadtree` -> `run_solver` -> `launch_local_solver` -> the supervisor.
Same case, same box, same image as the broken run above:

| | before (`01M0H8FWAX4G4M260VRGR2XZCC`) | after (`01M0HDZN5PP5QYQ9YRWEV6A6K2`) |
|---|---|---|
| `completion.publish_manifest_uri` | absent | `s3://trid3nt-runs/01M0HDZN5PP5QYQ9YRWEV6A6K2/publish_manifest.json` |
| narrated `max_depth_m` | 0.0 | 19.987058639526367 |
| narrated `mean_depth_m` | 0.0 | 12.292631149291992 |
| narrated `p95_depth_m` | 0.0 | 19.354148864746094 |
| narrated `flooded_area_km2` | 0.0 | 159.2748 |

COG truth read straight off the published peak raster
(`flood_depth_peak.tif`): max 19.9871, mean 12.2926, 707 888 valid cells. The
narrated `max_depth_m` matches the COG maximum to 1e-3. The seam built 146 layers
(1 standalone + 1 temporal group of 145 frames), unchanged.

---

## Cleanups landed alongside

- `server/dispatch/emitter.py`: the `list_run_frames` offload comment claimed the
  tool reads `publish_manifest.json`; ADR 0294 made it read `outputs.json` first.
- `workers/_raster_postprocess/postprocess.py`: 7 typographic dashes -> ASCII
  hyphens (4 in docstrings/comments, 3 in log format strings).

## Image rebuilds

The dash fix touches a worker module three images COPY, and the geoclaw
entrypoint's docstring was swept, so all three rebuilt with ABSOLUTE `-f` +
context paths and were provenance-checked by reading the in-image source:

| Image | Before | After | Delta |
|---|---|---|---|
| `trid3nt-local/sfincs:latest` | 555 118 566 B | 555 118 565 B | -1 B |
| `trid3nt-local/swan:latest` | 294 483 413 B | 294 483 405 B | -8 B |
| `trid3nt-local/geoclaw:latest` | 749 143 579 B | 749 143 562 B | -17 B |

Live solve through each: SFINCS the quadtree acceptance above; GeoClaw
`01M0HE3WZZQYK2VPS1AHT3E6PF` (peak + 7 frames + `outputs.json` +
`publish_manifest.json`); SWAN `01M0HE7HS3JC2N55K7W55583XC` (peak +
`outputs.json` + `publish_manifest.json`).

## Consequences

- ONE SWMM lane. There is no env flag that can route the urban engine anywhere
  but pyswmm-in-process, and no image reference that resolves to nothing.
- A self-S3 worker's manifest pointer survives the supervisor. The overwrite
  itself is left in place: it is the terminal signal the poller waits on, and it
  is the only writer that knows about cancel and classify_exit verdicts.
- The flood composer can no longer answer with confident zeros. If the carrier is
  unreadable the run reads as FAILED with a typed code, and the failure names the
  missing artifact.
- `output_uris: []` on a self-S3 run remains cosmetically wrong. No consumer reads
  it on these specs; if one ever does, that is a separate, deliberate change.
