# TRID3NT - agent charter

You are working in the TRID3NT repo: a QGIS plugin + local agent daemon
for AI-driven geospatial modeling. Read this, then the docs it points
at, BEFORE writing code. The structure and standards here are the
product of a deliberate refactor - inherit them; do not improvise.

## The map

- `trid3nt_server/` - the daemon package. Feature folders:
  `data/` (fetcher specs + router), `mesh/` (shared mesh layer),
  `workflows/` (engine composers), `gates/` (GateSpec engine + cards),
  `adapters/` (LLM providers - the ONLY place provider nouns appear),
  `server/` (session/turn/dispatch/protocol), `emission/`,
  `persistence/`.
- `workers/` - the solvers: one containerized worker per engine +
  mesh/qgis/postprocess legs. Worker code is INERT until its image is
  rebuilt (absolute -f/context paths, provenance-check, smoke through
  the image).
- `contracts/` - typed wire + registry contracts.
- `plugin/` - the QGIS dock plugin (installs as `trid3nt`).
- `tests/` - the offline suite. `scripts/` - drivers, smokes, image
  builds. `docs/` - decisions (ADRs), design (feature guides),
  validation (the board), proof/templates (NEVER delete anything there).

## The laws

1. Four-slice suite from repo root with `venvs/agent`:
   `env -u TRID3NT_CACHE_BUCKET python -m pytest tests/test_[a-e]*.py
   -p no:cacheprovider --timeout=300 -q` (then `[f-o]`, `[p-r]`,
   `[s-z]`). Baseline failures are EXACTLY 4 fetch_resolution in [f-o]
   + 2 river_dye in [p-r]. Anything else: investigate, never wave off.
2. Live gates for server changes: daemon restart + `scripts/ws_smoke.py`
   (all_passed) + the flood canary `scripts/run_sfincs_direct.py`
   (status=ok) with `set -a; source .env.local; set +a`.
3. Behavior-preserving refactors move code verbatim; every moved or
   renamed symbol's reference sites (tests, monkeypatch paths,
   source-inspection anchors) move WITH it - grep the old path to zero.
4. Delete, don't disable. Deletions register in docs/DELETION_LEDGER.md
   with trace evidence.
5. New tools/templates complete the registry checklist: categories,
   registration import, catalog-surfacing pins, EXPECTED_TEMPLATES,
   co-located corpus.yaml, retrieval top-8 check.
6. Gates on tools are DECLARED (GateSpec metadata + pure estimate/pin
   providers owned by the engine) - never hand-wired in server code.
   Input layers surface via the emit-on-fetch seam (`purpose=` on
   router fetches) - never hand-emitted.

## Writing code

Read `docs/CONVENTIONS.md` and follow its documentation budget: names
and structure carry meaning; comments carry constraints (never history,
citations, attributions, or milestones); knowledge lives in docs/.
LLM-facing tool docstrings are product material - rich, front-loaded,
citation-free. Private helpers get no docstring unless a non-obvious
constraint exists. ASCII hyphens only; no emojis.

Before editing a feature, read its `docs/design/` page if one exists;
if your change reshapes a feature, update that page in the same change.
Decisions get an ADR-lite note in `docs/decisions/`.
