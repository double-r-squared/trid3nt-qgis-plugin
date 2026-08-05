# HEC-RAS 2025 Beta -- Linux headless characterization worker (ADR 0127 spike)

HEC-RAS 2025 is HEC's ground-up **C#/.NET rewrite**: a single-`.h5` project, a new
explicit solver, a **native headless mesher**, and a documented `ras` CLI. NATE's
decision banks on this line to retire the 6.x **M3 STOP** (RASMapper's Windows-DLL
subgrid property tables blocking every real-AOI / rain-on-grid archetype) -- the
2025 `ras prepare` verb **computes those property tables headless**.

## Spike verdict: NO-GO-YET (2026-08-04)

- **Managed .NET 9 CLI is Linux-portable.** `dotnet ras.dll --help` prints the full
  headless verb pipeline on `dotnet/runtime:9.0`:
  `createterrain -> mesh -> prepare -> solve -> map` (+ clone/info/hash/explore/
  healthcheck/cleanup). `ras solve` takes a project/plan/ready-to-run `.h5` and a
  `--solver CPU|GPU`.
- **Native compute payload is win-x64 ONLY.** The public release
  (`HEC-RAS_2025_Beta.zip`, hec-downloads **1.0.44**, 2026-03-30, sha256
  `0df9cf0d29dc6cd6c50636bca9cf8206c0f83d1fa8a62a03c37f748daf9f5cf5`) is a
  self-contained win-x64 publish: `ras.deps.json` lists only
  `runtimes/win-x64/native/*` (`RasNativeParallel`, `hdf5`, `hecdss`, `libiomp5md`,
  ...) and `ras healthcheck` on Linux fails at `dlopen('gdal_wrap.so')`. HEC's
  download page offers HEC-RAS 2025 for **Windows 10/11 64-bit only** -- no
  Linux/container build is published. So **no headless SOLVE is possible yet.**

This worker is therefore the reproducible **characterization probe + flip-ready
host**, NOT a registered engine. Full analysis + the landing map:
`docs/decisions/0127-hecras-2025-spike.md`.

## What lands here

- `Dockerfile` -- multi-stage: `fetch` downloads + SHA-verifies the beta zip and
  keeps ONLY the portable managed .NET assemblies (drops the 159 MB of win-x64
  natives + the `.exe` apphosts); `runtime` = `dotnet/runtime:9.0` + those
  assemblies + a framework-dependent `ras.runtimeconfig.json` + the probe. 542 MB.
- `entrypoint.sh` -- runs `ras --version` / `--help` / `healthcheck`, classifies the
  verdict (managed-portable vs native-gap), writes `hecras2025_probe.json`, exits
  **3** on NO-GO-YET (a distinct, honest non-zero -- a characterized gap, not a crash).
- `probe_ras_cli.py` -- flat-importable parsers: `parse_verb_surface` (the `ras
  --help` verb map) + `classify_linux_run` (native-gap classifier). `test_probe_ras_cli.py`
  (5 cases) runs offline against the captured `fixtures/` outputs.
- `ras.fwdep.runtimeconfig.json` -- framework-dependent config that replaces the
  shipped self-contained one so the managed CLI loads on the Linux shared runtime.

## Build + run

```sh
docker build -t trid3nt-local/hecras2025:probe services/workers/hecras2025/

rundir=$(mktemp -d)
docker run --rm -v "$rundir":/data trid3nt-local/hecras2025:probe   # exit 3 = NO-GO-YET
cat "$rundir/hecras2025_probe.json"

# worker probe tests (flat-import, offline, no docker/network):
cd services/workers/hecras2025 && python3 -m pytest test_probe_ras_cli.py -q
```

## The flip path (when HEC ships the Linux natives)

Add the linux-x64 natives (`libRasNativeParallel.so`, the Linux SWIG `gdal_wrap.so`
+ GDAL, `libhdf5`, `hecdss`) beside the managed assemblies; swap the entrypoint from
the healthcheck probe to `createterrain -> mesh -> prepare -> solve -> map`,
bind-mount `/data` (the 6.x worker pattern). Re-characterize on every version bump
(the beta `ras` build is `-dev`, schema unstable). Confirm precipitation forcing is
in-build (roadmap "2026") before the rain-on-grid archetype.

## Provenance

HEC-RAS is public-domain U.S. Federal Government software, freely redistributable
(acknowledgment: U.S. Army Corps of Engineers, Hydrologic Engineering Center).
**BETA** -- USACE says do NOT use HEC-RAS 2025 for production studies.
