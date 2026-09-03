# `workflows/telemac/solving/` - the run, dispatched

TELEMAC is local-docker / worker-image only, so one module carries the whole
dispatch: stage the manifest, hand it to the generic `run_solver` seam, wait,
and surface the gates the run came back with.

The container is the engine room. It meshes nothing and fetches nothing, so no
refusal about a domain's geometry can arise inside it - the server chain refuses
those before a manifest is ever staged, which is why nothing here re-raises a
worker gate.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The door. Consumers import the module they mean. |
| `solve.py` | Stage, dispatch, wait, surface: the plan's only consequential node, and the compute class it is dispatched under. |
