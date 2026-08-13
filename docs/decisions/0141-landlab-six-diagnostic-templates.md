# 0141: Landlab six-row diagnostic/knob template wave

Date: 2026-08-05
Status: landed

## Context

First per-engine batch of the easy-tier grind (walkthrough-first process).
Six Landlab CAND-S board rows, all exec-mode on the existing landlab worker
(no image builds): storm-ensemble landslide sensitivity, overland-flow depth
timeseries, DEM pit-fill conditioning, lake extent/depth mapping, Hack's Law
basin scaling, and HAND wetness.

## Decision

Land all six as registered templates sharing one composer-boilerplate module
(`workflows/landlab/_composer_common.py`) rather than six copies. New
analyses are dispatch branches beside the signed chains; the signed
`landslide_probability` / `overland_flow` / `flow_accumulation` /
`green_ampt_overland_flow` chains stay byte-identical. Vector context layers
carry `style_preset="mesh_grid"` - fixing a latent missing-`style_preset`
bug that also affected the shipped `landlab_flow_accumulation` channel
vector. HAND uses `HeightAboveDrainageCalculator` (the actual landlab 2.11
class), unit-verified against the published API doctest grid.

## Consequence

Registry 193 -> 199; templates 35 -> 41. Live smokes on a Boulder CO
foothills 3DEP AOI: sensitivity slope +0.00149/mm-day, 9 animation frames
(max depth 2.06 m), 45 depressions / 45 lakes (138337 m3), Hack exponent
0.566 (classic ~0.5-0.6 range), mean HAND 49.4 m. Residuals: dedicated
raster style ramps for fill-depth/lake-bathymetry/HAND (currently reuse
continuous_flood_depth) and a log-domain drainage-area style expression.
