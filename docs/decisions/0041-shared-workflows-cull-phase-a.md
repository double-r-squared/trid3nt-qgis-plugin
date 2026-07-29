# 0041 - shared-workflows cull phase A (news + conservation + goes; P1 slider)

Context: `docs/specs/shared-workflows-cull-proposal.md` (NATE-approved 2026-07-29)
audited five `agent/workflows/shared/model_*` composers as suspected North-Star
wrappers. Per the cull doctrine a wrapper dies ONLY after a LIVE replication of
its process using retained tools -- grading = fired-tool set vs a catalog-validated
acceptable SET (deterministic) + output sanity (layers published; for animations,
the produced layer names auto-group under the plugin's `render/temporal.py`
name-token grouping). Function is never lost; only form changes.

Decision (phase A execution, 2026-07-29):

1. P1 -- `fetch_slider_timestamps` registered as a first-class atomic tool
   (`fetchers/imagery/fetch_slider_timestamps/`, own `corpus.yaml`), a THIN
   wrapper over the unchanged `_satellite_slider.fetch_slider_timestamps` helper
   enriched to a dict (count, ascending ints, earliest/latest ISO, cadence).
   `ttl_class="live-no-cache"` (an availability index turns over every few
   minutes). Primary `weather_atmosphere`, secondary `fire`. Registry +1.

2. CULLED (gate PASS live) -- 3 composers + their folders + tests + registry /
   categories / corpus references deleted:
   - `run_model_news_event_ingest` -- fired web_fetch + fetch_nws_event +
     fetch_storm_events_db -> aggregate_claims_across_sources -> geocode_location,
     reproducing the derived-param + geocoded-bbox review envelope.
   - `run_model_conservation_priority` -- fired geocode_location -> fetch_naip +
     compute_ndvi + fetch_mobi + fetch_gbif_occurrences + fetch_iucn_red_list_range
     (pure fan-out, no missing primitive). Fired-set matched exactly; GBIF
     published a live layer (the NAIP/NDVI/MoBI Planetary-Computer STAC tools hit
     a transient PC outage + IUCN needs an API key -- upstream/credential gaps on
     RETAINED tools that afflict the composer identically, graded outside the set).
   - `run_model_goes_fire_animation` -- fired fetch_slider_timestamps -> inline
     snap -> fetch_goes_animation -> 5 real published frames named
     `"GOES GeoColor step N <ISO> (GOES-19)"`; the plugin's `group_frame_layers`
     auto-grouped them into ONE scrubber sequence.

3. STOPPED (gate BLOCKED, wrapper KEPT) -- `run_model_satellite_fire_animation`.
   Its frame-peek (P1), imagery frames, and name-token scrubber grouping all pass
   via the shared GOES drive, and its FIRMS densest-hotspot clustering is proven
   deterministically offline, BUT the FIRMS-localization + FIRMS-overlay LIVE
   drive needs a `TRID3NT_FIRMS_MAP_KEY` absent in this environment. Per
   "any gate failure = STOP that cull, never force through", the composer + its
   tests + registration + categories + corpus are retained; its cull is deferred
   to a session with a FIRMS key (no missing primitive -- fetch_firms_active_fire
   is registered and retained).

4. `run_model_glm_lightning_animation` -- unchanged (proposal verdict EXTRACT-FIRST,
   blocked on P2 historical single-band ABI; out of phase-A scope).

5. Function re-homed:
   - Playground recipe `docs/playbooks/frame-animation-recipe.md` documents the
     frame-animation pattern (P1 -> snap -> imagery -> scrubber grouping), the
     FIRMS densest-hotspot AOI snippet (the ~15-line former `_densest_hotspot_bbox`
     + AOI precedence), and the news-ingest review-flow.
   - Corpus intents re-homed onto retained fetchers so the culled intents still
     surface via model-free `retrieve_visible_tools(prompt, None, 8)`: news ->
     `aggregate_claims_across_sources`; conservation -> `fetch_mobi`; GOES fire
     animation -> `fetch_goes_animation`.
   - `run_elmfire` door mismatch-redirect re-pointed off the culled goes composer
     onto `run_model_satellite_fire_animation` / `fetch_goes_animation`.

Consequence: registry 196 -> 194 (news -1, conservation -1, goes -1, P1 +1);
~2140 composer LOC removed (news 801 + conservation 507 + goes 832). The
satellite (1337 LOC) + glm (906 LOC) composers remain, each with a recorded,
credential-/primitive-gated path to a later cull. The offline suite's FAILED set
is unchanged (the 9 pre-existing fetch_resolution x4 + river_dye x5).
