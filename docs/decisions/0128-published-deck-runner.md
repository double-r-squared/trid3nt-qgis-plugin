# ADR 0128 -- Published-deck runner: the cited-SWMM-example template family

Status: accepted (2026-08-04) -- 3 of the 5 STOPped SWMM rows from ADR 0124 land
as thin templates over ONE shared published-deck runner; 2 STOP with recipes.
Follows: ADR 0124 (the SWMM network family -- rows #3-#7 STOPped because they cite
PRE-BUILT published `.inp` decks carrying capabilities neither the DEM-mesh builder
nor the GIS-network parser produces), ADR 0104 (the SWMM subprocess/headless solve
isolation), ADR 0106 (labeled synthetic_inputs), ADR 0107 (the two-mode input gate),
ADR 0120 (the template hygiene gate).

## Context

ADR 0124 landed the SWMM network family (import + dual-drainage) and STOPped rows
#3-#7 with a precise blocker: each cites a specific PUBLISHED example `.inp` deck
(openswmm.org / EPA Applications-Manual Example 8) carrying LID controls,
stage-storage curves, flow regulators, pumps, and RTC/PID control rules -- none of
which a nodes/conduits GIS export or a DEM-synthesized mesh produces. The honest
path named in ADR 0124 was a SEPARATE "published-deck runner": ingest a cited
published `.inp`, run it VERBATIM through the existing headless solver, postprocess
honestly. This wave builds that runner and rides it.

Triage first (the mandated step): each cited deck was OBTAINED and solved raw before
any template was written. The openswmm.org topic pages render the full deck inline
on the PUBLIC page (the clean forum download is login-gated, and the author-posted
decks carry no redistribution license), so the runner fetches the pinned public
page at runtime and extracts the deck deterministically -- it does NOT bake the deck
into the repo. The decks carry SCHEMATIC coordinates (local model units, not
lon/lat), so the honest product is CHARTS + typed scalars, NOT a georeferenced map.

### Per-deck triage (solve-first)

| Row | deck (source) | solved raw? | outcome |
| --- | --- | --- | --- |
| #3 swmm_lid_raingarden_wq | Topic 15609 "A Very Simple Two-Subcatchment WQ Model With and Without Rain Gardens" (R. Dickinson) | YES, continuity +0.000% | LANDED |
| #7 swmm_wwtp_detention_ponds | Topic 14400 "UV Plant with Detention Ponds" (R. James) | YES, continuity +0.000% | LANDED |
| #6 swmm_pump_pid_rtc | Topic 10082 "Example - PID Control for a Pump" (R. Dickinson, EXTRAN 3/4 composite) | YES, continuity -0.062% | LANDED |
| #4 swmm_green_grey_infra_storms | Topic 23670 (DryPond_100yr24hr.inp + LID_100yr24hr.inp) (M. Gregory) | PARTIAL -- the page hosts TWO decks; the LID deck solves (continuity -0.013%), the DryPond deck's inline extraction still bleeds duplicate RAINGAGE ids | STOPPED (recipe) |
| #5 swmm_cso_regulator_network | EPA Applications-Manual Example 8 (Combined Sewer Systems) | NO -- the raw `.inp` is not hosted downloadable anywhere; needs bespoke rebuild from the manual | STOPPED (recipe) |

## Decision -- the runner + per-row outcomes

### The shared runner (the machinery)

- **Core** (`agent/mesh/swmm_deck_runner.py`, NEW; sibling to `swmm_network.py`):
  a `PublishedDeck` registry (the CITATION + the honest run knobs per deck);
  `fetch_deck_text` (fetch the pinned public URL, extract the inline deck, typed
  `SWMM_DECK_UNAVAILABLE` on any miss -- never a silent dead-end); `extract_inline_deck`
  (deterministic tag-strip + contiguous-known-SWMM-section run, prose-trimmed,
  `select_index` for a multi-deck page); `apply_rain_scale` (deterministic
  text-edit -- multiply every `[TIMESERIES]` ordinate; only for a rainfall-forced
  deck); `solve_deck_text` (writes the deck, runs the SAME headless one-shot
  `swmm5_run` seam the network family uses, applies the Flow-Routing-Continuity
  honesty gate, reads the `.rpt` summaries); node/link/subcatchment series readers
  (chart feedstock, from the solved `.out`).
- **Composer** (`workflows/swmm/deck_runner/deck_runner.py`, NEW): ONE entry point
  `model_published_deck(deck_id, rain_scale, input_mode)` -- fetch -> optional
  override -> solve -> build the FORCING-appropriate charts -> return a typed
  `SWMMDeckRunResult`. All orchestration lives here; the templates are the surface.
