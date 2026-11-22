# `trid3nt/sdk/` - the case and its models

The SDK's own front, used from plain Python: a case is a directory that holds
what its runs read and wrote, and a Model over one registered template is set
by attribute, filled and run in this process. Nothing here asks a person or
picks a source; the method is `docs/design/sdk.md`.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The door: `case`, `Case`, `Model`, `Form`, `Result`, `Failure`. |
| `case.py` | `case(path)` -> a `Case`, the directory opened or created, whose attribute per registered template makes a Model over it; outside data copied in, `link(store)` and `pack()`. |
| `lock.py` | `Lock`: `trid3nt.lock`, every fetch read through it and pinned under `data/` by sha256, each pinned file checked on read. |
| `model.py` | `Model`: the one lookup by attribute, set checked by each input's own accept, `fill()` -> `Form`, `run()` -> `Result` or `Failure`, unattended, in its case directory, each writing its row of the lock. |
