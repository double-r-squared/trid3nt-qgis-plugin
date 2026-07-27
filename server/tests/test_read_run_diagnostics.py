"""Lane A tests: ``read_run_diagnostics`` dispatcher + per-engine parsers.

Offline-first (ZERO network): every case reads committed fixtures under
``fixtures/validation/<engine>/`` via the private ``_run_dir`` seam, plus a
dict-backed FakeS3 for the ONE production-path (S3 resolution) case. Fixtures
were captured once from MinIO / trimmed local listings (build-contract 5.1);
the tests never reach out.

ASCII only.
"""

from __future__ import annotations

import io
import json
import os

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.simulation.solver import solver
from trid3nt_server.tools.simulation import diagnostics as _diag
from trid3nt_server.tools.simulation.diagnostics import (
    DiagnosticsArtifactMissing,
    DiagnosticsEngineUnknown,
    DiagnosticsParseError,
    DiagnosticsRunNotFound,
    RunHandleUnresolved,
    read_run_diagnostics,
)

_resolve_run_handle = _diag._resolve_run_handle

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "validation")

_SFINCS_RID = "01KWRSECZGJEYBD44X6T0GRTT9"
_GEOCLAW_RID = "01KWT8BJ7QET79PTENW5XC8WAT"
_TELEMAC_FAIL_RID = "01KXHE0B8V025C9DRZ0B180HHT"
_TELEMAC_OK_RID = "01KXHE0B8V025C9DRZ0B180OK0"
_SWMM_RID = "01KY8FQ0ZJPBPXWP8KX7R0KV3F"
_MODFLOW_RID = "01KY3KWGTXN7V2BDFS65JHQHKE"

_ENVELOPE_KEYS = {
    "engine",
    "run_id",
    "status",
    "healthy",
    "mass_balance_pct",
    "mass_balance_source",
    "instability",
    "nonconverged_pct",
    "dry_cells",
    "warnings",
    "engine_specific",
    "sources",
    "notes",
}


def _run(engine_dir: str, rid: str) -> dict:
    return read_run_diagnostics(rid, _run_dir=os.path.join(FIX, engine_dir))


# --------------------------------------------------------------------------- #
# Registration + envelope schema.
# --------------------------------------------------------------------------- #


def test_tool_is_registered_with_expected_metadata():
    assert "read_run_diagnostics" in TOOL_REGISTRY
    meta = TOOL_REGISTRY["read_run_diagnostics"].metadata
    assert meta.cacheable is False
    assert meta.ttl_class == "live-no-cache"


@pytest.mark.parametrize(
    "engine_dir,rid",
    [
        ("sfincs", _SFINCS_RID),
        ("geoclaw", _GEOCLAW_RID),
        ("telemac", _TELEMAC_FAIL_RID),
        ("telemac_ok", _TELEMAC_OK_RID),
        ("swmm", _SWMM_RID),
        ("modflow", _MODFLOW_RID),
    ],
)
def test_envelope_shape_is_complete_for_every_engine(engine_dir, rid):
    env = _run(engine_dir, rid)
    assert set(env) == _ENVELOPE_KEYS
    assert env["run_id"] == rid
    assert isinstance(env["warnings"], list)
    assert isinstance(env["engine_specific"], dict)
    assert isinstance(env["notes"], list)
    assert set(env["sources"]) == {"completion_json", "diagnostics_files"}
    # Honesty: a reported mass-balance source only when a value is present.
    if env["mass_balance_pct"] is None:
        assert env["mass_balance_source"] is None
    else:
        assert env["mass_balance_source"] in ("reported", "derived")


# --------------------------------------------------------------------------- #
# Handle resolution.
# --------------------------------------------------------------------------- #


def test_resolve_bare_ulid():
    assert _resolve_run_handle(_SFINCS_RID) == (None, _SFINCS_RID)


def test_resolve_s3_run_prefix():
    handle = f"s3://trid3nt-runs/{_SFINCS_RID}/"
    assert _resolve_run_handle(handle) == ("trid3nt-runs", _SFINCS_RID)


def test_resolve_s3_object_uri_beneath_prefix():
    handle = f"s3://trid3nt-runs/{_SFINCS_RID}/flood_depth_peak.tif"
    assert _resolve_run_handle(handle) == ("trid3nt-runs", _SFINCS_RID)


