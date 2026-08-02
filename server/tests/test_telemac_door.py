"""TELEMAC engine-door slice (engine-door refactor, docs/specs/engine-rollout-contract.md).

Offline floor for the ``run_telemac`` door + ``telemac_river_dye`` template
(the NAME FLIP: the old ``run_telemac`` LLM tool re-tiered to the
``telemac_river_dye`` template; the freed ``run_telemac`` name is now the
read-only door):

(a) DOOR LISTING - direct-call the door on the live registry; assert the
    read-only concierge envelope shape (``kind == "engine_door"``, a non-empty
    ``templates[]`` whose every ``tool_name`` is a REGISTERED
    ``engine=telemac, tier=template`` tool, ``fidelity_brief`` +
    ``mismatch_redirect`` + ``next_action`` present). No solve.

(b) GATING EXCLUSION - ``telemac_river_dye`` (tier=template) is EXCLUDED from
    the default retrieval pool (absent from the discover index ``tool_names`` and
    from ``retrieve_visible_tools`` and from the fail-open floor); the door
    ``run_telemac`` (tier=door) is NOT excluded and is recognized as a
    gate-expander.

(c) REGISTRY MEMBERSHIP - the old ``run_telemac`` engine-tool is GONE as a
    workflow_dispatch tool; ``run_telemac`` is now registered tier=door and
    ``telemac_river_dye`` tier=template; the setter stays tier=general.

No network: the door reads the live registry; the index is rebuilt in-process.
"""

from __future__ import annotations

from trid3nt_server import server as agent_server
from trid3nt_server.agent import tools as agent_tools
from trid3nt_server.agent.tools.search.search_tools import search_tools as st
from trid3nt_server.agent.tools.search import tool_retrieval as tr
from trid3nt_server.agent.tools.simulation.telemac.run_telemac.run_telemac import run_telemac

_DOOR = "run_telemac"
_TEMPLATE = "telemac_river_dye"


# ---------------------------------------------------------------------------
# (c) REGISTRY MEMBERSHIP.
# ---------------------------------------------------------------------------


def test_name_flip_door_is_door_template_is_template():
    reg = agent_tools.TOOL_REGISTRY
    # run_telemac still exists, but it is now the read-only DOOR (not the old
    # workflow_dispatch engine tool).
    door = reg[_DOOR].metadata
    assert door.engine == "telemac" and door.tier == "door"
    assert door.source_class == "door"
    tmpl = reg[_TEMPLATE].metadata
    assert tmpl.engine == "telemac" and tmpl.tier == "template"
    assert tmpl.source_class == "workflow_dispatch"


def test_setter_stays_general():
    reg = agent_tools.TOOL_REGISTRY
    m = reg["set_telemac_parameters"].metadata
    assert m.tier == "general", "set_telemac_parameters must stay tier=general"


# ---------------------------------------------------------------------------
# (a) DOOR LISTING.
# ---------------------------------------------------------------------------


def test_door_listing_envelope_shape():
    env = run_telemac()
    assert env["engine"] == "telemac"
    assert env["kind"] == "engine_door"
    assert isinstance(env["templates"], list) and env["templates"], "non-empty listing"
    assert env["fidelity_brief"], "fidelity brief present"
    assert env["mismatch_redirect"], "mismatch redirect present"
    assert env["next_action"], "next_action present"
    # redirects name DOORS, not templates, and cover the neighbouring engines.
    redirect_text = " ".join(env["mismatch_redirect"].values())
    for door in ("run_modflow", "run_sfincs", "run_swmm", "run_geoclaw"):
        assert door in redirect_text, f"mismatch redirect must point at {door}"


def test_door_lists_only_registered_telemac_templates():
    reg = agent_tools.TOOL_REGISTRY
    env = run_telemac()
    listed = {c["tool_name"] for c in env["templates"]}
    assert _TEMPLATE in listed
    # every listed template is a REGISTERED engine=telemac tier=template (no
    # fabricated / not-yet-built template).
    for name in listed:
        m = reg[name].metadata
        assert m.engine == "telemac" and m.tier == "template", name
    # the card carries the curated (or derived) fields, never a fabricated arg.
    card = next(c for c in env["templates"] if c["tool_name"] == _TEMPLATE)
    assert card["question"], "template card carries a one-line question"
    assert isinstance(card["required_inputs"], list)


def test_door_listing_matches_registry_derivation():
    """The door listing is registry-derived (deterministic): the set of listed
    template names equals the set of engine=telemac tier=template registrations."""
    reg = agent_tools.TOOL_REGISTRY
    expected = sorted(
        n for n, e in reg.items()
        if getattr(e.metadata, "engine", None) == "telemac"
        and getattr(e.metadata, "tier", "general") == "template"
    )
    listed = sorted(c["tool_name"] for c in run_telemac()["templates"])
    assert listed == expected


# ---------------------------------------------------------------------------
# (b) GATING EXCLUSION + door recognition.
# ---------------------------------------------------------------------------


def test_template_excluded_door_present_in_index():
    st._reset_index_for_tests()
    try:
        idx = st._get_index()
        assert _TEMPLATE not in idx.tool_names, (
            "telemac_river_dye (tier=template) must be EXCLUDED from the default pool"
        )
        assert _DOOR in idx.tool_names, "the door (tier=door) stays in the pool"
    finally:
        st._reset_index_for_tests()


def test_template_never_surfaces_in_retrieval():
    st._reset_index_for_tests()
    try:
        st._get_index()
        vis = tr.retrieve_visible_tools(
            "a chemical spilled into the river, how far downstream does it travel",
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
    assert _DOOR in gx, "the TELEMAC door must be a registry-recognized gate-expander"
