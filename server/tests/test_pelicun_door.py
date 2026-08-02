"""PELICUN engine-door slice (engine-door refactor, docs/specs/engine-rollout-contract.md).

Offline floor for the ``run_pelicun`` door + the ``pelicun_damage_assessment``
template (PELICUN fold: the former ``pelicun_damage_with_buildings`` composer
folded into that ONE template's bbox auto-fetch input mode):

(a) DOOR LISTING - direct-call the door on the live registry; assert the
    read-only concierge envelope shape (``kind == "engine_door"``, a non-empty
    ``templates[]`` whose every ``tool_name`` is a REGISTERED
    ``engine=pelicun, tier=template`` tool, ``fidelity_brief`` +
    ``mismatch_redirect`` + ``next_action`` present). No solve.

(b) GATING EXCLUSION - both pelicun templates (tier=template) are EXCLUDED from
    the default retrieval pool (absent from the discover index ``tool_names`` and
    from ``retrieve_visible_tools`` and from the fail-open floor); the door
    ``run_pelicun`` (tier=door) is NOT excluded and is recognized as a
    gate-expander.

(c) REGISTRY MEMBERSHIP - the old ``run_pelicun_damage_assessment`` /
    ``run_pelicun_with_buildings`` names AND the folded
    ``pelicun_damage_with_buildings`` template name are all GONE; ``run_pelicun``
    is registered tier=door and the single ``pelicun_damage_assessment`` template
    is tier=template.

No network: the door reads the live registry; the index is rebuilt in-process.
"""

from __future__ import annotations

from trid3nt_server import server as agent_server
from trid3nt_server.agent import tools as agent_tools
from trid3nt_server.agent.tools.search.search_tools import search_tools as st
from trid3nt_server.agent.tools.search import tool_retrieval as tr
from trid3nt_server.agent.tools.simulation.pelicun.run_pelicun.run_pelicun import run_pelicun

_DOOR = "run_pelicun"
# PELICUN fold: ONE template (the with-buildings composer folded into its bbox
# auto-fetch input mode).
_TEMPLATES = {"pelicun_damage_assessment"}


# ---------------------------------------------------------------------------
# (c) REGISTRY MEMBERSHIP.
# ---------------------------------------------------------------------------


def test_old_pelicun_names_gone_and_door_templates_registered():
    reg = agent_tools.TOOL_REGISTRY
    # The old engine-door RENAME sources AND the folded with-buildings template
    # name are all gone (no alias): ONE pelicun_damage_assessment template remains.
    for dead in (
        "run_pelicun_damage_assessment",
        "run_pelicun_with_buildings",
        "pelicun_damage_with_buildings",
    ):
        assert dead not in reg, f"folded/old registered name {dead} must be REPLACED (no alias)"
    door = reg[_DOOR].metadata
    assert door.engine == "pelicun" and door.tier == "door"
    for t in _TEMPLATES:
        m = reg[t].metadata
        assert m.engine == "pelicun" and m.tier == "template", t
    # The single template keeps its cacheable pelicun_damage source_class.
    assert reg["pelicun_damage_assessment"].metadata.source_class == "pelicun_damage"


# ---------------------------------------------------------------------------
# (a) DOOR LISTING.
# ---------------------------------------------------------------------------


def test_door_listing_envelope_shape():
    env = run_pelicun()
    assert env["engine"] == "pelicun"
    assert env["kind"] == "engine_door"
    assert isinstance(env["templates"], list) and env["templates"], "non-empty listing"
    assert env["fidelity_brief"], "fidelity brief present"
    assert env["mismatch_redirect"], "mismatch redirect present"
    assert env["next_action"], "next_action present"
    # redirects name the neighbouring hazard DOORS (the hazard must run FIRST).
    redirect_text = " ".join(env["mismatch_redirect"].values())
    for target in ("run_sfincs", "run_openquake", "run_elmfire"):
        assert target in redirect_text, f"mismatch redirect must point at {target}"


def test_door_lists_only_registered_pelicun_templates():
    reg = agent_tools.TOOL_REGISTRY
    env = run_pelicun()
    listed = {c["tool_name"] for c in env["templates"]}
    assert _TEMPLATES.issubset(listed)
    # every listed template is a REGISTERED engine=pelicun tier=template (no
    # fabricated / not-yet-built template).
    for name in listed:
        m = reg[name].metadata
        assert m.engine == "pelicun" and m.tier == "template", name
    # cards carry the curated required inputs (never a fabricated required arg).
    card = next(c for c in env["templates"] if c["tool_name"] == "pelicun_damage_assessment")
    assert card["question"], "template card carries a one-line question"
    assert isinstance(card["required_inputs"], list)
    assert "hazard_raster_uri" in card["required_inputs"], "curated card lists the real required input"


def test_door_listing_matches_registry_derivation():
    """The door listing is registry-derived (deterministic): the set of listed
    template names equals the set of engine=pelicun tier=template registrations."""
    reg = agent_tools.TOOL_REGISTRY
    expected = sorted(
        n for n, e in reg.items()
        if getattr(e.metadata, "engine", None) == "pelicun"
        and getattr(e.metadata, "tier", "general") == "template"
    )
    listed = sorted(c["tool_name"] for c in run_pelicun()["templates"])
    assert listed == expected


# ---------------------------------------------------------------------------
# (b) GATING EXCLUSION + door recognition.
# ---------------------------------------------------------------------------


def test_templates_excluded_door_present_in_index():
    st._reset_index_for_tests()
    try:
        idx = st._get_index()
        for t in _TEMPLATES:
            assert t not in idx.tool_names, (
                f"{t} (tier=template) must be EXCLUDED from the default pool"
            )
        assert _DOOR in idx.tool_names, "the door (tier=door) stays in the pool"
    finally:
        st._reset_index_for_tests()


def test_templates_never_surface_in_retrieval():
    st._reset_index_for_tests()
    try:
        st._get_index()
        vis = tr.retrieve_visible_tools(
            "how much flood damage and repair cost will these buildings take",
            None,
            k=40,
        )
        for t in _TEMPLATES:
            assert t not in vis, "retrieve_visible_tools must never surface the template"
        floor = tr._full_registry_floor(set())
        for t in _TEMPLATES:
            assert t not in floor, "fail-open floor must not re-leak the template"
        assert _DOOR in floor, "fail-open floor must keep the door"
    finally:
        st._reset_index_for_tests()


def test_door_recognized_as_gate_expander():
    gx = agent_server._gate_expander_tool_names()
    assert _DOOR in gx, "the Pelicun door must be a registry-recognized gate-expander"
    edt = agent_server._engine_door_tool_names()
    assert _DOOR in edt, "the Pelicun door must be an engine-door"
