"""Unit + live tests for the MODFLOW 6 UZF+UZT vadose_transport deck (ADR 0228).

These assert the *deck construction* contract for the ``vadose_transport``
archetype (a DUAL-model GWF+GWT sim: a UZF unsaturated-flow ivertcon column
coupled to a UZT transport package keyed to the UZF flows), and the headline
PHYSICS: the tracer arrival time at the water table increases MONOTONICALLY with
the vadose-column thickness (depth to water table). The pure-shape assertions
need NO mf6 binary; the monotone-arrival assertion runs the local mf6 6.7.0
binary and is skipped when it is absent (mirrors fixtures/uzt_smoke, the proven
deck this productionizes).

Run:
    TRID3NT_MODFLOW_LOCAL=1 <agent-venv>/bin/python -m pytest \
        services/workers/modflow/test_gwt_adapter_vadose_transport.py -q
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gwt_adapter import (  # noqa: E402
    DEFAULT_VADOSE_THTS,
    DeckManifest,
    build_modflow_deck,
)

# A Corn-Belt ag setting (Tippecanoe County, Indiana) -- a natural place, no bbox.
LAT0, LON0 = 40.42, -86.90
BASE = dict(
    spill_location_latlon=(LAT0, LON0),
    contaminant="nitrate",
    release_rate_kg_s=1.0,
    duration_days=1.0,
    aquifer_k_ms=1e-4,
    porosity=0.3,
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
# Pure deck construction (no mf6): UZF + UZT present, dual model, flow-package.
# --------------------------------------------------------------------------- #


@pytest.fixture()
def vadose_deck(tmp_path):
    return build_modflow_deck(
        workdir=tmp_path,
        archetype="vadose_transport",
        vadose_thickness_m=4.0,
        **BASE,
    )


def test_vadose_deck_writes_uzf_and_uzt(vadose_deck, tmp_path):
    assert isinstance(vadose_deck, DeckManifest)
    assert vadose_deck.archetype == "vadose_transport"
    assert vadose_deck.vadose_present is True
    assert vadose_deck.gwt_present is True
    # 4 m thickness / 1 m cells = 4 unsaturated UZF cells.
    assert vadose_deck.vadose_n_layers == 4
    assert vadose_deck.vadose_thickness_m == pytest.approx(4.0)
    # A UZF unsaturated-flow package + a UZT transport package were written.
    assert (tmp_path / "gwf_model.uzf").is_file(), "UZF package not written"
    uzt_files = list(tmp_path.glob("*.uzt"))
    assert uzt_files, "UZT package not written"


def test_vadose_deck_is_dual_model_gwf_and_gwt(vadose_deck, tmp_path):
    """A DUAL-model sim: separate GWF + GWT name files + a GWF6-GWT6 exchange."""
    assert (tmp_path / "gwf_model.nam").is_file()
    assert (tmp_path / "gwt_model.nam").is_file()
    nam = (tmp_path / (vadose_deck.sim_name + ".nam")).read_text().lower()
    assert "gwf6-gwt6" in nam, "GWF-GWT exchange not registered in the sim namefile"


def test_uzt_keyed_to_uzf_flow_package(vadose_deck, tmp_path):
    """The UZT package names the UZF package via flow_package_name='uzf'."""
    uzt_txt = next(tmp_path.glob("*.uzt")).read_text().lower()
    assert "flow_package_name" in uzt_txt
    assert "uzf" in uzt_txt


def test_brooks_corey_defaults_are_labeled_on_manifest(vadose_deck):
    """Brooks-Corey thtr/thts/eps are echoed on the manifest (demo defaults)."""
    assert vadose_deck.vadose_thtr == pytest.approx(0.05)
    assert vadose_deck.vadose_thts == pytest.approx(DEFAULT_VADOSE_THTS)
    assert vadose_deck.vadose_eps == pytest.approx(4.0)
    assert vadose_deck.vadose_infiltration_conc == pytest.approx(1.0)


def test_thts_must_exceed_thtr(tmp_path):
    with pytest.raises(ValueError, match="must exceed"):
        build_modflow_deck(
            workdir=tmp_path, archetype="vadose_transport",
            vadose_thtr=0.4, vadose_thts=0.35, **BASE,
        )


# --------------------------------------------------------------------------- #
# Live physics: arrival time is MONOTONE in vadose thickness (the smoke's law).
# --------------------------------------------------------------------------- #


def _run_and_arrival(ws, thickness):
    dm = build_modflow_deck(
        workdir=ws, archetype="vadose_transport",
        vadose_thickness_m=thickness, **BASE,
    )
    r = subprocess.run([MF6_BIN], cwd=ws, capture_output=True, text=True)
    assert r.returncode == 0, f"mf6 rc={r.returncode}\n{r.stdout[-2000:]}"
    obs = np.genfromtxt(os.path.join(ws, dm.vadose_obs_file),
                        delimiter=",", names=True)
    t = np.atleast_1d(obs["time"])
    c = np.atleast_1d(obs["UZBOT"])
    idx = np.where(c >= 0.5 * dm.vadose_infiltration_conc)[0]
    t_arr = float(t[idx[0]]) if len(idx) else float("inf")
    return t_arr, float(c[-1])


@pytest.mark.skipif(not _mf6_available(), reason="mf6 binary not available")
def test_arrival_time_monotone_in_vadose_thickness(tmp_path):
    """PHYSICS: a deeper water table -> a later tracer arrival (purely advective
    UZF+UZT travel, matching modflow6-examples ex-gwt-uzt-2d). The exact assertion
    fixtures/uzt_smoke pins, now through the PRODUCTION deck builder."""
    arrivals = []
    finals = []
    for thickness in (2.0, 4.0, 8.0):
        ws = tmp_path / f"nv{int(thickness)}"
        ws.mkdir()
        ta, cf = _run_and_arrival(str(ws), thickness)
        arrivals.append(ta)
        finals.append(cf)
    assert all(
        arrivals[i] < arrivals[i + 1] for i in range(len(arrivals) - 1)
    ), f"arrival not monotone in vadose thickness: {arrivals}"
    # Purely-advective final breakthrough reaches the full infiltration conc.
    assert all(cf > 0.5 for cf in finals), f"tracer did not break through: {finals}"
