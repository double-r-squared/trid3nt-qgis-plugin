"""Unit + live tests for the MODFLOW 6 GWF+GWE heat-transport deck (ADR 0235).

Asserts the *deck construction* contract for the ``gwe_thermal`` archetype
family (a DUAL-model GWF+GWE sim: a warm-water injection WEL carrying an
AUXILIARY TEMPERATURE, mapped by the GWE SSM onto the energy-transport source,
coupled through a GWF6-GWE6 exchange) AND the headline PHYSICS proven in the
sandbox (scripts/sandbox/modflow/gwe_thermal_physics_sandbox.py):

  injection_plume: the downgradient thermal-plume warm-cell extent grows
    MONOTONICALLY with the injection temperature delta (heat twin of "plume
    extent grows with source strength").
  ates: seasonal charge/recover recovery efficiency is strictly in (0, 1) and
    RISES with cycle count (the aquifer thermal buffer pre-warms).

The pure-shape assertions need NO mf6 binary; the physics assertions run the
LOCAL mf6 6.7.0 binary and are skipped when it is absent. Replicates the
modflow6-examples GWE roster (ex-gwe-ates seasonal ATES + the radial/Barends
conductive-advective classes) -- see docs/decisions/0235-modflow-gwe.md.

Run:
    TRID3NT_MF6_BIN=$PWD/bin/mf6 venvs/agent/bin/python -m pytest \
        services/workers/modflow/test_gwt_adapter_gwe_thermal.py -q
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import flopy
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gwt_adapter import (  # noqa: E402
    DeckManifest,
    GWE_AMBIENT_TEMPERATURE_C,
    build_modflow_deck,
)

# St. Paul, Minnesota -- a real cold-climate ATES / geothermal-relevant setting
# (natural place name, no bbox). Aquifer thermal energy storage is deployed in
# the Upper Midwest for seasonal district heating/cooling.
LAT0, LON0 = 44.95, -93.09
BASE = dict(
    spill_location_latlon=(LAT0, LON0),
    contaminant="temperature",
    release_rate_kg_s=0.0,
    aquifer_k_ms=1.0e-4,
    porosity=0.20,
)

MF6_BIN = os.environ.get("TRID3NT_MF6_BIN") or str(
    Path(__file__).resolve().parents[3] / "bin" / "mf6"
)


def _mf6_available() -> bool:
    try:
        subprocess.run([MF6_BIN, "--version"], capture_output=True, check=True)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


# --------------------------------------------------------------------------- #
# Pure deck construction (no mf6): GWE present, dual model, WEL+aux, exchange.
# --------------------------------------------------------------------------- #
@pytest.fixture()
def plume_deck(tmp_path):
    return build_modflow_deck(
        workdir=tmp_path,
        archetype="gwe_thermal",
        gwe_mode="injection_plume",
        duration_days=120.0,
        injection_temperature_c=40.0,
        injection_rate_m3_day=250.0,
        **BASE,
    )


def test_gwe_deck_writes_gwe_and_exchange(plume_deck, tmp_path):
    assert isinstance(plume_deck, DeckManifest)
    assert plume_deck.archetype == "gwe_thermal"
    assert plume_deck.gwe_present is True
    assert plume_deck.gwe_mode == "injection_plume"
    assert plume_deck.gwt_present is True
    assert plume_deck.thermal_defaults_are_demo is True
    assert plume_deck.injection_temperature_c == 40.0
    assert plume_deck.ambient_temperature_c == GWE_AMBIENT_TEMPERATURE_C
    written = set(plume_deck.files)
    # GWE model packages + the GWF6-GWE6 exchange must be on disk.
    for suffix in (".dis", ".ic", ".adv", ".cnd", ".est", ".ssm", ".oc"):
        assert any(f.endswith(f"gwe_model{suffix}") for f in written), suffix
    assert any("gwfgwe" in f and f.endswith(".exg") for f in written)
    assert any(f.endswith("gwf_model.wel") for f in written)


def test_gwe_ates_periods_and_cycles(tmp_path):
    dk = build_modflow_deck(
        workdir=tmp_path,
        archetype="gwe_thermal",
        gwe_mode="ates",
        duration_days=720.0,
        n_cycles=3,
        injection_temperature_c=50.0,
        **BASE,
    )
    assert dk.gwe_mode == "ates"
    assert dk.n_cycles == 3
    # 1 steady spin-up + 3 x (inject + extract) = 7 periods.
    assert dk.n_stress_periods == 7
    assert dk.injection_periods == 3
    assert dk.recovery_periods == 3


def test_gwe_unknown_mode_rejected(tmp_path):
    with pytest.raises(ValueError, match="gwe_mode"):
        build_modflow_deck(
            workdir=tmp_path,
            archetype="gwe_thermal",
            gwe_mode="not_a_mode",
            duration_days=30.0,
            **BASE,
        )


def test_gwe_ates_requires_cycles(tmp_path):
    with pytest.raises(ValueError, match="n_cycles"):
        build_modflow_deck(
            workdir=tmp_path,
            archetype="gwe_thermal",
            gwe_mode="ates",
            duration_days=30.0,
            **BASE,
        )


# --------------------------------------------------------------------------- #
# Live physics (needs mf6): monotone thermal-plume heat content vs injection dT.
# The plume heat content = sum over cells of the temperature excess above
# ambient (a continuous physical measure; the warm-CELL count quantizes on the
# coarse 50 m production grid, so the integral is the robust monotone metric --
# the sandbox at 10 m proves the discrete warm-cell-extent form as well).
# --------------------------------------------------------------------------- #
def _run_and_heat_content(ws: Path, inject_dT: float) -> float:
    dk = build_modflow_deck(
        workdir=ws,
        archetype="gwe_thermal",
        gwe_mode="injection_plume",
        duration_days=120.0,
        injection_temperature_c=GWE_AMBIENT_TEMPERATURE_C + inject_dT,
        injection_rate_m3_day=400.0,
        **BASE,
    )
    r = subprocess.run([MF6_BIN], cwd=ws, capture_output=True, text=True)
    assert r.returncode == 0, f"mf6 rc={r.returncode}\n{r.stdout[-2000:]}"
    fp = flopy.utils.HeadFile(str(ws / dk.thermal_ucn_file), text="TEMPERATURE")
    temp = fp.get_alldata()[-1, 0]
    return float(np.clip(temp - GWE_AMBIENT_TEMPERATURE_C, 0, None).sum())


@pytest.mark.skipif(not _mf6_available(), reason="mf6 binary not available")
def test_gwe_plume_heat_content_monotone_in_injection_dT(tmp_path):
    h5 = _run_and_heat_content(tmp_path / "dT5", 5.0)
    h15 = _run_and_heat_content(tmp_path / "dT15", 15.0)
    h30 = _run_and_heat_content(tmp_path / "dT30", 30.0)
    assert h5 < h15 < h30, f"plume heat content not monotone: {h5}, {h15}, {h30}"


@pytest.mark.skipif(not _mf6_available(), reason="mf6 binary not available")
def test_gwe_ates_recovery_efficiency_bounded_and_rising(tmp_path):
    def eff(n_cycles: int) -> float:
        ws = tmp_path / f"ates{n_cycles}"
        dk = build_modflow_deck(
            workdir=ws,
            archetype="gwe_thermal",
            gwe_mode="ates",
            duration_days=360.0 * n_cycles,
            n_cycles=n_cycles,
            injection_temperature_c=GWE_AMBIENT_TEMPERATURE_C + 40.0,
            injection_rate_m3_day=300.0,
            **BASE,
        )
        r = subprocess.run([MF6_BIN], cwd=ws, capture_output=True, text=True)
        assert r.returncode == 0, f"mf6 rc={r.returncode}\n{r.stdout[-2000:]}"
        fp = flopy.utils.HeadFile(str(ws / dk.thermal_ucn_file), text="TEMPERATURE")
        kk = fp.get_kstpkper()
        last_extract_period = dk.n_stress_periods - 1  # 0-based last period = extract
        prod = [fp.get_data(kstpkper=k)[0, dk.well_row, dk.well_col]
                for k in kk if k[1] == last_extract_period]
        amb = GWE_AMBIENT_TEMPERATURE_C
        return (float(np.mean(prod)) - amb) / (dk.injection_temperature_c - amb)

    e1, e2, e3 = eff(1), eff(2), eff(3)
    assert 0.0 < e1 < 1.0, f"single-cycle efficiency out of (0,1): {e1}"
    assert e1 < e2 < e3, f"recovery efficiency not rising: {e1}, {e2}, {e3}"
