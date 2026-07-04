"""cht_sfincs quadtree+SnapWave deck-build SUBMIT path tests (coastal North Star).

The agent SUBMITS a GPL-isolated cht_sfincs deck-build AWS Batch job (it NEVER
imports cht_sfincs), polls the SAME completion.json the solve worker writes, and
reads the deck-build's OUTPUT manifest.json URI — which is byte-identical in
shape to what ``build_sfincs_model`` emits, so the EXISTING ``run_solver(
'sfincs', model_setup_uri=<that URI>)`` solve is unchanged.

These tests prove (all boto3 Batch + S3 calls MOCKED via the set_batch_client /
set_s3_client seams — NO real AWS):

1.  submit_sfincs_deckbuild submits to the deck-builder's OWN job-def
    (GRACE2_AWS_BATCH_JOB_DEF_SFINCS_DECKBUILDER, NOT the SFINCS solve job-def)
    with --build-spec-uri command + GRACE2_BUILD_SPEC_URI env; handle shape
    (workflow_name=aws-batch, jobId in workflows_execution_id, solver=
    sfincs-deckbuilder).
2.  build_sfincs_quadtree_deck = SUBMIT + poll S3 completion + return the deck
    manifest.json URI from completion.json output_uris.
3.  INERT until provisioned: a missing deck-builder job-def raises a clean typed
    DeckBuildError naming GRACE2_AWS_BATCH_JOB_DEF_SFINCS_DECKBUILDER (mirrors
    how SWMM stayed inert); a non-aws-batch backend raises DeckBuildError; a
    completed deck-build with no manifest in output_uris raises DeckBuildError —
    honest typed failure, never a silent success.
4.  GPL guard: the agent venv + agent code NEVER import cht_sfincs.
"""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError

import grace2_agent.tools.solver as solver_mod
from grace2_agent.tools.solver import (
    AWS_BATCH_WORKFLOW_NAME,
    SFINCS_DECKBUILDER_SOLVER,
    DeckBuildError,
    build_sfincs_quadtree_deck,
    set_batch_client,
    set_emitter_binding,
    set_runs_bucket,
    set_s3_client,
    submit_sfincs_deckbuild,
)
from grace2_contracts.execution import ExecutionHandle


# --------------------------------------------------------------------------- #
# Fakes (dict-backed; mirror test_solver_aws_batch.py)
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
    def __init__(self, *, submit_job_id: str = "deck-job-xyz789") -> None:
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
                    {"jobId": jid, "status": "FAILED", "statusReason": self.failed_jobs[jid]}
                )
            else:
                out.append({"jobId": jid, "status": "RUNNING"})
        return {"jobs": out}

    def terminate_job(self, jobId: str, reason: str) -> dict[str, Any]:  # noqa: N803
        self.terminate_calls.append((jobId, reason))
        return {}


def _seed_deck_completion(
    s3: FakeS3Client,
    run_id: str,
    *,
    runs_bucket: str,
    manifest_uri: str | None,
    status: str = "ok",
) -> None:
    """Seed the deck-build worker's completion.json (the deck manifest URI lives
    in ``output_uris``, the SAME field the solve worker uses)."""
    output_uris: list[str] = []
    if manifest_uri is not None:
        # Worker lists deck files THEN the manifest, mirroring real output.
        output_uris = [
            f"s3://deck-bucket/cache/static-30d/sfincs_deck/D/deck/sfincs.nc",
            f"s3://deck-bucket/cache/static-30d/sfincs_deck/D/deck/sfincs.inp",
            manifest_uri,
        ]
    payload = {
        "run_id": run_id,
        "status": status,
        "exit_code": 0 if status == "ok" else 1,
        "output_uris": output_uris,
        "started_at": "2026-06-18T00:00:00Z",
        "finished_at": "2026-06-18T00:00:30Z",
        "error": None if status == "ok" else "deck build failed",
    }
    s3.objects[(runs_bucket, f"{run_id}/completion.json")] = json.dumps(payload).encode()


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture()
def reset_seams():
    for setter in (set_s3_client, set_batch_client):
        setter(None)
    set_emitter_binding(None)
    set_runs_bucket(None)
    try:
        yield
    finally:
        for setter in (set_s3_client, set_batch_client):
            setter(None)
        set_emitter_binding(None)
        set_runs_bucket(None)


