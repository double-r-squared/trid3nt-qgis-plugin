# Muncie smoke fixture -- mesh wave M3 acceptance gate

HEC's own shipped **Muncie** test project (White River, Muncie IN) -- a combined
1D (61 cross-sections) + 2D (5765-cell flow area) unsteady model. It is the model
HEC uses to verify the Linux computation engines, so it doubles as the M3
acceptance gate ("no green Muncie, no M3").

## Provenance

- Source: HEC official `Linux_RAS_v66.zip`, `Linux_RAS_v66/Muncie/wrk_source/`.
  - Download: `https://www.hec.usace.army.mil/software/hec-ras/downloads/Linux_RAS_v66.zip`
  - Zip SHA-256: `e77271a473da5da28b5a95ebf019f77ba3d32fb6341ad43be3ad4a6004c60e4a`
- Public domain: HEC-RAS + its example projects are U.S. Federal Government work,
  freely redistributable (acknowledgment: U.S. Army Corps of Engineers, HEC).

## `wrk_source/` inputs (GUI-computed, unmodified)

| file | role |
| --- | --- |
| `Muncie.p04.tmp.hdf` | plan HDF (Results stripped) -- carries geometry + the GUI-computed hydraulic property tables; RASMapper built the 2D flow-area mesh + subgrid tables |
| `Muncie.x04` | 1D geometry (cross-sections) -- the `geom_suffix` argument (`x04`) |
| `Muncie.b04` | unsteady boundary conditions (flow hydrograph + normal depth) |

## The gate (two comparison bases, documented tolerances)

Run `muncie_smoke.py` (host: set `TRID3NT_HECRAS_ROOT` to an extracted
`Linux_RAS_v66/`; in-container: defaults to `/opt/hecras`):

- **Gate A -- hydraulic property tables** (`RasGeomPreprocess`): the Linux
  preprocessor rebuilds the 1D cross-section conveyance tables from `x04`. Since
  the geometry is unchanged, they must reproduce the GUI-computed tables EXACTLY.
  Gate: `max|diff| == 0` on the 1D `XSEC Value`/`Cell Value` + the 2D
  `Cells Volume Elevation Values` / `Faces Area Elevation Values` (verified all
  zero, 2026-08-03).
- **Gate B -- volume accounting** (`RasUnsteady`): mass-balance error.
  Community Docker repro (neeraip/hecras-v66-linux) reports Muncie at ~0.00%
  volume error / 0.001 ft WSE vs the Windows GUI. Gate: `|Error Percent| < 0.05%`
  (observed **0.005835%**; 2D max water surface **951.9 ft**).

## In-container

The worker `entrypoint.py` stages `wrk_source/` into the bind-mounted `/data`
rundir via `manifest.json`, runs `RasGeomPreprocess` then `RasUnsteady`, and
writes `hecras_metrics.json` (volume accounting + max WSE).
