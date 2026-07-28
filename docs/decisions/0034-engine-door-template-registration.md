# 0034 - engine-door template registration

Context: each simulation engine (SFINCS, SWMM, MODFLOW, TELEMAC, GeoClaw, SWAN,
Landlab, OpenQuake, ELMFIRE, plus the PELICUN damage tier) ships a family of
scenario/archetype "template" tools. Surfacing all of them in the default
retrieval pool would flood tool selection with near-duplicate engine variants.

Decision: template-tier tools are registered behind an engine "door".
- A tool is tagged with its engine and a `template` tier; template-tier tools are
  EXCLUDED from the default retrieval pool.
- A single `run_<engine>` door tool is the discoverable entry; selecting it
  expands the gate to admit that engine's templates for the turn.
- Registration is centralized (the tools package `__init__`), so adding an engine
  is one door plus its template set, not N loose top-level tools.

Consequence: tool selection stays legible (one door per engine, not a wall of
variants) while every template remains reachable once its engine is chosen.
Related: 0019 (search wins at scale; enumerate only what is small and hot), 0008.
