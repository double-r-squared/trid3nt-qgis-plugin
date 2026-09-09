# NLCD -> Manning n, ours beside pygeohydro's - measured, class by class

Asked by the HyRiver hydro stage: `docs/IDEAS.md` "HYRIVER, THE WIDER MAP" (a). Landed in the fold hydro stage, 2026-09-09.
**Nothing is switched.** Swapping this table changes run numbers, so it is an author
decision. This file is the measurement the decision reads.

## The two tables and where each comes from

**Ours.** `trid3nt_server/workflows/telemac/templates/rain_on_grid/cn_infiltration.py:79-98`
(`NLCD_CN_MANNING`), read by `workflows/telemac/helpers/infiltration.py:70`
(`landcover_cn_manning(c)[1]` per mesh node). Its own comment states the provenance:
the Manning column is **Godara, Bruland and Alfredsen (2024), Front. Water 6:1384205**,
Table 1, "the paper T2D column verbatim", mapped from the paper's six T2D land-cover
classes (bare rock, forest, open land, marsh, river, urban) onto the NLCD 2019/2021
legend so `fetch_landcover` drives the field directly. Six distinct n values across
18 NLCD codes, because the paper has six classes.

**pygeohydro's.** `pygeohydro.overland_roughness` (`nlcd.py:285-309`) is a lookup into
`pygeohydro.helpers.nlcd_helper()["roughness"]` (`helpers.py:96-116`). The docstring's
references list four sources; the roughness values are attributable to the last of
them, **Liu, Merwade and Jafarzadegan (2019), "Investigating the role of model
structure and surface roughness in generating flood inundation extents using one- and
two-dimensional hydraulic models", J. Flood Risk Management 12:e12347
(doi:10.1111/jfr3.12347)**. Nineteen values, one per NLCD code, all distinct.

The paper is closed access (Wiley 403; Semantic Scholar reports
`openAccessPdf status=CLOSED`), so the per-class numbers below are the values the
library ships - which is also exactly what a swap would adopt.

## The table

| NLCD | class | ours (Godara T2D) | pygeohydro (Liu et al.) | ratio lib/ours |
|---|---|---:|---:|---:|
| 11 | Open Water | 0.040 | 0.001 | 0.03x |
| 12 | Perennial Ice/Snow | 0.040 | 0.022 | 0.55x |
| 21 | Developed, Open Space | 0.050 | 0.0404 | 0.81x |
| 22 | Developed, Low Intensity | 0.100 | 0.0678 | 0.68x |
| 23 | Developed, Medium Intensity | 0.100 | 0.0678 | 0.68x |
| 24 | Developed, High Intensity | 0.100 | 0.0404 | 0.40x |
| 31 | Barren Land | 0.020 | 0.0113 | 0.56x |
| 41 | Deciduous Forest | 0.200 | 0.360 | 1.80x |
| 42 | Evergreen Forest | 0.200 | 0.320 | 1.60x |
| 43 | Mixed Forest | 0.200 | 0.400 | 2.00x |
| 45 | (NLCD 1992) Forested Wetland | unmapped | 0.400 | - |
| 46 | (NLCD 1992) Shrubland | unmapped | 0.240 | - |
| 51 | Dwarf Scrub | 0.050 | 0.240 | 4.80x |
| 52 | Shrub/Scrub | 0.050 | 0.400 | 8.00x |
| 71 | Grassland/Herbaceous | 0.050 | 0.368 | 7.36x |
| 72 | Sedge/Herbaceous | 0.050 | **unmapped** | - |
| 81 | Pasture/Hay | 0.050 | 0.325 | 6.50x |
| 82 | Cultivated Crops | 0.050 | 0.037 | 0.74x |
| 90 | Woody Wetlands | 0.200 | 0.086 | 0.43x |
| 95 | Emergent Herbaceous Wetlands | 0.200 | 0.1825 | 0.91x |

**17 codes in both. ZERO agree.** Ratio min 0.025x, median 0.81x, max 8.00x.

## What the differences actually mean for a run

Four disagreements are large enough to change a rain-on-grid answer, and they do not
all pull the same way:

1. **Vegetated open land (51, 52, 71, 81): 4.8x to 8.0x ROUGHER in the library.**
   0.05 -> 0.24-0.40. This is the single biggest effect: over a grassland or
   shrub-dominated catchment the overland wave slows by roughly the same factor,
   pushing the hydrograph peak later and lower. Our 0.05 is the paper's one
   "open land" class carrying scrub, grass, sedge and pasture together; the library
   separates them and puts scrub and grass near forest values.
2. **Forest (41, 42, 43): 1.6x to 2.0x ROUGHER in the library.** 0.20 -> 0.32-0.40.
   Same direction, smaller magnitude.
3. **Open water (11): 40x SMOOTHER in the library.** 0.040 -> 0.001. Ours is the
   paper's "river/open-water" channel n; 0.001 is not a channel roughness at all.
   On a rain-on-grid mesh whose water bodies are part of the domain this is the
   difference between a routed channel and a frictionless one.
4. **Urban (22, 23, 24): 1.5x to 2.5x SMOOTHER in the library**, and the library
   makes HIGH intensity (0.0404) SMOOTHER than LOW (0.0678), which is the opposite
   ordering to ours (all three 0.100). Ours follows the paper's single "urban" class.

Two coverage differences:

- The library has no **72 (Sedge/Herbaceous)**; `overland_roughness` returns `nan`
  there, so a mesh over Alaskan sedge would take NaN roughness rather than a value.
  Ours maps 72 to the open-land row.
- The library carries **45 and 46**, NLCD 1992 codes absent from the 2019/2021 legend
  `fetch_landcover` fetches. Dead rows for us.

Two structural differences beyond the numbers:

- **Fallback.** Ours falls back to the open-land row (0.05) for an unmapped code and
  says so; `overland_roughness` yields `nan`, which propagates into the friction field.
- **Coupling.** Our table is one object per class, `(CN2, Manning n, label)`, and the
  CN half is the paper's T2D column for the same class. Taking the library's n and
  keeping the paper's CN would mix two studies' parameterizations in one field, which
  is a change the paper's own calibration does not cover.

## The decision this asks for

**DESIGN-STOP for NATE.** Three options, stated, none taken:

- **(a) Keep ours.** One published study, CN and n from the same table, consistent
  with the rain-on-grid replication the template exists to reproduce. Cost: four
  classes are visibly coarse (all of scrub/grass/pasture at 0.05).
- **(b) Switch to the library's n, keep our CN.** Gains per-class resolution and the
  domain library's maintenance. Costs: it mixes two studies in one field, it inherits
  a NaN at code 72 and a 0.001 open water, and **every rain-on-grid number already
  produced changes** - the flagship canary and the six coarse pins would all move.
- **(c) Carry both, as a named author choice on the template.** The table becomes a
  declared lever (`roughness_table: godara | liu`) and neither is a shim. Cost: one
  more knob, and every proof states which table it ran.

Nothing in this file is a recommendation.
