# 0322 - The DATA class body, binding refusals, and first-class parking

Three rulings, one reshape of the declaration substrate. The suite baseline moves
with them: the two standing failures ADR 0321 recorded are gone, and every slice
is zero.

## 1. DATA is a class body; the role prefix is dead

A template's data is a CLASS BODY, one row per artifact:

```python
class DATA:
    centerline = tool("fetch_nhdplus_nldi_navigate", direction="DM", ...)
    ends = tool("endpoints", line=centerline)
    banks = tool("fetch_nhd_area_water", bbox=Ref("centerline.bbox"))
    reach_polygon = tool("section", polygon=banks, between=Ref("ends.between"))
    walls = Data.supplied(geometry="polyline").optional()
```

The attribute NAME is the row name (`Producer.__set_name__`), so nothing is
written twice. A reference from outside the body is attribute access
(`DATA.rain`) and yields the same `DataRef` the old `D.rain` did - with the
difference that a misspelled row is an `AttributeError` at the line that wrote
it rather than a string checked at registration. Row-to-row dataflow inside the
body is the plain identifier: `data_rows()` rewrites those producer objects into
`DataRef`s once, at collection, so the validator and the binder see one shape.
CLASS-BODY ORDER is the row order, which is what a ladder and a chain both read
down.

`Fetch.tool` / `Build.tool` are GONE - no alias, no shim. There is one author
word, `tool(...)`, and `ToolWord` also carries `tool.build_mesh(...)` so a
template imports one name for every ask it makes (the mesh router is reached
lazily; the singleton lives in `workflows/lib/data.py` and
`workflows/mesh/tool.py` re-exports the same object). `ReferenceProducer` and
`AuthoredProducer` collapse into one `Producer`: what a runner does to the world
is the REGISTRY's knowledge, and a second statement of it on the declaration is
a place for the two to disagree. `.supplied()` therefore rides every producer.

`D` survives for one reason: `workflows/telemac/workflow.py` names the agitation
template's `mesh` row from the FACADE (`_AGITATION_EXTRA`), which cannot import
the template's body.

## 2. A ref tail refuses at binding

`_deref` no longer walks a tail with `getattr(..., None)`. A tail naming a field
the referenced result does not define - or one that is there and empty, e.g.
`centerline.bbox` on a layer that carries no bbox - raises a typed
`REF_FIELD_MISSING` naming the ref and the field. The ParamRef-leak law extended
to attribute refs: no silent `None` escapes binding to be blamed on a runner
several frames later. A tail-less `Ref` is untouched, so an OPTIONAL context slot
still binds to `None` and the run still labels the absence.

## 3. Parked is a state, not an absent import

`register_workflow(parked="<reason>")`: the declaration is built and validated
exactly as always, then the tool is NOT registered and the generated function
refuses `TEMPLATE_PARKED` naming the reason. `telemac_rain_on_grid` re-registers
parked ("awaiting the worker-unification port of its mesh step") and the
commented-out import in `trid3nt_server/tools/__init__.py` is restored as a live
one - importing the module no longer decides registry membership.

That is the whole of ADR 0321's open design question. The parked state is READ,
not inferred: `PARKED_TEMPLATES` in `tests/test_door_dissolution.py` pins it the
way `EXPECTED_TEMPLATES` pins the registered set, and the corpus dead-key check
subtracts it (a parked template's corpus is part of its declaration and comes
back with it; the retrieval visible set is derived from the registry, so its
phrasings never reach the model).

## The new baseline

Measured 2026-08-30, repo root, `venvs/agent/bin/python`, commands verbatim from
ADR 0321 (the globs must reach the shell).

| slice | passed | skipped | failed | wall |
|---|---|---|---|---|
| `test_[a-e]*` | 1730 | 5 | 0 | 214.21s |
| `test_[f-o]*` | 4150 | 0 (1 xfailed) | 0 | 44.99s |
| `test_[p-r]*` | 1918 | 1 | 0 | 102.99s |
| `test_[s-z]*` | 1337 | 6 | 0 | 294.96s |
| `contracts/tests` | 789 | 0 | 0 | 5.42s |

9924 passed, **0 failed**. ADR 0321's "exactly the two named failures" clause is
superseded: the standing baseline is zero failures in every slice, and any
failure anywhere is a regression.
