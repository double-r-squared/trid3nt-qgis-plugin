# `workflows/telemac/helpers/` - what a declaration summons

The pieces a TELEMAC family reaches for that are neither the run it authors, the
solve it dispatches, nor the deliverable it publishes: where the reach is, what
falls on it and flows through it, where a derived release settles, what a dredge
digs, and how each of those refuses.

Nothing here decides what question is being asked. A declaration names the
helper it wants and what feeds it.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The door. Consumers import the module they mean. |
| `errors.py` | The pipelines' typed failures, each with the code the envelope carries. |
| `forcing.py` | Declared forcing DATA: net rain and evaporation, the storm a catchment is driven by, and the carrier discharge resolved at the reach. |
| `release_point.py` | Where a DERIVED release is settled inside the accepted mesh, and the mesh's own record of the domain a supplied point is tested against. |
| `reach.py` | The reach front of every river plan: geocode, seed, flowline, banks coverage, mesh coverage, the CFL timestep law. |
| `dredging.py` | NESTOR: the fields a maintenance dredge acts on, the actions it takes and the grade it digs to - as the CONTENT of the three files the module reads together. |
| `uniform_flow.py` | The depth a measured section conveys a flow at - one derivation, read as a reach's outflow stage and as a catchment outlet's whole Z(Q) curve. |
| `water_quality.py` | The two documented saturation relations a declaration derives oxygen from. |
