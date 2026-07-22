# Adding an engine

An "engine" is a real numerical solver (MODFLOW, SFINCS, PySWMM, OpenQuake,
GeoClaw, Landlab, SWAN, TELEMAC, ELMFIRE, ...) that the agent drives end to end:
the LLM assembles a typed run spec, a **worker** builds the solver deck and runs
it, and a **postprocess** step turns the raw outputs into a rendered `LayerURI`.
This doc covers the two patterns:

- **Adding an archetype to an existing engine** (a new QUESTION the engine can
  answer) -- the common, cheaper case. Precedents: the MODFLOW SFR
  `stream_depletion` and CSUB `land_subsidence` archetypes.
- **Adding a whole new engine** -- the sprint-17 precedent (GeoClaw, OpenQuake,
  Landlab, SWAN, TELEMAC).

The discipline for both (project norms):

- **Research the real pipeline first.** Ground the deck in how practitioners
  actually build this model, from primary sources, before writing code.
- **Smoke-first, local-first.** Prototype as a direct-call sandbox script against
  a tiny fixture, get the deck to converge, THEN promote to a registered tool.
- **Deploy is not "commit".** A worker-image engine only runs after its image is
  rebuilt and shipped; verify `deployed == HEAD` on the box.

---

## Pattern A - a new archetype on an existing engine (MODFLOW)

An archetype reuses the engine's worker + run plumbing and adds a new deck
branch + postprocess. The seam list, from the SFR/CSUB precedents:

1. **Contract archetype literal** --
   `contracts/src/trid3nt_contracts/modflow_contracts.py`: add the
   literal to `MODFLOWRunArgs.archetype` (the `Literal[...] | None` selector),
   plus any per-archetype input fields and a headline `LayerURI` subclass with
   pinned metrics + sign conventions (e.g. `StreamReachLayerURI`,
   `SubsidenceLayerURI`, `DrawdownLayerURI`).

2. **Worker deck author** --
   `services/workers/modflow/gwt_adapter.py`: a new branch that builds the
   FloPy/mf6 deck (packages, boundary conditions, OBS) for the archetype. Keep
   every non-target deck byte-identical.

3. **Run-tool threading** --
   `server/src/trid3nt_server/workflows/run_modflow.py`: thread the new
   fields + obs globs through to the worker.

4. **Postprocess** --
   `server/src/trid3nt_server/workflows/postprocess_modflow.py`: a
   `postprocess_<archetype>(run_outputs_uri, *, run_id, model_crs, deck_dir)`
   that reads the raw outputs and returns the headline `LayerURI` (+ charts). The
   off-box worker mirror is
   `services/workers/_modflow_postprocess/postprocess.py` (the
   `_ARCHETYPE_POSTPROCESS_RUNNERS` map).

5. **Dispatch registration** --
   `server/src/trid3nt_server/tools/run_modflow_archetype_tool.py`: add
   `"<archetype>": (postprocess_fn, "headline_attr")` to `ARCHETYPE_POSTPROCESS`.
   Also flag the archetype in `_NON_SCALAR_HEADLINES` (if the headline is a
   series/dict rather than a positive scalar) or `PRT_ARCHETYPES` (if it runs a
   two-simulation particle-tracking sequence) as applicable.

6. **Composer (the LLM-facing surface)** --
   `server/src/trid3nt_server/workflows/model_<x>_scenario.py`. This is the
   tool the model calls. It carries its OWN `@register_tool` (e.g.
   `run_model_<x>_scenario`), assembles a `MODFLOWRunArgs(archetype="<x>", ...)`,
   and dispatches to `run_modflow_archetype_job`. Model it on
   `server/src/trid3nt_server/workflows/model_sustainable_yield_scenario.py`.
   The archetype run-tool itself is NOT `@register_tool`'d -- the composers are
   the surface.

