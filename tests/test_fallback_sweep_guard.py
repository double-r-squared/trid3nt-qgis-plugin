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

from trid3nt_server.tools.fetchers._router.registration import _validate_hooks
from trid3nt_server.tools.fetchers._router.spec import compose_specs_from_tree
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
    from trid3nt_server.tools.fetchers._router import registration, stratified

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


def test_the_coastal_mesh_never_invents_a_bed_provenance():
    """With no rows and no note there is nothing to report, and the honest string
    says so. The shipped one named CUDEM + 3DEP land as though it had measured
    them -- a fabricated account is a naked substitution seen from the reader's
    side. The all-zero-coverage set rendered a dangling ``topobathy:``."""
    import importlib

    gm = importlib.import_module(
        "trid3nt_server.workflows.mesh.generate_mesh.generate_mesh"
    )

    class _Row:
        def __init__(self, rung, coverage):
            self.rung, self.coverage = rung, coverage

    class _Nothing:
        fallbacks: list = []
        fallback_note = None

    class _AllZero:
        fallbacks = [_Row("cudem_nearshore", 0.0), _Row("etopo_bathy_base", 0.0)]
        fallback_note = None

    for layer in (_Nothing(), _AllZero()):
        prov = gm._bed_provenance(layer)
        assert "UNMEASURED" in prov, prov
        assert "CUDEM" not in prov and "3DEP" not in prov, prov
        assert not prov.rstrip().endswith(":"), prov


def test_the_coastal_mesh_sizing_claim_comes_from_the_mesher(tmp_path):
    """``sizing_source`` claimed "distance-to-shore + wavelength-to-depth sizing"
    as a constant. The wavelength term is h_wl = T_M2*sqrt(g*h)/wl -- ~9.9 km even
    in 0.5 m of water -- so at any coastal max_edge_length it is clipped away and
    binds for zero nodes. The claim now copies the mesher's own report."""
    import importlib
    import json

    gm = importlib.import_module(
        "trid3nt_server.workflows.mesh.generate_mesh.generate_mesh"
    )

    coastal_src = inspect.getsource(gm._build_coastal)
    assert '"sizing_source": _sizing_source(' in coastal_src
    assert "wavelength-to-depth sizing" not in coastal_src, (
        "the composer must not assert a sizing term it cannot observe -- "
        "requesting the term (mesh_config wavelength=True) is not evidence it bound"
    )
    (tmp_path / "mesh_stats.json").write_text(json.dumps({
        "sizing_functions": [
            "feature_sizing(distance_to_shore)",
            "wavelength_sizing(shallow_water,wl=10) REQUESTED BUT NEVER BOUND "
            "(smallest h_wl 9903 m >= max_edge_length 150 m; the mesh size is "
            "distance-to-shore alone)",
        ],
    }))
    src = gm._sizing_source(tmp_path)
    assert "NEVER BOUND" in src and "distance-to-shore alone" in src
    # An unreadable report says so rather than reciting the old constant.
    assert "unreported by the mesher" in gm._sizing_source(tmp_path / "gone")


def test_the_mesh_carries_its_bed_note_into_the_artifact_provenance():
    """A degraded bed labeled only on the build turn is a degraded bed the SOLVE
    never hears about: the artifact outlives the turn."""
    import importlib

    gm = importlib.import_module(
        "trid3nt_server.workflows.mesh.generate_mesh.generate_mesh"
    )

    src = inspect.getsource(gm._stage_and_record)
    assert '"bed_fallback_note": built.get("bed_fallback_note")' in src