@pytest.fixture()
def deckbuild_env(monkeypatch: pytest.MonkeyPatch):
    """Fully-provisioned aws-batch + deck-builder job-def env."""
    monkeypatch.setenv("GRACE2_SOLVER_BACKEND", "aws-batch")
    monkeypatch.setenv("GRACE2_RUNS_BUCKET", "test-runs-bucket")
    monkeypatch.setenv("GRACE2_AWS_BATCH_QUEUE", "grace2-batch-queue")
    monkeypatch.setenv(
        "GRACE2_AWS_BATCH_JOB_DEF_SFINCS_DECKBUILDER", "grace2-sfincs-deckbuilder:3"
    )
    # The SOLVE job-def is a DIFFERENT image — present here to prove the deck
    # builder does NOT cross-route to it.
    monkeypatch.setenv("GRACE2_AWS_BATCH_JOB_DEF_SFINCS", "grace2-sfincs-solve:9")
    monkeypatch.setenv("AWS_REGION", "us-west-2")


# --------------------------------------------------------------------------- #
# 1. submit_sfincs_deckbuild — args + handle shape, OWN job-def
# --------------------------------------------------------------------------- #


def test_deckbuild_submit_uses_own_jobdef_and_handle_shape(
    reset_seams, deckbuild_env
) -> None:
    batch = FakeBatchClient()
    set_batch_client(batch)

    handle = submit_sfincs_deckbuild(
        build_spec_uri="s3://deck-bucket/cache/spec/build_spec.json",
        compute_class="small",
    )

    # Handle shape: deck-builder solver key + aws-batch poll sentinel + jobId.
    assert isinstance(handle, ExecutionHandle)
    assert handle.solver == SFINCS_DECKBUILDER_SOLVER == "sfincs-deckbuilder"
    assert handle.workflow_name == AWS_BATCH_WORKFLOW_NAME
    assert handle.workflows_execution_id == "deck-job-xyz789"

    assert len(batch.submit_calls) == 1
    call = batch.submit_calls[0]
    assert call["jobQueue"] == "grace2-batch-queue"
    # CRITICAL: the deck-build routes to its OWN job-def, NOT the SFINCS solve
    # image — the GPL boundary is two distinct images on one queue.
    assert call["jobDefinition"] == "grace2-sfincs-deckbuilder:3"
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
# 2. build_sfincs_quadtree_deck — SUBMIT + poll + deck manifest URI
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_deckbuild_full_flow_returns_deck_manifest_uri(
    reset_seams, deckbuild_env
) -> None:
    batch = FakeBatchClient()
    s3 = FakeS3Client()
    set_batch_client(batch)
    set_s3_client(s3)

    deck_manifest = (
        "s3://deck-bucket/cache/static-30d/sfincs_deck/D/manifest.json"
    )

    # We need the run_id the submit mints to seed the completion. Submit first,
    # then drive wait via build_sfincs_quadtree_deck would re-submit, so test the
    # two halves: capture run_id from submit by intercepting then seeding before
    # the poll. Simplest: monkeypatch new_ulid is overkill — instead run the
    # combined helper but seed completion lazily on the FIRST get_object miss.
    #
    # Easiest deterministic approach: submit, read run_id, seed, then poll via
    # the public wait path the helper uses. But build_sfincs_quadtree_deck does
    # submit+wait internally. So pre-seed using a get_object wrapper that, on the
    # first miss, plants the completion for whatever run_id is asked.
    real_get = s3.get_object

    def get_or_seed(Bucket: str, Key: str):  # noqa: N803
        if Key.endswith("/completion.json") and (Bucket, Key) not in s3.objects:
            run_id = Key.split("/")[0]
            _seed_deck_completion(
                s3, run_id, runs_bucket=Bucket, manifest_uri=deck_manifest, status="ok"
            )
        return real_get(Bucket, Key)

    s3.get_object = get_or_seed  # type: ignore[assignment]

    manifest_uri = await build_sfincs_quadtree_deck(
        "s3://deck-bucket/cache/spec/build_spec.json",
        compute_class="small",
        poll_interval_s=0,
    )

    assert manifest_uri == deck_manifest
    assert len(batch.submit_calls) == 1
    # The deck-build submitted to the deck-builder job-def.
    assert batch.submit_calls[0]["jobDefinition"] == "grace2-sfincs-deckbuilder:3"


