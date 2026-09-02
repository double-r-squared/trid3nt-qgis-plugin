# `docs/model/` - the system model, checked against the tree

The code already declares most of itself: registries, declarations and recipes
ARE model elements and are read out of the tree. What is authored here is only
INTENT - the interfaces the seams promise, the standing laws as requirements,
and the allocation of each law to the block that satisfies it and the test that
verifies it.

A model nobody can check rots, so nothing here is prose alone.
`scripts/model_check.py` reads these files and validates four rules against the
live code, and `tests/test_model_conformance.py` runs it in the offline suite:

1. every non-optional item of every interface USAGE is named by the module at
   that hop's writer end and by the module at its consumer end - resolved
   structurally, so a key mentioned only in a comment counts as neither.
   Per usage, not per definition: evidence pooled across the hops that share a
   contract leaves a severance in one module invisible while a sibling hop
   keeps supplying the item;
2. every `verify` names a test that exists;
3. every `forbid:` dependency rule holds against the import edges of the modeled
   modules, computed at check time;
4. every tree module that calls a modeled contract's `constructor:` is bound to
   a usage of that contract - an author nobody modeled is a writer no severance
   check covers.

The notation is SysML v2 TEXTUAL, restricted to the subset the checker reads:
`part def`, `part`, `port def`, `port`, `interface def` / `item`, `interface`
(connect), `requirement def`, `satisfy`, `verify`. Four doc-line conventions
carry what that subset has no place for - `code:` binds a block to the module it
IS, `forbid:` states a dependency rule, `constructor:` names a function that
builds a contract, and `pass-through:` marks the end of a hop that forwards the
contract verbatim, which therefore owes no item evidence and supplies none.

Item names are the tree's own key names. A model whose vocabulary drifts from
the code's cannot be checked against it.

## Files

| file | what it is |
| --- | --- |
| `solve-seam.sysml` | The TELEMAC solve seam: the blocks from deck author to the readers, the manifest / echo / completion / topology / accepted-mesh contracts by their real key names, and the standing laws with their satisfying block and verifying test. |
| `solve-seam-view.md` | GENERATED. The flow graph, the item tables and the requirement-to-test allocation, derived from `solve-seam.sysml` by `python scripts/model_check.py --view`. The suite fails while it is stale; never hand-edit it. |
