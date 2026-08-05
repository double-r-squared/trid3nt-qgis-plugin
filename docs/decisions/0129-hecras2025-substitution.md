# ADR 0129 -- HEC-RAS 2025 Beta: Linux native-SUBSTITUTION experiment (meshing verbs)

Status: accepted (2026-08-04)
Follows: ADR 0127 (the HEC-RAS 2025 headless spike -- NO-GO-YET: managed .NET 9 CLI
is Linux-portable, the public 1.0.44 beta ships win-x64 natives only). This ADR
answers NATE's decisive follow-up: do the beta's MESHING verbs (createterrain /
mesh / prepare) run on Linux when the OPEN-SOURCE native libraries are substituted
for HEC's win-x64 payload -- or do they, like the solver, require the Windows-only
compute kernel? Lands NO code (registry 188 unchanged by construction); the whole
experiment lives in `scratchpad/hecras2025_subst_proofs/` (image `hecras2025:subst-exp`).

## Context: the exact question NATE posed

ADR 0127 proved the managed `ras` CLI loads on Linux but `ras healthcheck` dies at
`GDALSetup.InitializeMultiplatform` dlopen'ing an absent `gdal_wrap.so`. The open
question NATE set: only if we CONFIRM the beta's code CANNOT be used do we build our
own preprocessor. The decisive sub-question -- is the meshing path gated on the
closed win-x64 solver kernel (`RasNativeParallel.dll`), or only on substitutable
open-source libraries (GDAL / HDF5 / SQLite / DSS)?

## The experiment (iterative, evidence-driven; all live)

Built `hecras2025:subst-exp` FROM the ADR 0127 probe, substituting the natives one
dlopen-failure at a time, then running the verb chain on a synthetic 32x32 EPSG:26915
GeoTIFF. Static confirmation via ILSpy decompilation of the verb handlers + the
GDAL/HDF5/DSS native-load code.

### Finding 1 -- every meshing verb initializes GDAL FIRST (never the solver)

Run natives-absent in the probe, all three verbs print `Initializing GDAL...` and
die there ("No GDAL directory found"). Decompiled: `CreateTerrainArgs`, `MeshArgs`,
`PrepareArgs` all call `GDALSetup.InitializeMultiplatform()` as step 1. The solver
kernel `RasNativeParallel.dll` is P/Invoked by EXACTLY ONE assembly, `Utility.Math.x86.dll`
(Cdecl, 13 fns), on the SOLVE path -- `ras.dll` itself has zero references to it.

### Finding 2 -- the GDAL discovery + native contract (decompiled)

`GDALSetup.FindGDALDirectory()` searches `<app>/GDAL`, env `RAS_GDAL`, then
`$HOME/Work/GDAL`. The dir needs `common/data/` (GDAL_DATA + PROJ_LIB), `bin/` (CLI
tools it shells out to), and `lib/` from which a `DllImportResolver` loads
`lib{gdal,gdalconst,ogr,osr}_wrap.so`. HDF5: `H5.Bindings` P/Invokes `hdf5` /
`hdf5_hl` (standard HDF5 C API). SQLite: `e_sqlite3`. DSS: `hecdss` (never dlopened
by a meshing verb).

### Finding 3 -- the substitution recipe that WORKS

| native | substitute | result |
| --- | --- | --- |
| GDAL | conda-forge **GDAL 3.11.5** + 4 SWIG C# wraps built from GDAL 3.11.5 source + a 2-line alias shim | healthcheck exit 0, GDAL 3.11.5 initialized |
| HDF5 | apt libhdf5 1.10.8 (or conda libhdf5) + unversioned `libhdf5.so`/`libhdf5_hl.so` symlinks | writes/reads the consolidated project `.h5` |
| SQLite | `SQLitePCLRaw.lib.e_sqlite3` 2.1.10 linux-x64 `libe_sqlite3.so` beside the app | resolves |
| DSS | none needed | not dlopened by any meshing verb |
| `RasNativeParallel` (solver kernel, closed win-x64) | **NONE -- never reached by a meshing verb** | -- |

### Finding 4 -- HEC ships a CUSTOMIZED GDAL C# binding (the real wall, not the kernel)

