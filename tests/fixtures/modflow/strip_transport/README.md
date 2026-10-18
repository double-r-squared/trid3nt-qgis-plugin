# strip_transport fixture

A solved one-model GWF + GWT simulation (MODFLOW 6.7.0, one 1x3-cell strip
DISV grid shared by both models through the GWF6-GWT6 exchange) produced by
`trid3nt_server/workflows/modflow/authoring/simulation.py` serialize on
2026-10-18. Banner: synthetic - the strip's geometry and values are hand
stated for reader tests; no real catchment, no claim about any field.

Contents are the run directory as written and solved (`mfsim.nam` names the
gwf and gwt models and the `gwf.gwfgwt` exchange; both name files name
`mesh.disv`), including `gwt.ucn` (CONCENTRATION) and `gwf.hds` (HEAD), the
two files the reader tests read through `modules/outputs.py`.

The strip is authored with quad cells staged beside a triangular display
(`_parse_2dm` splits E3T quads); tests rebuild the quad grid by monkeypatching
`mesh_nodes` (test scaffolding, like `staged_beside`).
