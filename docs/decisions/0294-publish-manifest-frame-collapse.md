# ADR 0294 -- the publish_manifest FRAME collapse (narrow scope: frames die, the metrics carrier lives)

Status: LANDED (docker S-class producers stop dual-writing frame entries +
`list_run_frames` reads `outputs.json` first + three image rebuilds + live proofs).
Date: 2026-08-19. Builds on ADR 0280 (the seam + the frozen `outputs.json` schema),
0281 (the GeoClaw + SWAN docker legs), 0282/0283/0284/0286/0287/0288 (the rest of
the emission campaign). Executes the NARROW half of
`docs/design/outputs-manifest-schema.md` Section 7.3 and closes DELETION_LEDGER
row 19.

## Context

The emission campaign put every solver engine's temporal result on ONE wire:
`outputs.json` + the emit-on-solve seam (`emission/outputs_seam.py`). Where the
producer was a docker RASTER worker (SFINCS, GeoClaw, SWAN) the migration
deliberately kept the worker DUAL-WRITING its frames -- once as `outputs.json`
entries, once as `publish_manifest.json` `layers[]` rows carrying a `frame_no` --
as an explicit one-release fallback while the images had not all rebuilt. Both
ADR 0280 and 0281 recorded that dual-write as SUPERSEDED-but-RETAINED and pointed
its removal at this row.

NATE's ruling (2026-08-19, premise-corrected): `publish_manifest.json` SURVIVES.
It is the metrics carrier (the top-level aggregates the composers read for their
narration scalars -- flat `outputs.json` entries carry no aggregates) and the
legacy register-only fallback. ONLY its FRAME entries die. The full 7.3 collapse
(retiring the file, the bespoke schema, and `register_published_manifest.py`)
stays QUEUED, and `publish_quantities` is untouched -- its own 4-engine condition
is unmet.

## The inventory (the coverage-law denominator)

Every site that WRITES a `frame_no`-bearing entry into `publish_manifest.json`,
and every site that READS one. Verdicts verified against the code, not the
kickoff.

### Producers

| Producer | Frame entries? | Writes `outputs.json`? | Verdict |
|---|---|---|---|
| `workers/_raster_postprocess/postprocess.py` (SFINCS depth + SnapWave waves) | yes | YES, same function, same ordered frames | DELETED -- only the `role="primary"` peak is written to `publish_manifest` |
| `workers/_geoclaw_postprocess/postprocess.py` | yes | YES | DELETED -- frames moved to a local `frames` list feeding `outputs.json` alone |
| `workers/_swan_postprocess/postprocess.py` | yes | YES | DELETED -- same shape as GeoClaw |
| `workers/_swmm_postprocess/postprocess.py` | yes | **NO** | **PARKED (fork for NATE)** -- see below |
| `workers/_modflow_postprocess/postprocess.py` | no (7 call sites, all `frame_no=None`, `role="primary"`) | n/a | no-op |
| `workers/_landlab_postprocess/postprocess.py` | no (1 site, `frame_no=None`) | n/a | no-op |
| `workers/_openquake_postprocess/postprocess.py` | no (1 site, `frame_no=None`) | n/a | no-op |
| `workflows/shared/publish_quantities.py` (in-memory `PublishManifest`) | frame-capable but never fed frames by its four live engines | n/a | OUT OF SCOPE -- its own ledger condition is unmet |

The host-exec / agent-side producers (SWMM in-agent, Landlab, MODFLOW transport,
TELEMAC, SCHISM, HEC-RAS, ELMFIRE) never wrote `publish_manifest` frames at all --
they write `outputs.json` through `workflows/shared/outputs_manifest_io`. They are
in the denominator and their verdict is "nothing to delete".

### Consumers

