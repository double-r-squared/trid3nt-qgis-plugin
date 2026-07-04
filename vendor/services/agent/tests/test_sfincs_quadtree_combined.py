"""COMBINED cht_sfincs quadtree deck-build + SFINCS solve SUBMIT path tests.

The combined worker fuses the GPL deck-builder + the MIT SFINCS solve binary into
ONE Batch image: the agent SUBMITS ONE job (it NEVER imports cht_sfincs), the
worker reads ONE build_spec, builds the refined quadtree+SnapWave deck LOCALLY,
runs SFINCS in that local deck dir (no S3 deck round-trip), and writes ONE
solve-schema completion.json (``sfincs_map.nc`` in ``output_uris``). So the agent
collapses the prior two-submit / two-poll / deck-round-trip chain into ONE submit
+ ONE poll against ONE job-def (``GRACE2_AWS_BATCH_JOB_DEF_SFINCS_QUADTREE``).

These tests prove (all boto3 Batch + S3 calls MOCKED via the set_batch_client /
set_s3_client seams — NO real AWS):

1.  submit_sfincs_quadtree submits to the COMBINED image's OWN job-def
    (GRACE2_AWS_BATCH_JOB_DEF_SFINCS_QUADTREE, NOT the deck-build-only NOR the
    solve-only job-def) with --build-spec-uri command + GRACE2_BUILD_SPEC_URI env;
    handle shape (workflow_name=aws-batch, jobId in workflows_execution_id,
    solver=sfincs-quadtree).
2.  run_sfincs_quadtree = SUBMIT + poll S3 completion + return the SOLVE
    RunResult DIRECTLY (status=complete, output_uri = runs prefix carrying
    sfincs_map.nc) — there is NO second run_solver.
3.  INERT until provisioned: a missing combined job-def raises a clean typed
    DeckBuildError naming GRACE2_AWS_BATCH_JOB_DEF_SFINCS_QUADTREE; a non-aws-batch
    backend raises DeckBuildError; a non-success terminal returns a RunResult with
    the failed/cancelled status (honest typed failure, never silent success).
4.  build_spec assembly: the agent-composed combined build_spec carries the v2
    refinement/cell-budget + buildings + rivers blocks AND still passes the
    deck-builder worker's (cht-free) validate_build_spec.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError

from grace2_agent.tools.solver import (
    AWS_BATCH_WORKFLOW_NAME,
    SFINCS_QUADTREE_SOLVER,
    DeckBuildError,
    run_sfincs_quadtree,
    set_batch_client,
    set_emitter_binding,
    set_runs_bucket,
    set_s3_client,
    submit_sfincs_quadtree,
)
from grace2_contracts.execution import ExecutionHandle, RunResult


# --------------------------------------------------------------------------- #
# Fakes (dict-backed; mirror test_sfincs_deckbuild.py)
# --------------------------------------------------------------------------- #


def _no_such_key(key: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": "NoSuchKey", "Message": f"missing {key}"}}, "GetObject"
    )


class FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.put_calls: list[tuple[str, str]] = []

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        if (Bucket, Key) not in self.objects:
            raise _no_such_key(Key)
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def put_object(self, Bucket: str, Key: str, Body: Any, **_kw: Any) -> dict:  # noqa: N803
        data = Body.read() if hasattr(Body, "read") else bytes(Body)
        self.objects[(Bucket, Key)] = data
        self.put_calls.append((Bucket, Key))
        return {}


class FakeBatchClient:
    def __init__(self, *, submit_job_id: str = "quad-job-abc123") -> None:
        self.submit_job_id = submit_job_id
        self.submit_calls: list[dict[str, Any]] = []
        self.describe_calls: list[list[str]] = []
        self.terminate_calls: list[tuple[str, str]] = []
        self.failed_jobs: dict[str, str] = {}

    def submit_job(self, **kwargs: Any) -> dict[str, Any]:
        self.submit_calls.append(kwargs)
        return {"jobId": self.submit_job_id, "jobName": kwargs.get("jobName")}

    def describe_jobs(self, jobs: list[str]) -> dict[str, Any]:  # noqa: N803
        self.describe_calls.append(list(jobs))
        out = []
        for jid in jobs:
            if jid in self.failed_jobs:
                out.append(
                    {
                        "jobId": jid,
                        "status": "FAILED",
                        "statusReason": self.failed_jobs[jid],
                    }
                )
            else:
                out.append({"jobId": jid, "status": "RUNNING"})
        return {"jobs": out}

    def terminate_job(self, jobId: str, reason: str) -> dict[str, Any]:  # noqa: N803
        self.terminate_calls.append((jobId, reason))
        return {}


def _seed_solve_completion(
    s3: FakeS3Client,
    run_id: str,
    *,
    runs_bucket: str,
    status: str = "ok",
) -> None:
    """Seed the COMBINED worker's solve-schema completion.json (sfincs_map.nc in
    output_uris — the load-bearing flood output, identical to the solve worker)."""
    output_uris: list[str] = []
    if status == "ok":
        output_uris = [
            f"s3://{runs_bucket}/{run_id}/sfincs_map.nc",
            f"s3://{runs_bucket}/{run_id}/manifest.json",
        ]
    payload = {
        "run_id": run_id,
        "status": status,
        "exit_code": 0 if status == "ok" else 1,
        "sfincs_stdout_uri": f"s3://{runs_bucket}/{run_id}/sfincs.stdout",
        "sfincs_stderr_uri": f"s3://{runs_bucket}/{run_id}/sfincs.stderr",
        "output_uris": output_uris,
        "started_at": "2026-06-18T00:00:00Z",
        "finished_at": "2026-06-18T00:05:00Z",
        "error": None if status == "ok" else "sfincs exited with non-zero code 1",
    }
    s3.objects[(runs_bucket, f"{run_id}/completion.json")] = json.dumps(payload).encode()


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture()
def reset_seams():
    for setter in (
        set_s3_client,
        set_batch_client,
    ):
        setter(None)
    set_emitter_binding(None)
    set_runs_bucket(None)
    try:
        yield
    finally:
        for setter in (
            set_s3_client,
            set_batch_client,
        ):
            setter(None)
        set_emitter_binding(None)
        set_runs_bucket(None)


@pytest.fixture()
def quadtree_env(monkeypatch: pytest.MonkeyPatch):
    """Fully-provisioned aws-batch + COMBINED quadtree job-def env."""
    monkeypatch.setenv("GRACE2_SOLVER_BACKEND", "aws-batch")
    monkeypatch.setenv("GRACE2_RUNS_BUCKET", "test-runs-bucket")
    monkeypatch.setenv("GRACE2_AWS_BATCH_QUEUE", "grace2-batch-queue")
    monkeypatch.setenv(
        "GRACE2_AWS_BATCH_JOB_DEF_SFINCS_QUADTREE", "grace2-sfincs-quadtree:5"
    )
    # The deck-build-only + solve-only job-defs are DIFFERENT images — present
    # here to prove the combined job does NOT cross-route to either of them.
    monkeypatch.setenv(
        "GRACE2_AWS_BATCH_JOB_DEF_SFINCS_DECKBUILDER", "grace2-sfincs-deckbuilder:3"
    )
    monkeypatch.setenv("GRACE2_AWS_BATCH_JOB_DEF_SFINCS", "grace2-sfincs-solve:9")
    monkeypatch.setenv("AWS_REGION", "us-west-2")


# --------------------------------------------------------------------------- #
# 1. submit_sfincs_quadtree — args + handle shape, OWN combined job-def
# --------------------------------------------------------------------------- #


def test_combined_submit_uses_own_jobdef_and_handle_shape(
    reset_seams, quadtree_env
) -> None:
    batch = FakeBatchClient()
    set_batch_client(batch)

    handle = submit_sfincs_quadtree(
        build_spec_uri="s3://deck-bucket/cache/spec/build_spec.json",
        compute_class="standard",
    )

    assert isinstance(handle, ExecutionHandle)
    assert handle.solver == SFINCS_QUADTREE_SOLVER == "sfincs-quadtree"
    assert handle.workflow_name == AWS_BATCH_WORKFLOW_NAME
    assert handle.workflows_execution_id == "quad-job-abc123"

    assert len(batch.submit_calls) == 1
    call = batch.submit_calls[0]
    assert call["jobQueue"] == "grace2-batch-queue"
    # CRITICAL: routes to the COMBINED image's OWN job-def — NOT the
    # deck-build-only image NOR the solve-only image.
    assert call["jobDefinition"] == "grace2-sfincs-quadtree:5"
    assert call["jobDefinition"] != "grace2-sfincs-deckbuilder:3"
    assert call["jobDefinition"] != "grace2-sfincs-solve:9"

    overrides = call["containerOverrides"]
    cmd = overrides["command"]
    assert "--run-id" in cmd and "--build-spec-uri" in cmd
    assert (
        cmd[cmd.index("--build-spec-uri") + 1]
        == "s3://deck-bucket/cache/spec/build_spec.json"
    )
    assert cmd[cmd.index("--run-id") + 1] == handle.run_id

    env = {e["name"]: e["value"] for e in overrides["environment"]}
    assert env["GRACE2_RUNS_BUCKET"] == "test-runs-bucket"
    assert env["GRACE2_RUN_ID"] == handle.run_id
    assert env["GRACE2_BUILD_SPEC_URI"] == "s3://deck-bucket/cache/spec/build_spec.json"
    assert env["GRACE2_OBJECT_STORE"] == "s3"


# --------------------------------------------------------------------------- #
# 2. run_sfincs_quadtree — ONE submit + ONE poll → the SOLVE RunResult directly
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_combined_full_flow_returns_solve_runresult(
    reset_seams, quadtree_env
) -> None:
    batch = FakeBatchClient()
    s3 = FakeS3Client()
    set_batch_client(batch)
    set_s3_client(s3)

    real_get = s3.get_object

    def get_or_seed(Bucket: str, Key: str):  # noqa: N803
        if Key.endswith("/completion.json") and (Bucket, Key) not in s3.objects:
            run_id = Key.split("/")[0]
            _seed_solve_completion(s3, run_id, runs_bucket=Bucket, status="ok")
        return real_get(Bucket, Key)

    s3.get_object = get_or_seed  # type: ignore[assignment]

    result = await run_sfincs_quadtree(
        "s3://deck-bucket/cache/spec/build_spec.json",
        compute_class="standard",
        poll_interval_s=0,
    )

    # ONE submit (NOT two — the deck-build + solve are fused).
    assert len(batch.submit_calls) == 1
    assert batch.submit_calls[0]["jobDefinition"] == "grace2-sfincs-quadtree:5"

    # The SOLVE RunResult is returned DIRECTLY: complete + the runs prefix
    # (postprocess_flood resolves sfincs_map.nc inside it). No second run_solver.
    assert isinstance(result, RunResult)
    assert result.status == "complete"
    assert result.output_uri == f"s3://test-runs-bucket/{result.run_id}/"


@pytest.mark.asyncio
async def test_combined_failed_solve_returns_failed_runresult(
    reset_seams, quadtree_env
) -> None:
    """A non-zero SFINCS exit → the combined completion.json status='error' →
    run_sfincs_quadtree returns a FAILED RunResult (the caller surfaces it as a
    failed envelope, exactly like a plain solve). Honest typed failure."""
    batch = FakeBatchClient()
    s3 = FakeS3Client()
    set_batch_client(batch)
    set_s3_client(s3)

    real_get = s3.get_object

    def get_or_seed(Bucket: str, Key: str):  # noqa: N803
        if Key.endswith("/completion.json") and (Bucket, Key) not in s3.objects:
            run_id = Key.split("/")[0]
            _seed_solve_completion(s3, run_id, runs_bucket=Bucket, status="error")
        return real_get(Bucket, Key)

    s3.get_object = get_or_seed  # type: ignore[assignment]

    result = await run_sfincs_quadtree(
        "s3://deck-bucket/cache/spec/build_spec.json", poll_interval_s=0
    )
    assert isinstance(result, RunResult)
    assert result.status == "failed"
    assert result.output_uri is None


# --------------------------------------------------------------------------- #
# 3. INERT until provisioned — clean typed DeckBuildError, never a crash
# --------------------------------------------------------------------------- #


def test_combined_inert_when_jobdef_unset(reset_seams, monkeypatch) -> None:
    """No combined job-def (and no generic fallback) → typed error naming the
    canonical env var. Mirrors how SWMM stayed inert until its job-def env."""
    monkeypatch.setenv("GRACE2_SOLVER_BACKEND", "aws-batch")
    monkeypatch.setenv("GRACE2_RUNS_BUCKET", "test-runs-bucket")
    monkeypatch.setenv("GRACE2_AWS_BATCH_QUEUE", "q")
    monkeypatch.delenv("GRACE2_AWS_BATCH_JOB_DEF_SFINCS_QUADTREE", raising=False)
    monkeypatch.delenv("GRACE2_AWS_BATCH_JOB_DEF", raising=False)
    set_batch_client(FakeBatchClient())

    with pytest.raises(
        DeckBuildError, match="GRACE2_AWS_BATCH_JOB_DEF_SFINCS_QUADTREE"
    ):
        submit_sfincs_quadtree("s3://b/build_spec.json")


def test_combined_inert_when_backend_not_aws_batch(reset_seams, monkeypatch) -> None:
    monkeypatch.setenv("GRACE2_SOLVER_BACKEND", "local-docker")  # non-aws-batch (gcp-workflows decommissioned -> now resolves to aws-batch)
    monkeypatch.setenv("GRACE2_RUNS_BUCKET", "test-runs-bucket")
    monkeypatch.setenv("GRACE2_AWS_BATCH_QUEUE", "q")
    monkeypatch.setenv("GRACE2_AWS_BATCH_JOB_DEF_SFINCS_QUADTREE", "jd:1")
    set_batch_client(FakeBatchClient())

    with pytest.raises(DeckBuildError, match="aws-batch"):
        submit_sfincs_quadtree("s3://b/build_spec.json")


def test_combined_inert_when_queue_unset(reset_seams, monkeypatch) -> None:
    monkeypatch.setenv("GRACE2_SOLVER_BACKEND", "aws-batch")
    monkeypatch.setenv("GRACE2_RUNS_BUCKET", "test-runs-bucket")
    monkeypatch.delenv("GRACE2_AWS_BATCH_QUEUE", raising=False)
    monkeypatch.setenv("GRACE2_AWS_BATCH_JOB_DEF_SFINCS_QUADTREE", "jd:1")
    set_batch_client(FakeBatchClient())

    with pytest.raises(DeckBuildError, match="GRACE2_AWS_BATCH_QUEUE"):
        submit_sfincs_quadtree("s3://b/build_spec.json")


def test_combined_rejects_plain_path(reset_seams, quadtree_env) -> None:
    set_batch_client(FakeBatchClient())
    with pytest.raises(DeckBuildError, match="s3:// / gs:// / file://"):
        submit_sfincs_quadtree("/tmp/build_spec.json")


def test_combined_submit_failure_raises_typed(reset_seams, quadtree_env) -> None:
    class BoomBatch(FakeBatchClient):
        def submit_job(self, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("AccessDeniedException: not authorized")

    set_batch_client(BoomBatch())
    with pytest.raises(DeckBuildError, match="submit_job failed"):
        submit_sfincs_quadtree("s3://b/build_spec.json")


# --------------------------------------------------------------------------- #
# 4. GPL guard still holds — the agent NEVER imports cht_sfincs
# --------------------------------------------------------------------------- #


def test_agent_code_does_not_import_cht_sfincs() -> None:
    agent_src = Path(__file__).resolve().parents[1] / "src" / "grace2_agent"
    assert agent_src.is_dir(), agent_src
    offenders: list[str] = []
    for py in agent_src.rglob("*.py"):
        text = py.read_text(encoding="utf-8", errors="ignore")
        for needle in (
            "import cht_sfincs",
            "from cht_sfincs",
            "import cht_utils",
            "from cht_utils",
        ):
            if needle in text:
                offenders.append(f"{py}: {needle}")
    assert not offenders, f"agent code must NOT import cht (GPL): {offenders}"


# --------------------------------------------------------------------------- #
# 5. build_spec assembly — v2 refinement/cell-budget + buildings + rivers blocks
#    AND the combined build_spec still passes the worker's (cht-free) validator.
# --------------------------------------------------------------------------- #


def _load_worker_validate():
    import importlib.util

    worker_entry = (
        Path(__file__).resolve().parents[3]
        / "services"
        / "workers"
        / "sfincs_deckbuilder"
        / "entrypoint.py"
    )
    if not worker_entry.is_file():
        pytest.skip(f"deck-builder worker source not present: {worker_entry}")
    spec = importlib.util.spec_from_file_location("_deckbuilder_entry", worker_entry)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # module-level imports are cht-free
    return mod


class _FakeModelSetup:
    def __init__(self, parameters: dict[str, Any]) -> None:
        self.parameters = parameters


class _FakeForcingSpec:
    def __init__(self, provenance: dict[str, Any]) -> None:
        self.provenance = provenance


def test_combined_build_spec_carries_v2_blocks(reset_seams, monkeypatch) -> None:
    """The agent-composed combined build_spec carries the v2 refinement/cell-budget
    + buildings (with mode) + rivers blocks, has schema_version v2, and still
    passes the deck-builder worker's (cht-free) validate_build_spec."""
    worker = _load_worker_validate()

    monkeypatch.setenv("GRACE2_STORAGE_BACKEND", "s3")
    monkeypatch.setenv("GRACE2_CACHE_BUCKET", "deck-cache-bucket")
    s3 = FakeS3Client()
    set_s3_client(s3)

    from grace2_agent.workflows.model_flood_scenario import (
        _compose_and_upload_deckbuild_spec,
    )

    model_setup = _FakeModelSetup(
        parameters={"crs": "EPSG:3857", "forcing_provenance": {}}
    )
    forcing_spec = _FakeForcingSpec(provenance={})

    build_spec_uri = _compose_and_upload_deckbuild_spec(
        bbox=(-82.0, 26.5, -81.8, 26.7),  # Fort Myers-ish coastal AOI
        topobathy_uri="s3://topo-bucket/topobathy.tif",
        bathymetry_present=True,
        model_setup=model_setup,
        forcing_spec=forcing_spec,
        surge_forcing={"waterlevel": {"timeseries_uri": "s3://f/bzs.csv"}},
        grid_resolution_m=30.0,
        duration_hr=24.0,
        # NEW combined inputs:
        buildings_uri="s3://b/osm_buildings.fgb",
        building_obstacle_mode="thin_dams",
        rivers_uri="s3://b/osm_rivers.fgb",
        refinement_levels=2,
        max_cells=1_500_000,
        output_dt_s=600.0,
    )

    assert build_spec_uri.startswith("s3://deck-cache-bucket/")
    s3_bucket, _, key = build_spec_uri[len("s3://"):].partition("/")
    composed = json.loads(s3.objects[(s3_bucket, key)])

    # v2 schema + the NEW blocks.
    assert composed["schema_version"] == "v2"
    assert composed["grid"]["refinement_levels"] == 2
    assert composed["grid"]["max_cells"] == 1_500_000
    assert composed["buildings"]["footprints_uri"] == "s3://b/osm_buildings.fgb"
    assert composed["buildings"]["mode"] == "thin_dams"
    assert composed["rivers"]["lines_uri"] == "s3://b/osm_rivers.fgb"
    assert composed["output"]["output_dt"] == 600.0

    # The worker's (cht-free) validator still accepts the enriched spec.
    normalized = worker.validate_build_spec(composed)
    assert normalized["aoi"]["target_epsg"] == 32617
    for k in ("x0", "y0", "nmax", "mmax", "dx", "dy"):
        assert k in normalized["grid"]
    assert normalized["grid"]["dx"] == 30.0
    # CAVEAT 2 still honored.
    overrides = worker.snapwave_inp_overrides(composed)
    assert overrides["snapwave_use_herbers"] == 1


