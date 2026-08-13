# ADR 0127 -- HEC-RAS 2025 Beta headless spike: characterization + NO-GO-YET

Status: accepted (2026-08-04)
Follows: ADR 0100 (mesh wave M3 -- the 6.x worker image + the headless-2D-mesh
WRITE STOP: RASMapper's subgrid property tables need Windows RASMapperLib DLLs),
ADR 0109 (HEC-RAS engine #11 landing, template-first, Muncie flow forcing), ADR
0125 (the levee-breach archetype + the archetype triage that PARKED the
headless-subgrid frontier for NATE), and the NATE-signed ras-commander feasibility
spike (`reports/design/ras-commander-feasibility-2026-08-03.md`, S2a Path-2).

Scope: a research + characterization SPIKE of the HEC-RAS 2025 Linux-native beta
-- NATE's decision that the 2025 line is the frontier path that retires the 6.x
Windows-Phase-1 blocker (real-AOI mesh authoring + rain-on-grid). This ADR
OBTAINS + characterizes the beta, BUILDS a probe worker, attempts ONE small
headless run, and maps the landing. It lands NO registered tool (registry 188
unchanged). The engine landing follows separately.

## Context: what NATE's decision banks on

The 6.x path (ADR 0100/0109/0125) is boxed in by ONE blocker: the 2D subgrid
property tables (`Cells Volume Elevation Values` / `Faces Area Elevation Values`)
that `RasUnsteady` requires are authored by RASMapper's Windows-only DLLs, and
`RasGeomPreprocess` does NOT build them on Linux. So every 6.x archetype is frozen
to a shipped RASMapper-built geometry -- no real-AOI authoring, no headless
rain-on-grid. HEC-RAS 2025 is HEC's ground-up C#/.NET rewrite with a NATIVE
mesher; NATE's decision is that its Linux-native headless surface retires that
blocker. The spike's job is the truth of the beta's CURRENT state.

## 1. OBTAINED + pinned (primary sources verified)

- Release: hec-downloads (github.com/HydrologicEngineeringCenter/hec-downloads)
  release **1.0.44**, name "HEC-RAS 2025 Beta", published **2026-03-30**.
- Asset: `HEC-RAS_2025_Beta.zip`, 133,535,791 bytes (127 MiB).
  URL: https://github.com/HydrologicEngineeringCenter/hec-downloads/releases/download/1.0.44/HEC-RAS_2025_Beta.zip
  **SHA-256 `0df9cf0d29dc6cd6c50636bca9cf8206c0f83d1fa8a62a03c37f748daf9f5cf5`**
  (verified on the characterization run 2026-08-04).
- `ras` engine build string: **0.1.0.2965-dev**; target **net9.0**,
  Microsoft.NETCore.App 9.0.9; publish RID **win-x64, self-contained**.
- Provenance: HEC-RAS is public-domain U.S. Federal Government software, freely
  redistributable (same terms as the 6.x worker, feasibility S6). BETA -- USACE
  says do NOT use HEC-RAS 2025 for production studies.

## 2. CHARACTERIZED -- the headless surface (honest, verified on Linux)

The 2025 architecture is a genuine cloud/headless redesign and is EXACTLY what
NATE's decision described:

- **Project model:** a single consolidated HDF5 (`{Geometry}.h5`); the docs pitch
  "copy `{GeometryName}.h5`" to move a model. Confirmed by the `H5.Bindings` dep +
  the `ras` HDF5 tooling verbs (decompose/diff/squeeze/explore/restore).
- **The `ras` CLI is the headless surface** (CommandLineParser + Spectre.Console).
  The full documented verb pipeline, in order:
  `createterrain` (RAS terrain from a priority-ordered raster set)
  -> `mesh` (generate the computational mesh from a geometry file)
  -> `prepare` ("Ingests all external data sources, **computes property tables**,
     and does any other preprocessing... Creates a single ready-to-run (R2R) file")
  -> `solve` ("Run the RAS solver on different inputs")
  -> `map` (result -> map/raster, with a `V:R,G,B|...` color-ramp).
  Plus `clone`, `info`, `hash`, `explore`, `healthcheck`, `cleanup`, `ui/gui`.
  **`prepare` computing the property tables headless is the exact capability the
  6.x M3 STOP lacked** -- the native mesher + preprocessor NATE's decision banks on.
- **Solve model:** `ras solve <source> <output>`; source resolves a project
  (`*.ras`), a plan (`*.h5`), or a ready-to-run (`*.r2r.h5`); output is a result
  `*.h5`. Flags: `--solver CPU|GPU` (a **CPU** solver exists -- GPU/CUDA is
  optional, via `Ras.Engine.CUDA.dll`) and `--core-count`.
- **Precipitation / physics legs (from the USACE 2025 page + the binary):** the
  engine carries a Precipitation concept (`get_Precipitation` symbol), but the
  official 2025 roadmap lists **precipitation/rain-on-grid as "2026"** (not
  confirmed in the current beta), **1D hydraulics "comes later"**, and
  **breaching / advanced structures "2027"**. Current beta = **2D hydraulics +
  native mesh generation (quad/cartesian/triangular) + basic terrain + basic
  structures**. So the rain-on-grid archetype NATE targets is NOT yet a confirmed
  beta capability even once Linux lands -- it is on the same 2026 roadmap.
- **A documented public API** is stated as a goal ("We want to support these
  groups with a documented, public API"); today the stable public headless
  contract is the `ras` CLI above (plus the AWS S3 project push/pull/presign
  verbs -- cloud-native distribution is baked in via `AWSSDK.S3`).

## 3. THE BUILD -- `services/workers/hecras2025/` (a probe, not a solver)

A multi-stage image (`trid3nt-local/hecras2025:probe`): stage `fetch` downloads +
SHA-verifies the beta zip and keeps ONLY the portable managed .NET assemblies;
stage `runtime` is `mcr.microsoft.com/dotnet/runtime:9.0` + those assemblies + a
framework-dependent `ras.runtimeconfig.json` + the probe entrypoint.

**Image size (container-hygiene, `docker history`): 542 MB.**
| layer | size |
| --- | --- |
| .NET 9 shared runtime base (debian + apt/ICU + dotnet) | ~293 MB (base image) |
| HEC-RAS 2025 managed .NET assemblies (283 DLLs) | 191 MB |
| framework-dep runtimeconfig + entrypoint + workdir | < 60 kB |

Hygiene actions: the 127 MB zip + curl/unzip stay in `fetch`; the **159 MB of
win-x64 NATIVE DLLs are DROPPED** (they cannot run on Linux); the `.exe` apphosts
are dropped. A further headless-only managed trim (dropping the WinUI/Visual GUI
managed set still in the 191 MB) is a future step once the Linux natives exist and
the exact solve assembly graph is pinned.

## 4. THE SMOKE -- the hard gate: NO-GO-YET (the honest truth)

There is **no beta-shipped example project** in the Windows zip (no `.rasproj` /
`.ras` / `.h5`), so the preferred "run a shipped example" smoke is unavailable;
the fallback synthetic-authoring smoke is likewise blocked. Both block at the SAME
root cause, proven empirically at two layers:

- **LAYER 1 (managed .NET CLI): PORTABLE -- runs on Linux.** With the shipped
  self-contained `ras.runtimeconfig.json` rewritten framework-dependent,
  `dotnet ras.dll --version` -> `ras 0.1.0.2965-dev` and `--help` prints the full
  verb surface (createterrain/mesh/prepare/solve/map...) on `dotnet/runtime:9.0`.
  The managed rewrite is genuinely cross-platform.
- **LAYER 2 (native compute/geospatial): BLOCKED -- no Linux payload published.**
  `ras healthcheck` reaches a MULTIPLATFORM native path
  (`GDALSetup.InitializeMultiplatform`) and `dlopen`s the **Linux** SWIG binding
  `gdal_wrap.so` / `libgdal_wrap.so` -- absent from the package (only Windows
  `gdal.dll`/`.exe` ship). `ras.deps.json` lists **only `runtimes/win-x64/native/*`**
  (13 natives: `RasNativeParallel.dll`, `hdf5.dll`, `hecdss.dll`, `libiomp5md.dll`,
  `e_sqlite3.dll`, ...) and **zero** linux-x64/linux-arm64 assets. The compute
  kernel `RasNativeParallel.dll` is a native Windows PE with no Linux `.so`
  anywhere. The official download page confirms it: HEC-RAS 2025 is offered for
  **Windows 10/11 64-bit only**; there is no Linux/container download.

The probe image reproduces this live (`docker run ... trid3nt-local/hecras2025:probe`
-> managed CLI runs, healthcheck hits the native gap, VERDICT NO-GO-YET, **exit 3**,
`hecras2025_probe.json = {"managed_cli_runs": true, "native_payload_present": false,
"go": false}`).

**VERDICT: NO-GO-YET.** NATE's direction is CORRECT -- the 2025 architecture
(portable managed CLI + multiplatform native design + `ras prepare` computing
property tables headless) is precisely the frontier that retires the 6.x M3 STOP.
The single missing artifact is HEC's Linux-native payload: the public 1.0.44 beta
is win-x64 only. **Version to watch:** the first hec-downloads release (or a
separate container/registry publish) carrying a **linux-x64** HEC-RAS 2025 payload.

## 5. THE LANDING MAP (when the Linux payload lands)

- **Rain-on-grid archetype on this path needs, in order:** (a) HEC's linux-x64
  natives (the gate); (b) confirmation that precipitation forcing is IN the shipped
  build (roadmap "2026" -- verify, do not assume); (c) `createterrain` fed from our
  `fetch_dem` / topobathy rasters (priority-ordered); (d) `mesh` + `prepare` to
  author the 2D mesh + property tables headless over a real AOI (no RASMapper --
  THIS is the unblock); (e) the precip boundary wired into the `.h5` project via
  `ras set` (headless "open a file, set a parameter, save") from our NOAA Atlas-14
  / NWM precip seam; (f) `ras solve --solver CPU`; (g) postprocess the result
  `.h5` (either `ras map` -> raster, or a pure-`h5py` depth/WSE read mirroring the
  6.x `postprocess_hecras`) -> depth COG, honestly reusing `continuous_flood_depth`.
- **Worker shape:** the probe image flips to a solver image by adding the linux-x64
  natives beside the managed assemblies -- entrypoint changes from healthcheck-probe
  to `createterrain -> mesh -> prepare -> solve -> map`, bind-mount `/data` (the 6.x
  worker pattern). No server contract rewrite: a new `run_solver('hecras2025_*')`
  local-docker spec + a template, deck-authoring + postprocess seams thin (feasibility
  S2a: a Path-2 swap stays worker-internal).
- **2025-vs-6.x cross-check (NATE's overlap-validation doctrine):** the Muncie
  White River question is expressible on BOTH solvers -- 6.x (frozen RASMapper
  geometry, `hecras_muncie_flood`) and 2025 (headless-authored mesh over the same
  reach). Run the same inflow question through both; agreement on peak depth / WSE /
  wet-cell extent within a documented tolerance validates the 2025 headless authoring
  against the production-stable 6.x result. This is the first landing's acceptance
  gate, not a later nicety.
- **Fidelity-label contract:** 2025 results carry a `solver="hecras2025-beta"` +
  `fidelity` note stamping BETA (USACE "not for studies") until 2025 reaches stable
  1.0; refinement-grade V&V claims stay on the 6.x production solver until then
  (fidelity-ladder doctrine: refinement-grade must be production-stable).
- **Version-pinning / churn policy:** pin the exact hec-downloads release tag +
  SHA-256 in the Dockerfile ARG (as done here for 1.0.44); the beta `ras` build
  string is `-dev` and the file schema is unstable, so treat every version bump as
  a re-characterization (re-run the probe + the cross-check) before advancing the
  fidelity label.

## Consequences

- New worker `services/workers/hecras2025/` (Dockerfile + entrypoint.sh +
  `probe_ras_cli.py` parser + `test_probe_ras_cli.py` (5 cases, flat-import) +
  `ras.fwdep.runtimeconfig.json` + captured CLI fixtures + README). NO server /
  workflows / tools / contracts / categories change -- **registry 188 unchanged by
  construction** (git: only the new untracked worker dir). Offline server suite
  untouched (no server code). Worker probe tests: 5 passed.
- Proofs under `scratchpad/hecras2025_proofs/` (release+SHA, package
  characterization, CLI verb surface, the Linux native-gap trace, the live image
  probe run + JSON verdict).
- The HEC-RAS 2025 migration stays QUEUED in the ledger (the strategic frontier of
  ADR 0125 finding 5), now with the precise CONDITION-to-land: **HEC publishes a
  linux-x64 HEC-RAS 2025 payload** (+ precip forcing confirmed in-build for the
  rain-on-grid archetype). Direction validated; enabling artifact not yet shipped.

## Open issues

- HEC's Linux/container build is not on hec-downloads yet; monitor the releases
  feed for a linux-x64 HEC-RAS 2025 asset (or a separately-published container).
- Precipitation forcing is on the 2026 roadmap, not confirmed in the current beta
  -- the rain-on-grid archetype is gated on BOTH Linux AND precip landing.
- The 191 MB managed layer still carries the WinUI/Visual GUI managed set; a
  headless-only trim awaits the real solve assembly graph.