- **Contract** (`SWMMDeckRunResult`, NEW): a `GraceModel` (NOT a `LayerURI` -- the
  schematic deck has no georeferenced layer) carrying the typed narration scalars +
  the LOUD `demonstration_note` (this is the cited example's network, not a user AOI)
  + `schematic_only=True` + labeled `synthetic_inputs`.

### #3 swmm_lid_raingarden_wq -- LANDED

Runs the cited two-subcatchment rain-garden WQ deck VERBATIM. Headline chart: the
runoff hydrograph WITH vs WITHOUT the rain-garden LID; the built-in expected-outcome
check (LID subcatchment has lower runoff) is surfaced as `headline.lid_reduces_runoff`.
- **LIVE cheap-smoke** (fetched live from openswmm Topic 15609): continuity +0.000%,
  2 subcatchments, wRG (with rain garden) peak runoff **1.501 CFS** < woRG (without)
  **1.863 CFS** (cumulative 22.58 < 27.12) -> `lid_reduces_runoff=True`. Chart emitted.
- Honest knob: `rain_scale` (multiply the published storm).

### #7 swmm_wwtp_detention_ponds -- LANDED

Runs the cited detention-pond deck VERBATIM. Headline chart: the pond stage recession
(storage routing through outlet weirs; no external storm -- an initial-storage
drain-down, labeled as such -- the example publishes no numeric results).
- **LIVE cheap-smoke** (Topic 14400): continuity +0.000%, 30 nodes / 40 links, 3
  storage ponds (pond_B recedes 3.316 -> 3.293), peak outfall **4.269 CMS**, 22
  conduits surcharged. Chart emitted.

### #6 swmm_pump_pid_rtc -- LANDED

Runs the cited PID pump-control deck VERBATIM. Headline chart: the wet-well (upstream
node) depth tracking the PID target + the pump flow.
- **LIVE cheap-smoke** (Topic 10082): continuity -0.062%, 61 nodes / 58 links, the
  PID rule holds wet-well `82309e` toward its **3.0 ft** target (achieved range
  0.019 -> 3.282 ft), pump `PUMP1@82309e-15009e` peak flow **86.2 CFS**. Chart emitted.

### #4 swmm_green_grey_infra_storms -- STOPPED (recipe)

Topic 23670 hosts TWO decks on ONE page (DryPond_100yr24hr.inp + LID_100yr24hr.inp);
the row's WHOLE POINT is the paired grey-vs-green comparison. The LID deck extracts +
solves clean (continuity -0.013%); the DryPond deck's inline block still bleeds into
duplicate RAINGAGE ids on extraction. Recipe to land: (a) tighten `extract_inline_deck`'s
two-deck boundary so deck0 hard-stops at deck1's `[TITLE]` (the `select_index` machinery
exists; the DryPond block just needs a clean upper bound), (b) add a `variant`
(dry_pond | lid) knob to the template, (c) solve BOTH and chart the paired
grey-vs-green runoff across the storm. Effort S.

### #5 swmm_cso_regulator_network -- STOPPED (recipe)

The EPA Applications-Manual Example 8 (Combined Sewer Systems) raw `.inp` is NOT
hosted as a plain download anywhere (verified in the candidate roster: not on
epa.gov, not in the USEPA/Stormwater-Management-Model GitHub repo, and the manual PDF
would not decode to text through WebFetch). This deck does NOT fit the runner's thesis
(ingest a CITED PUBLISHED `.inp`) because there is no obtainable `.inp` -- it is a
BESPOKE-SYNTHESIS candidate, orthogonal to the fetch-inline runner. Recipe to land:
either (a) obtain the Example-8 `.inp` from a licensed EPA-SWMM GUI install's Examples
folder and characterize redistribution, or (b) rebuild the regulator (transverse
weir + orifice) + pump + force-main network from the manual's documented parameters as
a bespoke deck (a distinct capability from the inline-fetch runner). Effort M-L.

## Consequences

- Three new `engine="swmm" tier="template"` tools; CODED tools +3. Registry 188 ->
  191; templates 30 -> 33. Retrieval corpus + model-free `retrieve_visible_tools`
  proof for all three (each surfaces for its NL probe). Template hygiene gate passes.
- The runner REUSES the network family's headless `swmm5_run` seam VERBATIM; NO edit
  to `raster_cell_mesh.py`, `swmm_network.py`, `urban_flood.py`, or ANY flood/SFINCS
  seam (grep-verified -- the only cross-import is the shared
  `read_flow_routing_continuity` reader, same as `swmm_network.py`).
- Sourcing is fetch-at-runtime from the pinned public URL; the author-posted decks
  are NOT baked (redistribution unclear). Offline tests use a SELF-AUTHORED tiny deck
  (not a redistributed deck), so the offline suite never depends on a live fetch.
- Demonstration-honesty: every result carries the LOUD `demonstration_note` (the deck
  is the cited example's schematic network, not a user AOI) + `schematic_only=True`;
  there is NO georeferenced map layer (the schematic coordinates are not lon/lat).
- Two rows remain STOPped with precise recipes (above); a future wave lands them.
