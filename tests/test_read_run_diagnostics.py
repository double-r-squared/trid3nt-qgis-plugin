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
from trid3nt_server.workflows.solver import solver
from trid3nt_server.workflows.solver import diagnostics as _diag
from trid3nt_server.workflows.solver.diagnostics import (
    DiagnosticsEngineUnknown,
    DiagnosticsParseError,
    DiagnosticsRunNotFound,
    RunHandleUnresolved,
    read_run_diagnostics,
)

_resolve_run_handle = _diag._resolve_run_handle

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "validation")

_TELEMAC_FAIL_RID = "01KXHE0B8V025C9DRZ0B180HHT"
_TELEMAC_OK_RID = "01KXHE0B8V025C9DRZ0B180OK0"

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
        ("telemac", _TELEMAC_FAIL_RID),
        ("telemac_ok", _TELEMAC_OK_RID),
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
    assert _resolve_run_handle(_TELEMAC_FAIL_RID) == (None, _TELEMAC_FAIL_RID)


def test_resolve_s3_run_prefix():
    handle = f"s3://trid3nt-runs/{_TELEMAC_FAIL_RID}/"
    assert _resolve_run_handle(handle) == ("trid3nt-runs", _TELEMAC_FAIL_RID)


def test_resolve_s3_object_uri_beneath_prefix():
    handle = f"s3://trid3nt-runs/{_TELEMAC_FAIL_RID}/r2d_river.slf"
    assert _resolve_run_handle(handle) == ("trid3nt-runs", _TELEMAC_FAIL_RID)


def test_resolve_rejects_non_ulid_handle():
    with pytest.raises(RunHandleUnresolved):
        _resolve_run_handle("not-a-run-handle")


def test_resolve_rejects_empty_handle():
    with pytest.raises(RunHandleUnresolved):
        _resolve_run_handle("")


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
        read_run_diagnostics(_TELEMAC_FAIL_RID, _run_dir=str(tmp_path))


def test_engine_unknown_for_unsupported_solver(tmp_path):
    (tmp_path / "completion.json").write_text(
        json.dumps(
            {"run_id": _TELEMAC_FAIL_RID, "status": "ok", "solver": "landlab",
             "landlab_stdout_uri": "s3://b/x/landlab.stdout", "output_uris": []}
        )
    )
    with pytest.raises(DiagnosticsEngineUnknown):
        read_run_diagnostics(_TELEMAC_FAIL_RID, _run_dir=str(tmp_path))


def test_parse_error_on_unreadable_completion(tmp_path):
    (tmp_path / "completion.json").write_text("{ not json at all\n")
    with pytest.raises(DiagnosticsParseError):
        read_run_diagnostics(_TELEMAC_FAIL_RID, _run_dir=str(tmp_path))


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
    src = os.path.join(FIX, "telemac")
    completion = json.load(open(os.path.join(src, "completion.json")))
    fake.objects[(bucket, f"{_TELEMAC_FAIL_RID}/completion.json")] = json.dumps(
        completion
    ).encode()
    for name in ("full_listing.log", "telemac_metrics.json"):
        fake.objects[(bucket, f"{_TELEMAC_FAIL_RID}/{name}")] = open(
            os.path.join(src, name), "rb"
        ).read()
    solver.set_s3_client(fake)
    solver.set_runs_bucket(bucket)

    # Resolve from an s3 OBJECT uri beneath the run prefix (no _run_dir).
    handle = f"s3://{bucket}/{_TELEMAC_FAIL_RID}/full_listing.log"
    env = read_run_diagnostics(handle)
    assert env["engine"] == "telemac"
    assert env["sources"]["completion_json"] == (
        f"s3://{bucket}/{_TELEMAC_FAIL_RID}/completion.json"
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
