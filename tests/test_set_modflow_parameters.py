"""Unit tests for ``set_modflow_parameters`` (LANE D, no network).

The parent is a tiny, from-scratch MODFLOW 6 simulation (2-layer structured
DIS grid, NPF/STO/RCHA/CHD/OC) built via real ``flopy.mf6`` calls in a
module-scoped fixture (no mf6 BINARY run -- ``write_simulation`` only; the
setter itself never executes mf6). Every setter call passes ``_work_dir`` (a
private test seam) so all writes land under ``tmp_path``, zero network.

Coverage:
1.  ``test_registered`` -- TOOL_REGISTRY entry + metadata.
2.  ``test_k_set_global`` -- op="set" with no ``layer`` touches BOTH layers;
    before/after read back from the WRITTEN child sim.
3.  ``test_k_scale_layer`` -- op="scale" restricted to ``layer=2`` leaves
    layer 1 untouched (layer-scope isolation).
4.  ``test_recharge_set_global``.
5.  ``test_recharge_rejects_layer`` -- recharge has no geologic-layer axis.
6.  ``test_ss_sy_scale_global``.
7a. ``test_out_of_plausible_range_warns`` -- an atypical-but-physical K (1e10,
    above the plausible band) warns (in_range=false) and proceeds.
7b. ``test_meaningless_value_hard_error`` -- K <= 0 raises the typed
    ``BoundsViolation`` (physically meaningless).
8.  ``test_layer_out_of_range_raises``.
9.  ``test_unknown_parameter_raises``.
10. ``test_parent_untouched`` -- copy-on-write proof (parent npf.k
    unchanged after the child write).
11. ``test_manifest_written``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

flopy = pytest.importorskip("flopy")

from trid3nt_server.agent.tools import TOOL_REGISTRY  # noqa: E402
from trid3nt_server.agent.tools.simulation._setter_envelope import (  # noqa: E402
    BoundsViolation,
    SetterInputError,
)
from trid3nt_server.agent.tools.simulation.modflow.set_modflow_parameters.set_modflow_parameters import (  # noqa: E402
    set_modflow_parameters,
)


def _build_parent_sim(ws: Path) -> None:
    """A minimal real 2-layer MODFLOW 6 GWF sim (npf.k=[10, 5], ss=1e-5,
    sy=0.2, recharge=0.001) -- write_simulation only, no mf6 binary run."""
    sim = flopy.mf6.MFSimulation(sim_name="mfsim", sim_ws=str(ws), exe_name="mf6")
    flopy.mf6.ModflowTdis(sim, time_units="DAYS", nper=1, perioddata=[(1.0, 1, 1.0)])
    flopy.mf6.ModflowIms(sim, complexity="SIMPLE")
    gwf = flopy.mf6.ModflowGwf(sim, modelname="gwf_model", save_flows=True)
    nrow, ncol, nlay = 5, 5, 2
    flopy.mf6.ModflowGwfdis(
        gwf, nlay=nlay, nrow=nrow, ncol=ncol, delr=100.0, delc=100.0,
        top=10.0, botm=[5.0, 0.0],
    )
    flopy.mf6.ModflowGwfic(gwf, strt=5.0)
    flopy.mf6.ModflowGwfnpf(gwf, icelltype=1, k=[10.0, 5.0], k33=1.0)
    flopy.mf6.ModflowGwfsto(gwf, iconvert=1, ss=1e-5, sy=0.2, transient={0: True})
    flopy.mf6.ModflowGwfrcha(gwf, recharge=0.001)
    flopy.mf6.ModflowGwfchd(
        gwf, stress_period_data=[[(0, 0, 0), 5.0], [(0, nrow - 1, ncol - 1), 4.0]]
    )
    flopy.mf6.ModflowGwfoc(gwf, head_filerecord="gwf_model.hds", saverecord=[("HEAD", "ALL")])
    sim.write_simulation()


@pytest.fixture()
def parent_sim(tmp_path: Path) -> Path:
    ws = tmp_path / "parent"
    ws.mkdir()
    _build_parent_sim(ws)
    return ws


def test_registered() -> None:
    assert "set_modflow_parameters" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["set_modflow_parameters"]
    assert entry.metadata.cacheable is False
    assert entry.metadata.ttl_class == "live-no-cache"
    assert entry.metadata.read_only_hint is False
    assert entry.metadata.idempotent_hint is False


def test_k_set_global(parent_sim: Path, tmp_path: Path) -> None:
    result = set_modflow_parameters(
        parent_model_uri=str(parent_sim),
        changes=[{"parameter": "k", "op": "set", "value": 20.0}],
        _work_dir=str(tmp_path / "work"),
    )
    assert result["engine"] == "modflow"
    [entry] = result["changes_applied"]
    assert entry["param"] == "k"
    assert entry["scope"] == "global"
    assert entry["before"] == pytest.approx(7.5)  # mean(10*25 + 5*25)/50
    assert entry["after"] == pytest.approx(20.0)
    assert entry["unit"] == "m/day"


def test_k_scale_layer(parent_sim: Path, tmp_path: Path) -> None:
    result = set_modflow_parameters(
        parent_model_uri=str(parent_sim),
        changes=[{"parameter": "k", "op": "scale", "factor": 3.0, "layer": 2}],
        _work_dir=str(tmp_path / "work"),
    )
    [entry] = result["changes_applied"]
    assert entry["scope"] == "layer:2"
    assert entry["before"] == pytest.approx(5.0)
    assert entry["after"] == pytest.approx(15.0)

    child_model_dir = Path(result["child_setup_uri"][len("file://"):]).parent / "model"
    reread = flopy.mf6.MFSimulation.load(sim_ws=str(child_model_dir))
    gwf2 = reread.get_model()
    k_arr = gwf2.get_package("npf").k.array
    assert k_arr[0].mean() == pytest.approx(10.0)  # layer 1 untouched
    assert k_arr[1].mean() == pytest.approx(15.0)


def test_recharge_set_global(parent_sim: Path, tmp_path: Path) -> None:
    result = set_modflow_parameters(
        parent_model_uri=str(parent_sim),
        changes=[{"parameter": "recharge", "op": "set", "value": 0.002}],
        _work_dir=str(tmp_path / "work"),
    )
    [entry] = result["changes_applied"]
    assert entry["before"] == pytest.approx(0.001)
    assert entry["after"] == pytest.approx(0.002)
    assert entry["unit"] == "m/day"


def test_recharge_rejects_layer(parent_sim: Path, tmp_path: Path) -> None:
    with pytest.raises(SetterInputError):
        set_modflow_parameters(
            parent_model_uri=str(parent_sim),
            changes=[{"parameter": "recharge", "op": "set", "value": 0.002, "layer": 1}],
            _work_dir=str(tmp_path / "work"),
        )


def test_ss_sy_scale_global(parent_sim: Path, tmp_path: Path) -> None:
    result = set_modflow_parameters(
        parent_model_uri=str(parent_sim),
        changes=[
            {"parameter": "ss", "op": "scale", "factor": 2.0},
            {"parameter": "sy", "op": "set", "value": 0.15},
        ],
        _work_dir=str(tmp_path / "work"),
    )
    by_param = {c["param"]: c for c in result["changes_applied"]}
    assert by_param["ss"]["before"] == pytest.approx(1e-5)
    assert by_param["ss"]["after"] == pytest.approx(2e-5)
    assert by_param["sy"]["after"] == pytest.approx(0.15)
    assert len(result["plausibility"]) == 2


def test_out_of_plausible_range_warns(parent_sim: Path, tmp_path: Path) -> None:
    # 1e10 m/day is far above the plausible band (1e-9..1e4) but positive/
    # physical: warn-and-proceed (in_range=false), NOT a hard reject.
    result = set_modflow_parameters(
        parent_model_uri=str(parent_sim),
        changes=[{"parameter": "k", "op": "set", "value": 1.0e10}],
        _work_dir=str(tmp_path / "work"),
    )
    [entry] = result["changes_applied"]
    assert entry["after"] == pytest.approx(1.0e10)
    [plaus] = result["plausibility"]
    assert plaus["in_range"] is False
    assert "WARNING" in plaus["note"]


def test_meaningless_value_hard_error(parent_sim: Path, tmp_path: Path) -> None:
    # Hydraulic conductivity <= 0 is physically meaningless -> hard error.
    with pytest.raises(BoundsViolation) as excinfo:
        set_modflow_parameters(
            parent_model_uri=str(parent_sim),
            changes=[{"parameter": "k", "op": "set", "value": -1.0}],
            _work_dir=str(tmp_path / "work"),
        )
    assert excinfo.value.error_code == "PARAM_BOUNDS_VIOLATION"
    assert excinfo.value.retryable is False


def test_layer_out_of_range_raises(parent_sim: Path, tmp_path: Path) -> None:
    with pytest.raises(SetterInputError):
        set_modflow_parameters(
            parent_model_uri=str(parent_sim),
            changes=[{"parameter": "k", "op": "set", "value": 5.0, "layer": 5}],
            _work_dir=str(tmp_path / "work"),
        )


def test_unknown_parameter_raises(parent_sim: Path, tmp_path: Path) -> None:
    with pytest.raises(SetterInputError):
        set_modflow_parameters(
            parent_model_uri=str(parent_sim),
            changes=[{"parameter": "not_a_real_param", "op": "set", "value": 1.0}],
            _work_dir=str(tmp_path / "work"),
        )


def test_parent_untouched(parent_sim: Path, tmp_path: Path) -> None:
    set_modflow_parameters(
        parent_model_uri=str(parent_sim),
        changes=[{"parameter": "k", "op": "set", "value": 999.0}],
        _work_dir=str(tmp_path / "work"),
    )
    reread_parent = flopy.mf6.MFSimulation.load(sim_ws=str(parent_sim))
    k_arr = reread_parent.get_model().get_package("npf").k.array
    assert k_arr[0].mean() == pytest.approx(10.0)
    assert k_arr[1].mean() == pytest.approx(5.0)


def test_manifest_written(parent_sim: Path, tmp_path: Path) -> None:
    result = set_modflow_parameters(
        parent_model_uri=str(parent_sim),
        changes=[{"parameter": "k", "op": "set", "value": 12.0}],
        _work_dir=str(tmp_path / "work"),
    )
    manifest_path = Path(result["child_setup_uri"][len("file://"):])
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text())
    assert manifest["engine"] == "modflow"
    assert manifest["parent_model"] == str(parent_sim)


def _resolve_mf6_binary() -> str | None:
    """Resolve the local mf6 binary the same way the modflow backend does
    (``$TRID3NT_MF6_BIN`` first -- see workflows/run_modflow._mf6_binary),
    falling back to the repo's committed ``bin/mf6`` and finally ``mf6`` on
    PATH. Returns an executable path, or None so the solves-check SKIPS when no
    mf6 is available (keeps the suite offline + portable)."""
    cand = os.environ.get("TRID3NT_MF6_BIN")
    if cand and Path(cand).is_file() and os.access(cand, os.X_OK):
        return cand
    repo_bin = Path(__file__).resolve().parents[1] / "bin" / "mf6"
    if repo_bin.is_file() and os.access(repo_bin, os.X_OK):
        return str(repo_bin)
    return shutil.which("mf6")


def _build_solvable_parent_sim(ws: Path, exe: str) -> None:
    """The same minimal 2-layer GWF sim as ``_build_parent_sim`` but with CHD
    heads INSIDE the active-cell range so it runs to Normal termination under a
    real mf6. (``_build_parent_sim`` places a CHD head of 4.0 below the layer-1
    bottom of 5.0 -- fine for its ``write_simulation``-only tests, but mf6
    rejects it at runtime, so it cannot serve a solves-check.)"""
    sim = flopy.mf6.MFSimulation(sim_name="mfsim", sim_ws=str(ws), exe_name=exe)
    flopy.mf6.ModflowTdis(sim, time_units="DAYS", nper=1, perioddata=[(1.0, 1, 1.0)])
    flopy.mf6.ModflowIms(sim, complexity="SIMPLE")
    gwf = flopy.mf6.ModflowGwf(sim, modelname="gwf_model", save_flows=True)
    nrow, ncol, nlay = 5, 5, 2
    flopy.mf6.ModflowGwfdis(
        gwf, nlay=nlay, nrow=nrow, ncol=ncol, delr=100.0, delc=100.0,
        top=10.0, botm=[5.0, 0.0],
    )
    flopy.mf6.ModflowGwfic(gwf, strt=8.0)
    flopy.mf6.ModflowGwfnpf(gwf, icelltype=1, k=[10.0, 5.0], k33=1.0)
    flopy.mf6.ModflowGwfsto(gwf, iconvert=1, ss=1e-5, sy=0.2, transient={0: True})
    flopy.mf6.ModflowGwfrcha(gwf, recharge=0.001)
    # Layer-1 upgradient head 8.0 (>= botm 5.0) + layer-2 downgradient head 2.0
    # (>= botm 0.0): both inside their cells, so mf6 solves to completion.
    flopy.mf6.ModflowGwfchd(
        gwf, stress_period_data=[[(0, 0, 0), 8.0], [(1, nrow - 1, ncol - 1), 2.0]]
    )
    flopy.mf6.ModflowGwfoc(gwf, head_filerecord="gwf_model.hds", saverecord=[("HEAD", "ALL")])
    sim.write_simulation()


def test_child_sim_solves_through_mf6(tmp_path: Path) -> None:
    """Solves-check (child deck must SOLVE, not merely round-trip through
    flopy): the child ``sim_ws`` a setter change produces runs to Normal
    termination under the real mf6 6.5.0 binary, exactly like its parent.
    Guards the group-D 'child deck must solve' regression class -- the sibling
    SFINCS setter once shipped an unsolvable child deck (a mangled sfincs.inp
    CRS line); this proves set_modflow_parameters does not have that defect
    class. Offline: uses the committed local mf6 binary, zero network; skips if
    no mf6 is available."""
    exe = _resolve_mf6_binary()
    if exe is None:
        pytest.skip("no mf6 binary available (set TRID3NT_MF6_BIN or provide bin/mf6)")

    def _solves(ws: Path) -> None:
        proc = subprocess.run(
            [exe], cwd=str(ws), capture_output=True, text=True, timeout=120
        )
        combined = proc.stdout + proc.stderr
        assert proc.returncode == 0 and "Normal termination" in combined, (
            f"mf6 did not terminate normally in {ws}: rc={proc.returncode}\n"
            f"{combined[-1500:]}"
        )

    parent_ws = tmp_path / "parent"
    parent_ws.mkdir()
    _build_solvable_parent_sim(parent_ws, exe)
    _solves(parent_ws)  # sanity: the parent is solvable to begin with

    result = set_modflow_parameters(
        parent_model_uri=str(parent_ws),
        changes=[
            {"parameter": "k", "op": "scale", "factor": 0.85},
            {"parameter": "sy", "op": "set", "value": 0.15},
        ],
        _work_dir=str(tmp_path / "work"),
    )
    child_ws = Path(result["child_setup_uri"][len("file://"):]).parent / "model"
    _solves(child_ws)  # the real assertion: the child sim solves under mf6