def test_resolve_rejects_non_ulid_handle():
    with pytest.raises(RunHandleUnresolved):
        _resolve_run_handle("not-a-run-handle")


def test_resolve_rejects_empty_handle():
    with pytest.raises(RunHandleUnresolved):
        _resolve_run_handle("")


# --------------------------------------------------------------------------- #
# SFINCS: derived mass balance + timing + honesty of the null path.
# --------------------------------------------------------------------------- #


def test_sfincs_derived_mass_balance_and_timing():
    env = _run("sfincs", _SFINCS_RID)
    assert env["engine"] == "sfincs"
    assert env["status"] == "ok"
    assert env["mass_balance_source"] == "derived"
    assert env["mass_balance_pct"] == pytest.approx(0.05, abs=1e-6)
    es = env["engine_specific"]
    assert es["finished"] is True
    assert es["avg_timestep_s"] == pytest.approx(4.689)
    assert es["runtime_s"] == pytest.approx(0.512)
    assert es["max_water_depth_m"] == pytest.approx(0.35)
    assert es["cuminf_m3"] == pytest.approx(0.0)
    assert env["healthy"] is True
    # A "derived" provenance note is required by the honesty floor.
    assert any("DERIVED" in n for n in env["notes"])


def test_sfincs_legacy_engine_recovery_from_stdout_field(tmp_path):
    # The real SFINCS completion.json has NO "solver" field -> engine must be
    # recovered from the "sfincs_stdout_uri" field name (legacy fallback).
    completion = json.load(open(os.path.join(FIX, "sfincs", "completion.json")))
    assert "solver" not in completion
    env = _run("sfincs", _SFINCS_RID)
    assert env["engine"] == "sfincs"


def test_sfincs_postfix_solver_field_recovery(tmp_path):
    # With the solver.py fix, completion.json carries "solver"; it must win.
    src = os.path.join(FIX, "sfincs")
    completion = json.load(open(os.path.join(src, "completion.json")))
    completion["solver"] = "sfincs"
    d = tmp_path / "run"
    d.mkdir()
    (d / "completion.json").write_text(json.dumps(completion))
    (d / "sfincs_map.nc").write_bytes(
        open(os.path.join(src, "sfincs_map.nc"), "rb").read()
    )
    (d / "sfincs.stdout").write_bytes(
        open(os.path.join(src, "sfincs.stdout"), "rb").read()
    )
    env = read_run_diagnostics(_SFINCS_RID, _run_dir=str(d))
    assert env["engine"] == "sfincs"
    assert env["mass_balance_source"] == "derived"


def test_sfincs_null_mass_balance_when_no_cumprcp(tmp_path):
    # A map file lacking cumprcp -> mass_balance null (never fabricated).
    import netCDF4
    import numpy as np

    src = os.path.join(FIX, "sfincs")
    completion = json.load(open(os.path.join(src, "completion.json")))
    d = tmp_path / "run"
    d.mkdir()
    (d / "completion.json").write_text(json.dumps(completion))
    (d / "sfincs.stdout").write_bytes(
        open(os.path.join(src, "sfincs.stdout"), "rb").read()
    )
    nc = str(d / "sfincs_map.nc")
    ds = netCDF4.Dataset(nc, "w", format="NETCDF4")
    ds.createDimension("n", 4)
    ds.createDimension("m", 4)
    ds.createDimension("time", 2)
    ds.createVariable("msk", "i4", ("n", "m"))[:] = np.ones((4, 4), "i4")
    h = ds.createVariable("h", "f8", ("time", "n", "m"))
    h[0] = np.zeros((4, 4))
    h[1] = np.full((4, 4), 0.1)
    ds.createVariable("hmax", "f8", ("n", "m"))[:] = np.full((4, 4), 0.2)
    ds.createVariable("status", "i4", ())[...] = 0
    ds.close()
    env = read_run_diagnostics(_SFINCS_RID, _run_dir=str(d))
    assert env["mass_balance_pct"] is None
    assert env["mass_balance_source"] is None
    assert any("cumulative precipitation" in n for n in env["notes"])
    # Still healthy: status ok + finished + no cumprcp is not a failure.
    assert env["healthy"] is True


