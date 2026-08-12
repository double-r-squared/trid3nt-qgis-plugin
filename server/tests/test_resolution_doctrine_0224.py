"""Resolution doctrine (ADR 0224) -- offline unit coverage.

Covers the three rulings landed in the schism surge + topobathy + payload seam:
  R-A native-default / explicit-coarsen resolution semantics (``_topobathy_fetch_kwargs``);
  R-B sampled payload estimator (measured vs analytic, px-cap, resolution scaling, cache);
  R-C honest GLOBAL-FALLBACK warning (skipped vs no-intersect vs unreachable vs datum-gated).
All pure -- no network, no fetch.
"""
from __future__ import annotations

import pytest

BBOX = (-95.05, 29.2, -94.6, 29.65)


# --------------------------------------------------------------------------- #
# R-C: honest GLOBAL-FALLBACK warning cause.
# --------------------------------------------------------------------------- #
from trid3nt_server.agent.tools.fetchers._router.hooks.topobathy import (  # noqa: E402
    _compose_fallback_warnings,
)


def _warn(**kw):
    base = dict(bbox=BBOX, cudem_status="no_intersect", cudem_count=0,
                regional_count=0, has_etopo=True, bathy_present=True,
                land_absent=False)
    base.update(kw)
    return _compose_fallback_warnings(**base)


def test_rc_no_intersect_may_claim_omission():
    w = _warn(cudem_status="no_intersect")
    assert "collection omits this coast" in w
    assert "GLOBAL-FALLBACK" in w


def test_rc_skipped_never_claims_omission():
    w = _warn(cudem_status="skipped")
    assert "SKIPPED" in w
    assert "collection omits this coast" not in w  # the 0221 lie is gone


def test_rc_index_unreachable_names_its_cause():
    w = _warn(cudem_status="index_unreachable")
    assert "could not be reached" in w
    assert "collection omits this coast" not in w


def test_rc_datum_gated_present_names_its_cause():
    w = _warn(cudem_status="present")
    assert "vertical-datum" in w
    assert "collection omits this coast" not in w


def test_rc_no_fallback_when_cudem_present():
    # CUDEM tiles painted the merge -> no global-fallback warning at all.
    assert _warn(cudem_status="present", cudem_count=8) is None


def test_rc_bathy_absent_beats_fallback_branch():
    w = _warn(bathy_present=False, has_etopo=False)
    assert "BATHYMETRY ABSENT" in w
    assert "GLOBAL-FALLBACK" not in w


def test_rc_land_absent_labeled_degrade_appended():
    w = _warn(cudem_status="present", cudem_count=8, land_absent=True)
    assert "land_absent" in w


# --------------------------------------------------------------------------- #
# R-A: native-default / explicit-coarsen fetch kwargs + CUDEM skip threshold.
# --------------------------------------------------------------------------- #
from trid3nt_server.agent.workflows.schism.tidal_hydro.tidal_hydro import (  # noqa: E402
    _CUDEM_SKIP_RES_M,
    _topobathy_fetch_kwargs,
)


def test_ra_native_reads_cudem_no_min_pixel():
    topo, dem = _topobathy_fetch_kwargs(None, force_bathy_base=True, skip_land=True)
    assert "min_pixel_m" not in topo and "resolution_m" not in topo
    assert "skip_cudem" not in topo  # native NEVER skips CUDEM (the 0221 fix)
    assert topo["force_bathy_base"] is True and topo["skip_land"] is True
    assert dem == {}


def test_ra_bare_native_is_byte_identical_default():
    # The tidal path calls with no flags -> empty kwargs (pre-doctrine behaviour).
    topo, dem = _topobathy_fetch_kwargs(None, force_bathy_base=False, skip_land=False)
    assert topo == {} and dem == {}


def test_ra_explicit_fine_reads_cudem_with_floor():
    topo, dem = _topobathy_fetch_kwargs(30.0, force_bathy_base=True, skip_land=True)
    assert topo["resolution_m"] == 30 and topo["min_pixel_m"] == 30.0
    assert "skip_cudem" not in topo  # 30 m << ETOPO native, CUDEM still refines
    assert dem["resolution_m"] == 30


def test_ra_coarse_at_threshold_skips_cudem():
    topo, _ = _topobathy_fetch_kwargs(_CUDEM_SKIP_RES_M, force_bathy_base=True, skip_land=True)
    assert topo["skip_cudem"] is True


