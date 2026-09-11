# `workflows/telemac/helpers/` - the pure physics and numerics

The relations a declaration or the assembler derives a number from and nothing
else: no fetch, no geometry, no file, no failure of its own. Each is a function
none of the composites, the typed inputs or the DATA rows express.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The door. Consumers import the module they mean. |
| `time_step.py` | The CFL step a mesh is solved at, coupled to the edge the accepted mesh was measured at, and the wall-clock estimate that bounds a solve's wait. |
| `uniform_flow.py` | The depth a measured section conveys a flow at - one derivation, read as a reach's outflow stage and as a catchment outlet's whole Z(Q) curve. |
| `water_quality.py` | The two documented saturation relations a declaration derives oxygen from. |
