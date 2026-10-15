# `modules/module_input/` - the bodies' input vocabulary

One JSON per exposed body, extracted from mf6's own definition files by
`scripts/extract_modflow_dictionary.py` and committed. Never hand edited: a
transcribed keyword table is a second answer to a question the engine already
answers, and the extractor refuses an unreadable default rather than guessing
one.

A row carries what the DFN says about one keyword - its raw name, the
identifier `<PACKAGE>_<VARIABLE>` spells it by, its type (a scalar word, or
ARRAY where the value is a constant, one per layer, or the NAME of an input
holding the grid, or STRUCT for a record, recarray or keystring block), its
size, its help, its block as the rubrique, its engine default, and whether the
DFN marks it not optional and gives it no default (mandatory). `modules/module.py`
reads a file here into the wrapper's slots.

## Files

| file | keywords | what it is |
| --- | --- | --- |
| `gwf.json` | 508 | The groundwater-flow body: every `gwf-*` package plus the simulation's TDIS and IMS. |
| `gwt.json` | 259 | The groundwater-transport body: every `gwt-*` package plus TDIS and IMS. |
| `gwe.json` | 230 | The groundwater-energy body: every `gwe-*` package plus TDIS and IMS. |
