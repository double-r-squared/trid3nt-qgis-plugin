# Combined SFINCS coastal quadtree worker (`sfincs_deckbuilder`)

> **Repurposed from deck-builder-only to the COMBINED build+solve worker.** The
> package directory keeps the historical `sfincs_deckbuilder` name (it is wired
> into the Dockerfile module path + the agent's Batch-submit seam), but the
> worker now does the FULL coastal job in ONE Batch job: BUILD the deck, then
> SOLVE it in-process.

A single GPL-bearing AWS Batch worker that, in **one** Batch job:

1. **BUILDS** a multi-level refined SFINCS **quadtree + SnapWave deck** from a
   build-spec JSON via Deltares `cht_sfincs` (GPL-3.0) — with auto-derived
   refinement, a cell-budget cap, and building obstacles;
2. **SOLVES** it by invoking the upstream `/usr/local/bin/sfincs` binary
   in-process on the *local* deck dir (no S3 round-trip); and
3. writes `sfincs_map.nc` + a **union** `completion.json` back to the object
   store.

This collapses what used to be **two** Batch job-defs (a deck-builder + a solve
shim, reached by the agent over an S3 + Batch-submit seam with two submits, two
polls, and one S3 round-trip of the deck) into **one** job-def
(`GRACE2_AWS_BATCH_JOB_DEF_SFINCS_QUADTREE`): one submit, one poll, no round-trip.

## Why one combined worker

`hydromt-sfincs` can only read/write one quadtree; only `cht_sfincs` can *build*
a refined multi-level connectivity table (`mu1/mu2/nu1/nu2/md1/md2/nd1/nd2` +
level flags). The deck-build half therefore needs the GPL library. The solve
half needs the MIT SFINCS binary. The old design split these across two images
because of the GPL boundary AND because the deck had to be uploaded then
re-downloaded. But the build half already populates a **local** deck dir and
already composes the exact manifest the solve worker reads — so the natural
combine is: after `build_deck()`, invoke the SFINCS binary on that same local
dir (reusing the solve worker's `_run_sfincs` pattern), skip the round-trip, and
write one completion.

## GPL boundary (still load-bearing)

`cht_sfincs` is **GPL-3.0**. It lives ONLY in this image and is imported ONLY by
`entrypoint.py` (lazily, inside `build_deck` + the refinement/obstacle helpers).
The **agent venv and all agent code never import `cht_sfincs`** — the
agent reaches this worker arms-length over the object-store + AWS-Batch-submit
seam (same pattern as before). The SFINCS solver binary is MIT-licensed and is
brought in by the `deltares/sfincs-cpu` base image; the combined image's license
is therefore `GPL-3.0-or-later AND MIT`.

## I/O contract

- **Input**: `--build-spec-uri s3://.../build_spec.json` (`schema_version: "v2"`,
  composed by the agent). Beyond the AOI / topobathy COG / base-grid / mask /
  SnapWave / forcing fields, the combined spec adds:
  - `grid.refinement_levels` (max auto-refinement levels, default 2),
    `grid.max_cells` (cell budget, default 2,000,000),
    `grid.refinement_polygons_uri` (optional explicit polygons, unioned in);
  - `buildings.footprints_uri` + `buildings.mode`
    (`thin_dams` | `raise_subgrid` | `exclude`) + `buildings.raise_height_m`;
  - `rivers.lines_uri` (OSM waterway centerlines, buffered into refinement).
- **Output** (all under `{scheme}://$GRACE2_RUNS_BUCKET/$GRACE2_RUN_ID/`):
  - `sfincs_map.nc` — the load-bearing flood output;
  - `sfincs.stdout` / `sfincs.stderr` — the solve run logs;
  - `manifest.json` — audit artefact (the deck→solve manifest; no longer fed to a
    second job, kept for replay/debug parity);
  - `deck/...` — optional deck audit upload (`output.upload_deck: true`);
  - `completion.json` — a **UNION** of the deck + solve schemas. It carries every
    key the agent's `wait_for_completion` reads (`status`, `exit_code`,
    `output_uris`, `sfincs_stdout_uri`/`sfincs_stderr_uri`, `started_at`,
    `finished_at`, `error`) PLUS a `deck` block (`nr_cells`, `nr_levels`,
    `manifest_uri`, `budget_notes`). `status` is `ok` only when BOTH the build
    succeeded AND the SFINCS binary exited 0.

## What the combine adds on top of the deck-build half

- **Auto-refinement** (`derive_refinement_polygons`) — derives the cht
  refinement-polygon GeoDataFrame (with a descending `refinement_level` column)
  from the inputs rather than a pre-baked URI: the topobathy 0 m NAVD88 contour
  (finest), the nearshore ~-2..0 m band, a slope threshold, OSM river centerlines
  buffered, OSM building footprints buffered, plus any explicit
  `grid.refinement_polygons_uri`. Vectorizes raster band masks via
  `rasterio.features.shapes` (no skimage dependency).
- **Cell-budget cap** (`estimate_quadtree_cells` + `apply_cell_budget`) —
  estimates the refined cell count from the base grid + per-level coverage, and
  drops the finest refinement levels (each costs `4**L`) until the estimate fits
  `grid.max_cells`, logging what it coarsened. Generalizes the regular-grid
  autoscale spirit to the quadtree. After the build it also records a hard
  provenance note if the realised `nr_cells` still over-ran.
- **Building obstacles** (`burn_building_obstacles`) — burns OSM footprints so
  water routes AROUND buildings: `thin_dams` along footprint exterior rings
  (blocked uv-faces, the default), `raise_subgrid` (raise sampled `z` at
  footprint face centres by `raise_height_m`), or `exclude` (drop those cells
  from the domain via the mask `exclude_polygon`).

## The two fixed caveats (preserved from the deck-build half)

- **CAVEAT 1 — SnapWave time column is tref-RELATIVE** (0.0, 7200.0, ...), not
  the SnapWave-internal epoch seconds the spike emitted. Enforced two ways:
  (a) `tref`/`tstart`/`tstop` set as proper datetimes so cht's
  `(time - tref).total_seconds()` is already 0-anchored, and (b) a post-write
  normalizer (`normalize_snapwave_time_columns`) that re-bases any bhs/btp/bwd/bds
  whose first time value is not ~0.
- **CAVEAT 2 — `snapwave_use_herbers = 1`** (infragravity-wave run-up), not 0.
  Forced in `snapwave_inp_overrides`. Deliberate opt-out: `snapwave.force_no_herbers: true`.

## Build (deferred — no docker on the dev box)

Single-stage `Dockerfile`, built from the **repo root** (`docker build -f
services/workers/sfincs_deckbuilder/Dockerfile .`). It bases on
`deltares/sfincs-cpu:sfincs-v2.3.3` (brings the SnapWave-capable
`/usr/local/bin/sfincs`) and pip-installs the `cht_sfincs` closure
(commit `159df40d`) into an isolated `/opt/venv`, preferring manylinux wheels for
the geo stack.

**The one build-time RISK to verify on EC2**: a GDAL/PROJ clash. The sfincs-cpu
base is Ubuntu 22.04 with its own system GDAL/PROJ; rasterio + geopandas in the
cht closure bundle their own GDAL/PROJ via manylinux wheels. We force the
wheel path (`--prefer-binary`); the build-time smoke imports `rasterio` +
`geopandas` + `cht_sfincs` AND tests the SFINCS binary in the SAME process so a
two-GDALs/two-PROJ_DATA clash trips at build time rather than at Batch exec. If
it trips, pin `PROJ_DATA`/`GDAL_DATA` to the wheel paths or fall back to a
`python:slim` multi-stage + `COPY` the sfincs binary across.

**Estimated image ~2.2–2.6 GB** uncompressed (sfincs-cpu base ~1.0–1.2 GB +
cht closure ~1.2 GB pruned). Heavier than either single worker (carries the
binary AND the GPL closure) and over the 2 GB AgentCore Runtime ceiling — fine
for AWS Batch (no cap). Inspect `docker history` + size on EC2 before any ECR
push (container-hygiene norm). The ECR build/push is a deferred EC2/SSM step.

## Tests

```bash
# Pure-python (no GPL library needed) — runs anywhere. Covers validation, both
# caveat fixes, the cell-budget estimator + cap, the SFINCS-binary invocation
# (with a fake echo binary), and the union completion.json:
python services/workers/sfincs_deckbuilder/test_entrypoint.py

# Full suite incl. a real cht_sfincs deck build with auto-refinement (against
# the spike venv where cht is installed):
services/workers/sfincs_quadtree_spike/.venv/bin/python \
    services/workers/sfincs_deckbuilder/test_entrypoint.py
```

The integration test builds a genuine multi-level quadtree (`nr_levels >= 2`)
with **auto-derived** refinement from a synthetic sloping-beach raster + a
SnapWave deck, and asserts BOTH caveat fixes in the emitted `sfincs.inp` +
`snapwave.*` files. The SOLVE half is unit-tested with a fake binary (no real
SFINCS executable on the dev box); the real binary runs only in-container.

## Agent-side follow-up (OUT OF SCOPE for this worker job)

This worker is self-contained, but the full collapse to one submit+poll requires
the agent-side change in `server/src/grace2_agent/tools/solver.py` +
`model_flood_scenario.py`: add a `sfincs-quadtree` solver key resolving
`GRACE2_AWS_BATCH_JOB_DEF_SFINCS_QUADTREE`, point `submit_sfincs_deckbuild` at
it, and DELETE the subsequent `run_solver("sfincs", model_setup_uri=...)` call
on the quadtree branch (the combined job already produced `sfincs_map.nc` under
the same run_id; read it from `completion.json.output_uris`). That edit is NOT
made here.
