# Instrument output

Two instruments write here, and nothing else lives in this folder. Neither file
is edited by hand: each is regenerated from the tree it describes, so a stale
one is a re-run away and a hand edit is lost at the next run.

The dated MEASUREMENTS this folder used to carry - censuses, conformance walks,
audits, LOC ledgers - are gone. A measurement is evidence about the day it was
taken, and evidence is not something the repo keeps: what a measurement proved
is stated as a constraint at the line it governs, as a requirement in
`docs/model/`, or as a row in `docs/DELETION_LEDGER.md`, and the reading itself
is retaken by re-running the instrument.

| what | written by | holds |
|---|---|---|
| `docstring-exemptions.md` | `tests/hygiene/test_docstring_standard.py` | every `# docstring-exempt: <reason>` marker in the tree, with the symbol and line that carries it. Past roughly ten entries the limit is re-argued rather than routed around. |
| `code-graph/` | `scripts/instruments/code_graph.py` | the code atlas: `SUMMARY.md`, `orphans.md` (modules unreachable from every declared root) and `dead_symbols.md` (vulture at min-confidence 80). `graph.json` is machine-only and untracked. |
