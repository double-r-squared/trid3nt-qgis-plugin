"""Offline tests for the SCHISM PaHM storm-surge landing (ADR 0217).

No docker / no network: exercises the pure Holland-1980 sflux physics + the surge
deck authoring (file inventory + the load-bearing param.nml / bctides toggles).
The live solve-through-the-image proof is the direct-harness drive
(scripts/run_schism_surge_direct.py), not a unit test.
"""

from __future__ import annotations

import asyncio
import math
from pathlib import Path

import numpy as np
import pytest
from scipy.spatial import Delaunay

from trid3nt_server.agent.workflows.schism import deck_authoring as D
from trid3nt_server.agent.workflows.schism import holland_sflux as H
from trid3nt_server.agent.workflows.schism.pahm_surge.pahm_surge import (
    _emit_track_overlay,
)


def _ike_track() -> list[H.TrackFix]:
    return [
        H.TrackFix(0.0, -91.5, 26.6, 94800.0, 48.9, 92600.0, 100800.0),
        H.TrackFix(12.0, -93.6, 28.1, 95000.0, 41.2, 101_860.0, 100800.0),
        H.TrackFix(24.0, -94.7, 29.3, 95200.0, 41.2, 92600.0, 100800.0),
        H.TrackFix(30.0, -95.4, 30.5, 96400.0, 30.9, 111_120.0, 100800.0),
    ]


def _demo_mesh():
    gx, gy = np.meshgrid(np.linspace(-95.4, -94.4, 12), np.linspace(28.8, 29.6, 11))
    pts = np.column_stack([gx.ravel(), gy.ravel()]).astype(float)
    cells = Delaunay(pts).simplices.astype(np.int64)
    depths = (2.0 + (29.6 - pts[:, 1]) * 30.0).astype(float)
    return pts, cells, depths


def test_holland_B_physical_range():
    # A deep pressure deficit with a strong Vmax lands inside the [1.0, 2.5] clamp.
    b = H.holland_B(vmax_ms=50.0, dp_pa=6000.0)
    assert 1.0 <= b <= 2.5
    # Degenerate inputs return the safe default, never a divide-by-zero.
    assert H.holland_B(0.0, 0.0) == pytest.approx(1.3)


def test_holland_profile_peaks_near_rmw_and_calm_eye():
    pc, pn, rmw, lat = 95000.0, 101300.0, 46000.0, 29.3
    b = H.holland_B(49.4, pn - pc)
    v_eye, _ = H.holland_profile(500.0, pc, pn, rmw, b, lat)
    v_rmw, p_rmw = H.holland_profile(rmw, pc, pn, rmw, b, lat)
    v_far, p_far = H.holland_profile(300_000.0, pc, pn, rmw, b, lat)
    assert v_eye < 5.0                 # calm eye
    assert v_rmw > v_far > 0.0         # wind peaks near Rmw, decays outward
    assert p_rmw < p_far               # pressure lowest near the center
    assert pc <= p_rmw <= pn


def test_write_sflux_air_structure_and_extends_past_run(tmp_path):
    import netCDF4

    field = H.write_sflux_air(
        tmp_path / "sflux", _ike_track(), (-95.6, 28.6, -94.0, 30.0),
        base_date=(2008, 9, 12, 6), sim_days=1.0, cadence_hr=1.0, tail_hours=6.0,
    )
    nc = tmp_path / "sflux" / "sflux_air_1.0001.nc"
    assert nc.exists()
    assert (tmp_path / "sflux" / "sflux_inputs.txt").exists()
    assert field.peak_wind_ms > 20.0            # a real hurricane wind field
    assert field.min_pressure_pa < 100_000.0    # sub-ambient core
    with netCDF4.Dataset(nc) as ds:
        for v in ("lon", "lat", "time", "uwind", "vwind", "prmsl", "stmp", "spfh"):
            assert v in ds.variables, v
        assert list(ds.variables["time"].base_date) == [2008, 9, 12, 6]
        t = np.asarray(ds.variables["time"][:])
        # sflux MUST extend past the run end (else SCHISM aborts the last step).
        assert t[-1] * 24.0 >= 1.0 * 24.0 + 5.9
        u = np.asarray(ds.variables["uwind"][:])
        assert u.shape[1:] == (field.n_lat, field.n_lon)


