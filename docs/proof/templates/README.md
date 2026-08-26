# Template proof renders

One render set per landed workflow template. These are the AS-SEEN-IN-QGIS
proofs: layers composited over the Esri World Imagery basemap approximating the
QGIS canvas, charts rendered exactly as the plugin chart dock draws them. When a
template is corrected, its proofs are regenerated and OVERWRITTEN in place under
the same names.

Debug renders (gradient relief spot-checks and similar) do NOT live here -
they are on-demand-only, generated when NATE asks, and go to a tmp folder
(/tmp/trid3nt_debug_renders/).

Audit folder: NEVER cleaned or pruned without NATE's explicit say-so.

## The layout: `<template>/<variant>/`

    docs/proof/templates/<template_name>/<variant>/<filename>

Filenames are unchanged - they are cited by name in ADRs, evidence JSONs and the
module-coverage board - and they still carry the template prefix, so a file that
escapes its folder is still identifiable. What moved is only the folder, and the
folder carries the two facts every reader was parsing out of the name anyway.

FOUR variants, and no more. A fifth would be a category nobody agreed on:

| variant | what it is |
| --- | --- |
| `coarse` | the default-resolution canary run. The baseline. |
| `refined` | the same question on a finer mesh. The pair that makes a resolution-sensitivity claim measurable. |
| `postmigration` | the same question re-run after a refactor, to show the representation changed and the numbers did not. |
| `addendum` | a proof that is none of those three: a gate-card walkthrough, a release-point acceptance case, a one-off diagnostic kept because it settled something. |

`trid3nt_server/testing/proof_paths.py` is the ONE place that builds these paths.
`canaries.evidence_path`, `scripts/render_selafin_animation.py` and the drive
scripts all ask it rather than joining their own; `render_all_layers_proof`
inherits the folder from the evidence JSON it renders from, so panels land beside
the run that produced them. An unknown variant REFUSES rather than quietly
creating a fifth folder.

The per-engine showcase KEEP-LIST (the delete-on-whim carve-out) points at these
folders: a template directory is the unit that is kept, so "retain the two most
complex showcase cases per engine" is now a statement about directories rather
than about a filename prefix.

Files still loose at the top level are the one-off physics-validation renders for
behaviours that are not templated workflows (individual solver checks, mesh
experiments, engine spot-probes). They are out of scope for this scheme by
design, not by oversight.

## Standing notes on individual sets

### ARTEMIS produces no animation GIF - by physics, not by omission

`artemis_harbor_agitation` is the one TELEMAC template whose proof set has no
`*_animation.gif`, and that is correct rather than incomplete. ARTEMIS is the
phase-resolving elliptic mild-slope (Berkhoff) solver: it solves a boundary-value
problem for a single monochromatic sea state and returns ONE field, the
steady-state agitation coefficient Kd. There is no time evolution to animate -
the run's own `completion.json` reports `ntimestep: null` and `duration_s: null`,
because the deck has no simulation clock at all.

The artemis proof set therefore ends at the peak frame
(`artemis_harbor_agitation_peak_frame.png`), which is the whole answer rather
than one sample of it. An audit that flags the missing GIF as a gap has found
the physics, not a hole in the record. The day ARTEMIS is driven across a
SPECTRUM of periods, the animation to render is Kd versus period - a sweep, not
a time series - and it would be a new artifact, not this one restored.

### Coastal runs are wet at t=0 by construction - but check the datum

NATE asked whether land being wet at the start of a coastal run is correct. The
mechanism is a correct cold start: the deck imposes a CONSTANT ELEVATION initial
free surface equal to the first boundary-series value, so every node whose bed
sits below that stage is wet at t=0. Frame 0 of the coastal SELAFIN is flat to
0.000e+00 standard deviation, and the wet set is exactly `{bed < initial stage}`
- no wet-mask threshold is painting anything.

What was NOT correct, until 2026-08-25, was the stage that cold start ran from.
The CO-OPS series is reported on a tidal datum (MLLW) and the bed is NOAA
DEM_all, which over US coasts serves the NCEI 1/9 arc-sec CUDEM tiles its own
catalog declares NAVD 88. `datum_offset_m` defaulted to 0.0, so the two were
never reconciled and the whole water column sat 0.232 m high at Apalachicola:
8220 nodes (12.0 km2) of marsh cold-started wet that should have been dry,
15725 of them (31.7 km2) above MHHW - land above the highest normal tide. The
offset is now derived per station from the gauge's own published datum table.

Any coastal proof render made BEFORE that fix overstates the wet extent. The
current canary reports `datum_offset_m: -0.232` and `sl_peak_m: 2.613` (NAVD 88)
against the pre-fix `2.845` (MLLW); a render whose metrics show a zero offset is
a pre-fix artifact and is kept as history, not as the current answer.

### The do-sag lever gap is closed - both cohort animations now run at 0.333 min

The 2026-08-25 river-dye pass recorded that `telemac_do_sag` could not be made
denser because its `PARAMS` declared no `output_interval_min`, though the deck
writer it calls has accepted one all along. That gap is now closed by the
declaration alone: `do_sag.py` gains the `Param` (bounds 0.1-1440 min, USER
door) and passes it into its `Physics("waqtel_o2", ...)` slot, which the
skeleton folds into the same `write_reach_deck(output_interval_min=...)`
keyword river-dye already used. No step, facade, worker or deck-writer code
changed - the workflow-only law held.

The refined do-sag canary consequently went from 7 frames to 31 over the same
600 s window. `refined_animation_frame_evolution.json` is the check that the
extra frames are worth their bytes: every consecutive pair of frames differs,
in both cohort GIFs, so the cadence bought new field data rather than repeats.

### The coastal inundation product SPLIT - two layers, one run (2026-08-25)

The peak raster used to be per-node max WATER DEPTH over every frame including
t=0, with no subtraction of the initial water line, so the permanently submerged
bay floor rendered in the same "inundation depth" ramp as land the tide actually
reached. The scalar and the picture disagreed: `flooded_land_km2` had always done
the `bed > init_wl` discrimination and the raster had not.

The run now publishes TWO layers and the roster is deliberate:

  * `coastal_inundation.tif` - PRIMARY. Peak depth over land that was DRY at t=0,
    on the same discrimination the scalar counts. This is the planning answer.
  * `coastal_depth_max.tif` - CONTEXT. The total peak water depth including the
    permanent bay: where the water is, rather than where the tide went.

Every scalar is unchanged; `inundation_peak_depth_m` and `inundation_basis` are
ADDED. A coastal proof render made before 2026-08-25 shows one layer where the
current run shows two, and its single raster is the CONTEXT one under the
primary's old name.

### Known open defect: the coastal chart renders blank

`coastal_tidal_surge_chart_coastal_stage_vs_inundation.png` and its `_refined`
twin are empty axes. That is chart-dock-exact: the persisted spec is a
horizontal `bar` mark carrying three real values, and the plugin's
`render_spec` draws nothing for it. The blank is shipped deliberately rather
than worked around - the defect is upstream in the dock's bar handling and is
its own job. The numbers themselves are in the run's `metrics.json`.
