"""Catalog-surfacing mechanisms: the two arm prerequisites and the identity gate.

Under the DEFAULT config the spec-served sources stay ``tier="general"`` and
ambient-declarable; with the arm flag on they leave the declarable pool for
``tier="catalog"`` while staying in the search index. The flag is read at import,
so flag-on registration is asserted in a subprocess. ASCII only."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

# --- Shared subprocess driver: run a snippet under a chosen arm env, parse the
#     trailing JSON line it prints. Mirrors the "each arm in its own process"
#     isolation guarantee (registration tier + the fetch_from_catalog signature
#     are import-time-frozen per process). ---

_DRIVER = r"""
import json, inspect, os
import trid3nt_server.main as m
m._import_tools_registry()
import trid3nt_server.server as srv
from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.fetchers._router import registration as reg
from trid3nt_server.tools.search.search_tools import search_tools as st
from trid3nt_server.tools.search.tool_retrieval import retrieve_ranked_tools

specs = sorted(reg.registered_spec_names())
dd = srv._default_declarable_registry()
st._reset_index_for_tests(); idx = st._get_index()
ranked = [n for n, _ in retrieve_ranked_tools("fuel moisture fire danger for this area", 25)]
ffc = TOOL_REGISTRY["fetch_from_catalog"].fn
out = {
    "arm": reg.catalog_arm(),
    "registry_size": len(TOOL_REGISTRY),
    "n_specs": len(specs),
    "gridmet_tier": getattr(TOOL_REGISTRY["fetch_gridmet"].metadata, "tier", "?"),
    "declarable_size": len(dd),
    "any_spec_in_declarable": any(s in dd for s in specs),
    "gridmet_in_index": "fetch_gridmet" in idx.tool_names,
    "gridmet_ranked_top25": "fetch_gridmet" in ranked,
    "ffc_params": list(inspect.signature(ffc).parameters.keys()),
    "ffc_doc_head": (ffc.__doc__ or "")[:48],
}
print("RESULT_JSON=" + json.dumps(out))
"""


def _run_arm(arm: str | None) -> dict:
    env = {k: v for k, v in _os_environ().items() if k != "TRID3NT_CATALOG_ARM"}
    if arm is not None:
        env["TRID3NT_CATALOG_ARM"] = arm
    proc = subprocess.run(
        [sys.executable, "-c", _DRIVER],
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )
    assert proc.returncode == 0, f"driver failed (arm={arm}):\n{proc.stderr[-2000:]}"
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT_JSON=")]
    assert line, f"no RESULT_JSON in stdout:\n{proc.stdout[-1000:]}"
    return json.loads(line[-1][len("RESULT_JSON="):])


def _os_environ() -> dict:
    import os

    return dict(os.environ)


#: How many tools the registry holds. Pinned so a tool that ARRIVES or LEAVES is
#: noticed rather than absorbed; the catalog arms never change it, they only
#: shrink the declarable POOL. Update this number only alongside the landing or
#: the removal that moves it.
_REGISTRY_SIZE = 163


# --------------------------------------------------------------------------- #
# Identity gate: DEFAULT config unchanged.
# --------------------------------------------------------------------------- #


def test_default_config_identity():
    r = _run_arm(None)
    assert r["arm"] is None
    # The roster PIN: a tool that leaves the registry has to be noticed, so the
    # arms assert the same number and a silent drop fails four tests at once.
    assert r["registry_size"] == _REGISTRY_SIZE
    assert r["n_specs"] == 99
    # They stay ambient (tier=general) and IN the declarable pool.
    assert r["gridmet_tier"] == "general"
    assert r["any_spec_in_declarable"] is True
    # fetch_from_catalog keeps its exact entry_id-only signature (no source param).
    assert r["ffc_params"] == ["entry_id", "params", "_extra_ignored"]
    assert r["ffc_doc_head"].startswith("Fetch bytes for a vetted catalog entry")


# --------------------------------------------------------------------------- #
# Arm 2 (discovery-expands-declaration) prerequisite.
# --------------------------------------------------------------------------- #


def test_arm2_specs_leave_pool_but_stay_indexed():
    r = _run_arm("2")
    assert r["arm"] == "2"
    assert r["registry_size"] == _REGISTRY_SIZE
    assert r["gridmet_tier"] == "catalog"
    assert r["any_spec_in_declarable"] is False  # every spec leaves the ambient pool
    # Every promoted spec leaves the ambient pool under this arm EXCEPT the ones
    # already outside it in the None baseline (tier="internal" absorptions). The
    # number is the measured difference, and it moves only when a spec lands or
    # leaves.
    assert r["declarable_size"] == _run_arm(None)["declarable_size"] - 98
    # Still searchable + rankable so a search hit can gate-expand it.
    assert r["gridmet_in_index"] is True
    assert r["gridmet_ranked_top25"] is True
    # Arm 2 keeps the provider FunctionDeclaration: fetch_from_catalog is unchanged.
    assert r["ffc_params"] == ["entry_id", "params", "_extra_ignored"]


# --------------------------------------------------------------------------- #
# Arm 1 (card-carried) prerequisite.
# --------------------------------------------------------------------------- #


def test_arm1_signature_and_pool():
    r = _run_arm("1")
    assert r["arm"] == "1"
    assert r["registry_size"] == _REGISTRY_SIZE
    assert r["gridmet_tier"] == "catalog"
    assert r["any_spec_in_declarable"] is False
    assert r["gridmet_in_index"] is True
    # fetch_from_catalog exposes the source branch ONLY under Arm 1.
    assert r["ffc_params"] == ["entry_id", "params", "source", "_extra_ignored"]


# --------------------------------------------------------------------------- #
# In-process: card projection + fetch-via-spec validation locus.
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def _registry_loaded():
    import trid3nt_server.main as m

    m._import_tools_registry()
    

def test_spec_card_content_fidelity(_registry_loaded):
    from trid3nt_server.tools.fetchers._router import registration as reg

    spec = reg._SPEC_REGISTRY["fetch_gridmet"]
    card = reg.spec_card(spec, relevance_score=1.23)
    assert card["name"] == "fetch_gridmet"
    # FULL docstring (not truncated at the provider ~1000-char limit).
    assert card["docstring"] == (spec.docstring or reg._synthesize_doc(spec))
    assert len(card["docstring"]) == len(spec.docstring or reg._synthesize_doc(spec))
    # Typed param schema per spec param.
    for pname, pspec in spec.params.items():
        assert pname in card["params"]
        assert card["params"][pname]["type"] == pspec.type
        assert card["params"][pname]["required"] == bool(pspec.required)
    # Honesty context + score present.
    for key in ("gates", "caveats", "endpoint_fallback"):
        assert key in card
    assert card["relevance_score"] == pytest.approx(1.23)


def test_search_spec_cards_ranks_expected_source(_registry_loaded):
    from trid3nt_server.tools.search.search_tools import search_tools as st
    from trid3nt_server.tools.fetchers._router import registration as reg

    st._reset_index_for_tests()
    st._get_index()  # warm
    cards = reg.search_spec_cards("fuel moisture fire danger weather", k=10)
    names = [c["name"] for c in cards]
    assert names, "no cards ranked (cold index?)"
    assert "fetch_gridmet" in names
    # Only spec-served sources are projected as cards.
    assert set(names) <= reg.registered_spec_names()


def test_fetch_via_spec_bad_args_raise_router_input_error(_registry_loaded):
    from trid3nt_server.tools.fetchers._router.errors import RouterInputError
    from trid3nt_server.tools.search.fetch_from_catalog.fetch_from_catalog import (
        _fetch_from_catalog_via_spec,
    )

    # Missing the required bbox (no network reached: validate_params raises first).
    with pytest.raises(RouterInputError):
        _fetch_from_catalog_via_spec("fetch_gridmet", {"variable": "not_a_real_var"})


def test_fetch_via_spec_unknown_source_raises(_registry_loaded):
    from trid3nt_server.tools.search.catalog_common import CatalogNotFoundError
    from trid3nt_server.tools.search.fetch_from_catalog.fetch_from_catalog import (
        _fetch_from_catalog_via_spec,
    )

    with pytest.raises(CatalogNotFoundError):
        _fetch_from_catalog_via_spec("fetch_not_a_source", {"bbox": [-83, 27, -82, 28]})


# --------------------------------------------------------------------------- #
# Arm 3 prerequisite: the pool exclusion plus the source-passthrough branch.
# --------------------------------------------------------------------------- #


def test_arm3_specs_leave_pool_and_source_param():
    """Arm 3 = the same pool exclusion as arms 1/2 (tier=catalog, still indexed)
    PLUS the fetch_from_catalog source-passthrough branch, which is how a
    pool-excluded source is still reached BY NAME."""
    r = _run_arm("3")
    assert r["arm"] == "3"
    assert r["registry_size"] == _REGISTRY_SIZE
    assert r["gridmet_tier"] == "catalog"
    assert r["any_spec_in_declarable"] is False  # every spec leaves the ambient pool
    assert r["declarable_size"] == _run_arm(None)["declarable_size"] - 98
    assert r["gridmet_in_index"] is True
    # fetch_from_catalog exposes the source branch under Arm 3 (like Arm 1).
    assert r["ffc_params"] == ["entry_id", "params", "source", "_extra_ignored"]


def test_default_declarable_excludes_catalog_tier(_registry_loaded):
    """The pool filter drops tier in {template, catalog} (a search hit re-adds
    the specific expanded name); a gate-expander result naming a catalog source
    resolves to a real, registered, pool-excluded tool -> declarable-on-expansion."""
    import trid3nt_server.server as srv

    fake_search_result = {
        "results": [{"tool_name": "fetch_gridmet"}, {"tool_name": "fetch_nwi_wetlands"}]
    }
    names = srv._tool_names_from_search_result(fake_search_result)
    assert names == ["fetch_gridmet", "fetch_nwi_wetlands"]
    from trid3nt_server.tools import TOOL_REGISTRY

    for n in names:
        assert n in TOOL_REGISTRY  # real + registered -> expander can declare it
