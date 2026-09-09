# Fetcher fold, the hydro stage - HyRiver, and the two rows that fold onto it

Charter: `docs/validation/fetcher-fold-census.md` (G5, G6, G7, the ESRI rows the
census assigns to pygeohydro). Rulings: `docs/IDEAS.md` "FETCHER FOLD RULED",
"FOLD STAGE 0 RULINGS", "FOLD VECTOR STAGE", "HYRIVER, THE WIDER MAP"
(2026-09-08 / 09). Stage 0's measurements: `docs/validation/fetcher-fold-stage0.md`.

Every number here is measured on this machine in `venvs/agent`. Each pre-fold
artifact was captured from a `git worktree` at `e6226c6d` - the commit this stage
started from - with `PYTHONPATH` pointed at the worktree from a neutral working
directory, which is the only way the editable install can be overridden.

## 1. What landed

| # | commit | row |
|---|---|---|
| 1 | `27bfd1b9` | `pynldas2==0.19.3` declared; the direct `pynhd` declaration dropped |
| 2 | `e91feb25` | `_router/hooks/hyriver.py`, the one shim every HyRiver call rides |
| 3 | `81d239b4` | `fetch_high_water_marks` onto `pygeohydro.STNFloodEventData` |
| 4 | `f80753b7` | `fetch_nldas2_forcing`, a NEW row on `pynldas2` |
| 5 | `12c8471e` | `fetch_fema_nfhl_zones` onto `pygeohydro.NFHL`, read by object id |
| 6 | `b47e3384` | the ledger rows and the two measurements this stage was asked for |

Registered tools **162 -> 163**: the one declared addition. No fold commit changed
the count. Specs 98 -> 99.

## 2. The shim, and why it is 160 lines and not 40

The ruling budgeted ~40 LOC on the tenacity pattern. Measured: **160 lines, of
which 58 are code**, 62 docstring, 8 comment, 32 blank.

The overshoot is the classification, and every arm of it was forced by a measured
failure:

| the shape a refusal arrives in | measured where | classified by |
|---|---|---|
| RFC 7807 `{"status": 404, ...}` returned AS DATA | Stage 0, `pynhd.NLDI` | `status` in the body |
| ESRI `{"error": {"code": 400, ...}}` returned AS DATA | Stage 0, the vector stage | `error.code` in the body |
| an ArcGIS error PAGE printing `<b>Code: </b>500` | this stage, FEMA NFHL | the same regex, reading through markup |
| a connection that never became a response | this stage, FEMA NFHL | the exception class - the only signal left |

The retry policy is `transport/client.py`'s, restated for a library that owns its
own HTTP: statuses `(429, 500, 502, 503, 504)`, base 0.5 s, cap 20 s, jitter, five
attempts. The `status_to_retry` kwarg the ruling names does not exist on the paths
`pygeohydro` actually uses (Stage 0, cell C6): the shim IS that setting.

Cache: `HYRIVER_CACHE_NAME` and `HYRIVER_CACHE_NAME_HTTP` under
`$TRID3NT_RUNS_DIR/hyriver-cache/`, `HYRIVER_CACHE_EXPIRE=3600` - the **dynamic-1h**
class, the shortest router cache window, so a library body can never outlive the
narrowest key the router would recompute. The library's own default is 604800 s.

## 3. Parity

Every folded spec against a pre-fold fetch of the same request.

### fetch_high_water_marks - four requests, exact

| case | request | pre | post |
|---|---|---:|---:|
| `hwm_michael` | Panhandle bbox + `event=Michael` | 48 | 48 |
| `hwm_fl_panhandle` | the same bbox, state-scoped | 48 | 48 |
| `hwm_harvey` | Houston bbox + `event=Harvey` | 722 | 722 |
| `hwm_tx_all` | the same bbox, state-scoped | 722 | 722 |

Columns, dtypes, every attribute value and every geometry equal. The files are
byte-identical but for **8 bytes** - the FlatGeobuf layer name's per-write random
suffix (`trid3nt_router_vec_<random>`).

### fetch_fema_nfhl_zones - two requests, exact

| case | request | pre | post |
|---|---|---:|---:|
| `nfhl_small` | 0.01 deg^2 Tampa Bay bbox | 326 | 326 |
| `nfhl_small_sfha` | the same bbox, `sfha_only=true` | 84 | 84 |

Summed area and summed length equal to the last digit, bounds multiset equal,
all exterior rings still counter-clockwise, and **18 features carrying 629 interior
rings preserved on both sides**. Same 8-byte layer-name difference. Wall clock
improved: 42.2 s -> 18.7 s on the full case.

The hole count is the reason the format is named rather than defaulted. Asked for
Esri JSON the client's converter tests ring containment with a LINE that contains a
point, which no interior ring's vertex lies on, so every hole returns as another
filled outer ring: measured, zero of the 629 survived, over-stating those 18
features by 13.8 percent and the AOI by 7.8.

## 4. The FEMA NFHL wall - measured, and unresolved

The row came to this stage because a cursor walk cannot tell a refusal from the end
of a layer: over a 0.2 deg^2 Tampa Bay bbox the cursor returned **6,000 of the 9,894
polygons the service itself counts** and called it an answer.

The object-id read cannot do that. It also cannot finish.