@pytest.mark.asyncio
async def test_the_supplied_mesh_solve_reads_the_bed_from_the_artifact(monkeypatch):
    """``tidal_hydro``'s supplied-mesh branch stamped ``basis="user"`` from a
    static template, so an ETOPO-filled bed arrived at the solve indistinguishable
    from a fully-CUDEM one. The bed IS the physics: it must arrive labeled."""
    import importlib
    import pathlib as _pl

    th = importlib.import_module(
        "trid3nt_server.workflows.schism.tidal_hydro.tidal_hydro"
    )

    async def _gate(_input_mode):
        return (
            ("pts", "tris", "depths"), "south", "accepted the case mesh",
            {"dem_source": "topobathy: cudem_nearshore 60%, etopo_bathy_base 40%",
             "bed_fallback_note": "40% of the AOI fell to the global ETOPO relief"},
        )

    monkeypatch.setattr(th, "_schism_mesh_precondition_gate", _gate)
    monkeypatch.setattr(
        th.deck_authoring, "author_coastal_tin_deck",
        lambda *_a, **_k: {"files": [], "n_nodes": 10, "n_elements": 12},
    )

    out = await th._build_coastal_tin_deck(
        _pl.Path("/tmp"), location_query=None, bbox=None, constituents=["M2"],
        tidal_amplitude_m=0.5, sim_days=5.0, open_boundary_side="south",
        input_mode=None, emitter=None,
    )
    bathy = next(e for e in out["review_entries"] if e.param == "bathymetry")
    assert bathy.basis == "fetched"
    assert bathy.consequence == "physics"
    assert "etopo_bathy_base 40%" in str(bathy.value)
    assert "DEGRADED BED" in (bathy.note or "")
    assert "MESH BED" in out["fallback_note"]


# --------------------------------------------------------------------------- #
# SHAPE 2b -- a TRANSPORT fault must not buy a different source.
#
# Both coastal composers refuse honestly when the bathymetry LADDER refuses, then
# fall through to the LAND-ONLY fetch_dem on a bare ``except Exception``. A
# ``TopobathyUpstreamError`` (an S3 5xx, a wedged tile read) is not a Ladder*
# subclass, so a transient fault swapped a tsunami/tidal bed for a land DEM --
# the exact substitution the honest refusal above it was written to prevent.
# --------------------------------------------------------------------------- #


def test_geoclaw_propagates_a_transport_fault_instead_of_the_land_dem():
    import importlib
    from dataclasses import replace

    from trid3nt_server.tools import TOOL_REGISTRY
    from trid3nt_server.tools.fetchers._router.hooks.topobathy import (
        TopobathyUpstreamError,
    )

    inund = importlib.import_module(
        "trid3nt_server.workflows.geoclaw.inundation.inundation"
    )

    def _boom(**_kw):
        raise TopobathyUpstreamError("CUDEM tile read wedged (503)")

    def _land(**_kw):  # pragma: no cover - reaching this IS the failure
        raise AssertionError("fetch_dem was called on a transport fault")

    topo, dem = TOOL_REGISTRY["fetch_topobathy"], TOOL_REGISTRY["fetch_dem"]
    TOOL_REGISTRY["fetch_topobathy"] = replace(topo, fn=_boom)
    TOOL_REGISTRY["fetch_dem"] = replace(dem, fn=_land)
    try:
        with pytest.raises(inund.GeoClawComposerError) as ei:
            inund._fetch_topo_for_geoclaw((-85.45, 29.90, -85.35, 30.00))
    finally:
        TOOL_REGISTRY["fetch_topobathy"] = topo
        TOOL_REGISTRY["fetch_dem"] = dem
    assert ei.value.error_code == "TOPOBATHY_UPSTREAM_ERROR"
    assert "RETRY" in str(ei.value)


@pytest.mark.asyncio
async def test_schism_propagates_a_transport_fault_instead_of_the_land_dem():
    import importlib
    from dataclasses import replace

    from trid3nt_server.tools import TOOL_REGISTRY
    from trid3nt_server.tools.fetchers._router.hooks.topobathy import (
        TopobathyUpstreamError,
    )

    th = importlib.import_module(
        "trid3nt_server.workflows.schism.tidal_hydro.tidal_hydro"
    )

    def _boom(**_kw):
        raise TopobathyUpstreamError("CUDEM tile read wedged (503)")

    def _land(**_kw):  # pragma: no cover - reaching this IS the failure
        raise AssertionError("fetch_dem was called on a transport fault")

    topo, dem = TOOL_REGISTRY["fetch_topobathy"], TOOL_REGISTRY["fetch_dem"]
    TOOL_REGISTRY["fetch_topobathy"] = replace(topo, fn=_boom)
    TOOL_REGISTRY["fetch_dem"] = replace(dem, fn=_land)
    try:
        with pytest.raises(th.SchismScenarioError) as ei:
            await th._fetch_bathymetry_cog((-85.45, 29.90, -85.35, 30.00))
    finally:
        TOOL_REGISTRY["fetch_topobathy"] = topo
        TOOL_REGISTRY["fetch_dem"] = dem
    assert ei.value.error_code == "TOPOBATHY_UPSTREAM_ERROR"
    assert "RETRY" in str(ei.value)


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
    from trid3nt_server.tools.fetchers._router.hooks import topobathy as tb

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

