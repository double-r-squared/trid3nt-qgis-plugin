"""Tests for ``compute_wave_nomograph`` (AWS SWAN-lecture nomograph tool).

Pure-analytic deterministic tool: no network, no fixtures, no cache.

Coverage:
- Known hand-computed fetch-limited case (SPM 1984 / CEM Part II-2 form).
- Regime classification: fetch-limited / duration-limited / fully-developed.
- Determinism (same inputs -> identical output).
- Input validation (positive wind/fetch; optional positive duration/depth).
- Registration in TOOL_REGISTRY with the expected metadata.
- Category membership (coastal) + corpus presence.
"""

from __future__ import annotations

import math

import pytest

from trid3nt_server.tools.processing.compute_wave_nomograph import (
    WaveNomographError,
    compute_wave_nomograph,
)

_G = 9.81


# ---------------------------------------------------------------------------
# Known-case formula checks (hand-computed)
# ---------------------------------------------------------------------------


def test_known_fetch_limited_case():
    """U=20 m/s, F=100 km -> Hs=3.2308 m, Tp=7.8549 s (SPM/CEM hand calc).

    X_hat = g*F/U^2 = 9.81*1e5/400 = 2452.5
    Hs = 1.6e-3 * sqrt(X_hat) * U^2/g = 3.230840 m
    Tp = 2.857e-1 * X_hat^(1/3) * U/g = 7.854904 s
    """
    out = compute_wave_nomograph(wind_speed_ms=20.0, fetch_km=100.0)
    assert out["regime"] == "fetch-limited"
    assert out["hs_m"] == pytest.approx(3.230840, rel=1e-5)
    assert out["tp_s"] == pytest.approx(7.854904, rel=1e-5)
    assert out["dimensionless_fetch"] == pytest.approx(2452.5, rel=1e-9)
    # effective fetch == geographic fetch when no duration cap.
    assert out["effective_fetch_km"] == pytest.approx(100.0, rel=1e-9)
    assert out["wind_speed_ms"] == 20.0
    assert "Shore Protection Manual" in out["method"]
    assert "computed_at" in out


def test_small_fetch_is_clearly_fetch_limited():
    """A weak wind over a short fetch produces a small fetch-limited sea."""
    out = compute_wave_nomograph(wind_speed_ms=10.0, fetch_km=10.0)
    assert out["regime"] == "fetch-limited"
    # X_hat = 9.81*1e4/100 = 981 ; Hs = 1.6e-3*sqrt(981)*100/9.81
    assert out["hs_m"] == pytest.approx(0.510841, rel=1e-5)
    assert out["tp_s"] == pytest.approx(2.893772, rel=1e-5)


def test_fully_developed_caps_govern_for_huge_fetch():
    """A very large fetch hits the Pierson-Moskowitz fully-developed caps."""
    out = compute_wave_nomograph(wind_speed_ms=20.0, fetch_km=100000.0)
    assert out["regime"] == "fully-developed"
    # Hs_fd = 0.2433 * U^2/g ; Tp_fd = 8.134 * U/g
    assert out["hs_m"] == pytest.approx(0.2433 * 20.0**2 / _G, rel=1e-6)
    assert out["tp_s"] == pytest.approx(8.134 * 20.0 / _G, rel=1e-6)


def test_duration_limit_reduces_effective_fetch():
    """A short storm duration caps the effective fetch (duration-limited)."""
    # Geographic fetch is huge so geography never binds; 1 hr duration governs.
    out = compute_wave_nomograph(
        wind_speed_ms=15.0, fetch_km=10000.0, duration_hr=1.0
    )
    assert out["regime"] == "duration-limited"
    # x_dur = (g*t/U / 68.8)^1.5 with t=3600 s -> eff fetch ~= 4.591 km
    assert out["effective_fetch_km"] == pytest.approx(4.591, abs=1e-2)
    assert out["effective_fetch_km"] < out["fetch_km"]


def test_long_duration_does_not_bind():
    """A long duration leaves the geographic fetch as the binding constraint."""
    out = compute_wave_nomograph(
        wind_speed_ms=20.0, fetch_km=100.0, duration_hr=48.0
    )
    assert out["regime"] == "fetch-limited"
    assert out["effective_fetch_km"] == pytest.approx(100.0, rel=1e-6)


