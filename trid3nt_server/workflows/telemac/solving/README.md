# `workflows/telemac/solving/` - the run, dispatched

TELEMAC is local-docker / worker-image only, so two modules carry the whole
dispatch: `run_telemac.py` REGISTERS what the box runs - one solver, named for
the engine, over one image and one spec - and `solve.py` hands the manifest the
assembler already staged to the generic `run_solver` seam, waits, and surfaces
the gates the run came back with. Which MODULE ran is the manifest's own
`case.module`, which the worker states back in its metrics and the run record
carries beside the engine.

The container is the engine room. It meshes nothing and fetches nothing, so no
refusal about a domain's geometry can arise inside it - the server chain refuses
those before a manifest is ever staged, which is why nothing here re-raises a
worker gate.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The door. Consumers import the module they mean. |
| `run_telemac.py` | The local-docker solve seam: one solver registration, one image, one spec, and the exit classifier that folds the worker's metrics into `completion.json`. |
| `solve.py` | Dispatch, wait, surface: the run's only consequential node, the ONE downloader every question reads its result back through, and the compute class it is dispatched under. |