| Consumer | Reads frame entries how | Verdict |
|---|---|---|
| `trid3nt_server/data/meta/list_run_frames/list_run_frames.py` | `read_publish_manifest` -> `frame_no is not None` | MIGRATED -- reads `outputs.json` FIRST (raster entries with a `t`, ordered by `t`), legacy `publish_manifest` frames only when no outputs manifest is readable |
| `workflows/sfincs/flood/flood.py` | legacy branch: `reg.layers` split on `role` | UNCHANGED -- the branch only runs when `outputs.json` is absent (legacy runs / a failed outputs write); a current run takes the seam branch |
| `workflows/geoclaw/inundation/inundation.py` | same seam-or-legacy fork | UNCHANGED, same reason |
| `workflows/swan/wave_field/wave_field.py` | same fork via `register_swan_wave_layers` | UNCHANGED, same reason |
| `workflows/swmm/urban_flood/urban_flood.py` | register-only branch: `_swmm_reg.layers[1:]` as frames | UNCHANGED -- tied to the parked SWMM docker producer |
| `workflows/landlab/susceptibility/susceptibility.py` | `role != "primary"` split | UNCHANGED -- its producer emits one primary layer and no frames, so the split is already empty |
| `workflows/openquake/psha/psha.py` | reads the manifest, no frame split | UNCHANGED |
| `workflows/shared/register_published_manifest.register_manifest_layers` | frame-AGNOSTIC (registers every entry; callers split) | UNCHANGED -- no frame logic to collapse |

## The parked fork -- the SWMM docker lane

`workers/_swmm_postprocess/postprocess.py` writes frame entries into
`publish_manifest.json` and writes NO `outputs.json`. The SWMM migration (ADR
0282) moved the HOST-EXEC lane (pyswmm in-agent, the default:
`TRID3NT_SWMM_LOCAL` unset) onto the seam; the OUT-OF-PROCESS lane
(`TRID3NT_SWMM_LOCAL=0` -> `run_solver` -> a `trid3nt-local/swmm` image) was never
migrated, and no such image exists on this box. Its frames are therefore NOT
superseded by `outputs.json`: deleting them would silently reduce that lane to
peak-only if it were ever revived.

The kickoff premise "the campaign closed 10/10 engines, so every frame dual-write
is superseded" is true for the three docker RASTER workers and false for this one.
The narrow ruling ("only the SUPERSEDED frame entries die") therefore leaves it
standing. NATE's call, two options:

- **(a) migrate it** -- give `_swmm_postprocess` the `outputs.json` writer the
  other three docker workers use, then delete its frame entries in a follow-up.
  Costs a worker image that does not currently exist.
- **(b) delete the lane** -- if the out-of-process SWMM lane is dead (no image,
  the AWS Batch backend it was built for is decommissioned), delete the lane, the
  worker, and the composer's register-only branch together. Recommended: this is
  the clean-as-you-go verdict, and it removes a consumer rather than feeding it.

Parked, not picked.

## Decision

1. The three docker RASTER worker postprocesses build the temporal frames into a
   local list that feeds `outputs.json` ONLY. `publish_manifest.json` receives the
   non-frame entries alone. `frame_count` stays and now reports exactly what its
   name promises (no reader depends on it; it is informational).
2. The degrade guards move with the frames: a frame-COG write failure and the
   "a lone frame is no animation" rule now operate on the frame list, not on a
   peak-plus-frames layer list (`len(frames) < 2` replaces `len(layers) < 3`).
3. Per-frame `metrics` computation on the frame path is deleted with the entries
   that carried it (GeoClaw `compute_geoclaw_depth_metrics` per frame, SWAN
   `_compute_wave_metrics` per frame) -- nothing read them; the peak's metrics are
   the narration source.
4. `list_run_frames` reads `outputs.json` first and keeps the legacy
   `publish_manifest` read as an explicit LEGACY-run fallback. Its `layer` filter
   now matches the entry's physical `quantity` as well as the web grouping `name`;
   `frames[]` gained `t` (seconds from run start, `None` on the legacy path).
5. The byte-equivalence bars in `tests/test_{outputs,geoclaw_outputs,swan_outputs}_seam.py`
   re-pin from "the whole register stream == the whole seam stream" (no longer
   meaningful -- the register path has only the peak) to "the PEAK row is
   field-for-field identical AND every seam frame renders with the peak's
   resolved style/rescale/legend/bbox", which is the substantive promise.

## Consequences

- ONE frame stream. A frame exists in exactly one manifest, so the two can never
  disagree, and a reader can no longer pick the stale one.
- `publish_manifest.json` shrinks to O(1) entries for the animated engines.
- The register-only legacy branch in the three composers degrades to peak-only for
  a run whose `outputs.json` write failed (the write is already best-effort inside
  a try/except). That is the campaign's stated degrade -- "a frame publish/read/emit
  miss degrades to peak-only, never sinking the run" -- now also true one level up.