@pytest.mark.asyncio
async def test_deckbuild_completed_without_manifest_raises_typed(
    reset_seams, deckbuild_env
) -> None:
    batch = FakeBatchClient()
    s3 = FakeS3Client()
    set_batch_client(batch)
    set_s3_client(s3)

    real_get = s3.get_object

    def get_or_seed(Bucket: str, Key: str):  # noqa: N803
        if Key.endswith("/completion.json") and (Bucket, Key) not in s3.objects:
            run_id = Key.split("/")[0]
            # status ok but NO manifest in output_uris → honest typed failure.
            _seed_deck_completion(
                s3, run_id, runs_bucket=Bucket, manifest_uri=None, status="ok"
            )
        return real_get(Bucket, Key)

    s3.get_object = get_or_seed  # type: ignore[assignment]

    with pytest.raises(DeckBuildError, match="no deck manifest"):
        await build_sfincs_quadtree_deck(
            "s3://deck-bucket/cache/spec/build_spec.json", poll_interval_s=0
        )


@pytest.mark.asyncio
async def test_deckbuild_failed_status_raises_typed(reset_seams, deckbuild_env) -> None:
    batch = FakeBatchClient()
    s3 = FakeS3Client()
    set_batch_client(batch)
    set_s3_client(s3)

    real_get = s3.get_object

    def get_or_seed(Bucket: str, Key: str):  # noqa: N803
        if Key.endswith("/completion.json") and (Bucket, Key) not in s3.objects:
            run_id = Key.split("/")[0]
            _seed_deck_completion(
                s3, run_id, runs_bucket=Bucket, manifest_uri=None, status="error"
            )
        return real_get(Bucket, Key)

    s3.get_object = get_or_seed  # type: ignore[assignment]

    with pytest.raises(DeckBuildError, match="did not complete"):
        await build_sfincs_quadtree_deck(
            "s3://deck-bucket/cache/spec/build_spec.json", poll_interval_s=0
        )


# --------------------------------------------------------------------------- #
# 3. INERT until provisioned — clean typed DeckBuildError, never a crash
# --------------------------------------------------------------------------- #


def test_deckbuild_inert_when_jobdef_unset(reset_seams, monkeypatch) -> None:
    """No deck-builder job-def (and no generic fallback) → typed error naming the
    canonical env var. Mirrors how SWMM stayed inert until its job-def env."""
    monkeypatch.setenv("GRACE2_SOLVER_BACKEND", "aws-batch")
    monkeypatch.setenv("GRACE2_RUNS_BUCKET", "test-runs-bucket")
    monkeypatch.setenv("GRACE2_AWS_BATCH_QUEUE", "q")
    monkeypatch.delenv("GRACE2_AWS_BATCH_JOB_DEF_SFINCS_DECKBUILDER", raising=False)
    monkeypatch.delenv("GRACE2_AWS_BATCH_JOB_DEF", raising=False)
    set_batch_client(FakeBatchClient())

    with pytest.raises(DeckBuildError, match="GRACE2_AWS_BATCH_JOB_DEF_SFINCS_DECKBUILDER"):
        submit_sfincs_deckbuild("s3://b/build_spec.json")


