"""The sweep guard: naked substitution may not come back.

A naked substitution is a path that serves DIFFERENT data than the request named,
without declaring a rung, firing the loudness gate or stamping an activation row.
Wave F2's inventory found four shapes of it. Each shape gets a structural guard
here, or -- where the fix is a separate wave -- a REGISTERED entry whose marker
must still match, so a change to the site cannot land without changing this file.

The guards are structural on purpose: a lint that greps for the word "fallback"
finds comments, and a lint that trusts a reviewer finds nothing.
"""

from __future__ import annotations

import inspect
import pathlib
import re

import pytest

from trid3nt_server.data.fetchers._router.registration import _validate_hooks
from trid3nt_server.data.fetchers._router.spec import compose_specs_from_tree
from trid3nt_server.fallbacks import (
    BELOW_PRIMARY_CLASSES,
    DEGRADATION_CLASSES,
    get_ladder,
    registered_ladders,
)

_REPO = pathlib.Path(__file__).resolve().parents[1]
_SERVER = _REPO / "trid3nt_server"


@pytest.fixture(scope="module")
def specs():
    return compose_specs_from_tree()


# --------------------------------------------------------------------------- #
# SHAPE 1 -- spec.endpoint_fallback: a SAME-DATA mirror chain, and nothing else.
#
# It used to be spelled ``fallback`` and carried nine lists, eight of which named
# a SIBLING TOOL. ``resolve_endpoints`` indexes the spec's own ``endpoints``, so
# those eight could never execute -- but the spec card printed them to the model
# as fallbacks this source has. A promise no code path can keep is the same lie
# as a silent swap, told the other way round.
# --------------------------------------------------------------------------- #


def test_every_endpoint_fallback_entry_names_an_endpoint_of_its_own_spec(specs):
    offenders = {
        s.name: [fb for fb in s.endpoint_fallback if fb not in s.endpoints]
        for s in specs.values()
        if any(fb not in s.endpoints for fb in s.endpoint_fallback)
    }
    assert not offenders, (
        "endpoint_fallback is SAME-DATA mirrors of this source's own endpoints. "
        "These entries name something else and can never execute:\n"
        f"  {offenders}\n"
        "A CROSS-DATASET alternative belongs on a declared fallback ladder."
    )


def test_no_endpoint_fallback_entry_names_a_registered_tool(specs):
    """The exact shape that shipped: a sibling TOOL name in the mirror list."""
    tool_names = set(specs)
    offenders = {
        s.name: [fb for fb in s.endpoint_fallback if fb in tool_names]
        for s in specs.values()
        if any(fb in tool_names for fb in s.endpoint_fallback)
    }
    assert not offenders, (
        f"a sibling tool name is not an endpoint mirror: {offenders}"
    )


def test_registration_refuses_a_cross_dataset_endpoint_fallback(specs):
    """The guard has to be at LOAD time, not only in this test file -- a spec
    tree is composed in production too."""
    bad = specs["fetch_gridmet"].model_copy(
        update={"endpoint_fallback": ["fetch_era5_reanalysis"]}
    )
    with pytest.raises(ValueError, match="names no endpoint of this spec"):
        _validate_hooks(bad)


def test_the_spec_card_key_says_what_the_mechanism_is():
    """``fallback`` on a card is ambiguous between a mirror hop and a ladder
    rung; the model reads this text and must not conflate them."""
    from trid3nt_server.data.fetchers._router import registration, stratified

    card_src = inspect.getsource(registration.spec_card)
    assert '"endpoint_fallback": list(spec.endpoint_fallback)' in card_src
    assert '"fallback": list(spec.fallback)' not in card_src
    render_src = inspect.getsource(stratified)
    assert "same-data endpoint mirrors" in render_src


# --------------------------------------------------------------------------- #
# SHAPE 2 -- a capability that fetches a DIFFERENT source than its name claims.
#
# ``generate_mesh._fetch_topobathy`` fetched ``fetch_dem(source="3dep")`` -- land
# only -- wrote it as ``topobathy.tif`` and sampled it into a COASTAL mesh's bed.
# Every wet node got the land DEM's flat ~0 m ocean fill: a fake landmass, the
# same class as the SWAN bathymetry rectangle.
# --------------------------------------------------------------------------- #


