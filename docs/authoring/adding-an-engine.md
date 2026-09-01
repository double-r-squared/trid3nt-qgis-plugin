# Adding an engine

An "engine" is a real numerical solver the agent drives end to end: a declared
template assembles the run, the engine's **facade** routes the declared physics
process to the steps that author its deck, a **worker** runs the solver inside
its image, and a **postprocess** turns the raw outputs into published layers plus
the typed scalars the agent narrates from.

The live engine is TELEMAC (`trid3nt_server/workflows/telemac/`), and it is the
precedent every path below names. This doc covers the two patterns:

- **Adding a template to an existing engine** (a new QUESTION the engine can
  answer) -- the common, cheaper case.
- **Adding a whole new engine** -- a facade, a worker image, and a postprocess.

The discipline for both (project norms):

- **Research the real pipeline first.** Ground the deck in how practitioners
  actually build this model, from primary sources, before writing code.
- **Smoke-first, local-first.** Prototype as a direct-call sandbox script against
  a tiny fixture, get the deck to converge, THEN promote to a registered
  template.
- **Deploy is not "commit".** A worker-image engine only runs after its image is
  rebuilt; a worker edit is INERT until then.

---

## Pattern A - a new template on an existing engine

A template is a DECLARATION, not a function: `PARAMS` and `DATA` class bodies
plus a pure `plan(ops)` the interpreter walks
(`docs/design/declarative-workflows.md` is the language reference). It reuses the
engine's facade, its steps and its worker, and adds a new physics process. The
seam list, from the TELEMAC templates:

1. **The declarations** -- `workflows/<engine>/<template>/declarations.py`: the
   `PARAMS` rows (each with its door, bounds, units and consequence tag) and the
   model-facing `DOC`. Nothing here executes.

2. **The template** -- `workflows/<engine>/<template>/<template>.py`: the `DATA`
   rows (every world-read declared, never performed in a step), the binding
   blocks (`Physics`, `Forcing`, the `tool.build_mesh` ask), `plan(ops)`, the
   `ANSWER` tuple, the chart builder, and the `register_workflow(...)` call.
   Model it on `workflows/telemac/rain_on_grid/rain_on_grid.py`.

3. **The process row** -- `workflows/<engine>/workflow.py`: a row in the facade's
   `_PROCESSES` table saying what the declared process means end to end - which
   deck step serializes it, which writer's signature the slots are checked
   against, which solve dispatches it, which reader publishes it. A process the
   facade does not know REFUSES at plan construction rather than solving into a
   reader that cannot describe the result.

4. **The steps** -- `workflows/<engine>/steps/`: the deck writer and the product
   reader the row names. A product raster declares the QUANTITY it computed; the
   style contract (`contracts/trid3nt_contracts/styles.yaml`) owns the preset.

5. **Discovery + wiring**:
   - import the template in `trid3nt_server/tools/__init__.py` so its
     registration fires at startup;
   - add the routing phrasings to the template package's `corpus.yaml` and run
     the retrieval-visibility check;
   - add a declaration test beside the other template tests, and a canary in
     `trid3nt_server/testing/canaries.py`.

---

## Pattern B - a whole new engine

Each new engine adds, roughly in order:

1. **A result contract** in `contracts/trid3nt_contracts/<engine>_contracts.py`:
   the headline `LayerURI` subclass carrying the typed scalars the agent
   narrates, mirroring `telemac_contracts.py`. Style presets are named here and
   declared once in `styles.yaml`.

2. **A worker** under `workers/<engine>/`: an `entrypoint.py` plus the deck
   builders. The worker is an ENGINE ROOM - a staged run dir in, results out, no
   network and no defaults of its own. Copy the shape from `workers/telemac/`.
   The engine is dispatched with `run_solver` from the agent side.

3. **A facade** at `workflows/<engine>/workflow.py`: the operations a template
   composes (`acquire_domain`, `author`, `solve`, `read`) and the `_PROCESSES`
   table behind them.

4. **A postprocess** that reads the raw solver outputs and returns the headline
   `LayerURI` (raster COG or vector), plus the results-mesh entry when the engine
   writes a native time-stepped mesh the client animates directly.

5. **Templates** on top of the facade, per Pattern A.

---

## The deploy seam

Engines run through `run_solver` on local docker; the image is resolved per
engine (`TRID3NT_<ENGINE>_IMAGE` env or the registered default). See
`workers/README.md` and `docs/site/engines.md`.

**Worker-image changes need a rebuild.** Build with the engine's build script
(`scripts/build_telemac_image.sh`) or the documented `docker build` line, then
smoke THROUGH the image and check its provenance. A code commit alone does NOT
update a built image. Server-side code (the template, the facade, the steps, the
contract, the postprocess) deploys by editing the tree + `make agent restart`.

---

## Smoke-first checklist

Before you register the template:

1. Research the real pipeline from primary sources.
2. Write a direct-call sandbox script that builds the deck against a tiny fixture
   and runs the solver locally; confirm it converges and the diagnostic quantity
   is physical.
3. Wire the postprocess -> headline `LayerURI`; render a proof (overlay the mesh
   wireframe in engine proof renders).
4. Only then register it, add the corpus queries, run the retrieval-visibility
   check, and add the canary + declaration test.