@pytest.mark.parametrize("res,skip", [(499.0, False), (500.0, True), (750.0, True)])
def test_ra_cudem_skip_threshold(res, skip):
    topo, _ = _topobathy_fetch_kwargs(res, force_bathy_base=True, skip_land=True)
    assert ("skip_cudem" in topo) is skip


# --------------------------------------------------------------------------- #
# R-B: sampled payload estimator.
# --------------------------------------------------------------------------- #
from trid3nt_server.agent.tools import payload_sampling as ps  # noqa: E402
from trid3nt_server.agent.tools.payload_sampling import (  # noqa: E402
    SampledDensity,
    estimate_mb,
)


@pytest.fixture(autouse=True)
def _clear_sample_cache():
    with ps._CACHE_LOCK:
        ps._CACHE.clear()
    yield
    with ps._CACHE_LOCK:
        ps._CACHE.clear()


def test_rb_analytic_fallback_labeled():
    est = estimate_mb("s", BBOX, analytic_mb=81.0, sample_fn=lambda b: None,
                      resolution_m=None)
    assert est.kind == "analytic" and est.mb == 81.0


def test_rb_analytic_stays_resolution_aware():
    native = estimate_mb("s", BBOX, analytic_mb=81.0, sample_fn=None, resolution_m=None)
    coarse = estimate_mb("s", BBOX, analytic_mb=81.0, sample_fn=None, resolution_m=100.0)
    assert coarse.mb < native.mb  # coarser -> fewer pixels -> smaller


def test_rb_measured_labeled_and_scales():
    d = SampledDensity(bytes_per_px=2.0, px_per_sq_deg=1.0e8)
    native = estimate_mb("m", BBOX, analytic_mb=1.0, sample_fn=lambda b: d, resolution_m=None)
    assert native.kind == "measured" and native.mb > 1.0


def test_rb_px_cap_bounds_huge_aoi():
    d = SampledDensity(bytes_per_px=4.0, px_per_sq_deg=1.2e9)
    huge = estimate_mb("h", (-125.0, 25.0, -65.0, 49.0), analytic_mb=1e9,
                       sample_fn=lambda b: d, resolution_m=None)
    # capped at 12000**2 px * 4 bytes = 576 MB, NOT the 1e9 analytic runaway.
    assert huge.px == ps.DEFAULT_PX_CAP ** 2
    assert huge.mb == pytest.approx(576.0, rel=1e-3)


def test_rb_cache_samples_region_once():
    calls = {"n": 0}
    d = SampledDensity(bytes_per_px=2.0, px_per_sq_deg=1.0e8)

    def sample(_b):
        calls["n"] += 1
        return d

    estimate_mb("c", BBOX, analytic_mb=1.0, sample_fn=sample, resolution_m=None)
    estimate_mb("c", BBOX, analytic_mb=1.0, sample_fn=sample, resolution_m=50.0)
    assert calls["n"] == 1  # second call hits the region cache


# --------------------------------------------------------------------------- #
# R-B wired: surge estimator + gate-detail text (analytic path, no network).
# --------------------------------------------------------------------------- #
from trid3nt_server.agent.workflows.schism.pahm_surge import pahm_surge as surge  # noqa: E402


@pytest.fixture
def _no_sample(monkeypatch):
    import trid3nt_server.agent.tools.fetchers._router.hooks.topobathy as tbh
    monkeypatch.setattr(tbh, "_sample_topobathy_density", lambda b: None)


def test_rb_surge_native_estimate_exceeds_explicit_coarse(_no_sample):
    native = surge.estimate_payload_mb(bbox=list(BBOX))
    coarse = surge.estimate_payload_mb(bbox=list(BBOX), resolution_m=199)
    assert native > coarse  # R-A: native default is the heavy one the gate warns on


def test_rb_surge_detail_native_offers_coarsening(_no_sample):
    txt = surge.estimate_payload_mb_detail(bbox=list(BBOX))
    assert "native bathymetry" in txt
    assert "suggested coarsening" in txt
    assert "proceed native / coarsen" in txt
    assert "(analytic)" in txt  # estimator KIND is surfaced


def test_rb_surge_detail_explicit_names_grid(_no_sample):
    txt = surge.estimate_payload_mb_detail(bbox=list(BBOX), resolution_m=30)
    assert "30 m grid" in txt and "analytic" in txt
