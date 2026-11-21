# PEST++ study files, recorded

Every file here was written by pyEMU 1.7.0 or PEST++ 5.2.27 in the `trid3nt-local/pestpp` worker
image, over the published Freyberg MF6 model (pypest/pyemu tag v1.7.0,
commit f7c98319e39ea68dacb74d3d96aa3ff7d4096014, `examples/freyberg_mf6`). The observations are the
base model's own outputs: a synthetic benchmark, read here for file shapes only.

- `study/` - the PstFrom interface the engine stages: `pestpp.pst` (five constant multipliers,
  725 observations), its five templates, two instruction files and the `mult/` files the templates
  write.
- `ies/` - from a pestpp-ies run of that study (10 realizations, 3 iterations, 7 agents):
  `pestpp.phi.actual.csv`, the ensembles `pestpp.0.par.csv` and `pestpp.3.par.csv`, the base
  realization's residuals `pestpp.3.base.rei`, and the worker's `pestpp_metrics.json`.
- `sen/` - `pestpp.msn`, the Method of Morris summary of a pestpp-sen run of the same study.
- `failure/` - `pestpp.rec` and `pestpp_metrics.json` of a pestpp-ies run of the same study with
  `mfsim.nam` removed, so every trial's model failed.
- `study_v2/` - the same control file written by pyEMU's `Pst.write(version=2)`: `pestpp.pst` and
  its two model-file tables (the parameter, group and observation tables it also writes are not
  kept; the engine does not read them).
- `defaults/` - one tiny parse-only study (`debug_parse_only`, one parameter written through `m.tpl`
  into `m.in`, one observation read off `m.out` through `m.ins`, its tables in the four CSVs) and
  the records pestpp-ies 5.2.27 wrote of it: `control.pst` states nothing (`control.rec` prints
  every control-data variable at the value it takes unstated), `pestpp.pst` states `noptmax 1`
  (`pestpp.rec` prints every option so; an unstated noptmax of 0 resets max_run_fail), and
  `names.pst` states, with `forgive_unknown_args`, every option the manual names whose value it
  can state back - the value the binary prints, else the manual's (`names.rec` warns each name
  the binary does not read). Left out: the options naming a file this study does not hold, the
  two whose printed or manual value the binary refuses back (`opt_direction`,
  `mou_simplex_factors`), and `ies_save_binary`, which the binary reads as `save_binary`. The
  dictionary extractor reads them all.
- `calibrate/` - from the `calibrate_modflow_check` canary (a SYNTHETIC zero-fetch aquifer held to a
  REAL USGS well): `deck/` and `manifest.json`, the deck and case section the finished water-table run staged;
  `study/`, the pestpp-ies study over it (`pestpp.pst` as the template wrote it, `pestpp.phi.actual.csv`, the
  last ensemble `pestpp.3.par.csv` and the base realization's residuals `pestpp.3.base.rei`, one well); and
  `calibrated_study_heads.csv`, the head the calibrated MODFLOW run itself wrote at that well's cell.
- `glm/` - from the pestpp-glm study of the `pestpp_calibrate_des_moines_glm` canary (a fetched MODFLOW water table,
  NPF_K and SFR_RHK against 15 published steady head targets, noptmax 3, 7 agents): `pestpp.pst` as the template
  wrote it, the objective per iteration `pestpp.iobj`, the parameters per iteration `pestpp.ipar`, the best set
  `pestpp.par` and the last iteration's `pestpp.3.par`, the best set's residuals `pestpp.rei` and the last
  iteration's `pestpp.rei3`, the Jacobian `pestpp.jcb` (PEST's own binary layout: every name fits it), and the
  worker's `pestpp_metrics.json`.
- `glm_freyberg/` - `pestpp.jcb` of a serial pestpp-glm run (noptmax 1) over the `study/` interface with its model:
  PEST++'s long-name binary layout. The targets are the base model's outputs, so it is read for its layout only.
- `des_moines_deck/` - the deck of the `pestpp_calibrate_des_moines` canary's finished MODFLOW run (a fetched one-layer
  water table, 12,880 DISV cells of the 100 m mesh), as its 2026-11-21 study staged it (study 01M7YDRJJ1KVRC71ZBYT54J63F):
  `mesh.disv.gz`, `gwf.npf.gz` and `gwf.sfr.gz` (gzipped, unchanged), `gwf.nam` (the `OBS6` line the study adds removed), `mfsim.nam`
  and `trid3nt.tdis`; and `pyemu_pilot_points.json`, what pyEMU 1.7.0 (flopy 3.10.0) computes over that grid in the
  `trid3nt-local/pestpp` image at a 1000 m spacing: the points `get_zoned_ppoints_for_vertexgrid` places, the ordinary
  kriging factors `OrdinaryKrige.calc_factors` (20 nearest, an ExpVario of contribution 1 and range 1000) gives every
  40th cell, and the GeoStruct covariance of the points scaled to a variance of 0.25 as `geostatistical_draws` scales
  it (log10 bounds 0.1 to 10 read as 4 standard deviations).
- `pilot_points/` - from the pestpp-ies study (01M7Z7VD1SH6KH3WJK3AS9Y1GB) of the
  `pestpp_calibrate_des_moines_pilot_points` canary: NPF_K laid by 221 pilot points at pyEMU's default over the run's
  mesh size, SFR_RHK one multiplier, 20 realizations drawn by PEST++ from the study's parcov, 3 iterations:
  `pestpp.0.par.csv` (the prior ensemble PEST++ drew), `pestpp.3.par.csv` (the last) and `pestpp.phi.actual.csv`.
- `telemac_points/study_points.txt` - the formatted results file TELEMAC-2D printed at the remarkable point of one trial of
  the Missouri study (pestpp-glm 01M7XZ64B9YPR52N9B7P87WP4W, the Hwy 92 gauge at its node), run in the
  `trid3nt-local/pestpp-telemac` image through `telemac_trial.py`: one block per printed variable, free surface 293.2002 m.
