# `modules/module_input/` - the PEST++ control options

One JSON, `pestpp.json`, its `keywords` extracted by `scripts/extract_pestpp_dictionary.py` and
committed. The rows are never hand edited. The source is the PEST++ 5.2.27 users manual at the
release tag (held to its git blob) - every control-variable table (GLM, SEN, OPT, IES, SWP, DA,
MOU, SQP), the run-management paragraphs, and the control-data variables whose type the manual
states - read against the 5.2.27 binary's own records of parse-only studies in
`tests/fixtures/pestpp/defaults`: a manual name the binary does not read is no row, and a row's
default is the value the binary prints for it unstated, the manual's beside it as
`manual_default` where the two differ. A default neither states is left unset. PSO is not here:
the release ships no `pestpp-pso`, and its variables sit in its own control-file sections.

A row carries the option's name as the manual spells it (lowercase; PEST++ reads names without
case) and the identifier it is written under (uppercase), its type (INTEGER, REAL, LOGICAL,
STRING, or ARRAY for a comma-separated list), the manual's role as its help, the tools that read
it as its rubrique (`pestpp-ies`, ..., `run management` for every tool, `control data` for the
control-file variables), its default, and the manual sections it was read from as its source.
`modules/module.py` reads the file into the wrapper's slots; `module_bounds.json` beside the
folder states no range for any option.

## Files

| file | keywords | what it is |
| --- | --- | --- |
| `pestpp.json` | 170 | Every option of the release's tools that the binary reads, one surface: a control file is read by whichever tool runs it. |
