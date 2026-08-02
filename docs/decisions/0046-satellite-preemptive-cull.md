# 0046 - satellite fire-animation composer preemptive cull

Context: docs/decisions/0041-shared-workflows-cull-phase-a.md STOPPED the cull of
`run_model_satellite_fire_animation` -- every other gate passed live (frame-peek
via the registered `fetch_slider_timestamps`, real imagery frames, name-token
scrubber grouping proven on the shared GOES drive, FIRMS densest-hotspot
clustering proven deterministically offline) but the FIRMS-localization +
FIRMS-overlay LIVE drive needed a `TRID3NT_FIRMS_MAP_KEY` absent from that
environment. Per "any gate failure = STOP that cull, never force through", the
composer was retained with its cull deferred to a session with a FIRMS key.

Decision (2026-07-29): NATE explicitly waived the missing FIRMS
live-verification requirement and approved a preemptive cull of
`run_model_satellite_fire_animation` -- the wrapper folder, its test file, the
`tools/__init__.py` registration import, the `categories.py` PRIMARY/SECONDARY
entries, and the `tool_query_corpus.yaml` block are removed; `fetch_firms_
active_fire` stays REGISTERED (no function is lost, only the credential-gated
live-overlay drive is untested this session). Docstring cross-references in
`run_elmfire`, `elmfire_fire_spread`, `fetch_wfigs_incident`, `fetch_viirs_
day_fire`, `fetch_goes_animation`, `fetch_goes_archive_animation`, and the
dormant `adapter.py` routing prose are re-pointed at the retained fetchers
(`fetch_goes_animation` / `fetch_goes_blend_animation` / `fetch_viirs_day_fire`)
and the frame-animation playground recipe instead of the deleted composer. The
wrapper's corpus intents are re-homed onto `fetch_goes_animation` (imagery,
general), `fetch_goes_blend_animation` (CIRA blend), `fetch_viirs_day_fire`
(JPSS/polar), and `fetch_firms_active_fire` (hotspot/localization) so the same
intents still route to a live tool via `retrieve_visible_tools`.

Consequence: registry 191 -> 190 (satellite composer removed, no replacement
registered -- the frame-animation playground recipe was already live from phase
A). All fire-animation function is retained via the registered fetchers plus
`docs/playbooks/frame-animation-recipe.md` Recipe A (imagery) and Recipe B
(FIRMS densest-hotspot AOI localization). Honest note: the FIRMS-localization
live-overlay path (`fetch_firms_active_fire` actually returning real hot
pixels) remains exercised end-to-end only in a session that has a
`TRID3NT_FIRMS_MAP_KEY` -- this cull removed the wrapper on NATE's explicit
waiver of that live check, not on a new live proof of the FIRMS leg.
