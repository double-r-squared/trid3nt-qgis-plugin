# `workflows/` - the template runtime and the engines

A workflow is a declaration: one `INPUTS` class body over one engine
module. A fill puts a value into each input, which accepts or rejects it; a
READY fill launches on an explicit act, and the engine's run is straight-line
code in one order - full fill, mesh, settle, one review, solve. `runtime/` is the template bodies and the machinery that runs them, `solver/` is the one executor, and each engine package holds the templates that
speak it and the one file that specializes the executor to it.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | Exports nothing; importing it registers the TELEMAC engine's local solve specs. |

## Subfolders

| folder | what it is |
| --- | --- |
| `runtime/` | The template runtime - the producer a row names, the fill, the skeleton and the run's records. See below. |
| `solver/` | The one executor, which knows no engine: `solver.py` (the box - launch, supervise, poll, dispatch-and-wait, download a result), `solve_progress.py` (the live cards of a solve - the Dispatch and Sim cards and the progress heartbeat on the Sim card), `code_provenance.py` (which code produced a run), `diagnostics/` (the one `read_run_diagnostics` dispatcher; each engine registers its own parser), `corpus.yaml` (routing phrasings). |
| `telemac/` | The TELEMAC engine: the module wrappers, the templates over them, the code that fills a template, holds it for review and runs it, and the one engine file the executor is specialized by. Has its own map. |

## `runtime/` - the template runtime

| file | what it is |
| --- | --- |
| `runtime/__init__.py` | The runtime's public surface, and the only import a template needs. |
| `runtime/data.py` | What produces a row: the `Producer` a declaration names, the one word it is written with (`tool`), and the shape check a supplied artifact is held to. The declaration itself is `inputs/input.py`. |
| `runtime/docstring.py` | The registered tool's model-facing docstring, rendered from the declarations in two views (routing, full). |
| `runtime/errors.py` | The runtime's typed errors, each carrying the code the emitter renders. |
| `runtime/fill.py` | The fill: each input accepts, rejects with its remedies, is missing, or takes the module's own default; a sourced input is fetched and ingested before it accepts or rejects, and a name the template writes that nothing of the run is called is refused. Filling never launches. One loop answers every input off the workflow's one lookup and leaves one verdict each, with its origin and source. The run's ONE mapping - stated values, fetched rows, stage products, keywords - and `read`, its one read by plain name. |
| `runtime/provenance.py` | The provenance rows a run's layer carries, off the fill's verdicts: one per answered value, and one for an absent optional value stating what the run derives. |
| `runtime/journal.py` | The run journal - one append-only JSONL line per completed run, its inputs in one map by name (value, origin, source), plus the outputs and choices channels a run writes into; the note and coverage channels are `inputs/run_notes.py`. |
| `runtime/resolution.py` | Resolution sensitivity: which of a run's published reads a coarse mesh gets wrong, and which way. |
| `runtime/run_output.py` | The run's persisted chart spec and metrics, written under its own prefix so the output outlives the turn that emitted them. |
| `runtime/workflow.py` | The workflow SKELETON and the registration factory: fill, launch a READY fill, post, publish, and the synthesized tool signature. |
