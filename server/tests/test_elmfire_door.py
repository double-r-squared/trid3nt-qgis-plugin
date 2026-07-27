"""ELMFIRE engine-door slice (engine-door refactor, docs/specs/engine-rollout-contract.md).

Offline floor for the ``run_elmfire`` door + ``elmfire_fire_spread`` template:

(a) DOOR LISTING - direct-call the door on the live registry; assert the
    read-only concierge envelope shape (``kind == "engine_door"``, a non-empty
    ``templates[]`` whose every ``tool_name`` is a REGISTERED
    ``engine=elmfire, tier=template`` tool, ``fidelity_brief`` +
    ``mismatch_redirect`` + ``next_action`` present). No solve.

(b) GATING EXCLUSION - ``elmfire_fire_spread`` (tier=template) is EXCLUDED from
    the default retrieval pool (absent from the discover index ``tool_names`` and
    from ``retrieve_visible_tools`` and from the fail-open floor); the door
    ``run_elmfire`` (tier=door) is NOT excluded and is recognized as a
    gate-expander.

(c) REGISTRY MEMBERSHIP - the old ``model_fire_spread`` name is GONE;
    ``run_elmfire`` is registered tier=door and ``elmfire_fire_spread``
    tier=template.

No network: the door reads the live registry; the index is rebuilt in-process.
"""

from __future__ import annotations

from trid3nt_server import server as agent_server
from trid3nt_server import tools as agent_tools
from trid3nt_server.tools.discovery.search_tools import search_tools as st
from trid3nt_server.tools.discovery import tool_retrieval as tr
from trid3nt_server.tools.simulation.elmfire.run_elmfire.run_elmfire import run_elmfire

_DOOR = "run_elmfire"
_TEMPLATE = "elmfire_fire_spread"


# ---------------------------------------------------------------------------
# (c) REGISTRY MEMBERSHIP.
# ---------------------------------------------------------------------------


def test_old_elmfire_name_gone_and_door_template_registered():
    reg = agent_tools.TOOL_REGISTRY
    assert "model_fire_spread" not in reg, (
        "the old registered name must be REPLACED (no alias)"
    )
    door = reg[_DOOR].metadata
    assert door.engine == "elmfire" and door.tier == "door"
    tmpl = reg[_TEMPLATE].metadata
    assert tmpl.engine == "elmfire" and tmpl.tier == "template"
    assert tmpl.source_class == "workflow_dispatch"


# ---------------------------------------------------------------------------
# (a) DOOR LISTING.
# ---------------------------------------------------------------------------


def test_door_listing_envelope_shape():
    env = run_elmfire()
    assert env["engine"] == "elmfire"
    assert env["kind"] == "engine_door"
    assert isinstance(env["templates"], list) and env["templates"], "non-empty listing"
    assert env["fidelity_brief"], "fidelity brief present"
    assert env["mismatch_redirect"], "mismatch redirect present"
    assert env["next_action"], "next_action present"
    # redirects name the neighbouring fire tools (NOT engine doors - debris-flow
    # is a general pfdf tool, the animations are general composers).
    redirect_text = " ".join(env["mismatch_redirect"].values())
    for target in ("model_debris_flow", "run_model_goes_fire_animation"):
        assert target in redirect_text, f"mismatch redirect must point at {target}"


def test_door_lists_only_registered_elmfire_templates():
    reg = agent_tools.TOOL_REGISTRY
    env = run_elmfire()
    listed = {c["tool_name"] for c in env["templates"]}
    assert _TEMPLATE in listed
    # every listed template is a REGISTERED engine=elmfire tier=template (no
    # fabricated / not-yet-built template).
    for name in listed:
        m = reg[name].metadata
        assert m.engine == "elmfire" and m.tier == "template", name
    # cards carry the curated (or derived) fields, never a fabricated required arg.
    card = next(c for c in env["templates"] if c["tool_name"] == _TEMPLATE)
    assert card["question"], "template card carries a one-line question"
    assert isinstance(card["required_inputs"], list)
    assert "ignition_lonlat" in card["required_inputs"], (
        "the curated card lists the real required inputs (ignition is never fabricated)"
    )


def test_door_listing_matches_registry_derivation():
    """The door listing is registry-derived (deterministic): the set of listed
    template names equals the set of engine=elmfire tier=template registrations."""
    reg = agent_tools.TOOL_REGISTRY
    expected = sorted(
        n for n, e in reg.items()
        if getattr(e.metadata, "engine", None) == "elmfire"
        and getattr(e.metadata, "tier", "general") == "template"
    )
    listed = sorted(c["tool_name"] for c in run_elmfire()["templates"])
    assert listed == expected


# ---------------------------------------------------------------------------
# (b) GATING EXCLUSION + door recognition.
# ---------------------------------------------------------------------------


def test_template_excluded_door_present_in_index():
    st._reset_index_for_tests()
    try:
        idx = st._get_index()
        assert _TEMPLATE not in idx.tool_names, (
            "elmfire_fire_spread (tier=template) must be EXCLUDED from the default pool"
        )
        assert _DOOR in idx.tool_names, "the door (tier=door) stays in the pool"
    finally:
        st._reset_index_for_tests()


def test_template_never_surfaces_in_retrieval():
    st._reset_index_for_tests()
    try:
        st._get_index()
        vis = tr.retrieve_visible_tools(
            "simulate wildfire spread from this ignition point",
            None,
            k=40,
        )
        assert _TEMPLATE not in vis, "retrieve_visible_tools must never surface the template"
        floor = tr._full_registry_floor(set())
        assert _TEMPLATE not in floor, "fail-open floor must not re-leak the template"
        assert _DOOR in floor, "fail-open floor must keep the door"
    finally:
        st._reset_index_for_tests()


def test_door_recognized_as_gate_expander():
    gx = agent_server._gate_expander_tool_names()
    assert _DOOR in gx, "the ELMFIRE door must be a registry-recognized gate-expander"
    edt = agent_server._engine_door_tool_names()
    assert _DOOR in edt, "the ELMFIRE door must be an engine-door"
