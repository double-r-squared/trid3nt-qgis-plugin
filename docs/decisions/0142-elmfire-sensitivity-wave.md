# 0142: ELMFIRE sensitivity wave - 3 landed, 5 honest STOPs

Date: 2026-08-05
Status: landed

## Context

Second per-engine grind batch: the eight ELMFIRE CAND-S board rows.
Triage against the installed ELMFIRE namelist surface overturned the S
label on five of them and one board framing.

## Decision

Land three as templates on a shared sensitivity spine
(`workflows/elmfire/sensitivity/_sensitivity_common.py`, shared
`ElmfireSensitivityLayerURI` contract, backward-compatible
`simulator_extra`/`outputs_extra`/`inputs_extra` namelist injection in
the deck builder - byte-identical when unused):

- `elmfire_length_to_width_ceiling_sensitivity` - REDESIGNED from the
  board's wind-sweep framing to a MAX_LOW sweep at fixed wind (the wind
  sweep cannot isolate the cap: dry-grass natural elongation exceeds any
  cap at high wind). Smoke: cap 3 binds (L/W 2.71), natural plateau
  4.625 from cap 7.5 up.
- `elmfire_live_fuel_moisture_sensitivity` - uniform LH_MOISTURE_CONTENT
  sweep; smoke burned area 0.421 -> 0.002 km2 over 30 -> 150% moisture.
- `elmfire_wind_fluctuation_randomization` - WIND_FLUCTUATIONS +
  RANDOMIZE_RANDOM_SEED members vs deterministic; smoke: randomized wind
  broadens the burn (0.527 km2 -> mean 1.092 km2, spread_fraction 0.15).

STOP the other five with recipes: crown-fire pair (needs a crown output
postprocess family + TARGET_CFL/BANDTHICKNESS time-control emission + a
canopy-constants validation loop; fold into ONE template), spotting pair
(needs the full &SPOTTING namelist group + spot-output postprocess
family; fold into ONE template), dead-fuel interpolation frequency
(no-op on constant decks; rides the future multi-band transient-weather
deck machinery shared with transient_wind_schedule_spread).

## Consequence

Registry 199 -> 202; templates 44. Board rows retagged S->M with the
recipes. The sensitivity spine is the reusable landing path for the
folded crown/spotting templates once their machinery exists.