# --------------------------------------------------------------------------- #
# SWMM: continuity + instability + node summaries.
# --------------------------------------------------------------------------- #


def test_swmm_continuity_and_instability():
    env = _run("swmm", _SWMM_RID)
    assert env["engine"] == "swmm"
    assert env["mass_balance_source"] == "reported"
    assert env["mass_balance_pct"] == pytest.approx(-0.018)
    es = env["engine_specific"]
    assert es["runoff_continuity_pct"] == pytest.approx(-0.135)
    assert es["flow_routing_continuity_pct"] == pytest.approx(-0.018)
    assert es["max_flow_instability_index"] == 3
    assert es["flooded_nodes"] == 0
    assert es["surcharged_nodes"] == 0
    assert env["nonconverged_pct"] == pytest.approx(0.0)
    assert env["healthy"] is True


def test_swmm_counts_populated_node_flooding(tmp_path):
    # Synthetic populated Node Flooding Summary -> non-zero flooded count.
    rpt = (
        "  Runoff Quantity Continuity     hectare-m            mm\n"
        "  **************************     ---------       -------\n"
        "  Continuity Error (%) .....        -0.1\n\n"
        "  **************************        Volume        Volume\n"
        "  Flow Routing Continuity        hectare-m      10^6 ltr\n"
        "  **************************     ---------     ---------\n"
        "  Flooding Loss ............         1.234         5.678\n"
        "  Continuity Error (%) .....        -0.5\n\n"
        "  *********************\n"
        "  Node Flooding Summary\n"
        "  *********************\n"
        "  ---------------------------------------\n"
        "  Node                 Hours   Volume\n"
        "  ---------------------------------------\n"
        "  J1                    0.50    1.20\n"
        "  J2                    0.10    0.30\n\n"
        "  **********************\n"
        "  Storage Volume Summary\n"
        "  **********************\n"
    )
    d = tmp_path / "run"
    d.mkdir()
    (d / "mesh.rpt").write_text(rpt)
    (d / "completion.json").write_text(
        json.dumps(
            {
                "run_id": _SWMM_RID,
                "status": "ok",
                "swmm_stdout_uri": f"s3://b/{_SWMM_RID}/swmm.stdout",
                "output_uris": [f"s3://b/{_SWMM_RID}/mesh.rpt"],
            }
        )
    )
    env = read_run_diagnostics(_SWMM_RID, _run_dir=str(d))
    assert env["engine_specific"]["flooded_nodes"] == 2
    assert env["engine_specific"]["flood_volume"] == pytest.approx(5.678)


# --------------------------------------------------------------------------- #
# MODFLOW: budget discrepancy + convergence + dry cells.
# --------------------------------------------------------------------------- #


def test_modflow_budget_and_convergence():
    env = _run("modflow", _MODFLOW_RID)
    assert env["engine"] == "modflow"
    assert env["mass_balance_source"] == "reported"
    assert env["mass_balance_pct"] == pytest.approx(0.0, abs=1e-9)
    es = env["engine_specific"]
    assert es["converged"] is True
    assert es["nonconverged_steps"] == 0
    assert es["dry_cells"] == 0
    assert es["per_model"] and es["per_model"][0]["model"] == "gwf_model.lst"
    assert env["nonconverged_pct"] == pytest.approx(0.0)
    assert env["healthy"] is True


