# `workflows/geoclaw/helpers/` - the pure physics and numerics

The relations a GeoClaw composite derives a number from and nothing else: no fetch, no
geometry, no file. A value outside a relation's published range is refused by name.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The door. Consumers import the module they mean. |
| `scaling.py` | The published magnitude-to-rupture-size regressions (one table row each, cited by table and page) and the slip a scalar moment makes over a rectangle. |
