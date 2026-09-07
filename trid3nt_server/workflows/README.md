# `workflows/` - the declarative library, the mesh front, the engines

A workflow is a declaration: `PARAMS` and `DATA` class bodies plus the steps the
door builds out of them, which the interpreter walks. `runtime/` is the language
and the machinery that executes it, `mesh/` builds the domain a solve runs on,
`solver/` dispatches the box, and each engine package holds the templates that
speak it.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The package door: workflows compose atomic tools into deterministic, LLM-free chains. |

## Subfolders

| folder | what it is |
| --- | --- |
| `runtime/` | The declarative library - the value types, the six doors, the validator, the interpreter, the skeleton and the run's records. See below. |
| `mesh/` | The one mesh front: router, meshers, session, gate, artifact. Has its own map. |
| `shared/` | Engine-agnostic seams the live engines need: AOI acquisition, the tide series, COG io, the layer-uri record, the publish/manifest seams, solve progress, supplied geometry, run products. |
| `solver/` | Solve dispatch and what came back: `solver.py` (the box), `code_provenance.py` (which code produced a run), `diagnostics/` (the one `read_run_diagnostics` dispatcher plus its per-engine parsers), `corpus.yaml` (routing phrasings). |
| `telemac/` | The TELEMAC engine: the module wrappers, eight templates over them, and the fill/run door. Has its own map. |

## `runtime/` - the declarative library

| file | what it is |
| --- | --- |
| `__init__.py` | The library's public surface, and the only import a template needs. |
| `accepts.py` | `Accepts` - what a template takes when something is SUPPLIED to it, role by role. |
| `data.py` | The `DATA` class body: one declared artifact per row, its producer, and the modifiers that ride the declaration. |
| `docstring.py` | The registered tool's model-facing docstring, rendered from the declarations in two views (routing, full). |
| `domain.py` | The `Domain` environment - the current spatial extent every spatial producer reads implicitly. |
| `errors.py` | The library's typed errors, each carrying the code the emitter renders. |
| `interpreter.py` | The interpreter: it walks the steps, binds late-bound reads, runs the ledger, and guards against a leaked ref. |
| `journal.py` | The run journal - one append-only JSONL line per completed run, plus the note channel a step writes into. |
| `ledger.py` | The step ledger: what one invocation may replay and what it may not. |
| `params.py` | The `PARAMS` class body: one declared value per row, its door, its bounds, its consequence tag, and the resolved-sheet views. |
| `plan.py` | The plan VALUE - steps, refs, modifiers, charts - plus the `Row` descriptor both declaration bodies are built from. |
| `rerun/` | The rerun-with-overrides primitive: derive a run from a run (`derive.py`), what it inherits (`reuse.py`), and the tool door onto it (`rerun_workflow.py`). |
| `resolution.py` | Resolution sensitivity: which answers a coarse mesh reads wrong, and which way. |
| `resolver.py` | The param resolver: the six doors in order, with bounds clamping and a provenance row per resolution. |
| `snapshot.py` | The run snapshot: what a finished run leaves behind so a child run can derive from it. |
| `temporal.py` | The declared temporal transforms - `.resample(...)` and `.normalize(units=...)` - and the conversions behind them. |
| `user_input.py` | The user-input species: clicks, sketches and typed values, normalized once. |
| `validate.py` | The plan validator - ref integrity, modifier legality and gate placement, all before any execution. |
| `validity.py` | Coupled validity: the cross-param rules a single `Param` declaration cannot express. |
| `workflow.py` | The workflow SKELETON and the registration factory: normalize, resolve, interpret, post, publish, and the synthesized tool signature. |