def test_modflow_nonconvergence_and_dry_cells(tmp_path):
    # Synthetic: 1 failed step of 3, plus explicit dry-cell notices. Also omits
    # the "solver" field so the mf6_stdout_uri engine-recovery path is exercised.
    mfsim = (
        " Solving:  Stress period:     1    Time step:     1\n"
        " Solving:  Stress period:     1    Time step:     2\n"
        " Solving:  Stress period:     2    Time step:     1\n"
        " ****FAILED TO MEET SOLVER CONVERGENCE CRITERIA IN TIME STEP 1\n"
    )
    gwf = (
        "  VOLUME BUDGET FOR ENTIRE MODEL AT END OF TIME STEP 1, STRESS PERIOD 1\n"
        "            IN - OUT =      -1.2E-02\n"
        " PERCENT DISCREPANCY =           0.03     PERCENT DISCREPANCY =    0.03\n"
        "  CELL (1,5,10) BECAME DRY\n"
        "  CELL (2,3,4) IS DRY\n"
    )
    d = tmp_path / "run"
    d.mkdir()
    (d / "mfsim.lst").write_text(mfsim)
    (d / "gwf_model.lst").write_text(gwf)
    (d / "completion.json").write_text(
        json.dumps(
            {
                "run_id": _MODFLOW_RID,
                "status": "ok",
                "mf6_stdout_uri": f"s3://b/{_MODFLOW_RID}/mf6.stdout",
                "output_uris": [
                    f"s3://b/{_MODFLOW_RID}/mfsim.lst",
                    f"s3://b/{_MODFLOW_RID}/gwf_model.lst",
                ],
            }
        )
    )
    env = read_run_diagnostics(_MODFLOW_RID, _run_dir=str(d))
    assert env["engine"] == "modflow"  # recovered from mf6_stdout_uri
    es = env["engine_specific"]
    assert es["nonconverged_steps"] == 1
    assert env["nonconverged_pct"] == pytest.approx(100.0 / 3.0, abs=0.01)
    assert es["converged"] is False
    assert es["dry_cells"] == 2
    assert env["mass_balance_pct"] == pytest.approx(0.03)
    assert env["healthy"] is False
    assert any("convergence" in w.lower() for w in env["warnings"])
    assert any("dry-cell" in w for w in env["warnings"])


# --------------------------------------------------------------------------- #
# GeoClaw: mass conservation signal + null final mass + Courant warnings.
# --------------------------------------------------------------------------- #


def test_geoclaw_mass_signal_and_null_final():
    env = _run("geoclaw", _GEOCLAW_RID)
    assert env["engine"] == "geoclaw"
    es = env["engine_specific"]
    assert es["mass_initial"] == pytest.approx(46341211214.59091)
    # Only an initial mass is reported -> final / ratio / mass_balance null.
    assert es["mass_final"] is None
    assert es["mass_ratio"] is None
    assert env["mass_balance_pct"] is None
    assert es["n_gauges"] == 1
    assert es["n_frames"] == 7
    assert env["instability"] == 4  # Courant exceedances
    assert any("Courant" in w for w in env["warnings"])
    assert env["healthy"] is True


# --------------------------------------------------------------------------- #
# TELEMAC: failed run (negative) + healthy run.
# --------------------------------------------------------------------------- #


def test_telemac_failed_run_is_unhealthy_and_mass_balance_null():
    env = _run("telemac", _TELEMAC_FAIL_RID)
    assert env["engine"] == "telemac"
    assert env["status"] == "error"
    assert env["engine_specific"]["correct_end"] is False
    assert env["engine_specific"]["npoin"] == 1711
    # Crashed listing has no balance line -> null, never fabricated.
    assert env["mass_balance_pct"] is None
    assert env["mass_balance_source"] is None
    assert env["healthy"] is False
    assert any("CORRECT END OF RUN" in w for w in env["warnings"])


def test_telemac_healthy_run_reports_listing_mass_balance():
    env = _run("telemac_ok", _TELEMAC_OK_RID)
    assert env["engine"] == "telemac"  # solver "telemac_river_dye" normalized
    assert env["status"] == "ok"
    assert env["engine_specific"]["correct_end"] is True
    assert env["mass_balance_source"] == "reported"
    assert env["mass_balance_pct"] == pytest.approx(0.004213, abs=1e-6)
    assert env["healthy"] is True


# --------------------------------------------------------------------------- #
# Typed errors (honesty floor: never a fabricated healthy envelope).
# --------------------------------------------------------------------------- #


def test_run_not_found_when_no_completion(tmp_path):
    with pytest.raises(DiagnosticsRunNotFound):
        read_run_diagnostics(_SFINCS_RID, _run_dir=str(tmp_path))


def test_artifact_missing_when_diagnostics_file_absent(tmp_path):
    # completion.json present (points at mesh.rpt) but the .rpt is not there.
    completion = json.load(open(os.path.join(FIX, "swmm", "completion.json")))
    (tmp_path / "completion.json").write_text(json.dumps(completion))
    with pytest.raises(DiagnosticsArtifactMissing):
        read_run_diagnostics(_SWMM_RID, _run_dir=str(tmp_path))


