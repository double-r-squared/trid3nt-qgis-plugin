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
