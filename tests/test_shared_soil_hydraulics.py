"""Tests for the shared pedotransfer soil-K seam (Saxton-Rawls 2006).

Offline, pure: no I/O, no network. Asserts the published function's physical
behaviour (texture ordering, plausibility clamp, provenance labeling) and the
typed input guard.
"""

from __future__ import annotations

import math

import pytest

from trid3nt_server.workflows.shared.soil_hydraulics import (
    K_CEIL_M_S,
    K_FLOOR_M_S,
    PedotransferK,
    SoilHydraulicsInputError,
    ksat_from_texture,
    saxton_rawls_ksat,
)


# Rawls et al. (1982) / Saxton-Rawls (2006) textural-class Ksat ranges (m/s):
# sandy media are highly conductive, clays are nearly impervious.
_TEXTURES = {
    "sand": (0.92, 0.03),
    "loamy_sand": (0.82, 0.06),
    "sandy_loam": (0.65, 0.10),
    "loam": (0.40, 0.20),
    "clay_loam": (0.30, 0.34),
    "clay": (0.20, 0.55),
}


def test_ksat_monotone_sand_to_clay() -> None:
    """K decreases monotonically from sand to clay (the physical ordering)."""
    ks = [ksat_from_texture(s, c).k_m_s for s, c in _TEXTURES.values()]
    assert all(ks[i] >= ks[i + 1] for i in range(len(ks) - 1)), ks


def test_ksat_textural_ranges_physical() -> None:
    """Each class lands in its natural-media band (sand fast, clay slow)."""
    k_sand = ksat_from_texture(*_TEXTURES["sand"]).k_m_s
    k_loam = ksat_from_texture(*_TEXTURES["loam"]).k_m_s
    k_clay = ksat_from_texture(*_TEXTURES["clay"]).k_m_s
    assert 1e-5 <= k_sand <= 1e-3, k_sand
    assert 1e-7 <= k_loam <= 1e-5, k_loam
    assert 1e-9 <= k_clay <= 1e-6, k_clay


def test_provenance_labeling_loud() -> None:
    """The result is labeled DERIVED / near-surface / not measured aquifer K."""
    pk = ksat_from_texture(0.4, 0.2, depth_label="5-15cm")
    assert isinstance(pk, PedotransferK)
    assert pk.basis == "pedotransfer_saxton_rawls_2006"
    assert "DERIVED" in pk.limitation and "NOT a measured aquifer" in pk.limitation
    assert pk.depth_label == "5-15cm"
    assert 0.02 <= pk.porosity <= 0.6
    d = pk.as_dict()
    assert d["basis"] == pk.basis and d["k_m_s"] == pk.k_m_s


def test_clamp_records_hit() -> None:
    """A texture that would extrapolate below the floor records clamped=True."""
    # Pure heavy clay -> very low K; assert the clamp floor + flag semantics.
    pk = ksat_from_texture(0.02, 0.95)
    assert K_FLOOR_M_S <= pk.k_m_s <= K_CEIL_M_S
    if pk.k_mm_hr * (1.0 / (1000.0 * 3600.0)) < K_FLOOR_M_S:
        assert pk.clamped is True


def test_input_guard_out_of_range() -> None:
    with pytest.raises(SoilHydraulicsInputError):
        ksat_from_texture(1.5, 0.1)
    with pytest.raises(SoilHydraulicsInputError):
        ksat_from_texture(0.4, -0.1)


def test_saxton_rawls_intermediates_ordered() -> None:
    """theta_1500 < theta_33 < theta_s (a valid moisture curve) for a real loam."""
    _k, inter = saxton_rawls_ksat(0.4, 0.2, 1.5)
    assert inter["theta_1500"] < inter["theta_33"] < inter["theta_s"]
    assert math.isfinite(inter["lambda"]) and inter["lambda"] > 0.0
