# beaver_creek_steady -- VERIFY results (ADR 0170 front)

Ran against `trid3nt-local/hecras:latest` (image id `5d4ac7cfbc8c`), on a
SCRATCH copy (`docker run --rm -v <scratch>:/data --entrypoint bash ...`).
Fixture files themselves untouched (verified `git status` clean after).

## RasGeomPreprocess: REFUSES this file -- confirms + generalizes the ADR
## 0170/0171 "missing plan HDF" diagnosis

```
$ docker run --rm -v <scratch>:/data --entrypoint bash trid3nt-local/hecras:latest \
    -c "cd /data && RasGeomPreprocess BEAVCREK.g01.hdf g01"

forrtl: No such file or directory
forrtl: severe (29): file not found, unit 5, file /data/io.x
  ... htabopen_ (Htabopen.for:107) <- MAIN__ (Htab.for:33) ...
```

This is the SAME fallback path (`Htabopen.for`/`Htab.for`, `io.x` on unit 5)
that fires when `RasGeomPreprocess` is invoked with **zero arguments** or a
bogus first argument. It does NOT reach the 1D cross-section conveyance code
`RasGeomPreprocess` ran successfully against Muncie's plan HDF in ADR 0170.

**Root cause, nailed down precisely (the new finding this fixture adds):**
`RasGeomPreprocess`'s modern 2-arg CLI (`<plan_hdf> <geom_suffix>`) only
engages when the first argument is an HDF5 file whose root HDF5 attribute
`File Type` reads `"HEC-RAS Results"` (a genuine per-PLAN artifact carrying
`Plan Data` / `Event Conditions` / `Geometry` top-level groups). `BEAVCREK.
g01.hdf` is a **GEOMETRY-ONLY** HDF (`File Type = "HEC-RAS Geometry"`, single
top-level group `Geometry`, no `Plan Data`, no `Event Conditions` -- verified
below). Passed a file lacking that shape, the program silently discards its
argv and falls into the legacy `io.x` batch-input code path -- the exact same
crash as running with no arguments at all:

```python
>>> h5py.File("BEAVCREK.g01.hdf")["/"].attrs["File Type"]
b'HEC-RAS Geometry'
>>> h5py.File("Muncie.p04.tmp.hdf")["/"].attrs["File Type"]   # ADR 0170's WORKING case
b'HEC-RAS Results'
```

So the ADR 0170 "missing steady reference deck" finding generalizes: it is
not merely that the shipped Muncie plan HDF is unsteady-typed -- **no public
HEC-RAS example project ships a `File Type="HEC-RAS Results"` (plan-level)
HDF for a STEADY plan at all.** `RasGeomPreprocess` never gets far enough to
even attempt the steady network-sizes read on a geometry-only file; it
rejects the input one level earlier, at argument recognition.

## RasSteady: reproduces the ADR 0170 `Read_siz` abort verbatim on THIS input

```
$ RasSteady BEAVCREK.g01.hdf g01

forrtl: severe (24): end-of-file during read, unit 15, file /data/BEAVCREK.g01.hdf
  read_siz_is_post_ (Read_siz.for:349) <- snetopen_ (Snetopen.for:189) <- MAIN__ (Snet.for:88)
```

Identical routine chain, identical line number, to ADR 0170's abort on the
Muncie unsteady plan HDF. This is now confirmed on TWO structurally
different inputs (an unsteady-typed plan HDF, and a geometry-only HDF with
no Plan Data at all) -- both are missing the same steady "network sizes"
structure `Read_siz.for` expects. Reinforces (does not merely repeat) ADR
0170's conclusion: the blocker is a genuinely absent artifact class, not an
input-specific quirk.

## No genuine "Finished Steady Flow Simulation" run achieved

Per the mission's directive: TESTED, did not succeed, documented honestly.
The one alternate `RasSteady` invocation this job also empirically checked
(`RasSteady <basename>.r04`, the exact syntax HEC's own `Muncie/run_steady.sh`
+ the bundled `RAS_v.6.6_Linux.pdf` slide deck use -- full transcript in
ADR 0172) is a DIFFERENT, narrower operation than a general steady-network
solve: it is a restart-file POST-PROCESSING pass (sentinel `Finished Post
Processing`, output `.O0N`), not the `Finished Steady Flow Simulation`
sentinel ADR 0170 named, and it requires an unsteady run to have completed
first -- it does not apply to a from-scratch steady deck like Beaver Creek
(which has no `.rNN` restart file; that file is only produced by an unsteady
run).

## HDF schema actually captured (real GUI output, not synthetic)

`BEAVCREK.g0N.hdf` root: `File Type="HEC-RAS Geometry"`, `File Version=
"HEC-RAS 6.3 Beta 2 Development"`, single top-level group `Geometry`:

- `Geometry/Cross Sections`: `Attributes`, `Station Elevation Info/Values`,
  `Manning's n Info/Values`, `Ineffective Info`/`Ineffective Blocks`,
  `Polyline Info/Parts/Points` -- the real 1D cross-section property-table
  input schema (a genuine GUI-authored reference, not reverse-engineered).
- `Geometry/River Centerlines`: `Attributes`, `Polyline Info/Parts/Points`.
- `Geometry/Structures`: `Attributes` (compound), `Bridge Coefficient
  Attributes`, `Pier Attributes`, `Pier Data`, `Centerline Info/Parts/
  Points`, `Profile Data`, `Table Info` -- a **BRIDGE** structure schema
  (piers), distinct from Muncie's lateral weir and the `baldeagle_connection`
  fixture's SA/2D connections. Bonus reference for any future bridge-deck
  authoring front (not scoped to this job).
- `Geometry/Land Cover (Manning's n)`: `Calibration Table`.

## What this unblocks

- The ADR 0170 recipe step 1 ("obtain a 1D steady reference fixture") is now
  DONE as far as public-domain availability goes -- but the recipe's
  premise (a public download would carry a computed steady PLAN hdf) does
  NOT hold: it does not exist in HEC's public distribution. The genuine
  unblock still requires either (a) a Windows HEC-RAS GUI compute session
  (out of scope here -- no Windows/GUI available), or (b) reverse-engineering
  the `Plan Data`/`Event Conditions`/steady-network-sizes HDF schema from the
  Fortran `Read_siz`/`Snetopen` readers with the ASCII `.pNN`/`.fNN` text as
  the only guide (the ADR 0170 multi-day leg, now DEFINITIVELY the only path
  -- no shortcut survives this fixture hunt).
- Real GUI-computed 1D cross-section + bridge-structure HDF schema (above) is
  still a genuine, durable diff reference for any future 1D authoring work,
  independent of the steady-solve wall.
