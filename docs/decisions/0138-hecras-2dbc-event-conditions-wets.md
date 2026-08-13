# ADR 0138 -- HEC-RAS: the 2D-BC-line `/Event Conditions` schema is FOUND, DECODED, and re-authored by our own code -- the fresh carved topology now WETS end-to-end (OI-FT1 CLOSED); `hecras_flood_2d` is GO

Status: accepted (2026-08-05)
Follows: ADR 0137 (the pure-2D fake-reach deck SOLVES but stays DRY; the precise
STOP named as the plan-HDF `/Event Conditions/Unsteady/Boundary Conditions`
2D-BC-line flow-hydrograph schema read by `read_un_q2d_bc_`), ADR 0136 (the fresh
carve solves through the 6.6 engines), ADR 0133/0135 (the geometry + BC-lines
writer). Closes the open item ADR 0136/0137 carried as OI-FT1.

This wave runs the OI-FT1 probe and CLOSES it. The ONE unshipped schema between
the fully-proven chain and `hecras_flood_2d` was found in shipped public-domain
HEC-RAS 6.6 pure-2D plan HDFs, decoded precisely, re-authored entirely by our own
code against our own carved geometry, and shown -- by a completing 6.6 solve on
the fresh carved mesh -- to WET the 2D area (0 -> 2068 wet cells).

Experiment-only-plus: NO server / tool / contract / registry change (registry
byte-identical by construction; coded-tools delta 0). New durable code + a decoded
schema doc under `services/workers/hecras2025/subst/crux/freshtopo/`; transcripts
under `scratchpad/oift1_probe_proofs/`. Nothing vendored.

## Where the schema was found

The community `github.com/neeraip/hecras-v66-linux` repo (UNLICENSED; read ONLY to
decode a file format from its shipped public-domain HEC example decks -- schema
facts are not code, and we vendor nothing) ships **five** pure-2D projects with
complete plan HDFs, and every one enumerates its 2D-BC lines in
`/Event Conditions/Unsteady/Boundary Conditions`:

| project | Flow Hydrograph 2D-BC line | Normal Depth 2D-BC line | wets (shipped Results) |
| --- | --- | --- | --- |
| `BEC_WO_Infiltration/model1` | `BCLine: inflow` (385 ord) | `BCLine: DS` | 976 / 2917 cells, 47 ft |
| `Muncie` (pure-2D) | `BCLine: US` (25 ord) | `BCLine: DS` | 2220 / 3403 cells, 7.4 ft |
| `BEC`, `AFTER_RUN/test_hdf`, `VA` | (flow / precip variants) | -- | yes |

Their Linux pipeline (`ras_preprocess.py`) writes an EMPTY Event-Conditions group
and forces via a `.b01` fake-reach `Upstream Flow Hydrograph - River: Fake River
Reach: Fake Reach RS: 100` -- the SAME inert form ADR 0137 used. The shipped
plan HDFs, however, carry a POPULATED Event-Conditions enumeration whose Flow
Hydrographs dataset holds the REAL hydrograph (model1: 57 -> 2372 cfs) while the
fake reach holds a flat placeholder. This is the direct confirmation of ADR 0137's
STOP hypothesis: **the engine wets the 2D area from the Event-Conditions 2D-BC
enumeration, NOT from the fake reach.** The fake reach is a required-but-inert 1D
placeholder; ADR 0137 solved-but-dry precisely because its Event Conditions was
empty.

## The decoded schema (our own words; byte-exact-verified)

`/Event Conditions/Unsteady/Boundary Conditions/{Flow Hydrographs,Normal Depths}/
"2D: <area> BCLine: <name>"`:

- Flow Hydrographs: `(N,2) float32` interleaved `[time, flow]`; attrs `2D Flow
  Area`, `BC Line`, `Check TW Stage='False'`, `Data Type='INST-VAL'`, `EG Slope
  For Distributing Flow` (f4), `Start/End Date`, `Interval`, `Node Index=1`,
  `Face Indexes` (i4[k]), `Face Point Indexes` (i4[k+1]), `Face Fraction` (f4[k]).
- Normal Depths: `(1,) float32` friction slope; same face attrs + `BC Line
  WS='Multiple'`.
- EC root: `@Completed Successfully='True'`, `@Date Processed`; Initial Conditions
  `@Startup Mode='Computed'`.

The KEY INSIGHT: the per-BC `Face Indexes`/`Face Point Indexes`/`Face Fraction`
are NOT independent -- they are the geometry `Boundary Condition Lines/External
Faces` rows for that line CLIPPED to `[0, Length]`, with `Face Fraction` the
clipped overlap fraction. This derivation reproduces the shipped enumeration
**byte-exact** for every BC line (flow AND normal-depth) across model1 and the
pure-2D Muncie (`scratchpad/oift1_probe_proofs/verify_clip.py`). Full schema doc:
`freshtopo/EVENT_CONDITIONS_2DBC_SCHEMA.md`.

