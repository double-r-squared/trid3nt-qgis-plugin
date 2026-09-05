# `templates/shared/` - the bodies a template LISTS

A shared body is a PART, never a parent: a template writes `parts = [RIVER]` and
the part's assertions merge under the PART's own name, so per-slot provenance
says where every value came from and a keyword that means something else in a new
setting is seen rather than inherited into silence. A keyword two parts both set
REFUSES by name unless the template settles it itself.

A body is created only when a good portion of a template is shared, and it must
have at least two users - one user folds back into its own template, and the
suite checks. A part carries the DATA rows and the MESH recipe it shares
alongside its slot assertions: a chain that produces the same artifacts for the
same reason is as much the shared thing as a keyword is.

## Files

| file | what it is |
| --- | --- |
| `river.py` | `RIVER` - what every river deck states about the WATER, whatever is carried in it - plus the chain that cuts the reach out of real geometry, the mesh recipe that triangulates it, the steps that establish the modelled world, the settle that measures it, and the rows every river run and every point release declare. |