def test_author_pahm_surge_deck_inventory_and_toggles(tmp_path):
    pts, cells, depths = _demo_mesh()
    res = D.author_pahm_surge_deck(
        tmp_path / "case", track=_ike_track(), mesh_bbox=(-95.4, 28.8, -94.4, 29.6),
        base_date=(2008, 9, 12, 6), points=pts, cells=cells, depths=depths,
        sim_days=1.0, open_boundary_side="south", dt_s=120.0,
    )
    case = tmp_path / "case"
    for name in ("hgrid.gr3", "hgrid.ll", "vgrid.in", "param.nml", "bctides.in",
                 "drag.gr3", "windrot_geo2proj.gr3", "station.in",
                 "sflux/sflux_air_1.0001.nc", "sflux/sflux_inputs.txt"):
        assert (case / name).exists(), name
    assert res["open_node_count"] > 0

    param = (case / "param.nml").read_text()
    # The load-bearing surge toggles (any regression here silently mis-forces).
    assert "nws = 2" in param
    assert "iwind_form = -1" in param
    # No ACTIVE nrampwind line (invalid &OPT member -> aborts init); the QA
    # template's commented `!  nrampwind` is inert and may remain.
    active = [ln for ln in param.splitlines()
              if "nrampwind" in ln and not ln.lstrip().startswith("!")]
    assert not active, active
    assert "start_year = 2008" in param

    # hgrid.gr3 is PROJECTED to metres; hgrid.ll reconstructs lon/lat node-for-node.
    g = (case / "hgrid.gr3").read_text().splitlines()
    ll = (case / "hgrid.ll").read_text().splitlines()
    assert g[1].split()[:2] == ll[1].split()[:2]        # same nelem/nnode
    gx = float(g[2].split()[1])
    assert abs(gx) > 100.0                                # metres, not degrees
    llx = float(ll[2].split()[1])
    assert -180.0 <= llx <= 180.0                         # lon/lat degrees

    # Still-water boundary (iettype=2), no tidal constituents -> surge is pure forcing.
    bctides = (case / "bctides.in").read_text()
    assert "0 nbfr" in bctides
    assert " 2 0 0 0" in bctides


class _FakeEmitter:
    """Minimal ``PipelineEmitter`` stand-in: captures ``add_loaded_layer`` calls."""

    def __init__(self) -> None:
        self.loaded: list[object] = []

    async def add_loaded_layer(self, layer) -> None:  # noqa: ANN001
        self.loaded.append(layer)


def test_emit_track_overlay_layer_uri_validates(monkeypatch):
    """Regression for the daemon-observed skip: ``LayerURI`` for the best-track
    overlay was missing the required ``style_preset`` field, so construction
    raised a pydantic ``ValidationError`` that the caller's best-effort
    try/except silently swallowed (log-only skip, no layer ever reached the
    Case). This exercises the real ``_emit_track_overlay`` code path end to
    end (S3 upload stubbed) and asserts the overlay actually reaches the
    emitter, fully validated, instead of raising loudly here if the
    construction ever breaks again."""
    from trid3nt_server.agent.tools.simulation.solver import solver as _solver

    monkeypatch.setattr(_solver, "_get_runs_bucket", lambda: "test-runs-bucket")

    class _FakeS3:
        def put_object(self, **kwargs):  # noqa: ANN003
            return {}

    monkeypatch.setattr(_solver, "_get_s3_client", lambda: _FakeS3())

    emitter = _FakeEmitter()
    asyncio.run(_emit_track_overlay(emitter, _ike_track(), "Ike"))

    assert len(emitter.loaded) == 1
    layer = emitter.loaded[0]
    assert layer.layer_type == "vector"
    assert layer.style_preset == "storm_track"
    assert layer.role == "context"
    assert layer.crs_authid == "EPSG:4326"
    assert layer.uri.startswith("s3://test-runs-bucket/")