The managed side P/Invokes **1826** `CSharp_OSGeof*` entrypoints. Stock GDAL 3.11.5
SWIG wraps cover **1737**; **89 are missing** -- 8 are GDAL Algorithm-API additions
(3.12+), and **81 are HEC's own binding extensions** absent from public GDAL of ANY
version: band arithmetic (`Band_BinaryOpBand`, `Band_IfThenElse`, `Band_Max/Min/MeanOfNBands`,
`Band_UnaryOp`), `ComputedBand`, fast-extent (`GDsCFastGetExtent`), `Dataset_AsMDArray`,
and an `OpenShared`/`Open` **overload** (stock exports plain `OpenShared___`; HEC
expects `OpenShared__SWIG_1___`). So a naive stock-GDAL substitution hits
`EntryPointNotFound` -- NOT the closed kernel, and NOT an unbuildable OS library, but
HEC's proprietary binding layer. The meshing path needed ONLY the `OpenShared`
overload from that set, reconstructed with a 2-line `extern "C"` alias forwarding to
stock `OpenShared`; the deep band-algebra / `ComputedBand` entrypoints were NOT
exercised by createterrain + mesh.

## Per-verb result (substituted GDAL 3.11.5 + HDF5 + SQLite)

| verb | exit | evidence |
| --- | --- | --- |
| `healthcheck` | **0** (was 3 on probe) | GDAL 3.11.5 init, reprojection healthcheck passed, 0->100% compute pass |
| `createterrain` | **0 -- COMPLETES** | reproj check, tile export + overviews, terrain `.h5` (5221 B) + intermediate `.tif` written |
| `mesh` | **0 -- RUNS clean** | init GDAL, "Computing Mesh", "Saving Geometry"; seeded by the terrain CoreLayer h5 (NO real 2D flow-area geometry available to author -- input-limited, NOT native-limited) |
| `prepare` | 255 -- INPUT gate | "Could not parse source" -- needs a valid `.ras` project/plan; none exists to seed. Passes GDAL init; no native wall |

The produced project `.h5` is a standard, readable HDF5 schema (h5dump):
`/Geometry`, `/Geometry/Mesh Topology/Arc Attributes`, `/Terrain`, `/Terrain/Attributes`,
`/Terrain/<raster>` -- readable with stock h5py/h5dump (relevant to any future
6.x-transplant characterization; no transplant attempted).

## Decision / verdict

**The meshing verbs do NOT require the Windows-only solver kernel.** They gate on GDAL
+ HDF5 (both substituted with open-source builds and WORKING) plus HEC's customized
GDAL C# binding. `createterrain` **works on Linux today**; `mesh` **runs to completion**;
`prepare` is **input-gated, not native-gated**. Per NATE's directive ("only if we
CONFIRM the code cannot be used do we build our own preprocessor") -- we do NOT confirm
impossibility. The owned-preprocessor decision is **not forced**; a substitution path
is viable and reproducibly documented.

This is verdict (a) for createterrain, (a)-with-caveat for mesh, (c)-partial for prepare.

## Consequences

- Reproducible recipe captured in `scratchpad/hecras2025_subst_proofs/`
  (`Dockerfile.subst-exp-v2` + `build2/`/`build3/` wrap-build scripts + the 4 shimmed
  `.so` + fixtures `T2.h5` / `T2.<raster>.tif` / `tiny_dem.tif`). Image
  `hecras2025:subst-exp` = **0.54 GB** (probe 0.14 GB base + conda GDAL 3.11 stack).
- No server / worker / tool / contract change; registry **188 unchanged** (git: only
  scratchpad + docs). Offline suite untouched (no server code).

## Open issues

- **The one untested leg is the crux one.** `prepare` computes the subgrid PROPERTY
  TABLES -- the exact capability the 6.x M3 STOP lacked (ADR 0100). It is gated here by
  INPUT (no seed project/geometry -- the beta ships none, and there is no geometry-
  authoring verb), AND it is the stage MOST likely to invoke HEC's 81 custom band-algebra
  / `ComputedBand` / fast-extent entrypoints (subgrid tables are raster-sampling band
  ops). Whether `prepare` completes on Linux therefore remains OPEN on two counts.
  Next step to close it: author (or obtain) a minimal valid 2025 `.ras`/geometry seed,
  then run `mesh` -> `prepare` and read the first `EntryPointNotFound` (if any) -- if it
  names a band-algebra/`ComputedBand` fn, that entrypoint is unreproducible from public
  GDAL and the property-table leg is blocked short of HEC publishing their binding source.
- The `OpenShared` alias is a semantically-correct reconstruction (verified: projection
  matched, terrain written correctly), but it is a workaround for HEC's unpublished
  binding fork; a full reproduction would need HEC's `.i` typemaps.
- GDAL exact version: HEC's binding is >= 3.11 (Algorithm API present) with ~81 custom
  additions; 3.11.5 stock is a near-superset and sufficed for terrain+mesh.
- Version-pinning: the beta `ras` build is `-dev` / schema-unstable; re-characterize
  every version bump (ADR 0127 policy carries).
