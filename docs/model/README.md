# `docs/model/` - the system of systems, checked against the tree

This file is the INDEX. The system is read as PLANES; each plane holds systems;
a system is modeled by the seam files that describe it. A plane no seam has
reached yet is listed here as NOT YET MODELED - a stated absence, because an
omission would read as a claim that nothing is there. Planes get modeled as
their seams are touched, never speculatively and never to fill in the table.

## The planes

| plane | what it holds | state |
| --- | --- | --- |
| **workflow** | the six systems a run passes through - fetcher, mesher, assembler, solver, products, runtime | MODELED in part: three seams, listed below |
| **tool** | the processing tools, the registry, how a tool is surfaced, retrieved and picked | MODELED in part: one seam, `tool-plane.sysml` |
| **intelligence** | LLM provider selection, the adapter, the routing between them | NOT YET MODELED |
| **user** | the chat dock, the canvas, what the model says back | NOT YET MODELED |
| **record** | the run journal, provenance, and this model with its checker | NOT YET MODELED |

## The workflow plane - six systems

A labeled SUBSET of the picture above, never the picture: this is what one run
passes through, and it states nothing about the tool, intelligence, user and
record planes that surround it.

| system | what it produces | modeled by |
| --- | --- | --- |
| **fetcher** | the measured data a run stands on | `data-seam.sysml` - the bed under a hydraulic run |
| **mesher** | the accepted mesh | `mesh-seam.sysml` |
| **assembler** | everything the box receives - steering file, manifest and aux, staged | `solve-seam.sysml`, its authoring end |
| **solver** | the box's run, its completion and its diagnostics | `solve-seam.sysml` |
| **products** | the layers, charts and packets read back out | `solve-seam.sysml`, its reader end |
| **runtime** | the plan a flow executes as - `workflows/lib/plan.py` and its interpreter, where "step" lives | NOT YET MODELED |

One seam can be drawn across more than one system, and the solve seam is: it
runs from the assembler through the box to the readers, so it is listed under
all three rather than cut to fit a row.

## What is authored here

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
A fifth convention places the seam: its FIRST line is
`// plane: <plane> | system: <system>`, required, and the derived view carries
it so no one seam's picture can be read as the whole.

Item names are the tree's own key names. A model whose vocabulary drifts from
the code's cannot be checked against it.

## Files

One `.sysml` per SEAM, and beside each one the view derived from it. A seam
added here is checked and its view gated by being written: the suite reads the
directory rather than a list somebody maintains.

| file | plane / system | what it is |
| --- | --- | --- |
| `solve-seam.sysml` | workflow / assembler -> solver -> products | The TELEMAC solve seam: the blocks from the assembler to the readers, the manifest / server-facts / completion / topology / accepted-mesh contracts by their real key names, and the standing laws with their satisfying block and verifying test. |
| `mesh-seam.sysml` | workflow / mesher | The mesh seam: the router, the recipe, the op tool, the session and its gate, the two mesher adapters and the GPL-isolated box behind one of them, the shared primitives, the artifact record and the topology writer - plus every other shipped driver, which binds here because the purity law is written over the directory they share - with the recipe laws, the box's isolation and the removal doctrine as requirements. |
| `tool-plane.sysml` | tool / code-exec | The code-exec box: the tool that drives it, the host end that stages and launches, the driver inside the container, and the dispatch that keeps the run off the event loop - with the network-none posture, the staged-data rule and the off-load as requirements. The plane's other systems are unmodeled and stated so above. |
| `data-seam.sysml` | workflow / fetcher | The data seam under a hydraulic run: the topobathy row's per-water-body-class bed ladders, the classifier that picks one from the rows the reach chain already holds, the BlueTopo source and its declaration, the ladder registry / walker / router, and the result model the datum and coverage ride on - with the signed bathymetry decisions as requirements, each doc naming the decision it lands. |
| `<seam>-view.md` | as its model | GENERATED. The seam's place, the flow graph, the item tables and the requirement-to-test allocation, derived from `<seam>.sysml` by `python scripts/model_check.py --model docs/model/<seam>.sysml --view`. The suite fails while one is stale; never hand-edit them. |
