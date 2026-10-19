# strip_energy fixture

A solved one-model GWF + GWE simulation (MODFLOW 6.7.0, one 1x3-cell strip
DISV grid shared by both models through the GWF6-GWE6 exchange) produced by
`trid3nt_server/workflows/modflow/authoring/simulation.py` serialize on
2026-10-18. Banner: synthetic - the strip's geometry and values are hand
stated for reader tests; no real aquifer, no claim about any field.

Contents are the run directory as written and solved, including `gwe.ucn`
(TEMPERATURE) and `gwf.hds` (HEAD), the two files the reader tests read
through `modules/outputs.py`.
