"""System-prompt snapshot tests.

The prompt is a small static core: honesty rules, the data-and-place recipes that span tools,
scope and narration. It carries no tool list. These are text snapshots: a substantive
rewording updates the prompt and the assertion together."""

from __future__ import annotations

import re

from trid3nt_server.model.adapters.adapter import system_prompt

SYSTEM_PROMPT = system_prompt()

#: Engine families purged from the registry. Their first name segment is gone
#: from every registered tool, so only an explicit list can hold them out; a
#: name returns here one line at a time as its engine lands again.
_RETIRED_ENGINE_NAMES = (
    "sfincs_flood",
    "swmm_urban_flood",
    "geoclaw_inundation",
    "swan_wave_field",
    "schism_coupled_waves",
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


PROMPT_CHAR_CEILING = 4000


def test_the_prompt_is_a_small_static_core() -> None:
    """No tool list rides the prompt: it is the same text whatever the registry holds."""
    assert len(SYSTEM_PROMPT) <= PROMPT_CHAR_CEILING
    assert "<<TOOLS>>" not in SYSTEM_PROMPT
    assert "YOUR TOOLS" not in SYSTEM_PROMPT


def test_the_prompt_sends_the_model_to_search_tools() -> None:
    assert "search_tools" in SYSTEM_PROMPT
    assert "find_sources, then fetch" in SYSTEM_PROMPT


def test_the_prompt_carries_no_history_or_labels() -> None:
    for phrase in ("Stage 0", "anchor A", "Invariant 7", "Seattle", "KANSAS", "live agent",
                   "assumptions_summary", "code_exec", "<thinking>"):
        assert phrase not in SYSTEM_PROMPT, phrase


def test_the_prompt_forbids_ending_at_a_precursor() -> None:
    flat = " ".join(SYSTEM_PROMPT.split())
    assert "Never end a turn at a precursor" in flat


def test_the_prompt_clips_an_admin_region_by_polygon() -> None:
    flat = " ".join(SYSTEM_PROMPT.split())
    assert "means the polygon, not its bbox" in flat
    assert "gdal:cliprasterbymasklayer" in flat
    assert "native:clip" in flat
    for kind in ("state", "county", "city", "ZCTA", "watershed"):
        assert kind in flat, kind


def test_the_prompt_states_the_layer_handle_contract() -> None:
    flat = " ".join(SYSTEM_PROMPT.split())
    assert "short handles (L1, L2, ...)" in flat
    assert "exactly as a prior tool result gave it" in flat
    assert "never retype a URI" in flat


def test_the_prompt_keeps_the_honesty_rules() -> None:
    flat = " ".join(SYSTEM_PROMPT.split())
    assert "Never fabricate numbers" in flat
    assert "Never supply a physical model input" in flat
    assert "the template's starting values" in flat
    assert "screening-grade unless calibrated" in flat
    assert '"display artifact"' in flat


def test_the_prompt_keeps_scope_location_and_fit_rules() -> None:
    flat = " ".join(SYSTEM_PROMPT.split())
    assert "never resume an earlier one" in flat
    assert "Every request stands alone for WHERE" in flat
    assert "read its bbox= off the [Case state] note" in flat


def test_the_prompt_keeps_the_narration_rules() -> None:
    flat = " ".join(SYSTEM_PROMPT.split())
    assert "Be concise" in flat
    assert "one short sentence" in flat
    assert "never end silently" in flat
    assert "cite its error_code" in flat
    assert "correct the argument and call again" in flat
    assert "never ask for a key in chat" in flat
    assert "No emojis" in flat


def test_the_prompt_still_names_no_absent_tool_in_fixed_text() -> None:
    assert "run_sfincs" not in SYSTEM_PROMPT
