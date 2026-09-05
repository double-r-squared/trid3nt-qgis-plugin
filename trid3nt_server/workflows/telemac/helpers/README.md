# `workflows/telemac/helpers/` - what a declaration summons

The pieces a TELEMAC family reaches for that are neither the run it authors, the
solve it dispatches, nor the deliverable it publishes: where the reach or the
catchment is, what falls on it and flows through it, what soaks in, what was
spilled, what chemistry runs, and how each of those refuses.

Nothing here decides what question is being asked. A declaration names the
helper it wants and what feeds it.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The door. Consumers import the module they mean. |
| `catchment.py` | The catchment a storm is solved over: the analysis window around its outlet, and the accepted mesh's own nodes. |
| `errors.py` | The pipelines' typed failures, each with the code the envelope carries. |
| `forcing.py` | Declared forcing DATA: net rain and evaporation, the storm a catchment is driven by, and the carrier discharge resolved at the reach. |
| `infiltration.py` | The infiltration surface: per-node curve numbers and Manning n, sampled from land cover at the mesh's own nodes. |
| `reach.py` | The reach front of every river plan: geocode, seed, flowline, banks coverage, mesh coverage, the CFL timestep law. |
| `dredging.py` | NESTOR: the fields a maintenance dredge acts on, the actions it takes and the grade it digs to - as the CONTENT of the three files the module reads together. |
| `oil.py` | The oil module's own preset, and the per-run Fortran this run's release is compiled into. |
| `oil_templates/` | The engine's own release routine, shipped here because the release coordinates are compiled INTO it. |
| `substance.py` | What was released: the literature die-off a named decaying substance carries, and the GAIA sediment body a bed question couples the solve with. |
| `uniform_flow.py` | The depth a measured section conveys a flow at - one derivation, read as a reach's outflow stage and as a catchment outlet's whole Z(Q) curve. |
| `water_quality.py` | WAQTEL water quality: the documented relations and the O2 process block. |
