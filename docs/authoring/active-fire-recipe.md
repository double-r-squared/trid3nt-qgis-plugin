# Active fire from GOES ABI

The MODIS contextual algorithm (Giglio, Descloitres, Justice and Kaufman 2003,
Remote Sensing of Environment 87:273-282, section 2.3; Collection 6 as Giglio,
Schroeder and Justice 2016, RSE 178:31-41), run over GOES ABI windows.

1. FETCH bands 2 (red, 0.64 um), 3 (veggie, 0.86 um), 7 (3.9 um), 14 (11.2 um)
   and 15 (12.3 um) over the window.
2. RASTER CALCULATOR for the DAYTIME CLOUD MASK - a cloud reads hot in the split
   window and would be a false fire:
   (rho0.65 + rho0.86) > 0.9 OR BT12 < 265 K OR ((rho0.65 + rho0.86) > 0.7 AND
   BT12 < 285 K).
3. RASTER CALCULATOR for the CANDIDATE SCREEN:
   BT3.9 > 310 K AND (BT3.9 - BT11) > 10 K.
4. NEIGHBOURHOOD STATISTICS over the cloud-free, non-candidate cells for the
   background mean and the mean absolute deviation of BT3.9 and of the split
   difference (BT3.9 - BT11).
5. RASTER CALCULATOR for the DETECTION: a candidate is a fire where the split
   difference exceeds its background by 3.5 MAD AND by 6 K, and BT3.9 exceeds its
   own background by 3.0 deviations. The algorithm's fourth daytime condition -
   the longwave brightness temperature within 4 K of one deviation above the
   background's - is NOT taken here. It was written for a 1 km cell, where a fire
   lifts the whole cell's longwave; in a 2 km ABI cell a sub-pixel flame does not,
   and terrain that is genuinely cool in the longwave carries the fire below the
   floor. Measured over a confirmed fire it rejects more than half the cells the
   other three keep, and over a scene with no fire in it, it rejects nothing they
   had not already rejected.
6. POLYGONIZE the detection mask for the fire pixels as features.

TEST: the western US replay, `active-fire-replay-west-conus.npz` beside this
file - GOES-18 bands 2, 3, 7, 14 and 15 over (-124, 33, -110, 49) at
2026-09-14T22:06Z. The sequence above finds FOUR burning cells in that window and
all four are one northern California fire; the cloud test masks 16% of the scene
and drops 424 cells out of the candidate screen before the contextual stage judges
them. `tests/derive/test_active_fire_recipe_replay.py` runs the sequence over that
scene, so a coefficient changed here moves a number a test reads.
