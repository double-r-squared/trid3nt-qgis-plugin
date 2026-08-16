"""ADR 0225 -- declared resolutions + the self-enforcing sweep.

NATE's clamp ruling: silent coercion to an undeclared resolution is BANNED. A tool
with a granularity-bearing param DECLARES the resolutions it can run
(:class:`ResolutionSpec`); an out-of-range ask is QUOTED BACK (typed/gated), never
silently snapped. This suite verifies:

1. The schema (:class:`ResolutionSpec`) validators + helpers.
2. The shared out-of-range behaviour (:func:`enforce_resolution`, the quote-back card).
3. Each adopted surface: in-range unchanged, out-of-range -> typed quote-back.
4. THE SELF-ENFORCING SWEEP: every registered tool with a numeric resolution-class
   param carries a declaration -- or is on the explicit PENDING allowlist. A FUTURE
   tool that ships a resolution param with neither FAILS this test, making the ruling
   self-enforcing (item 5 of the kickoff).
"""
from __future__ import annotations

import asyncio
import inspect
import pathlib
import re

import pytest

from trid3nt_contracts.tool_registry import AtomicToolMetadata, ResolutionSpec
from trid3nt_server.agent.tools import TOOL_REGISTRY
from trid3nt_server.agent.tools.resolution_declared import (
    ResolutionOutOfRangeError,
    enforce_resolution,
    resolution_review_note,
    resolve_resolution,
)


# --------------------------------------------------------------------------- #
# 1. Schema: ResolutionSpec validators + helpers.
# --------------------------------------------------------------------------- #

def _spec(**kw):
    base = dict(param="resolution_m", constraint_source="solver", rationale="test range")
    base.update(kw)
    return ResolutionSpec(**base)


def test_continuous_window_contains_and_phrase():
    s = _spec(min_value=20, max_value=200)
    assert s.contains(20) and s.contains(200) and s.contains(60)
    assert not s.contains(19.9) and not s.contains(200.1)
    assert s.range_phrase() == "20-200 m"


def test_open_bounds_phrase_and_contains():
    lo = _spec(min_value=10)
    assert lo.range_phrase() == ">=10 m"
    assert lo.contains(10) and lo.contains(1e6) and not lo.contains(9)
    hi = _spec(max_value=200)
    assert hi.range_phrase() == "<=200 m"
    assert hi.contains(1) and not hi.contains(201)


def test_discrete_options_contains():
    s = _spec(min_value=None, options=(1.0, 3.0, 10.0), unit="arcsec")
    assert s.contains(3.0) and not s.contains(2.0)
    assert "1, 3, 10 arcsec" in s.range_phrase()


def test_unbounded_requires_rationale_word():
    # no min/max/options + 'unbounded' in rationale is a legitimate declaration.
    u = _spec(rationale="unbounded: source native varies, no fixed cell")
    assert u.range_phrase() == "unbounded (m)"
    assert u.contains(1) and u.contains(1e9)
    # ... but an empty spec whose rationale does NOT say 'unbounded' is a defect.
    with pytest.raises(Exception):
        _spec(rationale="forgot to set bounds")


def test_window_and_options_mutually_exclusive():
    with pytest.raises(Exception):
        _spec(min_value=1, options=(1.0,))


def test_min_gt_max_rejected():
    with pytest.raises(Exception):
        _spec(min_value=200, max_value=20)


def test_docstring_line_names_owner_and_native():
    s = _spec(min_value=20, max_value=200, native_hint="3DEP 10 m")
    line = s.docstring_line()
    assert "20-200 m" in line and "mesh/solver" in line and "3DEP 10 m" in line
    assert "never silently snapped" in line
    # data-native owner label
    d = _spec(min_value=1, constraint_source="data", native_hint="CUDEM 3 m")
    assert "data-native" in d.docstring_line()


