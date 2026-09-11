"""System-prompt snapshot tests.

The prompt carries its named-tool follow-on dispatch instruction, so a turn does
not stop at a geocode precursor, and its geographic-clipping instruction, so an
admin-region ask is clipped to the boundary rather than a bbox. These are text
snapshots: a substantive rewording updates the prompt and the assertion together."""

from __future__ import annotations

import re

from trid3nt_server.adapters.adapter import SYSTEM_PROMPT



#: Engine families purged from the registry. Their first name segment is gone
#: from every registered tool, so only an explicit list can hold them out; a
#: name returns here one line at a time as its engine lands again.
_RETIRED_ENGINE_NAMES = (
    "sfincs_flood",
    "swmm_urban_flood",
    "geoclaw_inundation",
    "swan_wave_field",
    "schism_coupled_waves",
    "modflow_",
    "openquake_psha",
    "landlab_susceptibility",
    "elmfire_fire_spread",
    "pelicun",
    "publish_layer",
)


def test_system_prompt_names_no_absent_tool() -> None:
    """A prompt naming a tool the registry lacks routes the model at nothing.

    Two locks: no purged engine family by name, and every token sharing a first
    segment with a registered tool must itself be registered."""
    import trid3nt_server.tools as agent_tools

    flat = SYSTEM_PROMPT.lower()
    for name in _RETIRED_ENGINE_NAMES:
        assert name not in flat, f"purged tool name {name!r} is back in the prompt"

    registry = set(agent_tools.TOOL_REGISTRY)
    live_prefixes = {name.split("_", 1)[0] for name in registry if "_" in name}
    for token in set(re.findall(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b", SYSTEM_PROMPT)):
        if token.split("_", 1)[0] in live_prefixes and token not in registry:
            raise AssertionError(f"prompt names {token!r}, absent from the registry")


def test_system_prompt_states_the_live_modelling_surface() -> None:
    """The capability paragraph reads as question classes the live templates
    answer, and every family the ruling names is reachable."""
    flat = " ".join(SYSTEM_PROMPT.split())
    assert "The questions you can currently MODEL" in flat
    for name in (
        "telemac_river_dye",
        "telemac_do_sag",
        "telemac_rain_on_grid",
        "telemac3d_stratified_flow",
        "artemis_harbor_agitation",
    ):
        assert name in SYSTEM_PROMPT, f"live template {name!r} unreachable from the prompt"


def test_system_prompt_declares_honest_absence() -> None:
    """Where a class has no solver, the prompt tells the model to say so rather
    than route somewhere plausible."""
    flat = " ".join(SYSTEM_PROMPT.split())
    assert "Honest absence" in flat
    assert "not currently modeled" in flat
    assert "never route to a tool that does not exist" in flat


def test_system_prompt_fidelity_ladder_is_engine_name_free() -> None:
    """The ladder survives as a PRINCIPLE - rung chosen by the question, with
    calibration last - so it stops rotting as the engine roster changes."""
    flat = " ".join(SYSTEM_PROMPT.split())
    assert "Fidelity ladder" in flat
    assert "Choose the rung by the QUESTION" in flat
    assert "BELOW the useful range of a depth-averaged 2D run" in flat
    assert "CALIBRATION is the crux and comes LAST" in flat




def test_system_prompt_has_named_tool_followon_section() -> None:
    """Prompt must carry the Stage-0 anchor A2 routing fix."""
    assert "Named-tool follow-on dispatch" in SYSTEM_PROMPT


def test_system_prompt_lists_named_data_source_triggers() -> None:
    """A2 fix must name the verbatim dataset keywords the user types."""
    # A representative subset — full keyword list is in the prompt; the test
    # just guards against accidental deletion of the trigger vocabulary.
    for keyword in (
        "FEMA NFHL",
        "NEXRAD",
        "NWS alerts",
        "NLCD",
        "MRMS",
        "NWI",
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
    assert "show_nexrad_radar" in SYSTEM_PROMPT
    assert "geocode_location" in SYSTEM_PROMPT
    # And the flood-zone pair that carries the precursor-then-tool shape.
    assert "fetch_fema_nfhl_zones" in SYSTEM_PROMPT
    assert "Cape Coral" in SYSTEM_PROMPT




def test_system_prompt_has_geographic_clipping_section() -> None:
    """Prompt must carry the Stage-0 anchor A5 polygon-clip instruction."""
    assert "Geographic clipping pattern" in SYSTEM_PROMPT


def test_system_prompt_names_admin_polygon_clip_tools() -> None:
    """A5 fix must reference the admin-boundary fetcher + the raster clip tool +
    the vector clip surface. There is no clip_vector_to_polygon;
    vector clipping lives on spatial_query (ST_Within/ST_Intersects)."""
    assert "fetch_administrative_boundaries" in SYSTEM_PROMPT
    assert "clip_raster_to_polygon" in SYSTEM_PROMPT
    assert "spatial_query" in SYSTEM_PROMPT


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


# The compact layer-handle block replaces the handle-indirection,
# publish-discipline and full-AOI-extent prose.
# The harness now enforces these structurally: short handles (L1, L2, ...),
# typed rejection of unknown URIs, auto-publish/emit seams, and bbox
# auto-fill from the active AOI / case bbox.


def test_system_prompt_has_compact_layer_handle_block() -> None:
    """The compact replacement block states the handle contract positively."""
    flat = " ".join(SYSTEM_PROMPT.split())
    assert "short handles (L1, L2, ...)" in flat
    assert "pass the handle exactly as it appeared in a prior tool result" in flat
    assert "never retype or construct a URI" in flat


def test_system_prompt_says_layers_reach_map_automatically() -> None:
    """Map delivery is automatic — the prompt no longer begs for publish calls."""
    flat = " ".join(SYSTEM_PROMPT.split())
    assert "reach the user's map automatically" in flat


def test_system_prompt_says_omitted_bbox_autofills() -> None:
    """Omitted bbox args auto-fill from the active map extent / case area."""
    flat = " ".join(SYSTEM_PROMPT.split())
    assert "If you omit a bbox argument, it is auto-filled" in flat
    assert "active map extent or the case area" in flat


def test_system_prompt_removed_blocks_stay_removed() -> None:
    """Regression lock: the four prompt cuts must not creep back.
    The harness enforces these structurally; re-adding the prose re-spends
    ~1.6k tokens per turn for no behavior change."""
    flat = " ".join(SYSTEM_PROMPT.split())
    for phrase in (
        "Layer-handle indirection",          # handle-indirection block
        "URI_HANDLE_UNRESOLVED",
        "layer_handles",
        "Publish-to-map discipline",          # publish-discipline block
        "NOT pixels on the user's map",
        "publish_layer(layer_uri=<handle>",
        "Full-AOI extent for every overlay",  # full-AOI publish paragraph
        "It NEVER means shrink the area or the bbox",
        "ELEVATION colors",                   # trailing colored-relief note
    ):
        assert phrase not in flat, (
            f"removed-block phrase {phrase!r} reappeared - the publish-discipline block is cut"
        )


def test_system_prompt_keeps_always_narrate_section() -> None:
    """Guard that the A1 always-narrate section header survived the splice."""
    assert "Always-narrate after tools complete" in SYSTEM_PROMPT




def test_system_prompt_still_routes_rainfall_runoff() -> None:
    """The live rainfall-runoff question routes to the registered template
    directly, with no dissolved door name in front of it."""
    assert "telemac_rain_on_grid" in SYSTEM_PROMPT
    assert "run_sfincs" not in SYSTEM_PROMPT


def test_system_prompt_still_forbids_fabricated_numbers() -> None:
    """anti-fabrication guard survives the amendment."""
    assert "Never fabricate numbers" in SYSTEM_PROMPT


def test_system_prompt_forbids_inventing_physical_inputs() -> None:
    """The ask-dont-invent rule for physical MODEL INPUTS must be present.

    The model asks for a physical parameter it cannot fetch or derive, and names
    demo-default versus site-derived provenance in its narration."""
    assert "Never invent PHYSICAL MODEL INPUTS" in SYSTEM_PROMPT
    assert "synthetic_inputs" in SYSTEM_PROMPT
    assert "demo defaults versus site-derived" in SYSTEM_PROMPT


def test_system_prompt_has_input_review_instruction() -> None:
    """Two-mode input gate: the prompt must instruct the agent how to
    handle a user-gated INPUT REVIEW card -- present the resolved input table,
    collect edits, confirm before running."""
    assert "INPUT REVIEW card" in SYSTEM_PROMPT
    assert "param = value [basis, source]" in SYSTEM_PROMPT
    # "Do not run the solver until the user has approved" -- fragment avoids the
    # source line-wrap between "has" and "approved".
    flat = " ".join(SYSTEM_PROMPT.split())
    assert "Do not run the solver until the user has approved" in flat




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




def test_system_prompt_never_invents_contamination_forcing() -> None:
    """The prompt must keep the Invariant-9 never-invent rule so the model
    extracts (never fabricates) the spill forcing from the article/user."""
    flat = " ".join(SYSTEM_PROMPT.split())
    assert "NEVER INVENT a contamination parameter" in flat
    assert "Invariant 9" in SYSTEM_PROMPT
    assert "gallons / liters / barrels / tons" in flat


def test_system_prompt_routes_the_news_article_spill_to_the_river_plume() -> None:
    """The news-article path is a model-composed chain (web_fetch -> extract ->
    derive the forcing -> the registered river-plume template)."""
    assert "telemac_river_dye" in SYSTEM_PROMPT
    assert "web_fetch" in SYSTEM_PROMPT
    assert "NEWS ARTICLE" in SYSTEM_PROMPT


# Shaded/baked land cover uses the land cover AS the blend
# base (it is palette-aware); colored_relief is elevation colors, not
# land-cover classes. Mirrors the compute_blended_composite description fix.


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
    """The anti-substitution half: do not use compute_colored_relief as the
    base for shaded land cover. (The trailing elevation-colors rationale was
    cut — the prohibition sentence itself remains.)"""
    flat = " ".join(SYSTEM_PROMPT.split())
    assert "NOT substitute compute_colored_relief as the base" in flat


# Narration conciseness — be concise; do not re-explain the
# same thing across retries or recap every step verbosely each turn.


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
