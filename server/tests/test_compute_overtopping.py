"""Tests for ``compute_overtopping`` (AWS SWAN-lecture EurOtop tool).

Pure-analytic deterministic tool: no network, no fixtures, no cache.

Coverage:
- Known hand-computed EurOtop 2018 Ch.5 case (Eqn 5.10 / 5.11 + the 0.09 cap).
- Governing-formula selection (breaking vs max upper-limit).
- Monotonicity: higher crest freeboard -> less overtopping; rougher slope (lower
  gamma_f) -> less overtopping.
- Determinism (same inputs -> identical output).
- Cotangent-slope convenience inversion.
- Input validation (positive Hs/Tp/slope; non-negative freeboard; gammas in (0,1]).
- Registration in TOOL_REGISTRY + category membership + corpus presence.
"""

from __future__ import annotations

import math

import pytest

from trid3nt_server.tools.processing.compute_overtopping import (
    OvertoppingError,
    compute_overtopping,
)

_G = 9.81


# ---------------------------------------------------------------------------
# Known-case formula check (hand-computed)
# ---------------------------------------------------------------------------


def test_known_case_smooth_dike():
    """Hs=2.0 m, Tp=6.0 s, 1:2 slope (tan=0.5), Rc=3.0 m, smooth -> q hand calc.

    T_{m-1,0} = 6.0/1.1 = 5.4545 s
    L_{m-1,0} = g*T^2/(2pi) = 46.45 m ; s = 2/46.45 = 0.04306 ; xi = 0.5/sqrt(s)
    xi = 2.4097
    q*_break (Eqn 5.10) = 0.010996 ; q*_max (Eqn 5.11) = 0.005104 -> max governs
    q = q*_max * sqrt(g*Hs^3) = 0.045219 m^3/s/m
    """
    out = compute_overtopping(
        hs_m=2.0, tp_s=6.0, crest_freeboard_m=3.0, slope=0.5
    )
    assert out["breaker_parameter"] == pytest.approx(2.4097, abs=1e-3)
    assert out["governing_formula"] == "max"
    assert out["dimensionless_q"] == pytest.approx(0.005104, rel=1e-3)
    assert out["q_m3_s_per_m"] == pytest.approx(0.045219, rel=1e-3)
    assert out["q_l_s_per_m"] == pytest.approx(45.219, rel=1e-3)
    assert "EurOtop" in out["method"]
    assert out["slope_tan"] == pytest.approx(0.5, rel=1e-12)
    assert "computed_at" in out


def test_q_against_independent_recompute():
    """Re-derive q from the returned dimensionless_q and assert consistency."""
    out = compute_overtopping(hs_m=1.5, tp_s=7.0, crest_freeboard_m=2.0, slope=0.25)
    expected = out["dimensionless_q"] * math.sqrt(_G * out["hs_m"] ** 3)
    assert out["q_m3_s_per_m"] == pytest.approx(expected, rel=1e-12)


def test_higher_freeboard_reduces_overtopping():
    low = compute_overtopping(hs_m=2.0, tp_s=6.0, crest_freeboard_m=1.0, slope=0.5)
    high = compute_overtopping(hs_m=2.0, tp_s=6.0, crest_freeboard_m=4.0, slope=0.5)
    assert high["q_m3_s_per_m"] < low["q_m3_s_per_m"]


def test_roughness_reduces_overtopping():
    smooth = compute_overtopping(
        hs_m=2.0, tp_s=6.0, crest_freeboard_m=2.0, slope=0.5, gamma_f=1.0
    )
    rough = compute_overtopping(
        hs_m=2.0, tp_s=6.0, crest_freeboard_m=2.0, slope=0.5, gamma_f=0.55
    )
    assert rough["q_m3_s_per_m"] < smooth["q_m3_s_per_m"]


def test_zero_freeboard_allowed_and_large():
    """Rc=0 (crest at the water line) is valid and gives the largest discharge."""
    out = compute_overtopping(hs_m=2.0, tp_s=6.0, crest_freeboard_m=0.0, slope=0.5)
    assert out["crest_freeboard_m"] == 0.0
    assert out["q_m3_s_per_m"] > 0.0


