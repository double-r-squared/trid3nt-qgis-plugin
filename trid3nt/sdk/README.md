# `trid3nt/sdk/` - the case and its models

The SDK's own front, used from plain Python: a case is a directory that holds
what its runs read and wrote, and a Model over one pinned template is set
by attribute, filled and run in this process. Nothing here asks a person or
picks a source; the method is `docs/design/sdk.md`.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The door: `case`, `Case`, `Model`, `Form`, `Result`, `Failure`. |
| `case.py` | `case(path, strict=False)` -> a `Case`, the directory opened or created. Registered-template attributes and `model(name_or_path)` make models. It also copies and re-pins templates, copies outside data and supports `link(store)` and `pack()`. |
| `deck_state.py` | Encode reviewed state as a JSON graph and rebind it to pinned declarations and local files. |
| `lock.py` | `Lock`: `trid3nt.lock`, every fetch read through it and pinned under `data/` by sha256, each pinned file and template source checked on read. |
| `model.py` | `Model`: the one lookup by attribute, set checked by each input's own accept, `strict` and `steps` (the coverage steps it states over its template's), `fill()` -> `Form` (the verdicts, per-input ranked fetch requests and stated steps, each input's coverage and the warnings an unresolved shortfall leaves, and the written, checked deck), `run()` -> `Result` or `Failure` (the solve of that deck while it is current), unattended, in its case directory, each writing its row of the lock. |
| `run_messages.py` | The shared reviewed-run instruction, event and Stop messages. |
| `run_store.py` | Verify pinned served reads for the remote child and track its writes. |
| `script_run.py` | Rebind a reviewed deck to the script's pinned template, request stage-dependent reviews from its host, and report execution. |
| `study.py` | Run PEST++ stages, save and read immutable named studies, and reproduce their resolved decks on application. |
| `template.py` | Load pinned declarations without tool registration and describe upstream drift and its fix. |
| `version.py` | Recreate the model held by a version and verify its finished deck and mesh pins. |
| `view.py` | Draw recorded rasters, vectors, mesh datasets and charts in matplotlib. |
