# critical_creek_steady -- VERIFY results (ADR 0170 front)

Ran against `trid3nt-local/hecras:latest` (image id `5d4ac7cfbc8c`) on a
scratch copy; fixture files untouched.

## RasGeomPreprocess: same `io.x` fallback as `beaver_creek_steady`

```
$ RasGeomPreprocess CRITCREK.p01.hdf g01     # CRITCREK.p01.hdf does not exist

forrtl: severe (29): file not found, unit 5, file /data/io.x
  htabopen_ (Htabopen.for:107) <- MAIN__ (Htab.for:33)
```

Consistent with `beaver_creek_steady`: no `File Type="HEC-RAS Results"`
(plan-level) HDF exists for this project either -- Critical Creek is older
still (`Program Version=4.01`/`5.00`, legacy binary `.O0N` output, no
geometry HDF at all, unlike Beaver Creek's `.g0N.hdf`). `RasGeomPreprocess`
falls straight into the legacy `io.x` path.

## RasSteady: a DIFFERENT failure mode than `beaver_creek_steady` -- worth
## recording precisely (confirms the diagnostic is "no such file" vs.
## "wrong-shaped file", two distinct code paths)

```
$ RasSteady CRITCREK.p01.hdf g01     # file genuinely absent (vs. Beaver
                                      # Creek's g01.hdf, which EXISTS but is
                                      # the wrong shape)
File:
CRITCREK.p01.hdf
Not Found
```

No Fortran runtime crash, no stack trace -- a clean "File: ... Not Found"
message and exit 0. This is DIFFERENT from `beaver_creek_steady`'s abort
(`read_siz_is_post_`, `Read_siz.for:349`, EOF on unit 15), which fired
because `BEAVCREK.g01.hdf` EXISTS but lacks the expected `Plan Data`
structure. Together the two fixtures separate RasSteady's two distinct
early failure modes:

1. named HDF file does not exist -> clean "Not Found", no crash.
2. named HDF file exists but is not a `File Type="HEC-RAS Results"` plan HDF
   (or lacks the steady network-sizes section within one) -> Fortran EOF
   abort deep in `Read_siz.for`.

Neither reaches a "Finished Steady Flow Simulation" state. No genuine steady
solve was achieved on this fixture either -- documented per the mission's
loud-honesty directive.

## No HDF to schema-dump

This project ships no computed HDF of any kind (geometry or plan) -- it
predates HEC-RAS's HDF5 output entirely. Its schema value is purely the
ASCII `.g0N` cross-section text + `.F0N` steady-flow text (a second,
independent real-world steady deck alongside Beaver Creek's, for anyone
authoring a synthetic `.gNN`/`.fNN` steady deck by imitation -- ADR 0170's
step-4 recipe).

## What this adds beyond beaver_creek_steady

- A second, independently-sourced steady deck (breadth, not just depth).
- The clean "file not found" vs. "wrong-shaped-file abort" distinction above,
  which sharpens future error-handling/diagnostics if a steady front is ever
  built (a caller-facing error message can now distinguish "you forgot to
  stage the plan HDF" from "the plan HDF you staged is not steady-shaped").
