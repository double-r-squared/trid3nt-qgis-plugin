# `workflows/` - the declarative library, the mesh front, the engines

A workflow is a declaration: `PARAMS` and `DATA` class bodies plus the steps the
door builds out of them, which the interpreter walks. `runtime/` is the language
and the machinery that executes it, `mesh/` builds the domain a solve runs on,
`solver/` is the one executor, and each engine package holds the templates that
speak it and the one file that specializes the executor to it.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The package door: workflows compose atomic tools into deterministic, LLM-free chains. |

## Subfolders

| folder | what it is |
| --- | --- |
| `runtime/` | The declarative library - the value types, the six doors, the validator, the interpreter, the skeleton and the run's records. See below. |
| `mesh/` | The one mesh front: router, meshers, session, gate, artifact. Has its own map. |
| `solver/` | The one executor, which knows no engine: `solver.py` (the box - launch, supervise, poll, dispatch-and-wait, download a result), `compute_class.py` (the ladder a caller may name and the coercion onto it), `solve_progress.py` (the live progress heartbeat a long solve emits while it runs), `code_provenance.py` (which code produced a run), `diagnostics/` (the one `read_run_diagnostics` dispatcher plus its per-engine parsers), `corpus.yaml` (routing phrasings). |
| `telemac/` | The TELEMAC engine: the module wrappers, eight templates over them, the fill/run door, and the one engine file the executor is specialized by. Has its own map. |

## `runtime/` - the declarative library

| file | what it is |
| --- | --- |
| `runtime/__init__.py` | The library's public surface, and the only import a template needs. |
| `runtime/accepts.py` | `Accepts` - what a template takes when something is SUPPLIED to it, role by role. |
| `runtime/data.py` | The `DATA` class body: one declared artifact per row, its producer, and the modifiers that ride the declaration. |
| `runtime/docstring.py` | The registered tool's model-facing docstring, rendered from the declarations in two views (routing, full). |
| `runtime/domain.py` | The `Domain` environment - the current spatial extent every spatial producer reads implicitly. |
| `runtime/errors.py` | The library's typed errors, each carrying the code the emitter renders. |
| `runtime/interpreter.py` | The interpreter: it walks the steps, binds late-bound reads, runs the ledger, and guards against a leaked ref. |
| `runtime/journal.py` | The run journal - one append-only JSONL line per completed run, plus the note channel a step writes into. |
| `runtime/ledger.py` | The step ledger: what one invocation may replay and what it may not. |
| `runtime/params.py` | The `PARAMS` class body: one declared value per row, its door, its bounds, its consequence tag, and the resolved-sheet views. |
| `runtime/plan.py` | The plan VALUE - steps, refs, modifiers, charts - plus the `Row` descriptor both declaration bodies are built from. |
| `runtime/rerun/` | The rerun-with-overrides primitive: derive a run from a run (`derive.py`), what it inherits (`reuse.py`), and the tool door onto it (`rerun_workflow.py`). |
| `runtime/resolution.py` | Resolution sensitivity: which answers a coarse mesh reads wrong, and which way. |
| `runtime/resolver.py` | The param resolver: the six doors in order, with bounds clamping and a provenance row per resolution. |
| `runtime/run_products.py` | The run's persisted chart spec and metrics, written under its own prefix so the products outlive the turn that emitted them. |
| `runtime/snapshot.py` | The run snapshot: what a finished run leaves behind so a child run can derive from it. |
| `runtime/temporal.py` | The declared temporal transforms - `.resample(...)` and `.normalize(units=...)` - and the conversions behind them. |
| `runtime/validate.py` | The plan validator - ref integrity, modifier legality and gate placement, all before any execution. |
| `runtime/validity.py` | Coupled validity: the cross-param rules a single `Param` declaration cannot express. |
| `runtime/workflow.py` | The workflow SKELETON and the registration factory: normalize, resolve, interpret, post, publish, and the synthesized tool signature. |