def test_deckbuild_inert_when_backend_not_aws_batch(reset_seams, monkeypatch) -> None:
    """The deck-build is Batch-only (GPL isolated); a non-aws-batch backend must
    NOT silently do nothing — it raises a typed DeckBuildError.

    GCP decommissioned: the unset default is now aws-batch, AND the dead
    gcp-workflows value now resolves to aws-batch too, so pin local-docker (the
    only remaining non-aws-batch backend) to exercise the guard."""
    monkeypatch.setenv("GRACE2_SOLVER_BACKEND", "local-docker")  # non-aws-batch
    monkeypatch.setenv("GRACE2_RUNS_BUCKET", "test-runs-bucket")
    monkeypatch.setenv("GRACE2_AWS_BATCH_QUEUE", "q")
    monkeypatch.setenv("GRACE2_AWS_BATCH_JOB_DEF_SFINCS_DECKBUILDER", "jd:1")
    set_batch_client(FakeBatchClient())

    with pytest.raises(DeckBuildError, match="aws-batch"):
        submit_sfincs_deckbuild("s3://b/build_spec.json")


def test_deckbuild_inert_when_queue_unset(reset_seams, monkeypatch) -> None:
    monkeypatch.setenv("GRACE2_SOLVER_BACKEND", "aws-batch")
    monkeypatch.setenv("GRACE2_RUNS_BUCKET", "test-runs-bucket")
    monkeypatch.delenv("GRACE2_AWS_BATCH_QUEUE", raising=False)
    monkeypatch.setenv("GRACE2_AWS_BATCH_JOB_DEF_SFINCS_DECKBUILDER", "jd:1")
    set_batch_client(FakeBatchClient())

    with pytest.raises(DeckBuildError, match="GRACE2_AWS_BATCH_QUEUE"):
        submit_sfincs_deckbuild("s3://b/build_spec.json")


def test_deckbuild_rejects_plain_path(reset_seams, deckbuild_env) -> None:
    set_batch_client(FakeBatchClient())
    with pytest.raises(DeckBuildError, match="s3:// / gs:// / file://"):
        submit_sfincs_deckbuild("/tmp/build_spec.json")


