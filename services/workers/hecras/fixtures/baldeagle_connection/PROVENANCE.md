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

## g09 mesh added -- the Sayers Dam SA/2D connection SOLVE pair (ADR 0174)

ADR 0173 partitioned the connection blocker to a single data gap: the shipped
`pure2d_reference/BaldEagleDamBrk.x09` (`Section - Storage Area Connection Data`,
Sayers Dam, weir coef 3.1, HW/TW face-index pairing arrays) had no matching mesh,
and the seeded `g01.hdf` had no matching `.xNN`. This job seeds that missing mesh:
`BaldEagleDamBrk.g09.hdf` -- the 18066-cell `BaldEagleCr` 2D area whose sole
`Type="Connection"` structure IS Sayers Dam (weir coef 3.1, width 80, one gate
group, `Use 2D for Overflow=1`), the exact geometry the `x09` preprocessor was
emitted from.

### Source (matches the `x09`, NOT the g01/g11 above)

- `x09` is a **HEC-RAS 6.6** distribution artifact (`pure2d_reference` sha
  `ea239b50...`), so its matching mesh must come from the SAME distribution --
  `Example_Projects_6_6.zip`, NOT the `7_0` zip the g01/g11 fixtures above came
  from (a 7.0 mesh could renumber cells/faces vs the 6.2-authored `x09`).
- `https://github.com/HydrologicEngineeringCenter/hec-downloads/releases/download/1.0.33/Example_Projects_6_6.zip`
  (linked from the HEC-RAS download page; release tag `1.0.33`),
  whole-zip **432,389,121 bytes**, whole-zip SHA-256
  `ea239b506155a2dfeda2af80b3c2af948eef42c40218bcd65de472cfed386887` (the value
  recorded by `pure2d_reference/README.md` when the `x09`/`b06` were seeded;
  re-derivation would require the full 432 MB download, avoided here -- see below).
- Path inside the zip:
  `2D Unsteady Flow Hydraulics/BaldEagleCrkMulti2D/`
- Public domain, U.S. Federal Government work (USACE HEC).

### Members added (partial HTTP-Range extraction, per-member CRC-verified)

The two members were pulled by an HTTP-Range read of the zip's central directory
+ each member's local header/deflate stream (no full 432 MB download; the
tmpfs/disk-headroom directive). Each member's inflated bytes were verified against
the zip's own **CRC-32** manifest entry (integrity vs the distribution's own
checksum), and its SHA-256 recorded:

| member | inflated bytes | SHA-256 |
| --- | --- | --- |
| `BaldEagleDamBrk.g09.hdf` | 11,690,739 | `2f9a72892816824276efdd12fc758a1c89eba7beac5ec491f15b83b03965173a` |
| `BaldEagleDamBrk.g09` (GUI geometry text) | 659,649 | `8666056c38dfc0edfebdd27dcfd165495d37875c25ea034ec814dbd25eabe3d5` |

### Numbering-match evidence (the ADR 0174 GATE-1 check)

- **Provenance:** same zip (6_6, tag 1.0.33), same `BaldEagleDamBrk` project, same
  geometry index 09 -- `x09` is by construction `g09`'s preprocessor output.
- **Structural identity:** `g09.hdf` `Geometry/Structures/Attributes` carries ONE
  structure, `Type="Connection"` `Connection="Sayers Dam"` `Weir Coef=3.1`
  `Weir Width=80` `Gate Groups=1` `Use 2D for Overflow=1` -- byte-for-byte the
  `x09` `Conn 6 Sayers Dam` block (`T 3.1 ... 80 ... gate 1`).
- **Range:** every `x09` HW/TW face index (max 17779) and facepoint index
  (max ~19340) falls inside `g09`'s 37594 faces / 19529 facepoints.
- The `.xNN` preprocessor enumerates faces differently from the `.gNN.hdf` display
  face-array rows (a raw index->row lookup lands off-centerline), so the
  self-consistency of the pairing is proven by the SOLVE, not by row identity:
  the pair drives `RasGeomPreprocess`+`RasUnsteady` to `Finished` with vol-accounting
  error 0.0006% and NONZERO weir flow through the Sayers Dam connection (peak
  ~300k cfs, gate flow 0). See ADR 0174.