def test_cotangent_slope_is_inverted():
    """slope=2 (a 1:2 cot value) is interpreted as tan=0.5 and matches it."""
    tan_form = compute_overtopping(
        hs_m=2.0, tp_s=6.0, crest_freeboard_m=3.0, slope=0.5
    )
    cot_form = compute_overtopping(
        hs_m=2.0, tp_s=6.0, crest_freeboard_m=3.0, slope=2.0
    )
    assert cot_form["slope_tan"] == pytest.approx(0.5, rel=1e-12)
    assert cot_form["q_m3_s_per_m"] == pytest.approx(
        tan_form["q_m3_s_per_m"], rel=1e-12
    )


def test_determinism():
    a = compute_overtopping(hs_m=1.8, tp_s=8.0, crest_freeboard_m=2.5, slope=0.333)
    b = compute_overtopping(hs_m=1.8, tp_s=8.0, crest_freeboard_m=2.5, slope=0.333)
    assert a["q_m3_s_per_m"] == b["q_m3_s_per_m"]
    assert a["governing_formula"] == b["governing_formula"]


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [0.0, -1.0, "x", None, float("nan")])
def test_invalid_hs_raises(bad):
    with pytest.raises(OvertoppingError) as ei:
        compute_overtopping(hs_m=bad, tp_s=6.0, crest_freeboard_m=3.0, slope=0.5)
    assert ei.value.error_code == "HS_INVALID"


@pytest.mark.parametrize("bad", [0.0, -2.0, "x", None])
def test_invalid_tp_raises(bad):
    with pytest.raises(OvertoppingError) as ei:
        compute_overtopping(hs_m=2.0, tp_s=bad, crest_freeboard_m=3.0, slope=0.5)
    assert ei.value.error_code == "TP_INVALID"


@pytest.mark.parametrize("bad", [-1.0, "x", None])
def test_invalid_freeboard_raises(bad):
    with pytest.raises(OvertoppingError) as ei:
        compute_overtopping(hs_m=2.0, tp_s=6.0, crest_freeboard_m=bad, slope=0.5)
    assert ei.value.error_code == "FREEBOARD_INVALID"


@pytest.mark.parametrize("bad", [0.0, -0.5, "x", None])
def test_invalid_slope_raises(bad):
    with pytest.raises(OvertoppingError) as ei:
        compute_overtopping(hs_m=2.0, tp_s=6.0, crest_freeboard_m=3.0, slope=bad)
    assert ei.value.error_code == "SLOPE_INVALID"


@pytest.mark.parametrize("bad", [0.0, -0.1, 1.5, "x"])
def test_invalid_gamma_raises(bad):
    with pytest.raises(OvertoppingError) as ei:
        compute_overtopping(
            hs_m=2.0, tp_s=6.0, crest_freeboard_m=3.0, slope=0.5, gamma_f=bad
        )
    assert ei.value.error_code == "GAMMA_INVALID"


def test_extra_kwargs_ignored():
    out = compute_overtopping(
        hs_m=2.0, tp_s=6.0, crest_freeboard_m=3.0, slope=0.5, foo=1, structure="dike"
    )
    assert out["q_m3_s_per_m"] == pytest.approx(0.045219, rel=1e-3)


# ---------------------------------------------------------------------------
# Registration + discoverability
# ---------------------------------------------------------------------------


def test_registered_in_tool_registry():
    from trid3nt_server.tools import TOOL_REGISTRY

    assert "compute_overtopping" in TOOL_REGISTRY
    md = TOOL_REGISTRY["compute_overtopping"].metadata
    assert md.name == "compute_overtopping"
    assert md.cacheable is False
    assert md.ttl_class == "live-no-cache"
    assert md.read_only_hint is True
    assert md.open_world_hint is False
    assert md.idempotent_hint is True


def test_in_coastal_category():
    from trid3nt_server.categories import PRIMARY_CATEGORY, tools_for_category

    assert PRIMARY_CATEGORY.get("compute_overtopping") == "coastal"
    assert "compute_overtopping" in tools_for_category("coastal")


def test_has_corpus_entries():
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
    assert "compute_overtopping" in corpus
    assert len(corpus["compute_overtopping"]) >= 5
