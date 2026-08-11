# ADR 0220 - OpenQuake site-model batch: discrete NEHRP site-class amplification fold

Date: 2026-08-11

Status: accepted

## Context

The module-coverage-board OPENQUAKE "Site response / site amplification model"
section (adjacent to the secondary-perils rows on the ML shortlist) holds three
site-model rows:

- `site_model_vs30_amplification_build` -- ALREADY FOLDED (ADR 0182) into
  `openquake_psha` knob `vs30_compare`: a CONTINUOUS-Vs30 A/B that sweeps a
  GMPE's built-in Vs30 site term (reference rock 760 vs a softer reference Vs30)
  and overlays two single-site hazard curves. The board note flags the remaining
  slivers (a true per-site fetched-Vs30 `site_model.csv` MAP, and the discrete
  amplification table) as deferred.
- `discrete_amplification_function_apply` [CAND-M] -- apply a DISCRETE
  site-class amplification-function table (ampcode / EC8-or-NEHRP class ->
  amplfactor) INSTEAD of a continuous Vs30 GMPE term.
- `conterminous_us_nshm_classical_run` [CAND-L] -- the license-gated GEM-USGS
  NSHM mosaic source model (a separate, larger row, out of this batch's scope).

The engine is oq 3.25.1 with the GEM demos + qa_tests_data in-package. The
0149/0164/0182 folding precedent governs: a genuinely-new *calculator* earns a
new tool; a same-question-class *mechanism variant* rides as a knob on the
existing template.

Published-first anchor (no guessing the deck format): the OpenQuake qa_test
`classical/case_55` is the canonical amplification-convolution deck. Its
`amplification.csv` (`vs30_ref` header comment + `ampcode, PGA, sigma_PGA` rows),
`soil_intensities`, `amplification_csv` + `amplification_method = convolution` +
`vs30_tolerance`, and a `site_model` carrying an ampcode per site were read
byte-for-byte from the installed engine. OpenQuake carries the NEHRP site classes
natively (`openquake.hazardlib.site.ampcode_dt` codes A/B/C/D/E). The published
US discrete site-class amplification factors are the ASCE 7-22 / FEMA P-2078
site coefficients (Fpga for PGA): A 0.8, B 0.9, C 1.3, D 1.6, E 2.4, relative to
the class B/C reference rock (760 m/s).

## Decision

Fold `discrete_amplification_function_apply` into `openquake_psha` as the knob
`nehrp_amp_class` ("C" / "D" / "E"), the DISCRETE-table sibling of `vs30_compare`.
When set, a best-effort/non-fatal overlay runs the classical hazard curve at the
AOI centroid on the run's synthetic demo area source through the OQ
amplification-convolution path, once for the unamplified 760 m/s reference rock
(factor 1.0) and once per soft class C/D/E, each carrying the published Fpga
factor in a one-row `amplification.csv` and its ampcode in a one-site
`site_model.csv`. The four curves overlay in one legended log-log figure and the
caption reports the amplification factor for the highlighted class. Deterministic
median site coefficient (sigma 0), narrated as such.

Distinctness vs `vs30_compare` (the folding distinctness rule): `vs30_compare`
sweeps a GMPE's CONTINUOUS Vs30 term (one reference Vs30 vs another); this knob
convolves a DISCRETE published site-class TABLE decoupled from the GMPE Vs30 -
the exact "discrete site-class table instead of a continuous Vs30 GMPE term"
distinction the board row draws. It also answers the "soil-class comparison
(NEHRP classes)" question directly. It is NOT a new calculator (still
`calculation_mode = classical`), so a knob, not a tool.

Mechanics live in the shared `_local_oq.py` (the same local-`oq`-subprocess lane
as the `vs30_compare` A/B): `NEHRP_FPGA` / `NEHRP_VS30` published tables +
`render_site_model_csv` / `render_amplification_csv` / `render_classical_amp_job_ini`.
The composer helper `_emit_nehrp_amp_chart` mirrors `_emit_vs30_ab_chart`.

## Consequence

- One knob, no new registered tool: `openquake_psha` gains `nehrp_amp_class`.
  Registry count unchanged; coded-tools delta 0. No new categories / catalog /
  door-dissolution entries (a knob on an already-surfaced template).
- Live (Salt Lake City valley AOI, soft basin soil vs Wasatch rock, oq 3.25.1
  local subprocess ~8 s/curve): PoE monotone rock < C < D < E; at 0.556 g PGA
  class C 1.57x / D 1.96x / E 2.28x the reference-rock exceedance probability
  (ordering tracks Fpga 1.3 < 1.6 < 2.4; rock baseline 1.0). Proof
  `docs/proof/templates/openquake_psha_nehrp_amplification_chart.png`.
- Vs30 source decision: NO Vs30 raster fetcher is added in this batch. The
  discrete-class knob is intentionally Vs30-fetch-FREE (the user names a NEHRP
  class; the amplification is the published table, not a fetched grid). The
  deferred true per-site fetched-Vs30 `site_model.csv` MAP row (the second board
  sliver) still wants a real Vs30 source; the promotable path is the existing
  Wald-Allen slope-derived Vs30 field in the ADR 0164 secondary-perils machinery
  (already a per-site raster proxy), OR the USGS global Vs30 slope grid as a new
  fetcher spec. Deferred with a design note (see final report); not built here
  because it needs the heavier multi-site rasterized amplification-ratio MAP +
  its own honest cross-dataset provenance, and it partially overlaps the folded
  `vs30_compare`.
- Remaining board rows in this section: the gridded-Vs30 MAP sliver (deferred,
  above) and `conterminous_us_nshm_classical_run` (out of scope, license-gated).