def test_quote_back_composes_two_layers_and_measured():
    s = _spec(min_value=20, max_value=200, native_hint="3DEP 10 m")
    card = s.quote_back(5, measured="~180 MB measured")
    assert "5 m requested" in card
    assert "20-200 m" in card
    assert "3DEP 10 m" in card
    assert "~180 MB measured" in card
    assert "pick a resolution_m in range" in card


def test_metadata_resolution_spec_for():
    s = _spec(min_value=20, max_value=200)
    m = AtomicToolMetadata(name="x", ttl_class="live-no-cache", cacheable=False,
                           resolution_specs=(s,))
    assert m.resolution_spec_for("resolution_m") is s
    assert m.resolution_spec_for("nope") is None


# --------------------------------------------------------------------------- #
# 2. Shared out-of-range behaviour.
# --------------------------------------------------------------------------- #

def test_enforce_none_is_always_in_range():
    s = _spec(min_value=20, max_value=200)
    enforce_resolution(s, None)  # native default -- no raise


def test_enforce_in_range_no_raise():
    s = _spec(min_value=20, max_value=200)
    enforce_resolution(s, 20)
    enforce_resolution(s, 60)
    enforce_resolution(s, 200)


def test_enforce_out_of_range_raises_typed_quote_back():
    s = _spec(min_value=20, max_value=200, native_hint="3DEP 10 m")
    with pytest.raises(ResolutionOutOfRangeError) as ei:
        enforce_resolution(s, 5, measured="~180 MB")
    exc = ei.value
    assert exc.error_code == "INVALID_ARG"
    assert exc.tool_input_error.code == "INVALID_ARG"
    assert exc.tool_input_error.retryable is False
    assert "20-200 m" in str(exc) and "5 m requested" in str(exc) and "~180 MB" in str(exc)


def test_review_note_labels_within_range_derivation_only():
    s = _spec(min_value=20, max_value=200)
    assert resolution_review_note(s, 60, 60) is None            # unchanged -> no note
    n = resolution_review_note(s, 60, 90)                       # autoscaled within range
    assert n is not None and "60" in n and "90" in n and "20-200 m" in n
    nn = resolution_review_note(s, None, 199)                   # native resolved
    assert nn is not None and "native" in nn


# --------------------------------------------------------------------------- #
# 3. Adopted surfaces: in-range unchanged, out-of-range quote-back.
# --------------------------------------------------------------------------- #

def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_hecras_out_of_range_resolution_quotes_back():
    from trid3nt_server.agent.workflows.hecras.flood_2d.flood_2d import hecras_flood_2d
    r = _run(hecras_flood_2d(bbox=[-86.2, 40.15, -86.1, 40.25], resolution_m=5))
    assert r["status"] == "error"
    assert r["error_code"] == "HECRAS_INPUT_INVALID"
    msg = r["error_message"]
    assert "5 m requested" in msg and "20-200 m" in msg
    assert "pick a resolution_m in range" in msg


def test_hecras_resolution_resolve_in_range_unchanged():
    # ADR 0232: flood_2d resolves through the shared resolve_resolution seam. A small
    # AOI + in-range 60 m -> basis user, no autoscale note.
    from trid3nt_server.agent.workflows.hecras.flood_2d import flood_2d
    bbox = [-86.2, 40.20, -86.18, 40.22]
    r = resolve_resolution(
        60.0, spec=flood_2d._RES_SPEC,
        autoscale=lambda x: flood_2d._autoscale_resolution(bbox, x),
    )
    assert r.value == 60.0 and r.basis == "user" and r.note is None


def test_hecras_resolution_resolve_out_of_range_raises():
    from trid3nt_server.agent.workflows.hecras.flood_2d import flood_2d
    bbox = [-86.2, 40.15, -86.1, 40.25]
    for bad in (5.0, 500.0):
        with pytest.raises(ResolutionOutOfRangeError):
            resolve_resolution(
                bad, spec=flood_2d._RES_SPEC,
                autoscale=lambda x: flood_2d._autoscale_resolution(bbox, x),
            )


