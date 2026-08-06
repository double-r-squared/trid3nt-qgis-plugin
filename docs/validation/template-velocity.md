# TEMPLATE VELOCITY LEDGER

Updated at EVERY template close-out (standing rule). Times = builder-agent
wall-clock (incl its own suite runs + live smokes) + the orchestrator
close-out (~10-15 min: slice, commit, deploy, log). Sequential execution
per NATE 2026-08-04.

## Landings to date (wall-clock, measured)

| Wave (ADR) | Templates landed | Builder time | Per-template |
|---|---|---|---|
| HEC-RAS landing (0109) | 1 (engine #11 archetype) | ~86 min (incl 1 resume) | ~86 min - engine-class |
| SCHISM landing (0118) | 1 (engine #12 archetype) | ~84 min (incl 1 resume) | ~84 min - engine-class |
| S-tier wave 1 (0120) | 1 + the rename (+ the hygiene lint + 16 fixes) | ~80 min (2 resumes) | ~40 min effective |
| Easy-four pt 1 (0122) | 1 (folding 3 board rows) | ~47 min | ~47 min - feature-build |
| Easy-four pt 2 (0123) | 3 | ~71 min | ~24 min - recipes pre-scoped |
| Triage-only waves (0121) | 0 (13 ground-truthed) | ~35 min | scoping overhead, amortized |
| SWMM network family (0124) | 2 (network import + dual drainage, live Houston municipal net) | ~66 min | ~33 min - triage bought 5 honest STOPs + the published-deck-runner unlock |
| HEC-RAS archetypes (0125) | 1 (levee_breach; rain-on-grid honest STOP) | ~3.0 h (incl 21-min triage wave + an upstream API drop + image rebuild) | ~3 h - engine-adjacent archetype w/ in-container mechanics pinning |
| SCHISM candidates (0126) | 0 (2 triaged: CORIE deferred, WWM_Duck STOP) | ~2.0 h (incl a WWM binary verification build + a live GOTM-free WWM+SCHISM coupling spike proving Hs) | triage + build-spike; both recipes ready, WWM coupling de-risked to a single GOTM blocker |
| SCHISM coupled_waves + GOTM leg (0131) | 1 (engine #12 second archetype; the GOTM build blocker resolved) | ~2.5 h (incl the GOTM cmake-shim de-risk + the faithful itur=3 Duck run + cross-shore V&V + image rebuild) | ~2.5 h - engine-class (a NEW build leg: GOTM 3.2.5 cmake shim -> pschism_WWM_GOTM_TVD-VL) + the template landing on the 0118 exemplar |
| hecras_flood_2d promotion (0140) | 1 (fresh-AOI 2D flood; discharges the 0127-0139 beta arc into a registered template) | ~42 min builder (authoring image build + pipeline wiring + 2 live fresh-AOI acceptances + ~45 offline gates) | ~42 min promotion-of-proven-machinery; the 8-experiment arc itself (~2 days) was the real cost, amortized over every future authored-mesh row |
| Landlab six-row wave (0141) | 6 (storm-ensemble, of-timeseries, dem-conditioning, lake-mapping, hacks-law, hand; first per-engine grind batch) | ~50 min builder (all exec-mode, shared composer boilerplate, 6 live Boulder-AOI smokes + 83 offline gates + 9 proof renders) | ~8 min/template - the true-S floor when recipes are board-scoped and no image builds are needed |
| ELMFIRE sensitivity wave (0142) | 3 landed + 5 honest STOPs w/ recipes (5 of 8 S labels overturned + 1 board framing corrected in triage) | ~56 min builder + ~4 min categories fixup (11 live solver runs, shared sensitivity spine, 52 offline gates) | ~19 min/landed; the STOP recipes convert 5 mislabeled rows into scoped M work (crown pair, spotting pair, transient-weather leg) |
| GeoClaw knob wave (0143) | 2 landed + 6 triaged (1 already-covered, 2 STOP, 2 defer-landable Lagrangian fold, 1 defer) via live clawpack-5.14.0 introspection | ~38 min builder (2 real Crescent City tsunami solves, initial-mass diagnostic ~6.5e10 both, 164 offline gates) | ~19 min/landed; triage keeps converting mislabels into scoped recipes instead of forced builds |
| Landlab quality fix wave (0145) | 0 new (2 NATE-flagged defect fixes: lake discrimination floors + condition_dem opt-in; overland defect withdrawn after relief render) | ~28 min builder (2 re-smokes incl Horsetooth real-lakes AOI, 35 offline gates, proofs regenerated w/ colored-relief variants) | quality-wave class; NATE spot-checks against relief renders = the working QA loop |
| Pelicun validation wave (0146) | 4 templates folding 7 rows + 2 rows as regression tests + 2 STOPs w/ recipes (incl 1 resume after an upstream API drop) | ~34 min builder wall (closed-form gates to 0.0008, 97 offline gates, 5 dock-exact chart proofs) | ~8.5 min/template - folding turns 11 rows into 4 well-knobbed surfaces; triage found the REAL Assessment API was never used (follow-on: port the legacy template onto it) |
| GeoClaw depth-COG revision (0144) | 0 new (NATE-flagged: 2 pre-existing rasterizer/grid-plan bugs fixed at source, ADR 0123 contamination cleared, 2 NEW deck defects surfaced honestly) | ~48 min builder (real fort.q forensics, 2 live re-smokes, synthetic AMR-flatten regression, 102 gates) | quality-wave class; the uniform-value + rectangle-shape checks are now permanent regression gates |
| GeoClaw knob activation (0148) | 0 new (stale-image root cause: parse_build_spec silently dropped post-0143 fields; windows now govern refinement; amr_regions input-gated) | ~51 min builder + 2 proof-split resumes | quality-wave class; HARD LESSON minted: offline-green != deploy-green for worker code - every worker wave ends w/ image rebuild + live smoke through the image |
| SWAN physics-knob wave (0147) | 2 templates + 9 rows folded into deck knobs + 2 STOPs w/ recipes (netCDF: outcome already delivered via .mat->COG) | ~86 min builder (SWAN 41.51 manual-verified knobs, image rebuild per the 0148 lesson, 2 live AOIs incl the 32% shelf-friction demo, 107 offline gates) | ~43 min/template but 11 of 12 rows resolved; the deep-vs-shallow dissipation finding drove honest default axis/metric choices |
| OpenQuake logic-tree fold (0149) | 0 new tools - 3 rows FOLDED as knobs on the existing openquake_psha (logic_tree=source_models/gr_uncertainty, UHS+multi-PoE maps; +1 real UHS parse bug fixed) + 1 validated STOP recipe (scenario GMF) | ~34 min builder (324-realization live run in 11s, 144 offline gates) | the leanest disposition yet: 4 rows, zero registry growth - folding beats minting when the surface already exists |
| GeoClaw raw-mesh emission (0150) | 0 new tools (NATE directive: AMR mesh = first-class per-run layer through the shared inundation seam, all 3 geoclaw templates; raw cell-edge grid, honest decimation policy, 100% of L4 vertices over the user window) | ~24 min builder (live re-smoke, 75 patches / 4786 lines / 0.27 MB, 86 offline gates) | feature-wave class; the raw-mesh proof style supersedes the 0148 patch-family abstraction |
| SWMM mechanism-comparison wave (0151) | 12 rows -> 5 knob-consolidated templates (zero STOPs - all mechanisms real in swmm-toolkit 0.17.0; 6 version-specific deck findings baked into authoring) | ~46 min builder (11 live knob paths, continuity <0.6% everywhere, 123 offline gates, 5 dock-exact overlay charts) | ~9 min/template-equivalent; the shared synthetic-comparison spine is the reusable pattern for mechanism-class rows |
| SFINCS knob-fold wave (0152) | 13 rows resolved, 0 new tools (3 code folds, 4 already-shipping honestly reported, 6 STOPs w/ recipes incl the HIGH-value QGIS-native mesh netcdf follow-on) | ~30 min builder (2 live solves incl the flood canary status=ok, 108 offline gates) | zero-registry-growth again; SFINCS's easy tier closes the 7th engine |
| MODFLOW package-validation wave (0153) | 3 rows -> 1 three-case template (+1 STOP: no mp7 binary AND no published reference values - both fallbacks honestly dead) | ~34 min builder (3 live mf6 6.7.0 V&V solves; Sokol delta 2.0e-11 m; HFB grid-independence <8.7e-6; 227 offline gates) | analytical-V&V class: every case ships with a computed-vs-reference number, the cheapest trust the product can buy |
| TELEMAC CAND-S wave (0154) | 9 rows: 1 physics knob fold (wind stress, live-proven 7.35 cm setup) + 1 already-covered doc + 4 overlap rows -> verified cross-engine routing doctrine in the adapter prompt + 2 STOPs w/ recipes + 1 standardize-on-GAIA decision | ~43 min builder (image rebuild per the 0148 law, live Eel River smoke pair, 99 offline gates) | zero registry growth; the overlap-doctrine landing is the fidelity ladder made retrievable |
| GeoClaw tail wave (0155) | 5 rows: 2 knob folds (Lagrangian drift gauges + onshore fgmax mask, 39% output cut) + 3 bookkeeping flips per the 0143 verdicts (+1 schema-declaration fixup at close-out) | ~42 min builder + fix resume (image rebuilt, live Crescent City smokes: 3 drifters 125-258 m, masked fgmax exact-agrees on common cells) | GeoClaw easy tier CLEARED |
| TELEMAC proof-honesty follow-up (0154 amend) | 0 rows (NATE caught the smoke opting into constant_ribbon; wind proofs regenerated from the DEFAULT nhd_area path: real variable-width banks, 131 m mean, setup 8.09 cm consistent; ribbon render kept as contrast) | ~10 min resume | proofs showcase the default path - now explicit in the norms |
| SCHISM tail wave (0156) | 4 rows: 1 template folding 2 (transport validation: TVD retains 95.7% vs upwind 90.7% = 2.16x mixing; mass drift -0.69%/-1.04% in bound) + 1 STOP (iharind = empirically-confirmed silent no-op without USE_HA) + 1 DOC | ~38 min builder (live product-path smoke, 70 offline gates) | SCHISM easy tier CLEARED; the itr_met/tvd.prop + ibc=0 findings are baked into the deck authoring |

## Working rates (sequential, incl close-out share)

- TRUE-S (knob/pre-scoped recipe): ~30-50 min each
- FEATURE-BUILD M (new worker branch/parser/postprocess): ~45-90 min each
- ENGINE-CLASS (new worker/image/contract family): ~1.5-2 h each
- TRIAGE overhead: ~30-45 min per unscoped batch of ~10 rows (buys the
  recipes that make the 24-min rate possible - the 0123 evidence)

## Projections (sequential; revised at every close-out)

- SWMM network family (in flight, 7 interdependent, 2 heavy): ~4-6 h
- HEC-RAS x2 + SCHISM x2 (signed, scoped): ~3-5 h
- Blocked-13 (need machinery waves first): machinery ~2-4 h each family,
  then template rates apply
- Easy tier (~98 CAND-S, triage-first at wave-2's honest conversion
  uncertainty): ~55-90 h of loop wall-clock
