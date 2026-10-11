# `workflows/` - the template runtime and the engines

A workflow is a declaration: `PARAMS` and `DATA` class bodies over one engine
module. A fill puts a value into each input, which accepts or rejects it; a
READY fill launches on an explicit act, and the engine's run is straight-line
code - write, solve, read. `runtime/` is the template bodies and the machinery that runs them, `solver/` is the one executor, and each engine package holds the templates that
speak it and the one file that specializes the executor to it.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | Exports nothing; importing it registers the TELEMAC engine's local solve specs. |

## Subfolders

| folder | what it is |
| --- | --- |
| `runtime/` | The template runtime - the `PARAMS` and `DATA` bodies, the fill, the skeleton and the run's records. See below. |
| `solver/` | The one executor, which knows no engine: `solver.py` (the box - launch, supervise, poll, dispatch-and-wait, download a result), `solve_progress.py` (the live cards of a solve - the Dispatch and Sim cards and the progress heartbeat on the Sim card), `code_provenance.py` (which code produced a run), `diagnostics/` (the one `read_run_diagnostics` dispatcher; each engine registers its own parser), `corpus.yaml` (routing phrasings). |
| `telemac/` | The TELEMAC engine: the module wrappers, the templates over them, the code that fills a template, holds it for review and runs it, and the one engine file the executor is specialized by. Has its own map. |

## `runtime/` - the template runtime

| file | what it is |
| --- | --- |
| `runtime/__init__.py` | The runtime's public surface, and the only import a template needs. |
| `runtime/accepts.py` | `Accepts` - what a template takes when something is SUPPLIED to it, role by role. |
| `runtime/data.py` | The `DATA` class body: one declared artifact per row, its producer or the CLASS it needs, the CONTEXT row whose absence continues the run, and the modifiers that ride the declaration. |
| `runtime/docstring.py` | The registered tool's model-facing docstring, rendered from the declarations in two views (routing, full). |
| `runtime/errors.py` | The runtime's typed errors, each carrying the code the emitter renders. |
| `runtime/fill.py` | The fill: each input accepts, rejects with its remedies, is missing, or takes the module's own default; a sourced input is fetched and ingested before it accepts or rejects, and a name the template writes that nothing of the run is called is refused. Filling never launches. The run's ONE mapping - params, fetched rows, stage products, keywords - and `read`, its one read by plain name. |
| `runtime/journal.py` | The run journal - one append-only JSONL line per completed run, plus the outputs and choices channels a run writes into; the note and coverage channels are `inputs/run_notes.py`. |
| `runtime/levers.py` | The levers the runtime declares ONCE - the mesh resolution, the event time, the compute class - so a template's `PARAMS` keeps only its question's own inputs. |
| `runtime/params.py` | The `PARAMS` class body: one declared value per row, its default, its bounds and its unit; plus the seated values a run reads by name, each with its origin. |
| `runtime/resolution.py` | Resolution sensitivity: which of a run's published reads a coarse mesh gets wrong, and which way. |
| `runtime/resolver.py` | The param seating: a stated value or the declared default, held to its bounds - a value outside them refuses by name - with a provenance row per seating. |
| `runtime/run_output.py` | The run's persisted chart spec and metrics, written under its own prefix so the output outlives the turn that emitted them. |
| `runtime/workflow.py` | The workflow SKELETON and the registration factory: fill, launch a READY fill, post, publish, and the synthesized tool signature. |
