# `trid3nt/sdk/` - the case and its models

The SDK's own front, used from plain Python: a case is a directory that holds
what its runs read and wrote, and a Model over one registered template is set
by attribute, filled and run in this process. Nothing here asks a person or
picks a source; the method is `docs/design/sdk.md`.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The door: `case`, `Case`, `Model`, `Form`, `Result`, `Failure`. |
| `case.py` | `case(path, strict=False)` -> a `Case`, the directory opened or created, whose attribute per registered template makes a Model over it; outside data copied in, `link(store)` and `pack()`. |
| `lock.py` | `Lock`: `trid3nt.lock`, every fetch read through it and pinned under `data/` by sha256, each pinned file checked on read. |
| `model.py` | `Model`: the one lookup by attribute, set checked by each input's own accept, `strict` and `steps` (the coverage steps it states over its template's), `fill()` -> `Form` (the verdicts, each input's coverage and the warnings an unresolved shortfall leaves, and the written, checked deck), `run()` -> `Result` or `Failure` (the solve of that deck while it is current), unattended, in its case directory, each writing its row of the lock. |