def test_surge_out_of_range_resolution_quotes_back():
    from trid3nt_server.agent.workflows.schism.pahm_surge.pahm_surge import schism_pahm_surge
    r = _run(schism_pahm_surge(bbox=[-95.05, 29.2, -94.6, 29.65], resolution_m=5, sim_days=1.0))
    assert isinstance(r, dict) and r.get("status") == "error"
    assert r["error_code"] == "SCHISM_INPUT_INVALID"
    assert "25-1000 m" in r["error_message"] and "5 m requested" in r["error_message"]


def test_surge_spec_bounds():
    from trid3nt_server.agent.workflows.schism.pahm_surge.pahm_surge import _SURGE_RES_SPEC
    assert _SURGE_RES_SPEC.contains(25) and _SURGE_RES_SPEC.contains(1000)
    assert not _SURGE_RES_SPEC.contains(24) and not _SURGE_RES_SPEC.contains(1001)


def test_sfincs_quadtree_floor_enforced():
    from trid3nt_server.agent.workflows.sfincs.flood.flood import _SFINCS_QUADTREE_RES_SPEC
    enforce_resolution(_SFINCS_QUADTREE_RES_SPEC, 400)  # in range
    with pytest.raises(ResolutionOutOfRangeError):
        enforce_resolution(_SFINCS_QUADTREE_RES_SPEC, 5)  # sub-floor


def test_fetcher_data_native_declarations_present():
    for name in ("fetch_dem", "fetch_topobathy"):
        spec = TOOL_REGISTRY[name].metadata.resolution_spec_for("resolution_m")
        assert spec is not None and spec.constraint_source == "data"
        assert spec.native_hint  # the gate card quotes it


def test_schism_render_cap_is_overridable_output_artifact():
    from trid3nt_server.agent.workflows.schism.postprocess_schism import (
        OUTPUT_RASTER_CAP_SPEC,
        _adaptive_grid,
    )
    assert OUTPUT_RASTER_CAP_SPEC.unit == "px"
    bbox = [-95.05, 29.2, -94.0, 30.0]
    w_default, _ = _adaptive_grid(bbox, True)
    w_override, _ = _adaptive_grid(bbox, True, max_px_per_side=6000)
    # the override raises the cap -> a finer (>= default) display raster.
    assert w_override >= w_default


# --------------------------------------------------------------------------- #
# 4. THE SELF-ENFORCING SWEEP.
# --------------------------------------------------------------------------- #

#: Name tokens that mark a granularity-bearing parameter.
_RES_TOKENS = ("resolution", "edge_length", "cell_size", "grid_size", "mesh_resolution")

#: (tool, param) pairs with a numeric resolution-class param NOT yet carrying a
#: ResolutionSpec. This list is the ruling's escape hatch: it must SHRINK, never grow.
#: A NEW tool/param that is neither declared nor listed here FAILS the sweep, so the
#: declaration obligation is self-enforcing. Each entry is a queued follow-up.
_PENDING_DECLARATION: set[tuple[str, str]] = {
    ("compute_building_density", "cell_size_m"),
    ("compute_home_range_kde", "grid_size"),
    ("fetch_landcover", "resolution_m"),
    ("fetch_population", "target_resolution_m"),
    ("pelicun_damage_assessment", "cell_size_m"),
    ("swmm_dual_drainage_coupling", "target_resolution_m"),
    ("swmm_urban_flood", "target_resolution_m"),
}


def _is_numeric_annotation(annotation) -> bool:
    """True for a float/int (or Optional thereof) annotation, string or type form.

    ``from __future__ import annotations`` makes annotations STRINGS, so match both.
    A ``str``-typed mode selector (telemac ``mesh_resolution``) is EXCLUDED -- it is a
    preset, not a numeric resolution value.
    """
    s = str(annotation)
    return ("float" in s or "int" in s) and "str" not in s


