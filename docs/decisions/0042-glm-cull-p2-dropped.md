# 0042 - glm-lightning-animation cull; P2 dropped (revised gate)

Context: `docs/decisions/0041-shared-workflows-cull-phase-a.md` left
`run_model_glm_lightning_animation` uncut, blocked on the proposal's P2 (a
historical single-band ABI grayscale primitive) so the composer's baked
grayscale-ABI base could be reproduced in the playground. NATE (2026-07-29)
revised the gate: that baked base is a WEB-ERA ARTIFACT, not function to
preserve -- in QGIS the base map is native/switchable (or a fetched imagery
loop). See the dated addendum in `docs/specs/shared-workflows-cull-proposal.md`.

Decision (2026-07-29):

1. P2 DROPPED -- no historical single-band ABI primitive is built. The GLM
   gridding + GED purple colorizer + accumulation fan-out ALREADY live in the
   retained `fetch_glm_lightning` fetcher (ordered `step <N>` frames when
   `accumulation_window_s` is set). Moving-base asks are covered by the retained
   `fetch_goes_archive_animation` (ABI true_color / fire_temperature frames).

2. `run_model_glm_lightning_animation` CULLED (gate PASS live, 2026-07-29,
   Florida AOI, GOES-19, 2026-07-27 21:00..21:05Z). Replication =
   `fetch_glm_lightning(accumulation_window_s=60)` -> 5 ordered GED frames ->
   `publish_layer` per frame -> the plugin's `group_frame_layers` auto-grouped
   them into ONE scrubber sequence. Fired-set == acceptable set
   `{fetch_glm_lightning, publish_layer}`, no news/geocode/wrapper step. The
   moving-base variant co-published 3 `fetch_goes_archive_animation` true_color
   frames -> TWO scrubber groups ("lightning over satellite").

3. Removed: the composer folder + `test_model_glm_lightning_animation.py`, the
   `tools/__init__.py` registration import, both `categories.py` entries, the
   central `tool_query_corpus.yaml` block, the `tool_sweep.py` fixture, and the
   `test_always_offload_heavy_tools.py` per-frame-bake test (its off-loop
   concern dies with the composer). Registry 198 -> 197 (glm -1).

4. Function re-homed: animation intents onto `fetch_glm_lightning/corpus.yaml`
   (+ a moving-base intent on `fetch_goes_archive_animation/corpus.yaml`);
   model-free `retrieve_visible_tools(prompt, None, 8)` surfaces both retained
   fetchers in the top-8 for the culled intents. The playground pattern is
   Recipe D (+ moving-base variant) in
   `docs/playbooks/frame-animation-recipe.md`.

Consequence: ~906 composer LOC removed; no missing primitive, no new build. The
web-era baked-visible-base is intentionally not reproduced (native QGIS base
map). The satellite (1337 LOC) composer remains the last shared-workflows cull
candidate, still gated on a FIRMS-map-key live drive (0041 item 3).