def test_depth_is_recorded_but_uncorrected():
    """depth_m is echoed and noted, but no finite-depth cap is applied."""
    deep = compute_wave_nomograph(wind_speed_ms=18.0, fetch_km=40.0)
    shallow = compute_wave_nomograph(wind_speed_ms=18.0, fetch_km=40.0, depth_m=3.0)
    assert shallow["depth_m"] == 3.0
    # Same Hs/Tp because no depth correction is applied in this version.
    assert shallow["hs_m"] == pytest.approx(deep["hs_m"], rel=1e-12)
    assert any("finite-depth" in n or "depth_m=3" in n for n in shallow["notes"])


def test_determinism():
    """Same inputs -> identical numeric outputs (timestamp excepted)."""
    a = compute_wave_nomograph(wind_speed_ms=12.5, fetch_km=33.0)
    b = compute_wave_nomograph(wind_speed_ms=12.5, fetch_km=33.0)
    assert a["hs_m"] == b["hs_m"]
    assert a["tp_s"] == b["tp_s"]
    assert a["regime"] == b["regime"]


def test_hs_grows_monotonically_with_fetch():
    """Within the fetch-limited regime Hs increases with fetch."""
    small = compute_wave_nomograph(wind_speed_ms=15.0, fetch_km=10.0)
    big = compute_wave_nomograph(wind_speed_ms=15.0, fetch_km=80.0)
    assert big["hs_m"] > small["hs_m"]
    assert big["tp_s"] > small["tp_s"]


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [0.0, -5.0, "abc", None, float("nan")])
def test_invalid_wind_speed_raises(bad):
    with pytest.raises(WaveNomographError) as ei:
        compute_wave_nomograph(wind_speed_ms=bad, fetch_km=50.0)
    assert ei.value.error_code == "WIND_SPEED_INVALID"


@pytest.mark.parametrize("bad", [0.0, -1.0, "xx", None])
def test_invalid_fetch_raises(bad):
    with pytest.raises(WaveNomographError) as ei:
        compute_wave_nomograph(wind_speed_ms=15.0, fetch_km=bad)
    assert ei.value.error_code == "FETCH_INVALID"


@pytest.mark.parametrize("bad", [0.0, -2.0, "nope"])
def test_invalid_duration_raises(bad):
    with pytest.raises(WaveNomographError) as ei:
        compute_wave_nomograph(wind_speed_ms=15.0, fetch_km=50.0, duration_hr=bad)
    assert ei.value.error_code == "DURATION_INVALID"


@pytest.mark.parametrize("bad", [0.0, -3.0, "deep"])
def test_invalid_depth_raises(bad):
    with pytest.raises(WaveNomographError) as ei:
        compute_wave_nomograph(wind_speed_ms=15.0, fetch_km=50.0, depth_m=bad)
    assert ei.value.error_code == "DEPTH_INVALID"


def test_extra_kwargs_ignored():
    """LLM-invented kwargs are absorbed (job-0164 belt-and-suspenders)."""
    out = compute_wave_nomograph(
        wind_speed_ms=20.0, fetch_km=100.0, foo="bar", units="metric"
    )
    assert out["hs_m"] == pytest.approx(3.230840, rel=1e-5)


# ---------------------------------------------------------------------------
# Registration + discoverability
# ---------------------------------------------------------------------------


def test_registered_in_tool_registry():
    from trid3nt_server.tools import TOOL_REGISTRY

    assert "compute_wave_nomograph" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["compute_wave_nomograph"]
    md = entry.metadata
    assert md.name == "compute_wave_nomograph"
    assert md.cacheable is False
    assert md.ttl_class == "live-no-cache"
    assert md.read_only_hint is True
    assert md.open_world_hint is False
    assert md.idempotent_hint is True


def test_in_coastal_category():
    from trid3nt_server.categories import PRIMARY_CATEGORY, tools_for_category

    assert PRIMARY_CATEGORY.get("compute_wave_nomograph") == "coastal"
    assert "compute_wave_nomograph" in tools_for_category("coastal")


def test_has_corpus_entries():
    import trid3nt_server.tools.discovery.search_tools as dd  # noqa: F401  (ensures module import path exists)
    from pathlib import Path
    import yaml

    corpus_path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "trid3nt_server"
        / "data"
        / "tool_query_corpus.yaml"
    )
    corpus = yaml.safe_load(corpus_path.read_text())
    assert "compute_wave_nomograph" in corpus
    assert len(corpus["compute_wave_nomograph"]) >= 5
