"""System-prompt snapshot tests (job B-sys, Wave 4.10 Stage-0 anchor A2/A5).

Stage 0 baseline anchor A2 surfaced: when a user prompt names a verbatim tool
(e.g. "show me protected areas in Big Cypress" → expects
``fetch_wdpa_protected_areas``) and the agent successfully geocodes a
precursor location, the agent CURRENTLY ENDS the turn without dispatching the
named tool. job B-sys amends ``SYSTEM_PROMPT`` with an explicit "Named-tool
follow-on dispatch" instruction so Gemini does not stop at the precursor step.

Stage 0 anchor A5 surfaced the parallel geographic-clipping gap: when the user
says "in [admin-region]", the agent should use ``fetch_administrative_boundaries``
+ ``clip_raster_to_polygon`` / ``clip_vector_to_polygon`` rather than collapsing
to a rectangular bbox approximation that bleeds into neighboring regions.

These tests are text snapshots — they confirm the prompt carries the new
sections verbatim. If the prompt is reworded substantively, update both the
prompt and these assertions in the same commit so the routing intent stays
visible to reviewers.
"""

from __future__ import annotations

from trid3nt_server.adapter import SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# A2 — Named-tool follow-on dispatch
# ---------------------------------------------------------------------------


def test_system_prompt_has_named_tool_followon_section() -> None:
    """Prompt must carry the Stage-0 anchor A2 routing fix."""
    assert "Named-tool follow-on dispatch" in SYSTEM_PROMPT


def test_system_prompt_lists_named_data_source_triggers() -> None:
    """A2 fix must name the verbatim dataset keywords the user types."""
    # A representative subset — full keyword list is in the prompt; the test
    # just guards against accidental deletion of the trigger vocabulary.
    for keyword in (
        "WDPA",
        "NEXRAD",
        "NWS alerts",
        "NLCD",
        "MRMS",
        "GBIF",
        "MTBS",
        "LANDFIRE",
    ):
        assert keyword in SYSTEM_PROMPT, (
            f"named-data-source keyword {keyword!r} missing — A2 routing weakens"
        )


def test_system_prompt_forbids_ending_at_precursor() -> None:
    """The 'DO NOT end the turn at the precursor' instruction is the load-bearing
    sentence that fixes Stage-0 anchor A2."""
    assert "DO NOT end the turn at the precursor" in SYSTEM_PROMPT


def test_system_prompt_carries_named_tool_example() -> None:
    """A2 prompt must include at least one geocode → fetch_* → narrate example."""
    # NEXRAD + Florida is the canonical worked example.
    assert "fetch_nexrad_reflectivity" in SYSTEM_PROMPT
    assert "geocode_location" in SYSTEM_PROMPT
    # And the WDPA Big Cypress example that anchored the baseline finding.
    assert "fetch_wdpa_protected_areas" in SYSTEM_PROMPT
    assert "Big Cypress" in SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# A5 — Geographic clipping pattern (in [admin-region])
# ---------------------------------------------------------------------------


def test_system_prompt_has_geographic_clipping_section() -> None:
    """Prompt must carry the Stage-0 anchor A5 polygon-clip instruction."""
    assert "Geographic clipping pattern" in SYSTEM_PROMPT


def test_system_prompt_names_admin_polygon_clip_tools() -> None:
    """A5 fix must reference the admin-boundary fetcher + both clip tools."""
    assert "fetch_administrative_boundaries" in SYSTEM_PROMPT
    assert "clip_raster_to_polygon" in SYSTEM_PROMPT
    assert "clip_vector_to_polygon" in SYSTEM_PROMPT


def test_system_prompt_lists_admin_region_kinds() -> None:
    """A5 fix must name the admin-region categories that trigger the pattern."""
    for kind in ("state", "county", "city", "ZCTA", "watershed"):
        assert kind in SYSTEM_PROMPT, (
            f"admin-region kind {kind!r} missing — A5 trigger vocabulary weakens"
        )


def test_system_prompt_forbids_bbox_approximation() -> None:
    """A5 fix must explicitly reject bbox-as-region for admin-polygon prompts."""
    # The load-bearing prohibition: "DO NOT just hand the dataset's bbox..."
    assert "DO NOT just hand the dataset's bbox" in SYSTEM_PROMPT


def test_system_prompt_carries_admin_clipping_example() -> None:
    """A5 fix must include a Miami-Dade-style worked example."""
    assert "Miami-Dade" in SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# job-0270 — Publish-to-map discipline
