# `trid3nt/sdk/` - the case and its models

The SDK's own front, used from plain Python: a case names where runs are
recorded, and a Model over one registered template is set by attribute, filled
and run in this process. Nothing here asks a person or picks a source; the
method is `docs/design/sdk.md`.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The door: `case`, `Case`, `Model`, `Form`, `Result`, `Failure`. |
| `case.py` | `case(name)` -> a `Case`, whose attribute per registered template makes a Model over it. |
| `model.py` | `Model`: the one lookup by attribute, set checked by each input's own accept, `fill()` -> `Form`, `run()` -> `Result` or `Failure`, unattended. |