def test_engine_unknown_for_unsupported_solver(tmp_path):
    (tmp_path / "completion.json").write_text(
        json.dumps(
            {"run_id": _SWMM_RID, "status": "ok", "solver": "landlab",
             "landlab_stdout_uri": "s3://b/x/landlab.stdout", "output_uris": []}
        )
    )
    with pytest.raises(DiagnosticsEngineUnknown):
        read_run_diagnostics(_SWMM_RID, _run_dir=str(tmp_path))


def test_parse_error_on_garbage_diagnostics(tmp_path):
    (tmp_path / "mesh.rpt").write_text("not a swmm report at all\n")
    (tmp_path / "completion.json").write_text(
        json.dumps(
            {"run_id": _SWMM_RID, "status": "ok",
             "swmm_stdout_uri": "s3://b/x/swmm.stdout",
             "output_uris": ["s3://b/x/mesh.rpt"]}
        )
    )
    with pytest.raises(DiagnosticsParseError):
        read_run_diagnostics(_SWMM_RID, _run_dir=str(tmp_path))


def test_handle_unresolved_raises_typed():
    with pytest.raises(RunHandleUnresolved):
        read_run_diagnostics("garbage-not-a-ulid")


# --------------------------------------------------------------------------- #
# Production path: resolve an s3 handle + read via the solver S3 seam (FakeS3).
# --------------------------------------------------------------------------- #


class _FakeS3:
    """Dict-backed boto3-shaped S3 fake (kickoff-sanctioned dict seam)."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    def get_object(self, Bucket, Key):  # noqa: N803
        if (Bucket, Key) not in self.objects:
            from botocore.exceptions import ClientError

            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": Key}}, "GetObject"
            )
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def put_object(self, Bucket, Key, Body, **_kw):  # noqa: N803
        data = Body.read() if hasattr(Body, "read") else bytes(Body)
        self.objects[(Bucket, Key)] = data
        return {}


@pytest.fixture
def _reset_solver_seams():
    yield
    solver.set_s3_client(None)
    solver.set_runs_bucket(None)


def test_production_s3_resolution_reads_via_solver_seam(_reset_solver_seams):
    bucket = "trid3nt-runs"
    fake = _FakeS3()
    swmm_dir = os.path.join(FIX, "swmm")
    completion = json.load(open(os.path.join(swmm_dir, "completion.json")))
    rpt = open(os.path.join(swmm_dir, "mesh.rpt"), "rb").read()
    fake.objects[(bucket, f"{_SWMM_RID}/completion.json")] = json.dumps(
        completion
    ).encode()
    fake.objects[(bucket, f"{_SWMM_RID}/mesh.rpt")] = rpt
    solver.set_s3_client(fake)
    solver.set_runs_bucket(bucket)

    # Resolve from an s3 OBJECT uri beneath the run prefix (no _run_dir).
    handle = f"s3://{bucket}/{_SWMM_RID}/mesh.rpt"
    env = read_run_diagnostics(handle)
    assert env["engine"] == "swmm"
    assert env["mass_balance_pct"] == pytest.approx(-0.018)
    assert env["sources"]["completion_json"] == (
        f"s3://{bucket}/{_SWMM_RID}/completion.json"
    )


# --------------------------------------------------------------------------- #
# Solver.py surgical change: completion.json now records the "solver" field.
# --------------------------------------------------------------------------- #


def test_write_local_completion_records_solver_field(_reset_solver_seams):
    fake = _FakeS3()
    solver.set_s3_client(fake)
    solver._write_local_completion(
        fake,
        runs_bucket="trid3nt-runs",
        run_id="01TESTTESTTESTTESTTESTTEST",
        status="ok",
        exit_code=0,
        output_uris=[],
        stdout_uri="s3://trid3nt-runs/x/sfincs.stdout",
        stderr_uri=None,
        started_at="2026-07-24T00:00:00Z",
        error=None,
        extra={"scenario": "tsunami"},
        solver="sfincs",
    )
    key = ("trid3nt-runs", "01TESTTESTTESTTESTTESTTEST/completion.json")
    payload = json.loads(fake.objects[key])
    assert payload["solver"] == "sfincs"
    # extra still folded, and does not clobber solver.
    assert payload["scenario"] == "tsunami"