| what was tried | result |
|---|---|
| the library's own call, all batches in one gather, 2000 ids each | HTTP 500 on the first batch, repeatably |
| the same 2000 ids by direct POST, both `f=geojson` and `f=json` | HTTP 500, repeatably - the advertised `maxRecordCount` is not deliverable |
| 500 ids, one batch per call, under the shim's backoff | batch 1 answers, batch 2 exhausts five attempts |
| 1000 ids, one batch per call, 10 s pacing and four attempts with 20-40 s gaps | **5,894 of 9,894 in 2,198 s** (37 minutes). Batches 0-2 answered, 3-5 refused every attempt, 6 answered after a ~5 minute enforced pause, 7 on its fourth attempt, 8 refused, 9 answered. Four batches lost outright |

The pattern is a client-scoped degradation with recovery: roughly three large reads,
then minutes of refusal, then service again. It is not the format (both fail), not
the concurrency (serialized fails), and not one poisoned feature (the same batch
answers later). And patience alone does not close it: the most generous read
measured, 37 minutes of paced requests with four attempts each, still lost four
batches of ten.

**So the ruling's bar - "a 7,892-feature bbox must come back complete" - is met by
NEITHER the cursor nor the object-id read.** The cursor produced a silent partial;
the object-id read produces a loud refusal. The fold landed because refusing is the
honesty floor and a partial regulatory layer is the failure this row exists to have
stopped - but WHAT a metro-scale NFHL AOI should do is a design question, raised and
not taken. See the report's DESIGN-STOP.

## 5. What did NOT fold, and why

| row | census verdict | measured verdict |
|---|---|---|
| `fetch_nhdplus_nldi_navigate` (G5) | `pynhd.NLDI`, -130 | STAYS on `dataretrieval`. Stage 0 (b): `NLDI()` cannot be constructed at any version `pygeohydro==0.19.4` admits. Ledgered with the condition |
| `fetch_noaa_nwm_streamflow` (G7) | `pynhd.NLDI`, -180 | same, same ledger row |
| `fetch_usgs_nwis_gauges` (G6) | `pygeohydro.NWIS`, -380 | HELD until calibration signs off. Untouched |
| `fetch_usace_dams` (G1) | `pygeohydro.NID` for its code tables | REJECTED. We do not hand-carry them: `PRIMARY_DAM_TYPE` / `DAM_TYPES` / `PURPOSES` arrive as attribute VALUES, and the row's levers are `hazard_potential`, `state`, `min_height_ft` - none needs a domain table. What the hook carries is a 4-value hazard vocabulary and a USPS map, neither of which the library holds |
| `fetch_usace_levees` (G1) | `pygeohydro.NLD` | REJECTED. It already reads through GDAL's ESRIJSON driver with zero hook LOC; NLD would name the same three sub-layers the spec names as data, over a second HTTP stack |
| `fetch_gtsm_tide_surge`, `fetch_field_boundaries` (G7) | stay | untouched |
| `fetch_usgs_groundwater_levels` (G2) | hook stays | untouched, per the vector stage's resolution |

## 6. LOC, measured

| | + | - | net |
|---|---:|---:|---:|
| the fold half (`hyriver.py`, NFHL, HWM, `chained_resolution`) | 342 | 222 | **+120** |
| `fetch_nldas2_forcing`, a declared addition | 301 | 0 | +301 |
| `pyproject.toml` | 9 | 6 | +3 |
| tests | 409 | 91 | +318 |

The fold half is **+120 product LOC** against the census's -810 for G5 + G6 + G7.
Honest reasons, all measured: G5 and G7 did not fold at all (Stage 0 killed the
NLDI client), `fetch_usgs_nwis_gauges` is held, HWM's own fold is **-14** (354 ->
340 - the library replaced the request build and the JSON decode; the state table,
the event resolve, the WSE stamp and the envelope are the row's own answers), NFHL's
is **-5 net** (192 -> 184 hooks, +3 yaml), `tolerate_page_error` took **-15** with
it, and the shim costs **+160**.

## 7. Suite

From the repo root with `venvs/agent`, globs unquoted, `env -u TRID3NT_CACHE_BUCKET`,
`-p no:cacheprovider --timeout=300 -q`, each slice its own invocation. The full run
is at `12c8471e`; the two slices the later commits touch were re-run at `ab932d55`.

| slice | result | at |
|---|---|---|
| `tests/test_[a-e]*.py` | 1571 passed, 5 skipped | `ab932d55` |
| `tests/test_[f-o]*.py` | 4272 passed, 1 xfailed | `12c8471e` |
| `tests/test_[p-r]*.py` | 1704 passed, 2 skipped | `ab932d55` |
| `tests/test_[s-z]*.py` | 1471 passed, 5 skipped | `12c8471e` |
| `contracts/tests` | 425 passed | `12c8471e` |
| `plugin/tests` | 386 passed, 2 failed | `12c8471e` |

Zero failures across the five server slices and contracts. The two plugin reds are
the documented pre-existing Qt pair, `test_case_bbox.py::TestCaseBboxDock::test_case_bbox_dock_behaviors`
and `test_tool_picker.py::TestToolPickerQt::test_harness_green`, out of this stage's
scope and unchanged by it.

## 8. The two measurements this stage was asked for

- `docs/validation/nlcd-manning-tables.md` - our NLCD -> Manning table beside
  `pygeohydro.overland_roughness`, class by class, with each one's published source.
  17 codes in both and **zero agree**. A DESIGN-STOP: nothing is switched.
- `docs/validation/section-vs-hyriver.md` - `pynhd.flowline_xsection` and
  `py3dep.elevation_profile` measured against the section cut and the profile
  sampler. Neither replaces what it was proposed against.