#
# Keyed by AUDIT ROW, not by file: several rows live in one file and one row lives
# in two files, so a file key silently loses entries.
#
# A marker is the tightest STABLE anchor at the site -- a constant name, a counter
# increment, a log format string. It is deliberately not the whole line: an
# exact-whitespace marker false-alarms on a reformat. It is still only an anchor,
# not a semantic check: a site could in principle keep its constant and lose its
# defect, so a failure here means LOOK, not "broken".
_PARKED_SILENT_SUBSTITUTIONS: dict[str, tuple[str, str, str]] = {
    "row 11a -- COG CRS guess (agent copy)": (
        "trid3nt_server/workflows/shared/cog_io.py",
        'ds.attrs.get("crs", "EPSG:3857")',
        "audit row 11: a COG whose dataset carries no CRS is TAGGED EPSG:3857 and "
        "written, so pixel coordinates that were never Web Mercator get a Web "
        "Mercator georeference. Logged only. PARKED (ADR 0299 fork 4): raise vs "
        "keep guessing is NATE's call.",
    ),
    "row 11b -- COG CRS guess (worker copy)": (
        "workers/_raster_postprocess/cog.py",
        'ds.attrs.get("crs", "EPSG:3857")',
        "audit row 11, the unaudited second site: the same guess inside the worker "
        "postprocess package. PARKED (ADR 0299 fork 4) and, being worker code, its "
        "fix needs an image rebuild + smoke of its own.",
    ),
    "row 12a -- SFINCS wide active mask (worker copy)": (
        "workers/_sfincs_build/deck.py",
        "_MASK_FALLBACK_ZMIN",
        "audit row 12: an unreadable DEM range widens the active-cell mask so the "
        "domain includes cells a real range would exclude. The note EXISTS at "
        "workflows/sfincs/flood/flood.py and reaches only logger.warning. PARKED: "
        "the envelope wiring is a composer change, not a ladder.",
    ),
    "row 12b -- SFINCS wide active mask (agent copy)": (
        "trid3nt_server/workflows/sfincs/sfincs_builder.py",
        "_MASK_FALLBACK_ZMIN",
        "audit row 12, the independent SECOND copy of the mask computation on the "
        "agent side. Same silence, same physics consequence, and a fix to one copy "
        "leaves the other lying. PARKED with its twin.",
    ),
    "row 14 -- quadtree center-band refinement": (
        "workers/_sfincs_build/deck_quadtree.py",
        'refine_source = "center_band_fallback"',
        "audit row 14: with no z=0 land-sea interface resolved in the AOI, "
        "refinement follows a fixed cross-shore center band instead of the "
        "coastline, so resolution lands where the waves may not be. "
        "``refine_source`` is stamped in the deck and NOTHING downstream reads it "
        "back. PARKED (physics-loudness class).",
    ),
    "row 16 -- SWMM synthetic pipe diameters": (
        "trid3nt_server/mesh/swmm_network.py",
        "n_diam_default += 1",
        "audit row 16: a conduit with no size attribute takes "
        "DEFAULT_PIPE_DIAMETER_M, which sets its capacity. The count rides a "
        "free-text label_suffix on the layer name only -- no SyntheticInput, no "
        "swmm_contracts field. PARKED (the sub-area sibling IS labeled; these are "
        "not).",
    ),
    "row 17 -- SWMM roughness / imperviousness demo literals": (
        "trid3nt_server/mesh/raster_cell_mesh.py",
        '_phys.get("n_imperv", 0.012)',
        "audit row 17: subcatchment Manning n and imperviousness fall to historical "
        "literals that drive routing. The only log (run_swmm.py:204) fires when the "
        "user OVERRIDES them, never on the default path. PARKED (graded SILENT, not "
        "logged-only).",
    ),
    "row 18 -- SWMM outfall relocation": (
        "trid3nt_server/mesh/raster_cell_mesh.py",
        "_lowest_active_cell(",
        "audit row 18: with no lowest-boundary cell the drainage outfall moves to "
        "the globally lowest cell, with no log and no note. PARKED: needs a "
        "BuildResult provenance field the SWMM composers then narrate.",
    ),
    "row 19 -- MODFLOW SFR streambed gradient": (
        "workers/modflow/gwt_adapter.py",
        "DEFAULT_SFR_STREAMBED_GRADIENT",
        "audit row 19: the DEM-sampled rbot primary (river_dem_uri) IS on "
        "MODFLOWRunArgs but run_modflow never threads it, so the demo gradient "
        "drives SFR Manning flow on every stream_depletion run, unlabeled. PARKED "
        "(ADR 0299 fork 3, a law-9 refuse-vs-label question).",
    ),
    "row 20 -- landlab assumed 4326": (
        "workers/_landlab_postprocess/postprocess.py",
        "src_crs = dst_crs",
        "audit row 20: a landlab grid with no CRS tag is reprojected AS IF 4326; "
        "a projected grid lands in the wrong place at the wrong scale, silently. "
        "PARKED: the honest fix is to RAISE, which is a worker-behaviour change "
        "and needs its own image smoke.",
    ),
    "row 25 -- river-dye water-polygon domain": (
        "workers/telemac/telemac_river_dye_build.py",
        "water-polygon domain failed (%s) - ribbon fallback",
        "audit row 25: when the TRUE water-polygon domain build fails the mesh "
        "reverts to the geometric ribbon -- a different modeled domain. LOG.warning "
        "only; domain_mode is stamped internally and never reaches the model. "
        "PARKED (physics-loudness class).",
    ),
}