# --------------------------------------------------------------------------- #
# Bathymetry-failure policy (NATE ruling, 2026-08-11): fabricated bathymetry is
# never a silent fallback. Default -> typed SCHISM_BATHYMETRY_UNAVAILABLE stop;
# allow_synthetic_domain=True -> the declared idealized-shelf mechanism-demo mode.
# --------------------------------------------------------------------------- #
class _GateSentinel(Exception):
    """Raised by the stubbed input-review gate to abort before dispatch/solve."""


def _entries_by_param(entries):
    return {e.param: e for e in entries}


def test_bathy_fetch_failure_raises_typed_error_by_default(monkeypatch):
    """A real-bathymetry fetch failure with allow_synthetic_domain unset (default
    False) stops the run honestly instead of substituting a synthetic shelf."""
    import trid3nt_server.agent.workflows.schism.pahm_surge.pahm_surge as P
    from trid3nt_contracts.schism_contracts import SCHISM_BATHYMETRY_UNAVAILABLE

    async def _boom(*a, **kw):
        raise RuntimeError("fetch_topobathy/fetch_dem both unreachable")

    monkeypatch.setattr(P, "_fetch_bathymetry_cog", _boom)
    monkeypatch.delenv("TRID3NT_SCHISM_BATHY_PATH", raising=False)

    with pytest.raises(P.SchismSurgeError) as exc_info:
        asyncio.run(P.model_schism_pahm_surge(
            storm_name=None, year=None, location_query=None, bbox=None,
            sim_days=1.5, open_boundary_side="south", input_mode=None,
            allow_synthetic_domain=False,
        ))
    assert exc_info.value.error_code == SCHISM_BATHYMETRY_UNAVAILABLE
    assert "allow_synthetic_domain" in str(exc_info.value)


def test_bathy_fetch_failure_with_allow_synthetic_domain_runs_declared_synthetic(monkeypatch):
    """allow_synthetic_domain=True opts into the idealized-shelf mechanism-demo
    mode on a fetch failure -- reaches the input-review gate with a LOUD
    synthetic domain_provenance entry rather than raising."""
    import trid3nt_server.agent.workflows.schism.pahm_surge.pahm_surge as P

    async def _boom(*a, **kw):
        raise RuntimeError("fetch_topobathy/fetch_dem both unreachable")

    monkeypatch.setattr(P, "_fetch_bathymetry_cog", _boom)
    monkeypatch.delenv("TRID3NT_SCHISM_BATHY_PATH", raising=False)

    captured: dict = {}

    async def _fake_gate(*, tool_name, mode, entries, params):
        captured["entries"] = entries
        raise _GateSentinel()

    monkeypatch.setattr(P, "gate_input_review", _fake_gate)

    with pytest.raises(_GateSentinel):
        asyncio.run(P.model_schism_pahm_surge(
            storm_name=None, year=None, location_query=None, bbox=None,
            sim_days=1.5, open_boundary_side="south", input_mode=None,
            allow_synthetic_domain=True,
        ))
    entries = _entries_by_param(captured["entries"])
    assert entries["bathymetry"].value.startswith("SYNTHETIC")
    assert entries["domain_provenance"].value.startswith("SYNTHETIC")
    assert entries["domain_provenance"].basis == "default_demo"


def test_bathy_fetch_ok_real_path_unchanged(monkeypatch):
    """When the fetch succeeds, the knob is irrelevant and the domain provenance
    is stamped REAL, traced to the fetched source -- unchanged behaviour."""
    import trid3nt_server.agent.workflows.schism.pahm_surge.pahm_surge as P

    async def _ok(*a, **kw):
        return ("/tmp/fake_bathy.tif", "topobathy")

    def _fake_sample(points, dem_path):
        return np.full(len(points), 10.0, dtype=float)

    monkeypatch.setattr(P, "_fetch_bathymetry_cog", _ok)
    monkeypatch.setattr(P.deck_authoring, "sample_bathymetry_on_nodes", _fake_sample)
    monkeypatch.delenv("TRID3NT_SCHISM_BATHY_PATH", raising=False)

    captured: dict = {}

    async def _fake_gate(*, tool_name, mode, entries, params):
        captured["entries"] = entries
        raise _GateSentinel()

    monkeypatch.setattr(P, "gate_input_review", _fake_gate)

    with pytest.raises(_GateSentinel):
        asyncio.run(P.model_schism_pahm_surge(
            storm_name=None, year=None, location_query=None, bbox=None,
            sim_days=1.5, open_boundary_side="south", input_mode=None,
            allow_synthetic_domain=False,
        ))
    entries = _entries_by_param(captured["entries"])
    assert entries["domain_provenance"].value == "REAL"
    assert "SYNTHETIC" not in entries["bathymetry"].value
    assert entries["domain_provenance"].real_source_if_any == entries["bathymetry"].value