# ---------------------------------------------------------------------------


def test_system_prompt_has_publish_discipline_section() -> None:
    """Prompt must carry the job-0270 publish-to-map instruction — the live
    finding: Gemini computed a colored relief for Boulder, never called
    publish_layer, and the user saw nothing on the map."""
    assert "Publish-to-map discipline" in SYSTEM_PROMPT


def test_system_prompt_says_layers_invisible_until_published() -> None:
    """The load-bearing sentence: storage is not the map."""
    assert "NOT pixels on the user's map" in SYSTEM_PROMPT
    assert "publish_layer(layer_uri=<handle>" in SYSTEM_PROMPT


def test_system_prompt_forbids_claiming_display_without_wms_url() -> None:
    """The anti-fabrication half: never narrate a layer as displayed unless
    publish_layer returned a WMS URL this turn."""
    flat = " ".join(SYSTEM_PROMPT.split())
    assert (
        "NEVER claim a layer is displayed, shown, or \"added to the map\" "
        "unless publish_layer returned a WMS URL THIS turn" in flat
    )


def test_system_prompt_keeps_always_narrate_section() -> None:
    """The job-0270 insertion sits directly above the always-narrate clause —
    guard that the A1 section header survived the splice."""
    assert "Always-narrate after tools complete" in SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Regression — existing behaviors from job-0154 must survive the amendment
# ---------------------------------------------------------------------------


def test_system_prompt_still_routes_flood_modeling() -> None:
    """job-0154 routing instruction (flood → run_model_flood_scenario) survives."""
    assert "run_model_flood_scenario" in SYSTEM_PROMPT


def test_system_prompt_still_forbids_fabricated_numbers() -> None:
    """job-0154 anti-fabrication guard survives the amendment."""
    assert "Never fabricate numbers" in SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Wave 4.9 — vector layers must NOT be published via publish_layer
# ---------------------------------------------------------------------------


def test_system_prompt_has_vector_publish_prohibition_section() -> None:
    """Prompt must carry the raster-only publish guidance (vector render path)."""
    assert "publish_layer is for RASTER COGs ONLY" in SYSTEM_PROMPT


def test_system_prompt_forbids_publishing_vectors() -> None:
    """The load-bearing prohibition: never publish a vector layer."""
    assert "NEVER call publish_layer on a VECTOR layer" in SYSTEM_PROMPT


def test_system_prompt_names_vector_layer_kinds_and_extensions() -> None:
    """Vector trigger vocabulary: layer kinds + file extensions the agent must
    recognize as already-on-the-map vectors."""
    flat = " ".join(SYSTEM_PROMPT.split())
    for kind in ("roads", "rivers", "waterways", "administrative boundaries"):
        assert kind in flat, f"vector layer kind {kind!r} missing — render guard weakens"
    for ext in ("*.fgb", "*.geojson", "GeoParquet"):
        assert ext in flat, f"vector extension {ext!r} missing — render guard weakens"


def test_system_prompt_says_vectors_already_on_map() -> None:
    """The reason half: vectors are shown by their producing fetch tool, so
    publish_layer is a duplicate / error for them."""
    assert "ALREADY shown on the map" in SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Full-AOI extent — never shrink the area/bbox
# ---------------------------------------------------------------------------


def test_system_prompt_has_full_aoi_extent_section() -> None:
    """Prompt must carry the full-AOI-bbox-for-every-overlay guidance."""
    assert "Full-AOI extent for every overlay" in SYSTEM_PROMPT


def test_system_prompt_says_overlays_use_full_case_bbox() -> None:
    """Every area/overlay layer uses the SAME Case AOI bbox."""
    flat = " ".join(SYSTEM_PROMPT.split())
    assert "use the FULL Case AOI bounding box" in flat
    assert "the SAME bbox as the rest of the Case" in flat


def test_system_prompt_dont_fetch_all_never_means_shrink_bbox() -> None:
    """The load-bearing disambiguation: 'you don't need to fetch all' means
    fetch fewer layers, NEVER shrink the area/bbox."""
    flat = " ".join(SYSTEM_PROMPT.split())
    assert "It NEVER means shrink the area or the bbox" in flat


# ---------------------------------------------------------------------------
# 2026-06-17 — arg-error self-correct (Oklahoma-tornado bug)
# ---------------------------------------------------------------------------


