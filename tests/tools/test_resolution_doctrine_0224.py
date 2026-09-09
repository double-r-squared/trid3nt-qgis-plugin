"""Resolution doctrine (ADR 0224) -- offline unit coverage.

Covers the two rulings whose subject is still in the tree:
  R-B sampled payload estimator (measured vs analytic, px-cap, resolution scaling, cache);
  R-C honest GLOBAL-FALLBACK warning (skipped vs no-intersect vs unreachable vs datum-gated).
R-A (native-default / explicit-coarsen fetch kwargs) rode the schism surge legs and
left with them.
All pure -- no network, no fetch.
"""
from __future__ import annotations

import pytest

BBOX = (-95.05, 29.2, -94.6, 29.65)


# --------------------------------------------------------------------------- #
# R-C: honest GLOBAL-FALLBACK warning cause.
# --------------------------------------------------------------------------- #
from trid3nt_server.tools.fetchers._router.hooks.topobathy import (  # noqa: E402
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
# R-B: sampled payload estimator.
# --------------------------------------------------------------------------- #
from trid3nt_server.tools import payload_sampling as ps  # noqa: E402
from trid3nt_server.tools.payload_sampling import (  # noqa: E402
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


