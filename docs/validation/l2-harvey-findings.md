# L2 live findings - Hurricane Harvey / Buffalo Bayou (2026-07-25)

Two full live executions of scripts/run_l2_validation_harness.py (observed
gridMET forcing, 501 mm / 48 h, AOI -95.61,29.735,-95.43,29.79; 102 STN HWMs,
51/51 split; runs 01KYDKPZ... run 1, 01KYDRQC... run 2). Machinery: 8/8 PASS
both runs.

## Run 1 exposed the quantity bug (fixed same day)

Pairing compared model DEPTH against STN WATER-SURFACE ELEVATION (NAVD88):
RMSE 15.2 m with R2 0.897 - the "correlation" was HWM elevation tracking
terrain. Fix: quantity stamps on the HWM fetcher, quantity resolution +
WSE->depth conversion via DEM ground sampling in the pairing tool, typed
PairingQuantityMismatchError when unreconcilable, negative depths flagged
never clamped, peak_timing_error null for timeless obs.

## Run 2 - the honest held-out verdict (depth vs depth)

| metric       | baseline    | after manning 0.097->0.083 |
|--------------|-------------|-----------------------------|
| NSE          | -1.466      | -1.485                      |
| KGE          | -0.137      | -0.150                      |
| PBIAS        | +71.3%      | +73.8%                      |
| RSR          | 1.570       | 1.576                       |
| RMSE (m)     | 3.72        | 3.73                        |
| R2           | 0.436       | 0.443                       |
| peak_error   | +116.9%     | +117.2%                     |
| peak_timing  | null (timeless obs - correct) | null          |

## Interpretation (findings, not gates)

1. The model OVER-predicts depth at the marks by ~70% (PBIAS positive =
   over-prediction, spotpy convention). Expected drivers, in scope order:
   uniform area-mean rain (real Harvey rain was spatially structured; the
   forcing contract collapses to area-mean), zero storm-drainage / detailed
   infiltration, no channel routing, marks in locations the uniform-rain
   model ponds but reality drained.
2. The calibration signal is REAL and interpretable: lowering manning made
   every metric slightly worse -> the gradient points to HIGHER roughness /
   more infiltration. Exactly the loop the frozen group-E optimizer would
   climb.
3. R2 0.44 = moderate spatial pattern skill for a screening-grade
   uniform-rain pluvial model with no reservoir ops (Addicks/Barker releases
   dominated real Buffalo Bayou flooding - absent here by scope).
4. 9/102 marks flagged negative_depth (surveyed WSE below sampled ground) -
   datum/DEM edge cases kept visible, excluded from nothing silently.
5. Not comparable to published Harvey validations (those use radar rainfall,
   channel routing, reservoir ops). Next observational check: flood-extent
   CSI via fetch_flood_extent_observation.

## Hardening backlog from this arc

- Stamp quantity=depth_above_ground in postprocess_flood COG metadata
  (model side currently resolves via filename heuristic; reader is
  forward-compatible).
- gridMET fetcher: identity-geotransform bug on single-row bbox subsets
  (harness repairs from bbox; server-side fix ticket).
- Harness process lingers after printing the final table (non-daemon
  thread); reaped manually both runs.
- Time-varying + spatially-distributed forcing contract (OQ-6 v0.1 collapses
  to area-mean constant rate) - the single largest realism lever exposed.
