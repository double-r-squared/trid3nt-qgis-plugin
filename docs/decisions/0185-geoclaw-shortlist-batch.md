# ADR 0185 - GeoClaw shortlist grind batch 3 (Thacker V&V + fgout animation + front-away trio triage)

Status: Accepted
Date: 2026-08-08

## Context

The M/L sign-off shortlist (`docs/validation/ml-signoff-shortlist.md`) lists five
GeoClaw rows: two "ready NOW" (Thacker analytic V&V #9, fgout animation #10) and
three "front-away" (multi-subfault dtopo #18, single Okada dtopo #19, parametric
Holland Ike surge #20). Batch 3 triages all five against the LIVE GeoClaw surface
and dispositions each honestly.

GeoClaw surface as of this batch (the machinery the rows land on):

- `geoclaw_inundation` (+ the `amr_regions` / `regional_manning` /
  `gauge_timeseries` / `lagrangian` / `fgmax_mask` knobs) - the shared
  fetch -> stage -> solve -> postprocess chain (`model_geoclaw_inundation`).
- `geoclaw_storm_surge` - the ADR 0168 parametric-Holland surge deck (Holland-1980
  wind + pressure from a storm track; `wind_drag_law` none|garratt|powell).
- The 0144 finest-AMR-wins rasterization; the 0150 `mesh.geojson` emission; the
  0148/0158 image-rebuild + provenance laws; the 0158 strict `parse_build_spec`
  allowlist (`geoclaw-spec-3`).
- Worker deck author `services/workers/geoclaw/setrun_builder.py`: renders setrun
  with an fgmax monitor grid (`fgmax_tools`), a `maketopo.py` single-subfault Okada
  dtopo synth, a `render_storm_file` Holland track, a `qinit.xyz` dam-break column.
- Postprocess rasterizes fort.q AMR frames -> a peak-depth COG + per-frame COGs
  (the Phase-1 scrubber animation ALREADY exists off the fort.q frame cadence).

## Decision

### Row #20 (`parametric_holland_wind_surge_ike`) - VERIFIED SUBSUMED by ADR 0168

The parametric-Holland Ike surge row is ALREADY landed. ADR 0168 built the real
GeoClaw surge front (`geoclaw_storm_surge`) on the published NHC ATCF `bal092008`
Ike best track with a live docker smoke: peak coastal surge surface **5.4 m** on
the right (east) of the track near Bolivar (-94.21, 29.56), peak onshore inundation
3.77 m, coastal gauge peak 1.79 m - in the observed magnitude class (~4.5-6 m / 15-20
ft observed at Bolivar) AND on the physically-correct right-of-track side. The
board already carries it as `[LANDED]` (module-coverage-board.md line 530). The
drag-law A/B (Garratt vs Powell) met the ADR 0143 non-noop gate (peak surge 5.096
vs 4.953 m, delta 0.14 m).

Disposition: FLIP the shortlist row #20 from "one small front away" to LANDED (no
new code - this batch reconciles the stale shortlist row against the already-landed
0168 reality). Registry / EXPECTED_TEMPLATES unchanged.

### Rows #18 / #19 (Okada / finite-fault dtopo authoring) - STOP with recipe

The tsunami CONSUMPTION path already exists: `geoclaw_inundation(tsunami_dtopo_uri=)`
threads a prescribed `dtopo.tt3` into the deck (`GeoClawBuildSpec.dtopo_file`), and
`render_maketopo_dtopo` synthesizes a SINGLE-subfault Okada dtopo from `source_magnitude`
(Wells-Coppersmith Mw-scaled length/width/slip, one `dtopotools.SubFault`) when no
file is staged. What is MISSING for #18/#19 as first-class question classes is the
dtopo AUTHORING front:

- #18 (`multi_subfault_dtopo_from_finite_fault_model`): no capability to read a
  PUBLISHED multi-subfault finite-fault model (USGS `.fsp`/SRCMOD, or NOAA SIFT unit
  sources) and assemble a `dtopotools.Fault` of N subfaults -> `dtopo.tt3`. The
  worker's `create_dtopo_xy` path is single-subfault only.
- #19 (`okada_single_subfault_dtopo`): the single-Okada mechanism IS the worker's
  synthetic default, but it is not surfaced as a first-class dtopo-authoring tool
  (a user cannot ask "build me a dtopo from this one rectangular fault" and get a
  reusable dtopo layer + Okada deformation chart).

This is the board's "GeoClaw Okada-dtopo (#18/#19, 3)" small front (line 134). It
needs a real earthquake-source authoring capability beyond the demo Okada, so it is
STOPPED here (consistent with the 0143 okada-1d STOP: real seismic-source machinery
is disproportionate to fold into a template without its own front).

Recipe to land the Okada-dtopo authoring front:

1. `fetch_finite_fault_model(event)` fetcher (paradigm-B): resolve a published
   finite-fault model (USGS ComCat `finite-fault` product `.fsp`, or a NOAA SIFT
   unit-source combination) for a named US event (1964 Alaska/Prince William Sound
   is the #18 anchor) -> a normalized subfault table (lon/lat/depth/strike/dip/rake/
   slip/length/width per subfault). Citations to NATE (verified real) BEFORE build,
   per the US-cases paper-first doctrine.
2. `build_okada_dtopo` primitive (or a worker helper that generalizes
   `render_maketopo_dtopo`): assemble `dtopotools.Fault(subfaults=[...])` from either
   ONE user rectangle (#19) or the fetched N-subfault table (#18), `create_dtopo_xy`
   over the union box, write `dtopo.tt3`, and emit the seafloor-deformation field as
   a product raster + an Okada uplift/subsidence chart (honest: model, not observed).
3. Thread the authored dtopo into `geoclaw_inundation(tsunami_dtopo_uri=)` for the
   run-up (the consumption path already works). Live anchor: 1964 Alaska (real US),
   V&V against the published NGDC/NCEI runup catalog for Crescent City / Prince
   William Sound where our fetchers cover it.
4. Corpus.yaml + model-free `retrieve_visible_tools(prompt, None, 8)` + categories.py
   for the new tool(s); strict allowlist stays clean (no new build_spec field - the
   dtopo rides the existing `dtopo_file`).

### Rows #9 (Thacker) + #10 (fgout) - SCOPED with recipe, NOT landed this session

Both are genuine builds that each require the full worker-change -> image-rebuild
(0148/0158 absolute-path + provenance + smoke laws) -> live docker solve -> proof
render -> showcase-case seed cycle. Landing two brand-new engine features to the
live-verified quality bar in one session risks shipping unverified engine code,
which the honesty floor + the "flood canary after LARGE changes" + clean-as-you-go
doctrines forbid. They are dispositioned SCOPED here with ready-to-execute recipes;
the next GeoClaw session lands them fast off these recipes. Registry /
EXPECTED_TEMPLATES therefore UNCHANGED (231 / 73) - no half-built template
registered.

#### #9 Thacker analytic SWE V&V (`thacker_analytic_swe_validation`) recipe

Thacker (1981) closed-form: a frictionless paraboloid bowl `B(r) = -h0 (1 - r^2/a^2)`
with an initial planar-tilted free surface oscillates with EXACT period
`T = 2*pi / omega`, `omega = sqrt(8 g h0) / a`; the shoreline (wet-dry front)
oscillates sinusoidally with amplitude set by the initial tilt, and the surface stays
planar for all time. This is the wetting-drying V&V gold standard.

- This is a FULLY SYNTHETIC domain (no `fetch_topobathy` - the whole pipeline
  otherwise assumes a real AOI DEM). Add a `scenario="thacker"` (or a dedicated
  `render_setrun_thacker` + `maketopo` branch) to `setrun_builder.py`: author a
  paraboloid-bowl topo file + a qinit that sets the analytic tilted free surface at
  t=0, frictionless (`manning_n=0`), a closed (wall) domain, `sim_duration_s ~ 2-3 T`.
  Extend the strict allowlist (`geoclaw-spec-3 -> spec-4`) with the bowl params
  (`bowl_a_m`, `bowl_h0_m`, `bowl_eta_amp`, run in a synthetic planar CRS - Thacker
  is dimensionless-ish; pick metres). Rebuild the image (absolute paths, provenance,
  smoke) per 0148/0158.
- Postprocess: extract x-axis vs diagonal gauges, compute the numerical period +
  amplitude + shoreline position, and compare to the closed form. State the deltas:
  period error, amplitude decay (numerical dissipation), shoreline-position error,
  and mass/momentum conservation over the run. This is a V&V gate
  (`model_validation` category), CHART-LED: ONE figure overlaying numerical vs
  analytic surface at several phases with the deltas in the caption.
- Emission: per the synthetic-fixture rule, emit CHARTS/SCALARS only (or a raster on
  a NEUTRAL background with the caption stating it is a synthetic paraboloid bowl,
  NOT a geographic AOI). No Esri basemap (non-geographic). Overlay the AMR mesh
  wireframe in any field render per the mesh-in-proofs norm.
- Template `geoclaw_thacker_validation` (question CLASS = "does the wet-dry SWE+AMR
  solver conserve mass/momentum vs Thacker's exact bowl solution", NOT a place):
  corpus.yaml + `retrieve_visible_tools(prompt, None, 8)` + categories.py
  (model_validation) + `tools/__init__.py`; bump registry 231 -> 232 +
  EXPECTED_TEMPLATES 73 -> 74. Src: clawpack `examples/tsunami/bowl-radial`.
- Note the US-only doctrine: Thacker is a non-US idealized fixture kept ONLY as a
  V&V-doctrine cross-check (shortlist line 150), never as a hazard target.

#### #10 fgout smooth-animation frames (`fgout_animation_frames`) recipe

fgout = fixed-grid output interpolated at REGULAR time intervals, decoupled from the
AMR patch cadence -> SMOOTH animation frames (vs the coarse fort.q frame cadence the
current scrubber uses).

- `setrun_builder.py`: add an fgout block analogous to the fgmax block
  (`from clawpack.geoclaw import fgout_tools`; `fgout = fgout_tools.FGoutGrid()`;
  `fgout.point_style=2`; a uniform grid over the AOI at the AOI ambient dx;
  `fgout.output_style=1`, `fgout.tstart`/`tend`/`nout` from `sim_duration_s` +
  `output_frames`; `fgout.output_format='binary32'`; `fgout.file_prefix='fgout0001'`
  wait - `fgout_grid.append(fgout)` on `rundata.fgout_data`). Emit for tsunami +
  surge. Strict allowlist gains nothing new if reusing `output_frames`; else add an
  `fgout_frames` field (`spec-3 -> spec-4`) + rebuild the image per 0148/0158.
- Entrypoint: the fgout binaries (`fgout0001.b0001..bNNNN` + `.q` headers) land in
  `_output/` alongside fort.q; stage them into the downloaded output dir.
- Postprocess: read fgout frames via `fgout_tools.FGoutFrame` (uniform grid, so NO
  AMR flatten needed - a direct `h`/`eta` array per frame) and rasterize each to a
  per-frame COG on the SAME depth-frame convention `model_geoclaw_inundation`
  already emits (reuse `rasterize_frame_to_grid` -> the scrubber animation group).
  The fgout frames REPLACE the fort.q-derived animation frames when present (smoother
  cadence, uniform resolution); fort.q stays the peak-depth source.
- Demonstrate on the Crescent City tsunami deck (the landed AMR/Manning/fgmax smoke
  AOI). State: frame count (`output_frames`), cadence (`sim_duration_s/output_frames`
  s/frame), and that frames are SMOOTH (uniform-grid interpolated, one resolution)
  vs the fort.q AMR-patch baseline. Proof: a filmstrip of N fgout frames + a
  fort.q-vs-fgout cadence comparison note.
- Template `geoclaw_fgout_animation` (question CLASS = "smooth uniform-grid flood/
  tsunami animation at fixed times"): corpus + categories + `tools/__init__.py`;
  bump registry + EXPECTED_TEMPLATES. Src: clawpack.org/fgout.html +
  `examples/tsunami/chile2010_fgmax-fgout`. The combined fgmax+fgout row (#495) and
  the netcdf-transect row (#507) fold onto this same fgout machinery afterward.

## Consequence

- Row #20 reconciled to LANDED (already-landed 0168 reality; shortlist row was
  stale). Rows #18/#19 STOPPED behind the Okada-dtopo authoring front (recipe
  above). Rows #9/#10 SCOPED with ready-to-execute recipes (not landed this
  session - no unverified engine code).
- Registry pin UNCHANGED: 231. EXPECTED_TEMPLATES UNCHANGED: 73. No new tools, no
  pin bumps, no strict-allowlist changes - the tree stays clean.
- Doc-only edits: this ADR, the shortlist row dispositions, the board batch-3 note.

## Not done / future folds

- The Okada-dtopo authoring front (#18/#19) - a paradigm-B `fetch_finite_fault_model`
  + a `build_okada_dtopo` primitive; 1964 Alaska anchor.
- Thacker V&V (#9) - the synthetic paraboloid-bowl deck path + closed-form
  comparison (the first fully-synthetic GeoClaw fixture).
- fgout smooth animation (#10) - the `fgout_tools` uniform-grid frame source feeding
  the scrubber; then the combined fgmax+fgout and netcdf-transect folds.
