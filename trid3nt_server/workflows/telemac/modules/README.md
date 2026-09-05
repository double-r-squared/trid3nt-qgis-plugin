# `workflows/telemac/modules/` - the module surface

A workflow is a WRAPPER around a TELEMAC module, exposing the module's full
keyword surface. The engine already publishes that surface: every module's
dictionary names each keyword, its type, its allowed values, its default and its
help. The wrapper is that catalog, and nothing else that opines - it is the
analog of the engine's own defaults. Variance lives in templates, which stay
Python because people read them.

A class body extending a wrapper (or extending another body) asserts raw
keywords under the identifiers the image itself spells them by. A keyword the
module does not have, and a value the dictionary does not take, refuse BY NAME
at import; the sheet the body fills records per slot WHERE its value came from,
so nothing inherited or derived hides behind a number.

Two acts, and only two. `fill` is repeatable and decides nothing. `run` is
explicit, on a complete sheet, and it is where execution stops being held.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The door: the wrappers, and the two acts. |
| `module.py` | What a slot, a wrapper, a composite and an output ARE, and the catalog loader that makes a wrapper out of `catalog/<module>.json`. |
| `sheet.py` | The sheet - filled slots with their provenance, the files a composite named, the mandatory slots still open - and `fill` / `run`. |
| `telemac2d.py` | The TELEMAC-2D wrapper. |
