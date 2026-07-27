"""SWAN engine-door slice (engine-door refactor, docs/specs/engine-rollout-contract.md).

Offline floor for the ``run_swan`` door + ``swan_wave_field`` template:

(a) DOOR LISTING - direct-call the door on the live registry; assert the
    read-only concierge envelope shape (``kind == "engine_door"``, a non-empty
    ``templates[]`` whose every ``tool_name`` is a REGISTERED
    ``engine=swan, tier=template`` tool, ``fidelity_brief`` +
    ``mismatch_redirect`` + ``next_action`` present). No solve.

(b) GATING EXCLUSION - ``swan_wave_field`` (tier=template) is EXCLUDED from the
    default retrieval pool (absent from the discover index ``tool_names`` and
    from ``retrieve_visible_tools`` and from the fail-open floor); the door
    ``run_swan`` (tier=door) is NOT excluded and is recognized as a
    gate-expander.

(c) REGISTRY MEMBERSHIP - the old ``run_swan_waves`` name is GONE; ``run_swan``
    is registered tier=door and ``swan_wave_field`` tier=template.

No network: the door reads the live registry; the index is rebuilt in-process.
"""

from __future__ import annotations

from trid3nt_server import server as agent_server
from trid3nt_server import tools as agent_tools
from trid3nt_server.tools.discovery.search_tools import search_tools as st
from trid3nt_server.tools.discovery import tool_retrieval as tr
from trid3nt_server.tools.simulation.swan.run_swan.run_swan import run_swan

_DOOR = "run_swan"
_TEMPLATE = "swan_wave_field"


# ---------------------------------------------------------------------------
# (c) REGISTRY MEMBERSHIP.
# ---------------------------------------------------------------------------


def test_old_swan_name_gone_and_door_template_registered():
    reg = agent_tools.TOOL_REGISTRY
    assert "run_swan_waves" not in reg, (
        "the old registered name must be REPLACED (no alias)"
    )
    door = reg[_DOOR].metadata
    assert door.engine == "swan" and door.tier == "door"
    tmpl = reg[_TEMPLATE].metadata
    assert tmpl.engine == "swan" and tmpl.tier == "template"
    assert tmpl.source_class == "workflow_dispatch"


# ---------------------------------------------------------------------------
# (a) DOOR LISTING.
# ---------------------------------------------------------------------------


def test_door_listing_envelope_shape():
    env = run_swan()
    assert env["engine"] == "swan"
    assert env["kind"] == "engine_door"
    assert isinstance(env["templates"], list) and env["templates"], "non-empty listing"
    assert env["fidelity_brief"], "fidelity brief present"
    assert env["mismatch_redirect"], "mismatch redirect present"
    assert env["next_action"], "next_action present"
    # redirects name DOORS, not templates, and cover the neighbouring engines.
    redirect_text = " ".join(env["mismatch_redirect"].values())
    for door in ("run_sfincs", "run_geoclaw"):
        assert door in redirect_text, f"mismatch redirect must point at {door}"


def test_door_lists_only_registered_swan_templates():
    reg = agent_tools.TOOL_REGISTRY
    env = run_swan()
    listed = {c["tool_name"] for c in env["templates"]}
    assert _TEMPLATE in listed
    # every listed template is a REGISTERED engine=swan tier=template (no
    # fabricated / not-yet-built template).
    for name in listed:
        m = reg[name].metadata
        assert m.engine == "swan" and m.tier == "template", name
    # cards carry the curated (or derived) fields, never a fabricated required arg.
    card = next(c for c in env["templates"] if c["tool_name"] == _TEMPLATE)
    assert card["question"], "template card carries a one-line question"
    assert isinstance(card["required_inputs"], list)
    assert "bbox" in card["required_inputs"], "the curated card lists the real required input"


def test_door_listing_matches_registry_derivation():
    """The door listing is registry-derived (deterministic): the set of listed
    template names equals the set of engine=swan tier=template registrations."""
    reg = agent_tools.TOOL_REGISTRY
    expected = sorted(
        n for n, e in reg.items()
        if getattr(e.metadata, "engine", None) == "swan"
        and getattr(e.metadata, "tier", "general") == "template"
    )
    listed = sorted(c["tool_name"] for c in run_swan()["templates"])
    assert listed == expected


# ---------------------------------------------------------------------------
# (b) GATING EXCLUSION + door recognition.
# ---------------------------------------------------------------------------


def test_template_excluded_door_present_in_index():
    st._reset_index_for_tests()
    try:
        idx = st._get_index()
        assert _TEMPLATE not in idx.tool_names, (
            "swan_wave_field (tier=template) must be EXCLUDED from the default pool"
        )
        assert _DOOR in idx.tool_names, "the door (tier=door) stays in the pool"
    finally:
        st._reset_index_for_tests()


def test_template_never_surfaces_in_retrieval():
    st._reset_index_for_tests()
    try:
        st._get_index()
        vis = tr.retrieve_visible_tools(
            "give me a defensible nearshore wave field with Hs Tp and direction", None, k=40
        )
        assert _TEMPLATE not in vis, "retrieve_visible_tools must never surface the template"
        floor = tr._full_registry_floor(set())
        assert _TEMPLATE not in floor, "fail-open floor must not re-leak the template"
        assert _DOOR in floor, "fail-open floor must keep the door"
    finally:
        st._reset_index_for_tests()


def test_door_recognized_as_gate_expander():
    gx = agent_server._gate_expander_tool_names()
    assert _DOOR in gx, "the SWAN door must be a registry-recognized gate-expander"
    edt = agent_server._engine_door_tool_names()
    assert _DOOR in edt, "the SWAN door must be an engine-door"