# --------------------------------------------------------------------------- #
# resolution_m: a USER LEVER, autoscaled from the AOI (NATE ruling, 2026-08-11
# -- no hardcoded resolution cap; oversized requests trip the payload-warning
# gate, not a silent constant).
# --------------------------------------------------------------------------- #


def test_autoscale_surge_domain_small_medium_large_aoi():
    """The autoscale rule: resolution_m grows with AOI extent (finer for a small
    domain, coarser for a large one, both clamped to fetch_topobathy's own [25,
    1000] m resolution_m bounds); the TIN node grid scales the same direction and
    stays within its sane per-axis budget."""
    import trid3nt_server.agent.workflows.schism.pahm_surge.pahm_surge as P

    small = P._autoscale_surge_domain((-95.05, 29.20, -94.95, 29.30))   # ~10 km
    medium = P._autoscale_surge_domain(P._IKE_BBOX)                      # ~150 km (showcase)
    large = P._autoscale_surge_domain((-97.5, 26.0, -93.5, 30.0))        # ~450 km

    for result in (small, medium, large):
        assert P._SURGE_RES_MIN_M <= result["resolution_m"] <= P._SURGE_RES_MAX_M
        assert P._SURGE_TIN_DIM_MIN <= result["tin_nx"] <= P._SURGE_TIN_DIM_MAX
        assert P._SURGE_TIN_DIM_MIN <= result["tin_ny"] <= P._SURGE_TIN_DIM_MAX

    # Monotonic: a bigger AOI needs a coarser (larger-metre) fetch resolution to
    # keep the long-side pixel count bounded.
    assert small["resolution_m"] < medium["resolution_m"] < large["resolution_m"]
    # The showcase (greater-Galveston) domain lands near the previous hand-picked
    # 200 m constant -- the autoscaler reproduces it from the AOI, not a hardcode.
    assert 150.0 <= medium["resolution_m"] <= 260.0
    # Small AOI floors at the resolution_m minimum (a ~10 km domain at 750 px
    # target would ask for a sub-25 m grid; the floor keeps the fetch sane).
    assert small["resolution_m"] == pytest.approx(P._SURGE_RES_MIN_M)
    # Large AOI is still within the fetch_topobathy resolution_m ceiling.
    assert large["resolution_m"] < P._SURGE_RES_MAX_M


def test_autoscale_surge_domain_is_pure_no_network():
    """The autoscaler never touches the network/filesystem -- safe to call
    unconditionally before any fetch (it derives everything from the bbox)."""
    import trid3nt_server.agent.workflows.schism.pahm_surge.pahm_surge as P

    r1 = P._autoscale_surge_domain(P._IKE_BBOX)
    r2 = P._autoscale_surge_domain(P._IKE_BBOX)
    assert r1 == r2  # deterministic / pure