def test_system_prompt_steers_self_correct_on_arg_error() -> None:
    """On an ARG/VALIDATION error the agent must SELF-CORRECT and retry — never
    tell the user to wait. This is the steer half of the Oklahoma-tornado fix."""
    flat = " ".join(SYSTEM_PROMPT.split())
    assert "SELF-CORRECT" in flat
    assert "do not tell the user to wait" in flat


def test_system_prompt_says_full_state_name_accepted() -> None:
    """State-keyed tools now accept a full US state name, not only ISO codes —
    the agent must know it can pass 'Oklahoma' (not just 'OK')."""
    flat = " ".join(SYSTEM_PROMPT.split())
    assert "full US state name is accepted" in flat
    assert "Oklahoma" in flat


# ---------------------------------------------------------------------------
# Groundwater spill routing — parameterized vs. news-article
# ---------------------------------------------------------------------------


def test_system_prompt_has_groundwater_spill_routing_section() -> None:
    """Prompt must carry the parameterized-vs-article groundwater routing."""
    assert "Groundwater spill routing" in SYSTEM_PROMPT


def test_system_prompt_routes_parameterized_spill_to_run_modflow_job() -> None:
    """A parameterized spill (location + contaminant + rate + duration) goes
    DIRECTLY to run_modflow_job."""
    flat = " ".join(SYSTEM_PROMPT.split())
    assert "call run_modflow_job DIRECTLY" in flat
    # spill_location_latlon passed as a 2-element [lat, lon] array.
    assert "spill_location_latlon as a 2-element [lat, lon] array" in flat


def test_system_prompt_keeps_article_path_off_parameterized_spill() -> None:
    """The news-article path must NOT be used for parameterized spills; it needs
    a volume in gallons/liters/barrels/tons."""
    assert "Do NOT use\nrun_model_groundwater_contamination_scenario" in SYSTEM_PROMPT
    flat = " ".join(SYSTEM_PROMPT.split())
    assert "gallons / liters / barrels / tons" in flat


def test_system_prompt_still_routes_modflow_groundwater() -> None:
    """run_modflow_job + the article-ingest tool both remain named in the prompt."""
    assert "run_modflow_job" in SYSTEM_PROMPT
    assert "run_model_groundwater_contamination_scenario" in SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# job-0324 follow-up — shaded/baked land cover uses the land cover AS the blend
# base (it is palette-aware); colored_relief is elevation colors, not
# land-cover classes. Mirrors the compute_blended_composite description fix.
# ---------------------------------------------------------------------------


def test_system_prompt_has_shaded_landcover_base_section() -> None:
    """Prompt must carry the shaded/baked land-cover blend-base rule."""
    assert "Shaded / baked land cover" in SYSTEM_PROMPT


def test_system_prompt_says_pass_landcover_as_blend_base() -> None:
    """The load-bearing instruction: pass the fetch_landcover handle DIRECTLY as
    compute_blended_composite's base_layer_uri."""
    flat = " ".join(SYSTEM_PROMPT.split())
    assert "fetch_landcover" in flat
    assert "compute_blended_composite" in flat
    assert "base_layer_uri" in flat
    # land cover is palette-aware / paletted-categorical.
    assert "paletted" in flat.lower() or "color table" in flat.lower()


def test_system_prompt_forbids_colored_relief_as_landcover_base() -> None:
    """The anti-substitution half: colored_relief is elevation colors, NOT
    land-cover classes — do not use it as the base for shaded land cover."""
    flat = " ".join(SYSTEM_PROMPT.split())
    assert "compute_colored_relief" in flat
    assert "ELEVATION colors" in flat or "elevation colors" in flat.lower()
    assert "land-cover classes" in flat or "land-cover class" in flat


# ---------------------------------------------------------------------------
# Narration conciseness (user 2026-06-16) — be concise; do not re-explain the
# same thing across retries or recap every step verbosely each turn.
# ---------------------------------------------------------------------------


def test_system_prompt_has_narration_conciseness_section() -> None:
    """Prompt must carry the narration-conciseness rule."""
    assert "Narration conciseness" in SYSTEM_PROMPT


def test_system_prompt_says_do_not_re_explain_across_retries() -> None:
    """The load-bearing instruction: do not re-explain across retries / recap
    every step verbosely each turn."""
    flat = " ".join(SYSTEM_PROMPT.split())
    assert "Be concise" in flat
    assert "re-explain the same thing across retries" in flat
    assert "recap every" in flat or "recap" in flat
