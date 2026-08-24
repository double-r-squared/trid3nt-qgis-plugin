# `outputs.json` -- the emit-on-solve manifest (FROZEN, schema_version 1)

The append-only manifest a worker (or, for host-exec engines, the agent
acting as its own worker -- see the recon's gotcha #2) writes under the run
prefix so the emission seam can react to entries as they land. Companion to
`emission-campaign-cadence-recon.md` (the per-engine fact table this schema
must fit) and the existing `docs/design/emission.md` (the seam's home
folder). The wire shape is FROZEN at `schema_version = 1`; the typed writer +
reader live in `trid3nt_contracts.outputs_manifest`.

Settled (not relitigated here, per NATE's rulings): entries carry
`{kind, quantity, name, uri, t?, units?}` -- no roles, no flags.
`completion.json` (existing convention) is the finality signal. Frames
publish as they appear. Never omit a frame -- there is NO post-hoc frame
thinning ever; cadence resolves DECK-SIDE. Cadence is a deck-side lever via
the universal param name `output_interval_min`. Failure retracts nothing
(frames stand; `completion.json.status="error"` is the verdict, not a
retraction). An unknown quantity gets an honest neutral ramp plus a WARNING
log. QGIS Temporal Controller owns animation UX -- this manifest feeds it
data, not presentation.

## 0. v1 is AT-EXIT (the append-only contract is unchanged)

v1 publishes the whole `outputs.json` array AT EXIT: the leg writes its
entries as its postprocess produces them and the seam's final sweep
(Section 3) drains the array once `completion.json` appears. The append-only
wire contract (Section 2) is IDENTICAL whether entries land during the run or
all at once at exit -- so genuine DURING-RUN streaming is a later PER-ENGINE
capability (a leg grows a watcher thread that appends as frames land) that
requires NO schema change, only a producer change. The recon's "nothing
streams during a run today" is therefore a v1 non-issue: the seam already
handles a strictly-growing-or-unchanged array, and an at-exit whole-array
write is just the degenerate case of that same loop.

`kind="mesh"` (a native SELAFIN sibling QGIS/MDAL animates directly) is part
of the frozen entry contract but is NOT exercised this wave -- TELEMAC rides
it later (recon gotcha #1); v1 validates + log-only's a `mesh` entry.

## 1. The entry contract

```json
{
  "kind": "raster",
  "quantity": "flood_depth",
  "name": "Flood depth",
  "uri": "s3://trid3nt-runs/01QT26.../flood_depth_frame_07.tif",
  "t": 1800.0,
  "units": "meters"
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `kind` | string | yes | The seam's routing key -- `"raster"` (a single-band COG), `"mesh"` (a native mesh sibling QGIS/MDAL reads directly, e.g. TELEMAC SELAFIN), `"vector"` (a GeoJSON/vector layer), or `"scalar"` (a bare number/series with no map layer). Open-set: an unrecognized `kind` is a typed reject at write time, not a silent drop -- see Section 6. |
| `quantity` | string | yes | The physical quantity, e.g. `"flood_depth"`, `"dye_concentration"`, `"wave_height"`, `"burned_extent"`. The seam's styling-lookup KEY (Section 5). Free-text but drawn from a shared vocabulary as engines migrate (candidate: fold into `trid3nt_contracts.output_quantities`'s existing `quantity_id` -- see the recon's flag). |
| `name` | string | yes | The EXACT web-facing group/scrubber token, e.g. `"Flood depth"` / `"Flood depth step 7"`. Mirrors the existing `PublishManifestLayer.name` contract (`detectSequentialGroups` groups on this string) -- unchanged from today's manifest. |
| `uri` | string | yes | A bare `s3://` (or `gs://`) object key. NEVER a pre-templated tile URL -- the seam re-templates, exactly as `register_manifest_layers` does today. |
| `t` | number \| null | no | Seconds from run start. `null`/absent for a non-temporal artifact (a peak/final field, a scalar, a static input layer). Present and monotonically non-decreasing across a `quantity`'s own entries when temporal. |
| `units` | string | no | Free-text unit label (`"meters"`, `"mg/L"`, `"ft/min"`). Absent when not meaningful (`kind="mesh"`, most vectors). |

No `role`, no `style_preset`, no `metrics`, no `frame_no` on the entry itself --
those are TODAY's `PublishManifestLayer` fields (Section 5 explains where each
one's job goes instead). This is the deliberate flattening the settled design
calls for: the required core describes WHAT WAS WRITTEN AND WHEN, nothing about
how to draw it.

**AMENDMENT (schema_version 1, ADR 0280 EXECUTED 2026-08-17):** two OPTIONAL
render-hint fields REJOIN the entry -- `bbox` (per-COG EPSG:4326
`[minlon,minlat,maxlon,maxlat]`) and `band_stats`
(`{is_categorical, is_rgba, p2, p98}`). They are NOT part of the REQUIRED flat
core (`{kind,quantity,name,uri,t?,units?}`); a producer that ALREADY computed
them (every docker raster worker) writes them so the seam resolves the SAME bbox
+ rescale the register-only fast path did WITHOUT a per-COG re-read, which the
byte-equivalence bar (Section 7.1 lists bbox + band stats) requires. Absent
(host-exec engines that don't precompute), the seam degrades to the workflow AOI
bbox + a lazy per-COG stats touch (the unregistered-quantity neutral ramp only).
Tolerant-read: an old producer that omits them is byte-unchanged. This amends the
original "no `band_stats` / no `bbox` on the entry" line above -- the flat core is
unchanged; the render hints are optional and additive. See ADR 0280 EXECUTED.

**AMENDMENT (schema_version 1, ADR 0283 EXECUTED 2026-08-17):** one more OPTIONAL
field REJOINS the entry -- `crs_authid` (an EPSG authority id string, e.g.
`"EPSG:32617"`). It exists for `kind="mesh"` entries ONLY: a native SELAFIN mesh
sibling carries NO CRS of its own, and the plugin's `_add_mesh` sets
`QgsMeshLayer.setCrs(QgsCoordinateReferenceSystem(crs_authid))` from the row (0116).
CRS is PER-RUN (the reach's UTM zone), so it cannot live in the `quantity->style`
registry -- it rides the entry, exactly as the bespoke TELEMAC composer emit did
before the seam owned mesh publication. The seam threads it onto the mesh
`LayerURI` (`crs_authid=`). It is NOT part of the REQUIRED flat core; raster/vector
entries omit it (their COGs/GeoJSON are self-describing). Tolerant-read: an old
producer that omits it is byte-unchanged. Both the contracts writer/reader and the
worker mirror carry the field. See ADR 0283 EXECUTED.

`kind="mesh"` is NO LONGER log-only (superseding Section 0's "v1 validates +
log-only's a `mesh` entry"): `build_layers_from_outputs` now publishes a
`layer_type="mesh"` `LayerURI` per mesh entry (uri=the SELAFIN, style via
`resolve_style_preset(quantity)` -> `mesh_grid`, role `context`, `crs_authid` from
the entry, `bbox=None` so MDAL derives the extent, deterministic
`{quantity-base}-mesh-{run_id}` id, idempotent). The mesh sibling IS the TEMPORAL
artifact (MDAL animates every frame from the one file), so it is built even under
`frames_only=True` -- only the standalone peak raster + vectors are skipped there.

## 2. Append semantics

- **Entries are immutable once written.** A writer never edits or removes
  a prior entry -- a correction is a NEW entry (the `(quantity, t)` pair
  is not required to be unique; the seam takes the LAST entry it has seen
  for a given `(quantity, t)` as authoritative if a writer ever needs to
  supersede one, but this is a fallback, not the intended path).
- **Entries are ordered by `t`** within a `quantity`, ascending, with
  non-temporal entries (`t=null`) understood to have no ordering
  constraint relative to temporal ones. A writer MUST NOT reorder or
  reissue an already-written `t` for the same `quantity` except via the
  supersede fallback above.
- **Safe append pattern.** `outputs.json` is a JSON ARRAY at the top
  level (not a JSON-lines file -- object-store consumers read the whole
  object per poll anyway, and a bare array keeps the frozen schema
  trivially validated). A writer never does a partial-object PUT: it
  reads the current array (empty on the first frame), appends the new
  entry/entries, and PUTs the WHOLE array back as one object write. This
  mirrors the existing `publish_manifest.json` write-whole-object
  pattern (`_write_publish_manifest` in every worker entrypoint today) --
  no new object-store primitive is needed, S3/MinIO PUT is already
  atomic-per-object, so a poller never observes a torn write, only a
  strictly-growing-or-unchanged array between polls.
- A writer that crashes mid-run leaves `outputs.json` at its last
  successfully-appended state -- by design, per "failure retracts
  nothing": whatever frames landed stay visible even if the run later
  fails. `completion.json.status="error"` does not un-publish them.

## 3. `completion.json` interplay -- the seam watch-loop

`outputs.json` and `completion.json` are two different objects with two
different lifecycles: `outputs.json` grows monotonically during the run;
`completion.json` is written EXACTLY ONCE, at the very end, and its mere
existence is the terminal signal (`wait_for_completion`'s existing
contract, unchanged).

Today's poll loop (`trid3nt_server/workflows/solver/solver.py`,
`DEFAULT_POLL_INTERVAL_S = 10`) already polls for `completion.json` every
10 s alongside a SEPARATE 10 s live-progress heartbeat
(`workflows/shared/solve_progress.py`, elapsed/ETA only, no real solver
output). The watch-loop below is a THIRD concern riding the SAME cadence,
not a new timer:

```
async def watch_outputs(run_id, runs_bucket, emitter):
    seen_index = 0  # count of entries already published to the seam
    while True:
        completion = try_get_completion_s3(runs_bucket, run_id)  # non-blocking read, None if absent

        entries = try_get_outputs_json_s3(runs_bucket, run_id)  # None if absent/unparsed this poll
        if entries is not None:
            new_entries = entries[seen_index:]
            for entry in new_entries:
                publish_one_frame(entry, emitter)   # Section 5 obligations
            seen_index = len(entries)

        if completion is not None:
            # FINAL SWEEP: one more read in case a frame landed between the
            # last outputs.json poll and the completion.json write (the
            # writer's ordering in Section 6 makes this the common case,
            # not a race -- outputs.json is always written-to before
            # completion.json, but the POLLER can still observe them out
            # of order across two separate GETs).
            final_entries = try_get_outputs_json_s3(runs_bucket, run_id)
            if final_entries is not None and len(final_entries) > seen_index:
                for entry in final_entries[seen_index:]:
                    publish_one_frame(entry, emitter)
                seen_index = len(final_entries)
            return completion  # caller's existing wait_for_completion contract

        await asyncio.sleep(POLL_INTERVAL_S)  # 10s, same constant as today
```

- **Poll cadence**: reuse `DEFAULT_POLL_INTERVAL_S` (10 s) -- one more
  concern folded into the existing loop, not a new interval to tune.
- **Publish-on-appearance**: every entry present in a poll's array that
  was not present in the previous poll's array gets published, in order,
  that same poll -- never batched-and-dropped, never deduped away (a
  frame missed one poll and caught the next is still published once
  `seen_index` accounting is correct).
- **Final sweep**: the loop reads `outputs.json` ONE more time after it
  observes `completion.json`, closing the race where the last frame(s)
  landed in the gap between two GETs (see Section 6's ordering
  guarantee for why this is a narrow, not open-ended, race).
- **Stop**: the loop returns the moment `completion.json` is observed AND
  the final sweep is drained -- symmetric with `wait_for_completion`'s
  existing return contract, so this loop is a drop-in replacement for
  that function's inner poll body, not a parallel mechanism.

## 4. Version marker (the parser-version law)

Per the existing law (`publish_manifest.json`'s `schema_version`,
ADR 0158's "bump the parser version + reject unknown fields" precedent):

```json
{
  "schema_version": 1,
  "engine": "sfincs",
  "run_id": "01QT26...",
  "entries": [ { "kind": "raster", ... }, ... ]
}
```

`outputs.json` is versioned the SAME way `publish_manifest.json` is: a
top-level `schema_version` int, TWO surfaces gated on it, both SHIPPED in
`trid3nt_contracts.outputs_manifest`:

- the WRITER half (`new_manifest` / `build_entry` / `append_entries` /
  `serialize`) is PURE STDLIB so it is importable from BOTH the host-exec
  agent path (MODFLOW/SWMM, gotcha #2) AND a verbatim worker mirror
  (`workers/_raster_postprocess/outputs_manifest.py`, gated on the SAME
  `OUTPUTS_MANIFEST_SCHEMA_VERSION`), per `output_quantities.py`'s
  deploy-boundary precedent (the worker images ship `workers/**` but not
  `contracts`; the agent ships `contracts` but not `workers`). The worker
  mirror lands WITH the first docker-engine producer (the flood proving
  case), not before it is needed.
- the READER half (`OutputEntry` / `OutputsManifest` /
  `parse_outputs_manifest`) is tolerant Pydantic (`extra="ignore"` so an
  additive field never breaks an un-redeployed agent), agent-side only.

An unknown `schema_version` is a hard reject on the READ side (fall back to
no live frames, wait for `completion.json` only -- never a partial/best-guess
parse); a foreign entry `kind` is likewise a read-side reject. Neither MUST
EVER happen on the WRITE side (a worker image is pinned to one
`schema_version` for its whole life; bumping it is a coordinated worker-image
+ agent redeploy, same as today's manifest).

(Section 1 wraps entries in `{schema_version, engine, run_id, entries:
[...]}` rather than a bare array at the true top level, to carry the
version marker -- Section 2's "JSON array" language refers to the
`entries` field specifically.)

## 5. Obligations

### 5.1 The writer's obligations

- **Docker-path engines** (SFINCS, GeoClaw, SWAN, ELMFIRE, SCHISM,
  HEC-RAS, TELEMAC): the WORKER entrypoint owns the write, exactly where
  today's `publish_manifest.json` is written (before `completion.json`,
  per `_solve_postprocess_sweep`'s existing ordering) -- but now
  incrementally, from inside the solve loop or from a postprocess pass
  that can see intermediate dumps, not just once at the very end. This is
  the actual new work Section 6's migration section is about: today
  every engine (S-class included) writes the WHOLE manifest in one shot
  after `subprocess.run` returns. Getting genuine during-run appends
  requires either (a) a native solver hook/callback per timestep (rare;
  most of these binaries have none) or (b) a SEPARATE lightweight
  watcher thread inside the worker container that polls the solver's own
  output directory (e.g. new `fort.q*` files landing) while the solver
  subprocess runs, and appends to `outputs.json` as it notices them. (b)
  is the realistic default; it turns "during-run" into "near-real-time,
  bounded by the watcher's own poll interval," which is an honest,
  achievable version of "frames publish as they appear."
- **Host-exec engines** (MODFLOW `mf6`, SWMM `pyswmm` dev-primary path):
  there is no separate worker process to own the write (recon gotcha #2)
  -- the AGENT ITSELF, in the same coroutine/thread driving the solve,
  is the writer. This is architecturally different from every other
  engine and should be named as such rather than quietly special-cased:
  the agent process plays BOTH roles (solver driver AND manifest writer)
  for these two engines, so the append happens function-locally inside
  `run_modflow_local`/`run_swmm_local`, not via an object-store round
  trip to a container.
- A writer NEVER omits a frame it produced (the "never omit" ruling) --
  a frame that fails to encode (a corrupt COG, a degenerate field) is
  either retried within the SAME poll cycle or logged and skipped with a
  typed reason in the worker log, but the array position is never
  silently left out without a trace.

### 5.2 The seam's obligations

- **Styling lookup + neutral fallback**: given an entry's `quantity`, the
  seam resolves a style (colormap/rescale) from its OWN registry (the
  agent-owned equivalent of today's `_TITILER_STYLE_REGISTRY`, keyed by
  `quantity` rather than `style_preset` since the entry no longer carries
  one). An unrecognized `quantity` gets the "honest neutral ramp" --
  a fixed, quantity-agnostic greyscale/viridis rescale computed from the
  COG's own band stats at publish time (the seam must read enough of the
  COG to rescale when the registry has no entry -- this is the ONE place
  the seam still does a COG touch, unlike today's register-only path
  which never re-reads the COG because `band_stats` rides the manifest).
  This is a deliberate trade: dropping `band_stats` from the entry
  (Section 1) buys the flatter, role-free schema at the cost of a lazy
  read for unregistered quantities only -- registered quantities can
  still precompute/cache stats seam-side keyed by `quantity` once, since
  most runs of the same engine share a similar dynamic range.
- **Temporal stamping**: the seam is what turns a bare `t` (seconds from
  run start) into whatever timestamp semantics QGIS's Temporal Controller
  wants (an absolute datetime, or a relative-offset frame index) --
  entries carry the raw physical `t`, the seam owns the presentation
  mapping. This keeps `outputs.json` engine-agnostic: an engine writes
  seconds-since-start, never a wall-clock datetime it would have to
  invent.
- **Publish idempotence across polls**: the `seen_index` accounting in
  Section 3's loop is the seam's own bookkeeping, not something the
  manifest encodes -- a re-poll that re-reads an already-published entry
  (e.g. after a seam restart mid-run) must not double-emit a map layer.
  The seam's `observe_published_layer`/registry (unchanged from today)
  is naturally idempotent on `layer_id`, so the practical guard is: mint
  `layer_id` deterministically from `(quantity, t, run_id)` so a
  re-publish of the same entry resolves to the SAME layer id and is a
  no-op on the second call, rather than relying on `seen_index` alone
  surviving a seam restart.

### 5.3 Replay fields + the tolerance rule

The persisted layer record (whatever `LayerURI`/case-layer row a
re-opened Case rehydrates from) needs enough of the entry preserved to
replay the animation without re-polling `outputs.json` (which may no
longer exist if the run prefix has been GC'd -- retention, Section 7).
Candidate replay fields on the persisted record: `quantity`, `t`,
`uri`, `units`, plus the seam-resolved style key it computed at publish
time (so replay does not require re-resolving styling against a registry
that may have changed shape since). The TOLERANCE RULE: a persisted
record whose `uri` object no longer resolves (expired/GC'd) degrades to
"replay metadata only, no live raster" -- the Case still shows the frame
existed (`t`, `quantity`, `name`) without a broken map layer, matching the
existing honesty-floor norm ("never a broken load presented as fine").

## 6. Retention arithmetic

Measured from a real run prefix in the local MinIO runs bucket
(`s3://trid3nt-runs/01QT260807202750MEXBEACH/`, a SFINCS coastal run with
`output_interval_min` wired -- 25 map-output frames):

| Object | Size |
|---|---|
| `flood_depth_peak.tif` | 1,351,024 bytes (~1.29 MiB) |
| `flood_depth_frame_01.tif` .. `frame_25.tif` | 1,351,024 bytes each (constant -- same grid/overview structure per frame) |
| **26 raster COGs total** | **~33.5 MiB** |
| `publish_manifest.json` (26 layer entries, TODAY's shape: role/style_preset/band_stats/metrics per entry) | 19.0 KiB (~730 B/entry) |
| `sfincs_map.nc` (raw solver NetCDF, all frames) | 5.7 MiB |
| `sfincs.nc` (input deck) | 8.1 MiB |
| **Whole run prefix** | **47.4 MiB / 32 objects** |

Arithmetic this implies for `outputs.json` at scale:

- **Per-frame raster cost dominates**: the COGs are ~1.3 MiB each here
  (a coastal AOI at a moderate resolution) and CONSTANT across frames
  (overview-bearing COGs of the same grid don't shrink for a quiet
  frame) -- 144 frames (`MAX_FLOOD_FRAMES`, the current hard cap) at this
  size would be ~187 MiB of raster alone, before any other engine's run
  in the same session. This is the existing cap's real justification: it
  is a STORAGE bound as much as a UX-scrubber bound.
  - This measurement is representative of ONE engine's grid size at ONE
    resolution; a finer/quadtree grid or a larger AOI scales this
    number directly. The retention policy therefore needs to be a
    BYTE budget per run (or per session), not a frame-count budget --
    144 frames of a 30 m CONUS-scale grid is a very different number of
    bytes than 144 frames of a 5 m neighborhood grid.
- **`outputs.json` itself is cheap relative to the rasters it points
  at**: the settled entry shape (Section 1) is FLATTER than today's
  19 KiB/26-entries (~730 B/entry) manifest -- dropping `band_stats`,
  `role`, `metrics`, `bbox` per entry brings a single entry down to
  roughly `{kind, quantity, name, uri, t, units}`'s ~150-200 bytes of
  JSON. At 144 entries that is ~25-30 KiB, i.e. `outputs.json` stays a
  sub-30-KiB object even at the frame cap -- cheap enough that the
  poll-and-reread-whole-array pattern (Section 2) is not a bandwidth
  concern; it is the ~150 MiB+ of actual COGs that retention policy
  needs to bound, not the manifest.
- **Retention policy candidate**: age out run prefixes (the WHOLE
  prefix, COGs + manifest + raw NetCDF/HDF together) on a TTL, same as
  any other object-store lifecycle rule -- `outputs.json`'s replay-field
  tolerance rule (Section 5.3) is exactly what makes that safe: a
  Case that outlives its run prefix's TTL degrades gracefully instead of
  breaking.

## 7. Migration

### 7.1 Flood-as-proving-case plan

SFINCS pluvial `flood` is the proving case: it already has (a) worker-side
`publish_manifest.json` writing (Phase 4 lineage), (b) all-steps
subsampled-to-cap frame emission, (c) register-only agent-side consumption
-- the closest existing analogue to the target `outputs.json` shape of any
engine (recon work-class **S**). The byte-equivalence bar: for a FIXED
flood run (fixed AOI, fixed `output_interval_min`, fixed random/forcing
seed), the SET of published map layers (COG bytes, bbox, band stats,
narration scalars) the plugin renders from the NEW `outputs.json` +
seam-styling path must be byte-identical to what today's
`publish_manifest.json` + `register_manifest_layers` path renders for the
SAME run. The MANIFEST shape differs (flatter entries, Section 1); the
RENDERED OUTPUT must not. This makes the migration provable with the
existing flood canary (`scripts/run_sfincs_direct.py`, status=ok) plus a
byte-diff of the COG set between the old and new code paths on the same
solved `sfincs_map.nc` -- no new solver run needed to validate the seam
change itself.

### 7.2 Engine order, ranked by work class

1. **S-class first** (SFINCS flood, SFINCS surge/waves, GeoClaw
   inundation, SWAN nonstationary) -- these already write worker-side
   manifests; the migration is a schema/field rework on an EXISTING write
   point, not new plumbing. SFINCS flood is the proving case (7.1); the
   other three follow once the seam-side styling/replay machinery is
   proven against it.
2. **M-class second** (SWMM urban/dual, Landlab overland_flow_timeseries,
   TELEMAC rain_on_grid) -- multi-step data + the subsample machinery
   already exist; these need the write point moved (agent-side on-box ->
   a proper worker/host-exec boundary write per recon gotcha #2 for
   SWMM) or a genuinely new streaming write inside an already-Python
   in-process loop (Landlab, the least effort of the three since there is
   no subprocess boundary to cross at all).
3. **L-class last, and NOT uniform effort within L** -- three different
   kinds of "L" that should not be scheduled as one bucket:
   - **Peak/final-only postprocess with real multi-step raw data
     underneath** (MODFLOW, SCHISM, HEC-RAS, most TELEMAC modules): the
     hard part is building the frame-extraction leg itself (reading N
     steps instead of one final `nanmax`), not the manifest. Do these
     ONE AT A TIME with a native V&V case per the fidelity-ladder norm,
     not as a batch.
   - **No true per-timestep artifact at all** (ELMFIRE): needs a design
     decision (wire `DTDUMP` to real intermediate ToA dumps and read
     them, vs. keep the current derived-threshold animation and just
     wrap it in `outputs.json` entries) BEFORE any code lands -- flagged
     here, not resolved.
   - **Native-mesh-temporal mismatch** (TELEMAC, all modules): needs the
     `kind="mesh"` entry-type question (recon gotcha #1) resolved with
     NATE/the campaign owner before ANY TELEMAC work starts, since it is
     a schema question, not an implementation one.

### 7.3 The collapse step

**EXECUTED-NARROW (2026-08-19, ADR 0294).** NATE's ruling narrowed the collapse:
`publish_manifest.json` SURVIVES as the metrics carrier (the composers read its
top-level aggregates for their narration scalars; flat `outputs.json` entries
carry none) and as the legacy register-only fallback. ONLY its FRAME entries
died. The three docker RASTER workers (SFINCS `_raster_postprocess`, GeoClaw,
SWAN) now write the non-frame entries alone; `list_run_frames` reads
`outputs.json` first and keeps a LEGACY-run `publish_manifest` frame fallback.
Ledger row 19 is DELETED at that scope. What is written below stays QUEUED: the
file, the bespoke schema, and `register_published_manifest.py` are still live and
still carry non-frame entries.

Once every engine leg is migrated: delete `workers/_raster_postprocess/manifest.py`
+ `contracts/trid3nt_contracts/publish_manifest.py`'s bespoke schema (superseded
by `outputs.json`'s `schema_version`), delete each engine's bespoke
per-engine frame-selection duplication that is not already centralized in
`workflows/shared/frames.py`, and retire `register_published_manifest.py`
in favor of the seam's own `outputs.json` consumer (Section 5.2). Per the
deletion-ledger norm, each of these should be registered as a QUEUED
deletion with its CONDITION-to-delete ("last engine migrated off
`publish_manifest.json`") at the point the FIRST engine migrates, not
discovered later -- and per the flag in the recon's "does not survive
contact" section, this collapse should also explicitly decide the fate of
the `output_quantities.py`/`publish_quantities.py` scaffold (fold into
`outputs.json`'s writer side, or delete as superseded) rather than leaving it
as a second, unfinished mechanism next to a shipped one.

CORRECTION (verified against the code 2026-08-16, supersedes the recon's
"`get_output_registry` returns `()` for every engine except OpenQuake"): the
scaffold is NOT default-off/empty. `OUTPUT_QUANTITIES` carries LIVE
`default_on=True` quantities for FOUR engines -- `openquake` (hazard-curves,
uhs), `modflow` (plume-ts, water-table, drawdown, dewatering-rate,
budget-partition, mounding, recovery-efficiency, hydroperiod), `swmm`
(flooding-losses, ponded-volume, conduit-flow, conduit-velocity), `landlab`
(drainage-area, slope, relative-wetness, discharge, factor-of-safety) -- and
each engine's postprocess (`postprocess_openquake` / `postprocess_modflow` /
`postprocess_swmm` / `postprocess_landlab`) binds readers and CALLS
`publish_quantities`, producing REAL product layers, with dedicated tests
(`test_{modflow,swmm,landlab,openquake}_step3_quantities`,
`test_publish_quantities`, `test_output_quantity_style_presets`). Deleting the
scaffold is therefore NOT "migrate OpenQuake's styling need and delete" -- it
retires a live 4-engine feature. NATE's item-6 deletion ruling was premised on
the stale recon fact; the deletion is REGISTERED as QUEUED in the ledger with
the CONDITION "all four live consumers migrate onto the outputs.json writer +
quantity->style registry (one at a time, byte-equivalence per engine)", NOT
executed in this wave. See ADR 0280.
