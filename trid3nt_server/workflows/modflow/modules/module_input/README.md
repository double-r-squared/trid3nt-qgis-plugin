# `modules/module_input/` - the bodies' input vocabulary

One JSON per exposed body, its `keywords` extracted from mf6's own definition
files by `scripts/extract_modflow_dictionary.py` and committed. The keyword rows
are never hand edited: a transcribed keyword table is a second answer to a
question the engine already answers, and the extractor refuses an unreadable
default rather than guessing one. The `inputs` section is authored - each
template's values by name, with their prose, type and unit - and the extractor
writes it back beside the fresh rows.

A row carries what the DFN says about one keyword - its raw name, the
identifier `<PACKAGE>_<VARIABLE>` spells it by, its type (a scalar word, or
ARRAY where the value is a constant, one per layer, or the NAME of an input
holding the grid, or STRUCT for a record, recarray or keystring block), its
size, its help, its block as the rubrique, its engine default, and whether it
is mandatory. Only `optional true` is optional - a bare `optional` line marks K
and ICELLTYPE, which mf6 refuses a run without - and a required entry carries
no default: its DFN `default_value` is flopy's illustration, not a value mf6
uses, except on a required flag (READASARRAYS), the package's own tag. An entry whose own description says mf6 runs without it stated
(a stated default, or a waiver where it is unread) is not mandatory, and neither is one shaped by an optional dimension alone (CSUB's period list over MAXSIG0).
`modules/module.py` reads a file here into the wrapper's slots.

## Files

| file | keywords | what it is |
| --- | --- | --- |
| `gwf.json` | 511 | The groundwater-flow body: every `gwf-*` package plus the simulation's TDIS and IMS and the OBS utility. |
| `gwt.json` | 262 | The groundwater-transport body: every `gwt-*` package plus TDIS, IMS and OBS. |
| `gwe.json` | 233 | The groundwater-energy body: every `gwe-*` package plus TDIS, IMS and OBS. |
