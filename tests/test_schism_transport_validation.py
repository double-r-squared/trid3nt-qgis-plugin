"""SCHISM transport-scheme validation template test set (ADR 0156).

Offline + deterministic: no docker, no S3. Covers the contract round-trip, the
deck-authoring (tvd.prop toggle + temperature front + baroclinic 3D param), the
mixing/mass metric arithmetic on synthetic temperature series, the input-guard
surface, and the registration/corpus pins.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from trid3nt_contracts.schism_contracts import (
    SCHISM_TRANSPORT_SCHEMES,
    SchismTransportValidationResult,
)


# --------------------------------------------------------------------------- #
# 1. Contract round-trip
# --------------------------------------------------------------------------- #
def test_transport_result_contract():
    r = SchismTransportValidationResult(
        question="q",
        tvd_variance_retained_pct=95.7,
        upwind_variance_retained_pct=90.7,
        excess_mixing_factor=2.16,
        tvd_mass_drift_pct=-0.7,
        upwind_mass_drift_pct=-1.05,
        validated=True,
    )
    assert r.schematic_only is True
    assert r.excess_mixing_factor > 1.0
    dumped = r.model_dump()
    assert dumped["upwind_variance_retained_pct"] < dumped["tvd_variance_retained_pct"]


# --------------------------------------------------------------------------- #
# 2. Deck authoring: tvd.prop toggle + temperature front + baroclinic param
# --------------------------------------------------------------------------- #
def test_stage_transport_scheme_deck_toggle(tmp_path: Path):
    from trid3nt_server.agent.workflows.schism import deck_authoring as da

    for scheme, want_flag in (("tvd", "1"), ("upwind", "0")):
        d = tmp_path / scheme
        info = da.stage_transport_scheme_deck(d, scheme=scheme, sim_days=1.0)
        names = {f.name for f in info["files"]}
        assert {"hgrid.gr3", "vgrid.in", "param.nml", "bctides.in", "temp.ic",
                "salt.ic", "tvd.prop"} <= names
        # tvd.prop is uniformly the scheme flag
        flags = {line.split()[1] for line in (d / "tvd.prop").read_text().splitlines() if line.strip()}
        assert flags == {want_flag}, f"{scheme}: {flags}"
        # param.nml is baroclinic (live tracer) with itr_met=3
        param = (d / "param.nml").read_text()
        assert "ibc = 0" in param
        assert "itr_met = 3" in param
        # temp.ic carries a genuine front (both hot and cold values present)
        temp_vals = [float(l.split()[-1]) for l in (d / "temp.ic").read_text().splitlines()[2:] if l.strip()]
        assert max(temp_vals) > min(temp_vals), "temp.ic must be a front, not uniform"
        assert info["n_layers"] >= 3 and info["nscribe"] >= 5


def test_stage_transport_scheme_deck_rejects_unknown_scheme(tmp_path: Path):
    from trid3nt_server.agent.workflows.schism import deck_authoring as da
    with pytest.raises(da.SchismDeckError):
        da.stage_transport_scheme_deck(tmp_path / "x", scheme="weno")


# --------------------------------------------------------------------------- #
# 3. Mixing + mass metric arithmetic (synthetic series -> the contrast)
# --------------------------------------------------------------------------- #
def _fake_read(var_start: float, var_end: float, mass_drift_frac: float, n=6):
    """Build a read_transport_temperature-shaped dict with a controlled contrast."""
    t = np.arange(n, dtype=float) * 3600.0
    variance = np.linspace(var_start, var_end, n)
    mass = np.linspace(15.9, 15.9 * (1.0 + mass_drift_frac), n)
    return {
        "t_hr": (t - t[0]) / 3600.0, "variance": variance, "mass": mass,
        "t_min": np.full(n, 10.0), "t_max": np.full(n, 20.0),
        "node_final": np.array([10.0, 20.0]), "n_times": n, "n_nodes": 2, "n_layers": 5,
    }


def test_compare_transport_schemes_contrast():
    from trid3nt_server.agent.workflows.schism import postprocess_schism as pp

    tvd = _fake_read(20.0, 19.3, -0.004)      # retains ~96.5%
    upwind = _fake_read(20.0, 18.2, -0.007)   # retains ~91%
    c = pp.compare_transport_schemes(tvd, upwind, t_hot=20.0, t_cold=10.0)
    assert c["upwind_variance_retained_pct"] < c["tvd_variance_retained_pct"]
    assert c["excess_mixing_factor"] > 1.0  # upwind mixes more
    assert abs(c["tvd_mass_drift_pct"]) < 3.0 and abs(c["upwind_mass_drift_pct"]) < 3.0
    assert c["validated"] is True


def test_compare_flags_mass_blowup_as_unvalidated():
    from trid3nt_server.agent.workflows.schism import postprocess_schism as pp
    tvd = _fake_read(20.0, 19.3, -0.004)
    upwind = _fake_read(20.0, 18.2, -0.20)  # 20% mass drift -> fails sanity gate
    c = pp.compare_transport_schemes(tvd, upwind, t_hot=20.0, t_cold=10.0)
    assert c["validated"] is False


# --------------------------------------------------------------------------- #
# 4. Input guard + registration pins
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_sim_days_out_of_range_returns_typed_error():
    from trid3nt_server.agent.workflows.schism.transport_validation.transport_validation import (
        schism_transport_validation,
    )
    out = await schism_transport_validation(sim_days=99.0)
    assert isinstance(out, dict) and out["status"] == "error"
    assert out["error_code"] == "SCHISM_INPUT_INVALID"


def test_registered():
    from trid3nt_server.agent.tools import TOOL_REGISTRY

    e = TOOL_REGISTRY.get("schism_transport_validation")
    assert e is not None
    assert e.metadata.engine == "schism" and e.metadata.tier == "template"


def test_corpus_surfaces_the_template():
    from trid3nt_server.agent.tools.search.tool_retrieval import retrieve_visible_tools
    visible = retrieve_visible_tools(
        "compare SCHISM transport schemes upwind vs TVD numerical mixing", None, 8
    )
    assert "schism_transport_validation" in visible