def test_the_parked_silent_substitutions_are_still_exactly_these():
    """Each parked site must still be findable by its marker.

    A failure here is GOOD NEWS or a REGRESSION, never noise: either the site was
    fixed (delete its row, cite the ADR) or it moved (update the marker). What it
    must never do is drift out of the register unnoticed.
    """
    missing: list[str] = []
    for row, (rel, marker, _verdict) in _PARKED_SILENT_SUBSTITUTIONS.items():
        path = _REPO / rel
        if not path.exists() or marker not in path.read_text(encoding="utf-8"):
            missing.append(f"{row} :: {rel} :: {marker!r}")
    assert not missing, (
        "a registered naked-substitution site no longer matches its marker. If it "
        "was FIXED, delete the row and name the ADR; if it MOVED, update the "
        "marker:\n  " + "\n  ".join(missing)
    )


def test_the_register_carries_a_verdict_for_every_entry():
    for row, (rel, marker, verdict) in _PARKED_SILENT_SUBSTITUTIONS.items():
        assert rel and marker and len(verdict) > 80, row
        assert "PARKED" in verdict, row
        # A marker that carries its own indentation or line break pins the file's
        # FORMATTING as well as its behaviour, and reformatting is not a finding.
        assert marker == marker.strip() and "\n" not in marker, row


def test_the_register_covers_every_parked_row_the_adr_names():
    """ADR 0299 parks rows 11, 12, 14, 16, 17, 18, 19, 20 and 25. The register is
    the mechanism that keeps that set from shrinking quietly, so it must actually
    hold them -- the F2 gap the F2b review found was a register of three."""
    parked_rows = {"11", "12", "14", "16", "17", "18", "19", "20", "25"}
    registered = {
        key.split()[1].rstrip("ab") for key in _PARKED_SILENT_SUBSTITUTIONS
    }
    assert registered == parked_rows, f"missing: {parked_rows - registered}"


def test_the_degradation_classes_are_the_only_gated_ones():
    """``enhancement`` was added to the schema in F2. If it ever joins the gated
    set, a FINER source starts asking permission to be better."""
    assert DEGRADATION_CLASSES == {"same_data", "cross_dataset", "synthetic"}
    assert "enhancement" in BELOW_PRIMARY_CLASSES
    assert "enhancement" not in DEGRADATION_CLASSES
