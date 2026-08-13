# SFINCS nws-event composer -- forcing characterization (ADR 0102 target 5)

Author: engine specialist (provenance-chain wave 1, 2026-08-03)
Verdict: CO-OPS tide/surge wiring is NOT a fetcher-wiring gap here -> CHARACTERIZE
+ SKIP (no code change). The composer already consumes REAL OBSERVED forcing.

## What the composer forces its SFINCS run with today

`model_nws_flood_event_scenario` (`workflows/sfincs/model_nws_flood_event_scenario/`)
is a deliberately PLUVIAL-OBSERVED pipeline:

1. `fetch_nws_alerts_conus(event_types=FLOOD_WARNING_EVENT_TYPES)` where
   `FLOOD_WARNING_EVENT_TYPES = ("Flood Warning", "Flash Flood Warning")` -- these
   are pluvial / fluvial riverine warnings, NOT coastal.
2. `fetch_mrms_qpe(bbox=warning_bbox, accumulation="24h")` -- MRMS is
   gauge-corrected radar QPE, a REAL OBSERVED precipitation accumulation.
3. `model_flood_scenario(bbox=warning_bbox, forcing_raster_uri=mrms_layer.uri, ...)`
   -- the SFINCS run is forced by the observed MRMS raster (the Case-3
   observed-precip branch in `flood.py`), NOT a synthetic design storm.

So the dominant forcing (precipitation) is already FETCHED and OBSERVED. The audit
row for the nws composer noted "CO-OPS tide forcing unwired"; that gap is real for
a COASTAL run but does not apply to THIS pipeline.

## Why CO-OPS is not wired (and should not be, in this leg)

- The pipeline's warning set is pluvial/fluvial only. There is no
  "Coastal Flood Warning" / "Storm Surge Warning" event type in
  `FLOOD_WARNING_EVENT_TYPES`, so no coastal-surge forcing is applicable to the
  events it models.
- `model_flood_scenario` ALREADY owns the full coastal surge auto-wire
  (CO-OPS -> GTSM -> parametric, `flood.py` `_autowire_coastal_surge_forcing` /
  `_resolve_surge_forcing_from_fetchers`), gated behind `coastal=True` / a
  `surge_forcing` dict. The nws composer passes NEITHER, so the pluvial path is
  correct for its events.
- Wiring CO-OPS into the nws composer would mean ADDING coastal-flood/storm-surge
  warning support (a NEW capability + a coastal branch that flips `coastal=True`),
  not filling a have-but-not-wired fetcher gap. That is out of scope for wave 1.

## Consequence

No code touched in leg 5. `flood.py` / `sfincs_builder.py` untouched -> the FLOOD
CANARY is NOT mandated for this wave. If a future job adds coastal NWS warning
types to the pipeline, the wiring is a one-line `coastal=True` (or a resolved
`surge_forcing`) into the existing `model_flood_scenario` delegation -- the
CO-OPS fetcher chain is already there.