def test_deckbuild_submit_failure_raises_typed(reset_seams, deckbuild_env) -> None:
    class BoomBatch(FakeBatchClient):
        def submit_job(self, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("AccessDeniedException: not authorized")

    set_batch_client(BoomBatch())
    with pytest.raises(DeckBuildError, match="submit_job failed"):
        submit_sfincs_deckbuild("s3://b/build_spec.json")


def test_deckbuild_error_is_solver_dispatch_error_subclass() -> None:
    """DeckBuildError must subclass SolverDispatchError so existing
    ``except SolverDispatchError`` handlers + the emitter classifier catch it."""
    from grace2_agent.tools.solver import SolverDispatchError

    assert issubclass(DeckBuildError, SolverDispatchError)
    assert DeckBuildError.error_code == "DECK_BUILD_FAILED"


# --------------------------------------------------------------------------- #
# 4. GPL guard — the agent NEVER imports cht_sfincs
# --------------------------------------------------------------------------- #


def test_agent_code_does_not_import_cht_sfincs() -> None:
    """Hard GPL boundary: no file under services/agent/src imports cht_sfincs /
    cht_utils. The deck authoring lives ONLY in the separate Batch worker image;
    the agent reaches it over batch.submit + S3, never a Python import."""
    agent_src = Path(__file__).resolve().parents[1] / "src" / "grace2_agent"
    assert agent_src.is_dir(), agent_src
    offenders: list[str] = []
    for py in agent_src.rglob("*.py"):
        text = py.read_text(encoding="utf-8", errors="ignore")
        for needle in ("import cht_sfincs", "from cht_sfincs", "import cht_utils", "from cht_utils"):
            if needle in text:
                offenders.append(f"{py}: {needle}")
    assert not offenders, f"agent code must NOT import cht (GPL): {offenders}"


def test_cht_sfincs_not_importable_in_agent_venv() -> None:
    """The GPL library must not be installed in the agent venv at all."""
    import importlib.util

    assert importlib.util.find_spec("cht_sfincs") is None, (
        "cht_sfincs (GPL-3.0) must NOT be in the agent venv — it belongs only in "
        "the dedicated deck-builder Batch worker image."
    )


# --------------------------------------------------------------------------- #
# 5. Cross-boundary contract: the agent-composed build_spec is ACCEPTED by the
#    deck-builder worker's (cht-free) validate_build_spec.
# --------------------------------------------------------------------------- #


def _load_worker_validate():
    """Import the deck-builder worker's pure-python validate_build_spec WITHOUT
    importing cht_sfincs (the cht import is lazy inside build_deck). Skips if the
    worker image source dir isn't present (it is built by a separate track)."""
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


def test_agent_build_spec_passes_worker_validation(reset_seams, monkeypatch) -> None:
    """The build_spec the agent composes + uploads validates cleanly against the
    deck-builder worker's contract (complete grid, int target_epsg, parseable
    times, output URIs, use_herbers=1)."""
    worker = _load_worker_validate()

    monkeypatch.setenv("GRACE2_STORAGE_BACKEND", "s3")
    monkeypatch.setenv("GRACE2_CACHE_BUCKET", "deck-cache-bucket")
    s3 = FakeS3Client()
    set_s3_client(s3)

    from grace2_agent.workflows.model_flood_scenario import (
        _compose_and_upload_deckbuild_spec,
    )

    # ModelSetup with the realistic param shape build_sfincs_model emits: a
    # projected ``crs`` + forcing_provenance, but NO explicit base grid (the
    # agent derives it from the bbox).
    model_setup = _FakeModelSetup(
        parameters={
            "crs": "EPSG:3857",  # unsuitable → agent snaps to UTM
            "forcing_provenance": {},
        }
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
    )

    # It was uploaded to S3 (boto3 seam), under the cache bucket.
    assert build_spec_uri.startswith("s3://deck-cache-bucket/")
    assert s3.put_calls, "build_spec must be uploaded to S3"

    # Pull the uploaded JSON back out + run it through the WORKER's validator.
    s3_bucket, _, key = build_spec_uri[len("s3://"):].partition("/")
    raw = s3.objects[(s3_bucket, key)]
    composed = json.loads(raw)

    normalized = worker.validate_build_spec(composed)  # raises on contract miss
    # int target_epsg is a real UTM zone (Fort Myers → 32617).
    assert normalized["aoi"]["target_epsg"] == 32617
    # Base grid is complete + metric.
    for k in ("x0", "y0", "nmax", "mmax", "dx", "dy"):
        assert k in normalized["grid"]
    assert normalized["grid"]["dx"] == 30.0
    # Times parsed (tstop after tstart).
    assert normalized["_parsed_times"]["tstop"] > normalized["_parsed_times"]["tstart"]
    # CAVEAT 2 — the worker's snapwave override sees use_herbers=1 from the spec.
    overrides = worker.snapwave_inp_overrides(composed)
    assert overrides["snapwave_use_herbers"] == 1
    # output URIs present for run_solver to consume.
    assert normalized["output"]["manifest_uri"].endswith("manifest.json")


# --------------------------------------------------------------------------- #
# 6. QUADTREE forcing fix (run 01KVRJK7333NP2XC64PBHABZ11):
#    (issue 1) LOCAL surge forcing files (the bzs CSV + bnd FGB the auto-wired /
#    parametric surge wrote under /tmp) are UPLOADED to S3 + the build_spec's
#    waterlevel forcing URIs are rewritten to s3:// (the remote Batch worker can
#    only download s3:// / gs://  -  a /tmp path crashes _split_object_uri).
#    (issue 2) a COASTAL quadtree run synthesises a parametric SnapWave wave
#    boundary (offshore Hs/Tp/dir points) so wavebnd>0.
# --------------------------------------------------------------------------- #


def test_quadtree_build_spec_uploads_local_forcing_and_rewrites_to_s3(
    reset_seams, monkeypatch, tmp_path
) -> None:
    """A coastal quadtree build_spec must carry s3:// waterlevel forcing URIs.

    The auto-wired surge writes bzs/bnd to LOCAL /tmp paths; on the quadtree path
    those are uploaded to the cache bucket and the build_spec waterlevel block is
    rewritten to s3:// (NOT the local path) so the remote deck-builder can
    download them. The worker validator must still accept the result.
    """
    worker = _load_worker_validate()

    monkeypatch.setenv("GRACE2_STORAGE_BACKEND", "s3")
    monkeypatch.setenv("GRACE2_CACHE_BUCKET", "deck-cache-bucket")
    s3 = FakeS3Client()
    set_s3_client(s3)

    from grace2_agent.workflows.model_flood_scenario import (
        _compose_and_upload_deckbuild_spec,
    )

    # Materialise LOCAL forcing files on disk (mirrors what the parametric /
    # CO-OPS surge adapter writes under /tmp/grace2-sfincs-forcing/).
    bzs_path = tmp_path / "bzs-2018dcd9c22e.csv"
    bnd_path = tmp_path / "bnd-2018dcd9c22e.fgb"
    bzs_path.write_text("time,1\n0,0.3\n3600,3.8\n")
    bnd_path.write_bytes(b"\x66\x67\x62fake-flatgeobuf-bytes")

    model_setup = _FakeModelSetup(parameters={"crs": "EPSG:3857"})
    forcing_spec = _FakeForcingSpec(provenance={})

    build_spec_uri = _compose_and_upload_deckbuild_spec(
        bbox=(-82.0, 26.5, -81.8, 26.7),
        topobathy_uri="s3://topo-bucket/topobathy.tif",
        bathymetry_present=True,
        model_setup=model_setup,
        forcing_spec=forcing_spec,
        # LOCAL paths  -  exactly what triggered the worker crash.
        surge_forcing={
            "waterlevel": {
                "timeseries_uri": str(bzs_path),
                "locations_uri": str(bnd_path),
            }
        },
        grid_resolution_m=30.0,
        duration_hr=24.0,
        return_period_yr=100,
        is_coastal=True,
    )

    assert build_spec_uri.startswith("s3://deck-cache-bucket/")
    s3_bucket, _, key = build_spec_uri[len("s3://"):].partition("/")
    composed = json.loads(s3.objects[(s3_bucket, key)])

    wl = composed["forcing"]["surge_forcing"]["waterlevel"]
    # ISSUE 1: the build_spec waterlevel URIs are s3:// (NOT local /tmp paths).
    assert wl["timeseries_uri"].startswith("s3://deck-cache-bucket/")
    assert wl["locations_uri"].startswith("s3://deck-cache-bucket/")
    assert not wl["timeseries_uri"].startswith("/")
    assert not wl["locations_uri"].startswith("/")
    # The forcing files were actually uploaded under this deck's forcing/ prefix.
    assert any(
        k.endswith("bzs-2018dcd9c22e.csv") for (_b, k) in s3.put_calls
    ), s3.put_calls
    assert any(
        k.endswith("bnd-2018dcd9c22e.fgb") for (_b, k) in s3.put_calls
    ), s3.put_calls
    # The uploaded bytes round-trip (the worker would download these).
    up_bucket, _, up_key = wl["timeseries_uri"][len("s3://"):].partition("/")
    assert s3.objects[(up_bucket, up_key)] == bzs_path.read_bytes()

    # ISSUE 2: a COASTAL run carries a parametric SnapWave wave boundary with
    # offshore incident-wave points (Hs/Tp/dir) so the worker's wavebnd>0.
    sw_bc = composed["forcing"]["surge_forcing"]["snapwave_boundary"]
    assert sw_bc and sw_bc.get("points"), "coastal run must carry wave boundary points"
    for pt in sw_bc["points"]:
        assert {"x", "y", "hs", "tp", "wd", "ds"} <= set(pt)
        assert pt["hs"] > 0.0 and pt["tp"] > 0.0
    # The worker resolves the wave boundary from forcing.surge_forcing.
    blocks = worker.resolve_forcing_blocks(composed)
    assert blocks["snapwave_boundary"] and blocks["snapwave_boundary"]["points"]
    # And the whole spec still validates against the worker contract.
    worker.validate_build_spec(composed)


def test_quadtree_build_spec_leaves_remote_uris_and_inland_has_no_wave_bc(
    reset_seams, monkeypatch
) -> None:
    """Already-s3:// forcing URIs pass through untouched; an INLAND (non-coastal)
    quadtree run emits NO synthetic wave boundary (path unchanged)."""
    monkeypatch.setenv("GRACE2_STORAGE_BACKEND", "s3")
    monkeypatch.setenv("GRACE2_CACHE_BUCKET", "deck-cache-bucket")
    s3 = FakeS3Client()
    set_s3_client(s3)

    from grace2_agent.workflows.model_flood_scenario import (
        _compose_and_upload_deckbuild_spec,
    )

    model_setup = _FakeModelSetup(parameters={"crs": "EPSG:3857"})
    forcing_spec = _FakeForcingSpec(provenance={})

    build_spec_uri = _compose_and_upload_deckbuild_spec(
        bbox=(-90.1, 29.9, -89.9, 30.1),
        topobathy_uri="s3://topo-bucket/topobathy.tif",
        bathymetry_present=False,
        model_setup=model_setup,
        forcing_spec=forcing_spec,
        # Already-remote URI  -  must be left exactly as-is (no re-upload).
        surge_forcing={"waterlevel": {"timeseries_uri": "s3://f/bzs.csv"}},
        grid_resolution_m=30.0,
        duration_hr=24.0,
        return_period_yr=100,
        is_coastal=False,  # INLAND  -  no synthetic wave boundary
    )

    s3_bucket, _, key = build_spec_uri[len("s3://"):].partition("/")
    composed = json.loads(s3.objects[(s3_bucket, key)])
    wl = composed["forcing"]["surge_forcing"]["waterlevel"]
    # Already-remote URI untouched (not re-uploaded under the deck prefix).
    assert wl["timeseries_uri"] == "s3://f/bzs.csv"
    # No forcing-file upload happened (only the build_spec.json itself).
    forcing_uploads = [k for (_b, k) in s3.put_calls if "/forcing/" in k]
    assert forcing_uploads == [], forcing_uploads
    # INLAND: no synthetic wave boundary block.
    assert "snapwave_boundary" not in composed["forcing"]["surge_forcing"]


def test_synthesize_parametric_wave_boundary_scales_with_return_period() -> None:
    """The parametric offshore wave Hs scales monotonically with the ARI and the
    boundary points are emitted in the deck CRS (projected, not lon/lat)."""
    from grace2_agent.workflows.model_flood_scenario import (
        _parametric_wave_hs_m,
        _synthesize_parametric_wave_boundary,
    )

    assert _parametric_wave_hs_m(10) < _parametric_wave_hs_m(100) < _parametric_wave_hs_m(500)

    bbox = (-82.0, 26.5, -81.8, 26.7)
    bc = _synthesize_parametric_wave_boundary(bbox, target_epsg=32617, return_period_yr=100)
    assert bc["points"]
    # Projected UTM coordinates are O(1e5..1e6), not lon/lat in [-180,180].
    for pt in bc["points"]:
        assert abs(pt["x"]) > 1000.0 and abs(pt["y"]) > 1000.0
        assert 0.0 <= pt["wd"] <= 360.0


def test_quadtree_build_spec_sets_dtwave_and_time_varying_wave_boundary(
    reset_seams, monkeypatch, tmp_path
) -> None:
    """DEFECT 2: the coastal build_spec snapwave block carries ``dtwave`` (pinned
    to the fine output cadence, capped 600 s) AND the synthesized wave boundary
    points carry the time-varying storm-envelope series. The worker contract still
    validates. The DEM is unreachable in this fake S3 -> the boundary falls back
    to the bathy-unaware 4-edge placement (depth-aware is exercised in
    test_wave_boundary_depth_aware)."""
    worker = _load_worker_validate()

    monkeypatch.setenv("GRACE2_STORAGE_BACKEND", "s3")
    monkeypatch.setenv("GRACE2_CACHE_BUCKET", "deck-cache-bucket")
    s3 = FakeS3Client()
    set_s3_client(s3)

    from grace2_agent.workflows.model_flood_scenario import (
        _compose_and_upload_deckbuild_spec,
    )

    build_spec_uri = _compose_and_upload_deckbuild_spec(
        bbox=(-85.55, 29.92, -85.35, 30.12),  # Mexico Beach-ish
        topobathy_uri="s3://topo-bucket/topobathy.tif",  # not in fake S3 -> fallback
        bathymetry_present=True,
        model_setup=_FakeModelSetup(parameters={"crs": "EPSG:3857"}),
        forcing_spec=_FakeForcingSpec(provenance={}),
        surge_forcing={"waterlevel": {"timeseries_uri": "s3://f/bzs.csv"}},
        grid_resolution_m=30.0,
        duration_hr=6.0,
        return_period_yr=100,
        is_coastal=True,
        output_dt_s=300.0,  # fine coastal cadence
    )

    s3_bucket, _, key = build_spec_uri[len("s3://"):].partition("/")
    composed = json.loads(s3.objects[(s3_bucket, key)])

    # DEFECT 2 (dtwave): pinned to min(output_dt, 600) = 300 s.
    assert composed["snapwave"]["dtwave"] == 300.0
    # The worker resolves it as a bare ``dtwave`` knob (not snapwave_*).
    knobs = worker.snapwave_inp_overrides(composed)
    assert knobs["dtwave"] == 300.0

    # Cap holds when the output cadence is coarse.
    build_spec_uri2 = _compose_and_upload_deckbuild_spec(
        bbox=(-85.55, 29.92, -85.35, 30.12),
        topobathy_uri="s3://topo-bucket/topobathy.tif",
        bathymetry_present=True,
        model_setup=_FakeModelSetup(parameters={"crs": "EPSG:3857"}),
        forcing_spec=_FakeForcingSpec(provenance={}),
        surge_forcing={"waterlevel": {"timeseries_uri": "s3://f/bzs.csv"}},
        grid_resolution_m=30.0,
        duration_hr=6.0,
        return_period_yr=100,
        is_coastal=True,
        output_dt_s=3600.0,  # coarse -> capped to 600
    )
    s3b, _, key2 = build_spec_uri2[len("s3://"):].partition("/")
    composed2 = json.loads(s3.objects[(s3b, key2)])
    assert composed2["snapwave"]["dtwave"] == 600.0

    # DEFECT 2 realism: time-varying wave boundary series on every point.
    sw_bc = composed["forcing"]["surge_forcing"]["snapwave_boundary"]
    assert sw_bc["_prov_time_varying"] is True
    assert sw_bc["points"]
    for pt in sw_bc["points"]:
        assert pt["time_s"] and pt["hs_series"] and pt["tp_series"]
        n = len(pt["time_s"])
        assert len(pt["hs_series"]) == n and len(pt["tp_series"]) == n
        # Hs ramps to the peak scalar at the storm centre, lower at the ends.
        assert pt["hs_series"][0] < pt["hs"]
        assert max(pt["hs_series"]) <= pt["hs"] + 1e-6

    # Whole spec still validates against the worker contract.
    worker.validate_build_spec(composed)
