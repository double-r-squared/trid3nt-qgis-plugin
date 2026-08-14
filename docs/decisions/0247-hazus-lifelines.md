# ADR 0247 - HAZUS earthquake lifeline-network DL template

Date: 2026-08-13
Status: accepted

## Context

ADR 0246 (triage sweep 2) flipped the three HAZUS lifeline rows
(`transportation_network_seismic_damage`, `potable_water_network_seismic_damage`,
`electric_power_network_seismic_damage`) from "unknown/unsurfaced" to
READY-TO-LAND: pelicun 3.9.0 ships all three DamageAndLossModelLibrary lifeline
fragility datasets + auto-population scripts in the server venv (no worker image),
and a single HwyBridge AIM was proven executing end-to-end through the shared
`run_dl_calculation` seam. The sweep flagged two remaining fault points as
"landing details, not machinery": the exact R2D AIM key spellings for
water/power, and a Losses/Options config block.

This ADR lands the collapse recipe: one registered template with a
`lifeline_class` knob.

## Decision

Built `pelicun_hazus_lifeline_seismic_dl_run` (a clone of the proven
`pelicun_hazus_seismic_dl_run` building template) with a `lifeline_class` knob
{transportation | potable_water | electric_power} selecting the DL_Method,
assetType, and per-class AIM builder. Methodology-anchor scope per the ADR-0246
LAND recipe: tabular DL + a chart, no map/mesh render, proof = chart PNGs.

Two fault points, root-caused in-venv and fixed:

1. **Exact AIM keys** - mined directly from the bundled `pelicun_config.py`
   auto-pop scripts (which *consume* the keys, so authoritative): bridge
   `assetSubtype='HwyBridge'` + BridgeClass/StateCode/YearBuilt/NumOfSpans/
   MaxSpanLength/Skew/DeckWidth/StructureLength; pipe `type='Pipe'` +
   Diam/Len/material/year; substation `type='Substation'` + Voltage/Anchored.

2. **The water/power `'Options'` KeyError** - the real root cause was NOT a
   missing Losses block. pelicun's DL_calculation merges the harness-injected
   assessment Options (Seed + SampleSize) into `config_ap['DL']['Options']` by
   *direct key access*; the bundled water and power auto-pops return a `DL` block
   with **no `Options` key** (only buildings + transportation include one), so
   that merge raises `KeyError: 'Options'`. Fix: a small documented shim in the
   shared `_dl_calculation.py` harness wraps pelicun's `auto_populate` (under the
   existing process-serialization lock) to guarantee `DL/Options` exists
   post-auto-pop. Reproducibility (the injected Seed) now holds for every asset
   class, and no vendored pelicun file is edited.

Per-asset damage extraction: the harness gained an opt-in `detailed_results` that
parses `DMG_sample.zip` (columns `<component>-<loc>-<dir>-<ds>`) into per-component
damage-state probabilities. This is required because the HAZUS potable-water and
electric-power fragilities carry **no repair-cost consequence** - their DL_summary
holds only collapse/irreparable placeholders, so a damage-state distribution IS
the product. Bridges keep the full repair-cost / repair-time loss summary; pipes
report the HAZUS leak (DS1) / break (DS2) per-segment split.

Honest scope (no invented data): every asset attribute and every ground-motion
intensity is a labeled knob evaluated at a stated shaking level - a methodology /
coverage anchor, NOT a per-asset assessment over a fetched inventory + hazard
field. A live per-city HwyBridge fetch needs National Bridge Inventory structural
attributes (span count/length, skew, deck width, year, state) that no repo fetcher
carries and OSM lacks; fabricating them per bridge would violate the
no-invented-data norm. Buried water mains are absent from OSM (only water-tower /
storage-tank Tank assets are present). OSM `power=substation` (+ voltage tag) and
HIFLD power_plants (Generation) ARE fetchable. The per-asset vector-layer
live-city variant (NBI fetcher + OSM substation/tank fetch + OpenQuake
scenario-GMF per-asset demand) is queued follow-on, not a blocker.

## Consequence

Board: the three lifeline rows -> LANDED under one template. Registry
251 -> 252. A durable harness fact: any pelicun auto-pop whose `DL` block omits
`Options` (water, power) would otherwise crash the DL_calculation Seed merge - the
shim absorbs it generally, so future lifeline/network fragilities ride the same
seam. The `detailed_results` damage-state parser is reusable by any future
consequence-free HAZUS fragility.

Files: `workflows/pelicun/hazus_lifeline_seismic_dl_run/{hazus_lifeline_seismic_dl_run.py,corpus.yaml,__init__.py}`,
`workflows/pelicun/_dl_calculation.py` (shim + `detailed_results` + damage-sample
parser), `categories.py`, `tools/__init__.py`, `tests/test_door_dissolution.py`,
`tests/test_catalog_surfacing.py`, `tests/test_hazus_lifeline_seismic_dl_run.py`,
`scripts/proof_hazus_lifeline_seismic.py`,
`docs/proof/templates/pelicun_hazus_lifeline_seismic_dl_run_*.png`.