def test_combined_build_spec_omits_optional_blocks_when_absent(
    reset_seams, monkeypatch
) -> None:
    """Buildings + rivers blocks are absent when no footprints/rivers are passed
    (a pure quadtree run with no obstacles is still valid)."""
    monkeypatch.setenv("GRACE2_STORAGE_BACKEND", "s3")
    monkeypatch.setenv("GRACE2_CACHE_BUCKET", "deck-cache-bucket")
    set_s3_client(FakeS3Client())
    s3 = FakeS3Client()
    set_s3_client(s3)

    from grace2_agent.workflows.model_flood_scenario import (
        _compose_and_upload_deckbuild_spec,
    )

    build_spec_uri = _compose_and_upload_deckbuild_spec(
        bbox=(-82.0, 26.5, -81.8, 26.7),
        topobathy_uri="s3://topo-bucket/topobathy.tif",
        bathymetry_present=True,
        model_setup=_FakeModelSetup(parameters={"crs": "EPSG:3857"}),
        forcing_spec=_FakeForcingSpec(provenance={}),
        surge_forcing=None,
        grid_resolution_m=30.0,
        duration_hr=24.0,
        # No buildings / rivers.
    )
    s3_bucket, _, key = build_spec_uri[len("s3://"):].partition("/")
    composed = json.loads(s3.objects[(s3_bucket, key)])
    assert "buildings" not in composed
    assert "rivers" not in composed
    # Defaults still present for refinement + budget.
    assert composed["grid"]["refinement_levels"] == 2
    assert composed["grid"]["max_cells"] == 2_000_000