7. **Discovery + wiring** (same as any tool -- see `writing-a-tool.md`):
   - import the composer in `server/src/trid3nt_server/tools/__init__.py`
     (the `from ..workflows import model_<x>_scenario as _model_<x>_scenario`
     pattern) so its `@register_tool` fires at startup;
   - add `PRIMARY_CATEGORY` (usually `hazard_modeling`) in `categories.py`;
   - add `tool_query_corpus.yaml` queries + run the
     `retrieve_visible_tools(prompt, None, 8)` visibility check;
   - add any chart payloads in `chart_tools.py`;
   - add a test + an mf6 smoke fixture under
     `services/workers/modflow/fixtures/<x>_smoke/`.

---

## Pattern B - a whole new engine

Precedent: the sprint-17 engines wired in `tools/__init__.py` (`run_geoclaw_tool`,
`run_openquake_tool`, `run_landlab_tool`, `run_swan_tool`, `run_telemac_tool`).
Each new engine adds, roughly in order:

1. **Deck / result contract** in
   `contracts/src/trid3nt_contracts/` (a `<engine>_contracts.py` with the
   run args + the headline `LayerURI` subclass), mirroring
   `modflow_contracts.py`.

2. **A worker** under `services/workers/<engine>/`: an `entrypoint.py` plus the
   deck author. Existing worker dirs to copy the shape from: `modflow/`,
   `geoclaw/`, `openquake/`, `landlab/`, `swan/`, `telemac/`, `swmm/`. The engine
   is dispatched with `run_solver('<engine>')` from the agent side.

3. **A postprocess** that reads the raw solver outputs and returns a headline
   `LayerURI` (raster COG or vector FlatGeobuf), plus an off-box postprocess
   mirror if the engine offloads (see `services/workers/_geoclaw_postprocess/`,
   `_landlab_postprocess/`, `_swan_postprocess/`).

4. **A composer + bridge tool** on the agent side: a
   `workflows/model_<engine>_scenario.py` composer carrying `@register_tool`
   (`run_<engine>_...`), and a thin `tools/run_<engine>_tool.py` bridge that
   imports the composer. Import the bridge in `tools/__init__.py`.

5. **Discovery + wiring**: `categories.py` entry, `tool_query_corpus.yaml`
   queries + the retrieval-visibility check, tests, and a smoke fixture.

---

## Local vs cloud deploy (the deploy seam)

Engines run one of two ways, chosen per engine/archetype:

- **Local-docker / in-process** -- small, fast archetypes run without Batch. The
  MODFLOW PRT, saltwater-intrusion, SFR, and CSUB archetypes run via a local mf6
  path (`run_modflow_local`); ELMFIRE runs a local `trid3nt/elmfire:dev`
  container; SFINCS local uses `deltares/sfincs-cpu`. In the offline/local build,
  engine backends are selected by env (`TRID3NT_MODFLOW_LOCAL=1`,
  `TRID3NT_SOLVER_BACKEND=local-docker`).

- **Local docker worker image** -- containerized engines run via `run_solver`
  on local docker (image resolved per engine: `TRID3NT_<ENGINE>_IMAGE` env or
  the registered default).

**Worker-image changes need a rebuild.** Build with the engine's
`build_<engine>_image.sh` (or the documented `docker build` line in
docs/site/install.md). A code commit alone does NOT update a built image -
verify the running image matches your change. Server-side code (the composer,
contract, postprocess, wiring) deploys by editing `server/` + `make agent`.

---

## Smoke-first checklist

Before you register the tool:

1. Research the real pipeline from primary sources.
2. Write a direct-call sandbox script that builds the deck against a tiny fixture
   and runs the solver locally; confirm it converges and the diagnostic quantity
   is physical (e.g. GeoClaw's "Total mass at initial time" ~1e9+ for a real
   wave, not 1e5).
3. Wire the postprocess -> headline `LayerURI`; render a proof (overlay the mesh
   wireframe in engine proof renders).
4. Only then promote to the composer + `@register_tool`, add corpus + category,
   run the retrieval-visibility check, and add the smoke fixture + test.