def _numeric_resolution_params() -> list[tuple[str, str]]:
    hits: list[tuple[str, str]] = []
    for name, entry in TOOL_REGISTRY.items():
        try:
            sig = inspect.signature(entry.fn)
        except (TypeError, ValueError):
            continue
        for pname, param in sig.parameters.items():
            if any(tok in pname for tok in _RES_TOKENS) and _is_numeric_annotation(param.annotation):
                hits.append((name, pname))
    return hits


def test_every_resolution_param_is_declared_or_pending():
    """SELF-ENFORCING: a resolution-class param must carry a ResolutionSpec (or be an
    explicitly-listed pending follow-up). A future tool that forgets FAILS here."""
    undeclared: list[tuple[str, str]] = []
    for tool, param in _numeric_resolution_params():
        meta = TOOL_REGISTRY[tool].metadata
        if meta.resolution_spec_for(param) is not None:
            continue
        if (tool, param) in _PENDING_DECLARATION:
            continue
        undeclared.append((tool, param))
    assert not undeclared, (
        "resolution-class params with NEITHER a ResolutionSpec NOR a _PENDING_DECLARATION "
        f"entry (ADR 0225 -- declare the range or add to the shrinking pending list): {undeclared}"
    )


def test_pending_list_has_no_stale_entries():
    """The pending allowlist may only shrink: an entry that is ALREADY declared, or
    whose param no longer exists, is stale and must be removed."""
    live = set(_numeric_resolution_params())
    stale = []
    for tool, param in _PENDING_DECLARATION:
        meta = TOOL_REGISTRY.get(tool)
        if meta is None or (tool, param) not in live:
            stale.append((tool, param, "param gone"))
        elif meta.metadata.resolution_spec_for(param) is not None:
            stale.append((tool, param, "now declared -- remove from pending"))
    assert not stale, f"stale _PENDING_DECLARATION entries: {stale}"


def test_declared_specs_are_wellformed():
    """Every declared spec across the registry validates + names a real param."""
    for name, entry in TOOL_REGISTRY.items():
        sig_params = set()
        try:
            sig_params = set(inspect.signature(entry.fn).parameters)
        except (TypeError, ValueError):
            pass
        for spec in entry.metadata.resolution_specs:
            assert spec.rationale, f"{name}:{spec.param} missing rationale"
            assert spec.constraint_source in ("solver", "data")
            # the declared param must be a real argument of the tool.
            if sig_params:
                assert spec.param in sig_params, (
                    f"{name} declares a ResolutionSpec for {spec.param!r} which is not a param"
                )


# --------------------------------------------------------------------------- #
# 5. THE RESOLVE-SEAM SWEEP (ADR 0232).
# --------------------------------------------------------------------------- #

#: A private per-template resolution-resolve helper (the ``_resolution_with_basis``
#: class flood_2d shipped) is BANNED: the one seam is
#: :func:`resolve_resolution`. This name-pattern catches any workflow file that
#: re-grows its own, so the consolidation cannot silently un-happen.
_BANNED_RESOLVE_HELPER = re.compile(r"^\s*def\s+\w*resolution_with_basis\w*\s*\(", re.M)


def test_no_workflow_defines_its_own_resolution_resolve_helper():
    """SELF-ENFORCING (ADR 0232): no workflow file may define a private
    ``*resolution_with_basis*`` function -- the resolve+basis+note pattern lives ONCE in
    resolve_resolution. A template that re-hand-rolls it FAILS here."""
    workflows_dir = (
        pathlib.Path(__file__).resolve().parents[1]
        / "trid3nt_server" / "agent" / "workflows"
    )
    offenders = [
        str(py.relative_to(workflows_dir))
        for py in workflows_dir.rglob("*.py")
        if _BANNED_RESOLVE_HELPER.search(py.read_text(encoding="utf-8"))
    ]
    assert not offenders, (
        "workflow files define their own resolution-resolve helper instead of calling "
        f"resolve_resolution (ADR 0232 -- consolidate the resolve seam): {offenders}"
    )