def test_the_coastal_mesh_bed_comes_from_a_topobathy_source():
    import importlib

    gm = importlib.import_module(
        "trid3nt_server.workflows.mesh.generate_mesh.generate_mesh"
    )

    src = inspect.getsource(gm._fetch_coastal_bed)
    assert 'TOOL_REGISTRY["fetch_topobathy"]' in src
    assert "fetch_dem" not in src, (
        "fetch_dem is LAND-ONLY: sampling it into a coastal mesh bed paints a "
        "fake landmass under every wet node"
    )
    assert "fallback=_COASTAL_BED_FALLBACK" in src
    assert gm._COASTAL_BED_FALLBACK == ("etopo_bathy_base",)
    assert get_ladder("fetch_topobathy").alternative("etopo_bathy_base") is not None


def test_the_coastal_mesh_reports_the_bed_that_actually_painted():
    """The old provenance string was a constant claiming CoNED that was never
    fetched. What a mesh says its bed is must come from the activation rows."""
    import importlib

    gm = importlib.import_module(
        "trid3nt_server.workflows.mesh.generate_mesh.generate_mesh"
    )

    class _Row:
        def __init__(self, rung, coverage):
            self.rung, self.coverage = rung, coverage

    class _Layer:
        fallbacks = [_Row("cudem_nearshore", 0.5), _Row("regional_fine", 0.5)]
        fallback_note = None

    prov = gm._bed_provenance(_Layer())
    assert "cudem_nearshore 50%" in prov and "regional_fine 50%" in prov
    assert "CoNED" not in inspect.getsource(gm._build_coastal)


# --------------------------------------------------------------------------- #
# SHAPE 3 -- an undeclared contributor to a declared result.
#
# A source the ladder does not declare paints part of the answer, so the rows a
# reader gets do not add up to the raster they are reading.
# --------------------------------------------------------------------------- #


def test_every_ladder_is_a_complete_account_of_its_own_rungs():
    for name, ladder in registered_ladders().items():
        below = ladder.rungs[
            ladder.rungs.index(ladder.primary_rung) + 1:
        ]
        assert below, f"ladder {name} declares no rung below its primary"
        for rung in below:
            assert rung.consequence in BELOW_PRIMARY_CLASSES, (
                f"{name}/{rung.name}: {rung.consequence}"
            )
        assert ladder.terminal.consequence == "refuse"


def test_the_bathymetry_ladder_declares_every_source_that_can_paint():
    """The four sources ``_rung_coverage`` can report must each be a rung, or the
    walker logs an unknown key and a model cannot account for the raster."""
    from trid3nt_server.data.fetchers._router.hooks import topobathy as tb

    declared = {r.name for r in get_ladder("fetch_topobathy").rungs}
    measured = set(tb._rung_coverage(0.5, 0.25, 0.25) or {})
    assert measured <= declared, f"undeclared contributors: {measured - declared}"
    assert get_ladder("fetch_topobathy").alternative("regional_fine") is None, (
        "regional_fine is FINER than the primary; permitting it through "
        "fallback= would price a free upgrade as a degradation"
    )


def test_the_user_supplied_rung_is_visible_in_the_tool_schema(specs):
    """A rung the model cannot see is a rung nobody can take. ``dem_uri`` was
    absorbed by ``**_extra_ignored`` for the whole of F1."""
    ladder = get_ladder("fetch_topobathy")
    param = ladder.user_rung.supplies_param
    assert param in specs["fetch_topobathy"].params
    assert param in specs["fetch_topobathy"].docstring


# --------------------------------------------------------------------------- #
# SHAPE 4 -- every EXPOSED fetch_topobathy call site declares its rung.
#
# A gate that fires only for opt-in callers is not a floor (F1b). The mesh joined
# the four composers in F2.
# --------------------------------------------------------------------------- #

_TOPOBATHY_CALL = re.compile(r'fetch_topobathy(?:"\]\.fn)?\s*\(', re.M)

