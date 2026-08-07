# beaver_creek_steady -- HEC-RAS Applications Guide Example 2 (Beaver Creek)

## What this is

A 1D STEADY-flow example project (Beaver Creek near Kentwood, Louisiana),
HEC's own "Applications Guide Example 2" -- the canonical steady-flow
teaching case (bridge/culvert hydraulics, four plans covering different
computation methods). Seeded for ADR 0170 (HEC-RAS 1D steady / RasSteady
front).

## Source

- Zip: `Example_Projects_7_0.zip` ("HEC-RAS 7.0 Example Projects" -- "This
  file contains all of the HEC-RAS example projects", 408 MB per the HEC
  download page).
- Official download page (linked the exact URL below):
  `https://www.hec.usace.army.mil/software/hec-ras/download.aspx`
- Direct download URL (HEC's own GitHub release mirror -- `hec-downloads` is
  the official HEC org distributing installers/examples since the USACE site
  stopped hosting large binaries directly):
  `https://github.com/HydrologicEngineeringCenter/hec-downloads/releases/download/1.0.45/Example_Projects_7_0.zip`
- Zip SHA-256: `fac04f071e624c841b20e943e3b68f351b4531f565a0c24ab7d885cf9e38d523`
  (427,304,097 bytes; verified against the `Content-Length` the server sent
  and re-hashed after download).
- Path inside the zip: `Applications Guide/Example 2 - Beaver Creek/`.
- Public domain: HEC-RAS + its example projects are U.S. Federal Government
  work ("developed with U.S. Federal Government resources and is therefore
  in the public domain. It may be used, copied, distributed, or
  redistributed freely" -- HEC's own terms, already relied on for the
  `muncie_smoke` fixture). Acknowledgment: U.S. Army Corps of Engineers,
  Hydrologic Engineering Center.

## Contents (all files verbatim from the zip, no edits)

| file | role |
| --- | --- |
| `BEAVCREK.prj` | project file -- lists 4 plans, geometry g01-g04, description |
| `BEAVCREK.g01`..`g04` | 1D geometry text (cross-sections; the 4 geometries differ by bridge-modeling method) |
| `BEAVCREK.g01.hdf`..`g04.hdf` | **GUI-computed GEOMETRY-ONLY HDF** (root attr `File Type=HEC-RAS Geometry`) -- real cross-section property tables, no Plan Data |
| `BEAVCREK.f01`, `.f02` | steady flow text (profiles + boundary conditions) |
| `BEAVCREK.p01`..`p04` | plan text (`Program Version=5.00`, `Subcritical Flow`, `Run UNet=0` -- genuine STEADY plans, not unsteady) |
| `BEAVCREK.h01` | bridge/culvert hydraulic-table cache |
| `BEAVCREK.dss`, `.dsc` | DSS time-series container + descriptor |
| `BEAVCREK.rasmap` | RASMapper project (not needed for the CLI solve; kept small, harmless) |
| `BEAVCREK.SedCap01` | sediment-capacity cache (unused by the steady front; harmless) |

Trimmed: the zip's `BCREEK.BMP` (a scanned drawing image) was dropped -- pure
documentation art, no solver/schema value. Nothing else was trimmed; this
project ships no terrain rasters.

Size: 596 KB seeded (zip total 427 MB) -- essentially the whole example
project, since it is already small.

## Plans

| plan | title | geom | flow |
| --- | --- | --- | --- |
| p01 | Press/Weir Method | g01 | f01 |
| p02 | Energy Method | g02 | f01 |
| p03 | Press/Weir Method : New Le, Lc | g03 | f01 |
| p04 | Energy Method : New Le, Lc | g04 | f01 |

See `schema_notes.md` for the VERIFY results (RasGeomPreprocess/RasSteady
behavior against `trid3nt-local/hecras:latest`).
