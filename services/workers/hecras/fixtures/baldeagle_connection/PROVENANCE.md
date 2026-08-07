# baldeagle_connection -- HEC-RAS "BaldEagleCrkMulti2D" example (SA/2D + 2D/2D connections)

## What this is

HEC's official multi-2D-area Bald Eagle Creek dam-break study (Lock Haven,
PA) -- "put together to show the various ways you can link 1D and 2D
elements" (the project's own `.prj` description). Seeded for ADR 0171
(HEC-RAS structure-authoring / SA-2D + 2D-2D connection front). This is the
EXACT reference ADR 0171 named as the recipe ("the Bald Eagle Creek
`dam-breach-analysis-with-2d-areas` tutorial is the anchor").

## Source

- `https://github.com/HydrologicEngineeringCenter/hec-downloads/releases/download/1.0.45/Example_Projects_7_0.zip`
  (linked from `https://www.hec.usace.army.mil/software/hec-ras/download.aspx`,
  "HEC-RAS 7.0 Example Projects", 408 MB)
- SHA-256 `fac04f071e624c841b20e943e3b68f351b4531f565a0c24ab7d885cf9e38d523`
  (427,304,097 bytes)
- Path inside the zip: `2D Unsteady Flow Hydraulics/BaldEagleCrkMulti2D/`
- Public domain, U.S. Federal Government work (USACE HEC), same terms as
  `muncie_smoke`.

## Size discipline -- what was seeded vs. what was in the source folder

The full source folder is **~350 MB** (13 geometries `g01`-`g13` covering
every 1D/2D linkage variant + terrain rasters + precip DSS + soils/land-cover
rasters). Per the mission's size-discipline directive, only the files needed
to (a) run the two plans that carry a `Type="Connection"` structure and (b)
diff the connection HDF schema were kept:

**Kept (8.4 MB total):**

| file | why |
| --- | --- |
| `BaldEagleDamBrk.prj` | project index |
| `BaldEagleDamBrk.g01`, `.g01.hdf` | geometry carrying the SA-to-2D dam-break connection + 3 internal-levee connections (see schema below) |
| `BaldEagleDamBrk.g11`, `.g11.hdf` | geometry carrying a genuine **2D-Flow-Area-to-2D-Flow-Area** connection (`BaldEagleCr` <-> `Upper 2D Area`) -- the exact ADR 0171 "face-pairing frontier" case; no reference for this topology existed anywhere in the repo before this fixture |
| `BaldEagleDamBrk.p01`, `.p02` | plans using g01 ("SA to Detailed 2D Breach [FEQ]") |
| `BaldEagleDamBrk.p18` | plan using g11 ("2D to 2D Run") |
| `BaldEagleDamBrk.u01`, `.u01.hdf` | unsteady flow forcing for p01/p02 |
| `BaldEagleDamBrk.u10`, `.u10.hdf` | unsteady flow forcing for p18 |

**Trimmed (not seeded, sizes from the zip listing):**

| item | size | why dropped |
| --- | --- | --- |
| `Terrain/Terrain50.baldeagledem.tif` | 178 MB | 2D subgrid property tables are already baked into the GUI-computed `g0N.hdf` (the same fact ADR 0170 established for Muncie's 1D tables); the Linux `RasGeomPreprocess` M3 wall means these terrain-derived subgrid tables are NOT recomputable on Linux anyway, so the raw DEM buys nothing for this front |
| `Precipitation/precip.2018.09.dss` | 66.6 MB | gridded-precip forcing, unused by the plans kept (p01/p02/p18 use boundary hydrographs, not gridded precip) |
| `Bald_Eagle_Creek.dss` | 30.7 MB | DSS time-series container for plans/geometries not seeded |
| `Soils Data/*.tif`, `Land Classification/*.tif` | ~24 MB combined | infiltration/land-cover rasters for the gridded-precip plan (p06), not seeded |
| `g02,g03,g06,g08,g09,g10,g12,g13` + their `.hdf` | ~30 MB combined | redundant geometry variants (other 1D/2D linkage demos not in this front's scope -- multi-2D PMF study, refined-grid dam break, bridges, etc.); `g13` ("2D Levee Structure") was extracted then DROPPED after inspection showed it duplicates g01's levee-connection schema with one fewer 2D area |
| `NLD/`, `GISData/`, `.rasmap`, `.dsc`, `.bco06`/`.b06` (unused plan's boundary file) | small | GIS overlay/display metadata, not solver inputs for the kept plans |

## Plans kept

| plan | title | geom | flow |
| --- | --- | --- | --- |
| p01 | SA to Detailed 2D Breach FEQ | g01 | u01 |
| p02 | SA to Detailed 2D Breach | g01 | u01 |
| p18 | 2D to 2D Run | g11 | u10 |

See `schema_notes.md` for the HDF `Geometry/Structures` schema dump (the
ADR 0171 diff reference) and the VERIFY results.