## Our author + the WET solve (the advance)

`freshtopo/hecras_event_conditions.py` re-authors the schema against OUR carved
geometry (derives the faces from OUR External Faces; nothing copied).
`freshtopo/build_chippewa_wetting_deck.py` builds the ADR 0137 clean pure-2D
fake-reach deck over the same fresh NW-quadrant carve (2068 real cells, 171-pt
perimeter) and adds the 2D-BC Event-Conditions forcing on the carved `Inflow` BC
line (+ a `DS` normal-depth outlet). It SOLVES end-to-end through production 6.6
`RasGeomPreprocess` + `RasUnsteady`:

| run | wet cells | max depth ft | max WSE ft | vol err % | flux in / out |
| --- | --- | --- | --- | --- | --- |
| ADR 0137 (empty EC) | **0 (dry)** | -- | 946.93 | 0.000 | 6743.8 / 6743.8 |
| inflow-only + EC (no outlet) | 2068 | 1118 (fills) | 2043 | 0.58 | 141176 / 6743.8 |
| **EC + DS outlet, x1.0** | **1906** | **12.22** | **946.94** | **0.011** | **141176 / 141011** |
| **EC + DS outlet, x1.5** | **1986** | **16.64** | **948.40** | **0.009** | **208368 / 208136** |

The decisive delta vs ADR 0137: identical geometry + fake reach, the ONLY change
is the authored Event-Conditions 2D-BC flow hydrograph -- and the fresh 2D area
goes from 0 to WET (boundary flux-in 6743.8 -> 141176). `read_un_q2d_bc_` does NOT
segfault on the authored group (the ADR 0137 blind-authoring segfault was the
missing schema, now supplied).

With a DS 2D-BC normal-depth OUTLET the solve is clean and PHYSICAL: partial
wetting (1906 / 2068 cells, not domain-filling), balanced flux in/out (drains),
vol err 0.011%, and a max WSE of 946.94 ft that MATCHES the ADR 0136/0137 full-
Muncie baseline (946.93 ft) -- an independent consistency check. The x1.5 flow
scale gives the acceptance-style delta ON the 2D area: +80 wet cells, +4.4 ft max
depth, +1.5 ft max WSE, flux-in x1.48, monotone -- the completing run genuinely
consumes the forcing.

## Consequences

- No server / tool / contract / registry change; registry byte-identical.
  Coded-tools delta **0** (worker-local authoring components). New durable code
  under `freshtopo/`: `hecras_event_conditions.py` (the EC author),
  `build_chippewa_wetting_deck.py` (the wetting deck build),
  `EVENT_CONDITIONS_2DBC_SCHEMA.md` (the decoded schema, our words),
  `test_event_conditions.py` (4 offline gates, green). No `flood.py` / SFINCS /
  `publish_layer` / registry reference. No server import-graph change; server
  baseline delta zero by construction (files under `services/workers/`, not
  collected by `server/tests/`).
- Image hygiene: no image built; throwaway `--rm` containers on the pre-existing
  `trid3nt-local/hecras:latest`. The community clone (~270 MB) was read to decode
  the format and DELETED after; nothing vendored. Proofs under
  `scratchpad/oift1_probe_proofs/`; durable footprint ~20 KB.
- Engine note: RasGeomPreprocess/RasUnsteady require `HDF5_USE_FILE_LOCKING=FALSE`
  on this host's overlay filesystem (else an HDF5 lock `errno=11` on the plan
  HDF); folded into the solve invocation.

## Open issues / ledger

- **OI-FT1 -- CLOSED.** The 2D-BC-line `/Event Conditions` flow-hydrograph schema
  is found, decoded, re-authored by our code, and empirically wets the fresh carve
  (0 -> 2068 cells). No further acquisition path (the ADR 0137 Windows-GUI-once
  option is moot).
- **OI-FT2 (the template -- now GO).** `hecras_flood_2d` + its archetype + the
  formalized authoring worker stage can land: the geometry, fresh-topology,
  pure-2D-deck, AND forcing halves are all proven. Acceptances: a Muncie self-check
  + a genuinely-new small US AOI, each showing wet cells where terrain is low + a
  monotone flow-scale delta ON the 2D area. Physical refinement remaining for the
  template (not for OI-FT1): tune the inflow magnitude + confirm the DS outlet
  drains so wetting is partial/physical rather than domain-filling.
- Carries ADR 0134 OI-D (precipitation Meteorology+DSS) and ADR 0132 OI-3
  (2025 `ras` `-dev` schema-unstable) + OI-4 (virtual-cell SanityCheck).