#: file -> why this call site needs no ``fallback=``. Empty means: it declares one.
_TOPOBATHY_CALLERS_WITHOUT_A_RUNG: dict[str, str] = {}


def test_every_topobathy_call_site_declares_a_rung():
    offenders: list[str] = []
    for path in _SERVER.rglob("*.py"):
        if "__pycache__" in path.parts or "hooks/topobathy.py" in path.as_posix():
            continue
        text = path.read_text(encoding="utf-8")
        if not _TOPOBATHY_CALL.search(text):
            continue
        rel = path.relative_to(_REPO).as_posix()
        if rel in _TOPOBATHY_CALLERS_WITHOUT_A_RUNG:
            continue
        if "fallback=" not in text:
            offenders.append(rel)
    assert not offenders, (
        "these fetch_topobathy callers can take a coverage gap with no declared "
        "rung, so a partial-CUDEM AOI either refuses or degrades unannounced:\n  "
        + "\n  ".join(offenders)
    )


# --------------------------------------------------------------------------- #
# THE REGISTER -- naked substitutions that are NOT fixed, with their verdicts.
#
# These are the audit's SILENT physics/data rows. They are not ladders: no
# alternative SOURCE exists to declare, only a default constant or an assumed
# value, so the fix is the loudness class (ADR 0299's parked wave), not F2's
# declared-degradation regime. They are registered here so the set cannot grow
# quietly and so fixing one forces this table to change with it.
# --------------------------------------------------------------------------- #

_PARKED_SILENT_SUBSTITUTIONS: dict[str, tuple[str, str]] = {
    "workers/_landlab_postprocess/postprocess.py": (
        "src_crs = dst_crs",
        "audit row 20: a landlab grid with no CRS tag is reprojected AS IF 4326; "
        "a projected grid lands in the wrong place at the wrong scale, silently. "
        "PARKED: the honest fix is to RAISE, which is a worker-behaviour change "
        "and needs its own image smoke.",
    ),
    "trid3nt_server/mesh/raster_cell_mesh.py": (
        "_lowest_active_cell(active, cell_elev)",
        "audit row 18: with no lowest-boundary cell the drainage outfall moves to "
        "the globally lowest cell, with no log and no note. PARKED: needs a "
        "BuildResult provenance field the SWMM composers then narrate.",
    ),
    "workers/_sfincs_build/deck.py": (
        "_MASK_FALLBACK_ZMIN",
        "audit row 12: an unreadable DEM range widens the active-cell mask so the "
        "domain includes cells a real range would exclude. The note EXISTS at "
        "workflows/sfincs/flood/flood.py and reaches only logger.warning. PARKED: "
        "the envelope wiring is a composer change, not a ladder.",
    ),
}


def test_the_parked_silent_substitutions_are_still_exactly_these():
    """Each parked site must still be findable by its marker.

    A failure here is GOOD NEWS or a REGRESSION, never noise: either the site was
    fixed (delete its row, cite the ADR) or it moved (update the marker). What it
    must never do is drift out of the register unnoticed.
    """
    missing: list[str] = []
    for rel, (marker, _verdict) in _PARKED_SILENT_SUBSTITUTIONS.items():
        path = _REPO / rel
        if not path.exists() or marker not in path.read_text(encoding="utf-8"):
            missing.append(f"{rel} :: {marker!r}")
    assert not missing, (
        "a registered naked-substitution site no longer matches its marker. If it "
        "was FIXED, delete the row and name the ADR; if it MOVED, update the "
        "marker:\n  " + "\n  ".join(missing)
    )


def test_the_register_carries_a_verdict_for_every_entry():
    for rel, (marker, verdict) in _PARKED_SILENT_SUBSTITUTIONS.items():
        assert marker and len(verdict) > 80, rel
        assert "PARKED" in verdict, rel


def test_the_degradation_classes_are_the_only_gated_ones():
    """``enhancement`` was added to the schema in F2. If it ever joins the gated
    set, a FINER source starts asking permission to be better."""
    assert DEGRADATION_CLASSES == {"same_data", "cross_dataset", "synthetic"}
    assert "enhancement" in BELOW_PRIMARY_CLASSES
    assert "enhancement" not in DEGRADATION_CLASSES