- Worker code is INERT until rebuild: this landed with all three images rebuilt and
  a live solve through each.

## Live close-out (2026-08-19)

All three images rebuilt with ABSOLUTE `-f` + context paths
(`docker build -f /home/nate/Documents/trid3nt-local/workers/<engine>/Dockerfile -t
trid3nt-local/<engine>:latest /home/nate/Documents/trid3nt-local`) and
provenance-checked by importing the postprocess INSIDE each image and asserting the
new code is the code that shipped. Sizes (`docker image inspect .Size`) barely move
-- the change is a few hundred lines in a COPY'd source tree:

| Image | Before | After | Delta |
|---|---|---|---|
| `trid3nt-local/sfincs:latest` | 555 067 331 B | 555 118 483 B | +51 KB |
| `trid3nt-local/geoclaw:latest` | 749 231 221 B | 749 143 561 B | -88 KB |
| `trid3nt-local/swan:latest` | 294 449 448 B | 294 483 406 B | +34 KB |

Live runs THROUGH the rebuilt images (each: `status=ok`, seam path taken,
`publish_manifest.json` present WITHOUT frame entries, metrics intact):

- **SFINCS** `01M0H8FWAX4G4M260VRGR2XZCC` -- Mexico Beach coastal quadtree via the
  composer (`sfincs_flood(quadtree=True)` -> `sfincs-quadtree` dispatch on
  `trid3nt-local/sfincs:latest`). `outputs.json` = 146 entries (peak + 145 frames,
  `t` 0..43200 s at 300 s); `publish_manifest.json` = 1 layer
  (`flood-depth-peak`, `frame_no=None`), `frame_count=145`, metrics
  `max_depth_m=19.987 / mean_depth_m=12.293 / p95_depth_m=19.354 /
  flooded_cell_count=176972`; the seam built 146 layers (1 standalone + 1 temporal
  group); `list_run_frames` returned 145 frames from `outputs.json`.
- **GeoClaw** `01M0H882Q83BCPDMHKC3AF0TWC` -- tsunami inundation via
  `model_geoclaw_inundation`. `outputs.json` = 8 entries (peak + 7 frames, `t`
  0/300/.../1800 s from `fort.t`); `publish_manifest.json` = 1 layer
  (`geoclaw-depth-peak`, `frame_no=None`), `frame_count=7`, metrics
  `max_depth_m=33.356 / flooded_area_km2=15.312 / max_inundation_m=1.821 /
  arrival_time_s=36.94`; `list_run_frames` returned all 7 frames.
- **SWAN** `01M0H8RNCGHBV96KFFXNE6HZRW` -- nonstationary storm wave field.
  `outputs.json` = 20 entries (peak + 19 frames); `publish_manifest.json` = 1
  layer; `max_hs_m=6.081` read from the metrics carrier; the seam built 20 layers
  (1 standalone + 1 temporal group). The stationary run
  `01M0H8NK4ECH36Y2WZAPEPJTN2` is the peak-only control (1 entry, 1 layer).

**Legacy-run proof.** `01KZWT7J3T0V95E8HF0E5S8XHF` (a GeoClaw run predating
`outputs.json`: `publish_manifest.json` with 7 `frame_no`-bearing layers, no
outputs manifest) -- `list_run_frames` served all 7 through the fallback, `t=None`
on every row. A second candidate (`01KZFRZTEK1Z53DWRXAQ7H4TPD`, SFINCS, 144 frame
entries) returns empty, but for a PRE-EXISTING reason unrelated to this change: its
`completion.json` carries no `publish_manifest_uri`, and `read_publish_manifest`
has always required that pointer.

**Flood canary** (`scripts/run_sfincs_direct.py`, the CLAUDE.md law-3 canary):
`status=ok`, run `01M0H83DMDFW9J09QX172NZZCQ`, depth COG + 7 frame COGs published.
Note it exercises the ON-BOX lane, not a worker image -- the regular-grid local
lane runs the bare upstream `deltares/sfincs-cpu` image (`TRID3NT_SFINCS_IMAGE`),
which has no worker entrypoint, so it writes neither manifest. The quadtree
dispatch (`TRID3NT_SFINCS_BUILD_IMAGE`, default `trid3nt-local/sfincs:latest`) is
the SFINCS lane this change touches, and it is the lane proven above.