def test_explicit_resolution_m_overrides_autoscale(monkeypatch):
    """An explicit resolution_m always wins over the autoscale suggestion and is
    forwarded verbatim to the bathymetry fetch, even when it implies a much
    finer/larger fetch than the autoscaled default -- oversized requests are the
    user's right (no silent resolution ceiling)."""
    import trid3nt_server.agent.workflows.schism.pahm_surge.pahm_surge as P

    captured: dict = {}

    async def _fake_fetch(bbox, *, screening_res_m=None, force_bathy_base=False):
        captured["screening_res_m"] = screening_res_m
        return "/tmp/fake_bathy.tif", "topobathy"

    def _fake_sample(points, dem_path):
        return np.full(len(points), 10.0, dtype=float)

    monkeypatch.setattr(P, "_fetch_bathymetry_cog", _fake_fetch)
    monkeypatch.setattr(P.deck_authoring, "sample_bathymetry_on_nodes", _fake_sample)
    monkeypatch.delenv("TRID3NT_SCHISM_BATHY_PATH", raising=False)

    captured_gate: dict = {}

    async def _fake_gate(*, tool_name, mode, entries, params):
        captured_gate["entries"] = entries
        raise _GateSentinel()

    monkeypatch.setattr(P, "gate_input_review", _fake_gate)

    # An explicit fine resolution the AOI-driven autoscale would NOT have picked.
    with pytest.raises(_GateSentinel):
        asyncio.run(P.model_schism_pahm_surge(
            storm_name=None, year=None, location_query=None, bbox=None,
            sim_days=1.5, open_boundary_side="south", input_mode=None,
            allow_synthetic_domain=False, resolution_m=37.5,
        ))

    assert captured["screening_res_m"] == pytest.approx(37.5)
    entries = _entries_by_param(captured_gate["entries"])
    assert entries["resolution_m"].value == pytest.approx(37.5)
    assert entries["resolution_m"].basis == "user"


def test_resolution_m_provenance_auto_when_not_supplied(monkeypatch):
    """When resolution_m is left None, the resolved value + basis='derived' come
    from the autoscaler (not the user) -- the auto-vs-user distinction the
    granularity-gate doctrine requires must be visible in the envelope."""
    import trid3nt_server.agent.workflows.schism.pahm_surge.pahm_surge as P

    async def _fake_fetch(bbox, *, screening_res_m=None, force_bathy_base=False):
        return "/tmp/fake_bathy.tif", "topobathy"

    def _fake_sample(points, dem_path):
        return np.full(len(points), 10.0, dtype=float)

    monkeypatch.setattr(P, "_fetch_bathymetry_cog", _fake_fetch)
    monkeypatch.setattr(P.deck_authoring, "sample_bathymetry_on_nodes", _fake_sample)
    monkeypatch.delenv("TRID3NT_SCHISM_BATHY_PATH", raising=False)

    captured: dict = {}

    async def _fake_gate(*, tool_name, mode, entries, params):
        captured["entries"] = entries
        raise _GateSentinel()

    monkeypatch.setattr(P, "gate_input_review", _fake_gate)

    with pytest.raises(_GateSentinel):
        asyncio.run(P.model_schism_pahm_surge(
            storm_name=None, year=None, location_query=None, bbox=None,
            sim_days=1.5, open_boundary_side="south", input_mode=None,
            allow_synthetic_domain=False, resolution_m=None,
        ))

    entries = _entries_by_param(captured["entries"])
    expected = P._autoscale_surge_domain(P._IKE_BBOX)["resolution_m"]
    assert entries["resolution_m"].basis == "derived"
    assert entries["resolution_m"].value == pytest.approx(round(expected, 1))


def test_estimate_payload_mb_reuses_topobathy_model_resolution_scaled():
    """schism_pahm_surge's declared payload estimator reuses fetch_topobathy's own
    bbox-area model (never a parallel check) and scales it down for the coarser
    SCREENING resolution this tool actually fetches at -- a bigger AOI or a finer
    explicit resolution_m both raise the estimate."""
    import trid3nt_server.agent.workflows.schism.pahm_surge.pahm_surge as P

    small_bbox = (-95.05, 29.20, -94.95, 29.30)
    est_default = P.estimate_payload_mb(bbox=list(P._IKE_BBOX))
    est_small = P.estimate_payload_mb(bbox=list(small_bbox))
    est_fine = P.estimate_payload_mb(bbox=list(P._IKE_BBOX), resolution_m=10.0)

    assert est_default > 0.0
    assert est_small < est_default  # smaller AOI -> smaller estimate
    # Forcing native (10 m) resolution over the showcase AOI must estimate a much
    # bigger payload than the coarse autoscaled default.
    assert est_fine > est_default * 100.0
