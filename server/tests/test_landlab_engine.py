"""Targeted tests for the Landlab surface-process engine (sprint-17 — NEW engine).

The Landlab analogue of ``test_postprocess_swmm`` / ``test_run_swmm_local_chain``.
Landlab runs OFF-BOX ONLY (AWS Batch), so these tests pin the agent-side modules
in ISOLATION with ``run_solver`` / ``wait_for_completion`` / boto3 / the DEM
fetch MOCKED (the engine is not registry-wired yet — the orchestrator runs the
full suite after merging the shared-append snippets):

1. **Contract round-trip** — ``LandlabRunArgs`` defaults + analysis-synonym
   normalization + JSON round-trip; ``LandlabSusceptibilityLayerURI`` is a
   ``LayerURI`` subtype carrying the three narration scalars. (no IO)
2. **build_spec arg-assembly** — ``run_landlab.build_landlab_build_spec`` maps a
   validated ``LandlabRunArgs`` onto the worker build_spec dict. (no IO)
3. **stage_landlab_manifest** — uploads the DEM + a worker-contract manifest to
   the staging bucket via a mocked boto3 client; the manifest carries the
   build_spec + the legacy ``gs_uri`` input shape. (boto3 mocked)
4. **postprocess** — a synthetic Landlab field COG (probability of failure /
   depth) reprojects to a valid EPSG:4326 COG (upload stubbed) and the narration
   scalars are computed (worker ``result`` block preferred, recompute fallback).
5. **Composer arg-assembly** — ``model_landslide_scenario`` drives the full
   fetch -> stage -> run_solver -> wait -> postprocess -> publish chain with
   every external call MOCKED, and returns a ``LandlabSusceptibilityLayerURI``.

rasterio + numpy are required for tests (4)+(5); skipped if absent. Tests
(1)-(3) need only numpy.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.landlab_contracts import (
    LandlabRunArgs,
    LandlabSusceptibilityLayerURI,
)


# ===========================================================================
# (1) Contract round-trip — no IO.
# ===========================================================================
def test_run_args_defaults_and_normalization():
    """Defaults land; analysis synonyms normalize on the FIRST attempt; an
    unknown analysis still raises the honest Literal error."""
    a = LandlabRunArgs(bbox=(-122.5, 45.4, -122.4, 45.5))
    assert a.analysis == "landslide_probability"
    assert a.target_resolution_m == 30.0
    assert a.n_monte_carlo >= 1
    # synonyms -> canonical
    for syn in ("landslide", "susceptibility", "slope_stability", "FoS", "infinite slope"):
        assert (
            LandlabRunArgs(bbox=a.bbox, analysis=syn).analysis
            == "landslide_probability"
        ), syn
    for syn in ("overland", "runoff", "surface flow"):
        assert LandlabRunArgs(bbox=a.bbox, analysis=syn).analysis == "overland_flow", syn
    # an unknown analysis fails the Literal (honest error, no silent wrong chain).
    with pytest.raises(Exception):
        LandlabRunArgs(bbox=a.bbox, analysis="teleport")


def test_run_args_json_roundtrip():
    a = LandlabRunArgs(
        bbox=(-122.5, 45.4, -122.4, 45.5),
        analysis="overland_flow",
        rainfall_intensity_mm_hr=80.0,
        storm_duration_hr=3.0,
    )
    a2 = LandlabRunArgs.model_validate_json(a.model_dump_json())
    assert a2.analysis == "overland_flow"
    assert a2.rainfall_intensity_mm_hr == pytest.approx(80.0)
    assert a2.bbox == a.bbox


def test_susceptibility_layer_is_layeruri_with_scalars():
    L = LandlabSusceptibilityLayerURI(
        layer_id="landlab-susceptibility-run-x",
        name="Landslide susceptibility",
        layer_type="raster",
        uri="s3://b/landlab_susceptibility.tif",
        style_preset="continuous_landslide_susceptibility",
        role="primary",
        units="probability",
        unstable_area_fraction=0.12,
        min_factor_of_safety=0.93,
        mean_probability_of_failure=0.21,
    )
    assert isinstance(L, LayerURI)
    assert L.unstable_area_fraction == pytest.approx(0.12)
    assert L.min_factor_of_safety == pytest.approx(0.93)
    assert L.mean_probability_of_failure == pytest.approx(0.21)
    # range validators
    with pytest.raises(Exception):
        LandlabSusceptibilityLayerURI(
            layer_id="x", name="n", layer_type="raster", uri="s3://b/k.tif",
            style_preset="continuous_landslide_susceptibility",
            unstable_area_fraction=1.5,  # > 1
            min_factor_of_safety=0.5, mean_probability_of_failure=0.1,
        )


# ===========================================================================
# (2) build_spec arg-assembly — no IO.
# ===========================================================================
def test_build_landlab_build_spec_maps_args():
    from trid3nt_server.workflows.run_landlab import build_landlab_build_spec

    a = LandlabRunArgs(
        bbox=(-122.5, 45.4, -122.4, 45.5),
        analysis="landslide_probability",
        target_resolution_m=25.0,
        soil_cohesion_pa=12345.0,
        n_monte_carlo=42,
    )
    spec = build_landlab_build_spec(a)
    assert spec["analysis"] == "landslide_probability"
    assert spec["target_resolution_m"] == pytest.approx(25.0)
    assert spec["soil_cohesion_pa"] == pytest.approx(12345.0)
    assert spec["n_monte_carlo"] == 42
    # rainfall keys are present too (harmless for the landslide chain).
    assert "rainfall_intensity_mm_hr" in spec
    assert "storm_duration_hr" in spec


# ===========================================================================
# (3) stage_landlab_manifest — boto3 mocked.
# ===========================================================================
def test_stage_landlab_manifest_uploads_dem_and_manifest(tmp_path, monkeypatch):
    from trid3nt_server.workflows import run_landlab as RL

    # a tiny on-disk "DEM" file (stage only uploads bytes; no rasterio needed).
    dem = tmp_path / "dem.tif"
    dem.write_bytes(b"FAKE_DEM_BYTES")

    puts: dict[str, bytes] = {}

    class _FakeS3:
        def put_object(self, *, Bucket, Key, Body, ContentType=None):  # noqa: ANN001
            puts[Key] = Body.read() if hasattr(Body, "read") else Body
            return {}

    monkeypatch.setattr(RL, "_get_s3_client", lambda: _FakeS3(), raising=False)
    # _get_s3_client is imported lazily inside stage_landlab_manifest from
    # ..tools.simulation.solver; patch it there.
    from trid3nt_server.tools.simulation import solver as _solver

    monkeypatch.setattr(_solver, "_get_s3_client", lambda: _FakeS3())
    from trid3nt_server.tools import cache as _cache

    monkeypatch.setattr(_cache, "storage_scheme", lambda: "s3")
    monkeypatch.setenv("TRID3NT_CACHE_BUCKET", "test-cache-bucket")

    a = LandlabRunArgs(bbox=(-122.5, 45.4, -122.4, 45.5), analysis="landslide_probability")
    staging = RL.stage_landlab_manifest(a, dem_path=str(dem), run_id="run-stage")

    assert staging.run_id == "run-stage"
    assert staging.manifest_uri.startswith("s3://test-cache-bucket/")
    assert staging.manifest_uri.endswith("manifest.json")
    assert staging.dem_uri.endswith("dem.tif")
    # the DEM bytes were uploaded.
    dem_key = next(k for k in puts if k.endswith("dem.tif"))
    assert puts[dem_key] == b"FAKE_DEM_BYTES"
    # the manifest carries inputs[].gs_uri (legacy field name) + build_spec.
    man_key = next(k for k in puts if k.endswith("manifest.json"))
    man = json.loads(puts[man_key].decode())
    assert man["dem_dest"] == "dem.tif"
    assert man["inputs"][0]["gs_uri"] == staging.dem_uri
    assert man["inputs"][0]["dest"] == "dem.tif"
    assert man["build_spec"]["analysis"] == "landslide_probability"
    assert man["outputs"] == ["*.tif"]


def test_stage_landlab_manifest_typed_error_on_upload_failure(tmp_path, monkeypatch):
    from trid3nt_server.workflows import run_landlab as RL
    from trid3nt_server.workflows.run_landlab import LandlabWorkflowError

    dem = tmp_path / "dem.tif"
    dem.write_bytes(b"x")

    class _BoomS3:
        def put_object(self, **kw):  # noqa: ANN003
            raise RuntimeError("s3 down")

    from trid3nt_server.tools.simulation import solver as _solver
    from trid3nt_server.tools import cache as _cache

    monkeypatch.setattr(_solver, "_get_s3_client", lambda: _BoomS3())
    monkeypatch.setattr(_cache, "storage_scheme", lambda: "s3")
    monkeypatch.setenv("TRID3NT_CACHE_BUCKET", "test-cache-bucket")

    a = LandlabRunArgs(bbox=(-122.5, 45.4, -122.4, 45.5))
    with pytest.raises(LandlabWorkflowError) as exc:
        RL.stage_landlab_manifest(a, dem_path=str(dem), run_id="run-x")
    assert exc.value.error_code == "LANDLAB_STAGING_FAILED"


# ===========================================================================
# (4) postprocess — synthetic field COG, upload stubbed (rasterio required).
# ===========================================================================
rasterio = pytest.importorskip("rasterio")


def _write_synthetic_field_cog(path: Path, *, values: np.ndarray) -> None:
    """Write a synthetic probability-of-failure field in a metric CRS (UTM)."""
    from rasterio.crs import CRS
    from rasterio.transform import from_origin

    nrows, ncols = values.shape
    profile = {
        "driver": "GTiff",
        "dtype": "float32",
        "count": 1,
        "height": nrows,
        "width": ncols,
        "crs": CRS.from_epsg(32610),  # UTM 10N (valid projected metres)
        "transform": from_origin(500000.0, 5000000.0, 30.0, 30.0),
        "nodata": float("nan"),
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(values.astype("float32"), 1)


def test_compute_landlab_metrics_landslide():
    """Probability field: unstable fraction = cells >= 0.75; mean PoF over active."""
    from trid3nt_server.workflows.postprocess_landlab import compute_landlab_metrics

    field = np.full((4, 4), np.nan)
    field[0, 0] = 0.9   # unstable
    field[0, 1] = 0.8   # unstable
    field[1, 1] = 0.2   # stable
    field[2, 2] = 0.5   # stable
    m = compute_landlab_metrics(field, analysis="landslide_probability")
    assert m["active_cell_count"] == 4
    assert m["unstable_area_fraction"] == pytest.approx(2 / 4)
    assert m["mean_probability_of_failure"] == pytest.approx((0.9 + 0.8 + 0.2 + 0.5) / 4)
    # min_fos is not derivable from probability alone -> 0 (authoritative = worker).
    assert m["min_factor_of_safety"] == 0.0


def test_compute_landlab_metrics_overland():
    """Depth field: unstable=wet fraction (>=0.05 m); min_fos carries peak depth."""
    from trid3nt_server.workflows.postprocess_landlab import compute_landlab_metrics

    field = np.full((3, 3), np.nan)
    field[0, 0] = 0.10   # wet
    field[0, 1] = 0.02   # dry (sub-threshold)
    field[1, 1] = 1.50   # wet, peak
    m = compute_landlab_metrics(field, analysis="overland_flow")
    assert m["unstable_area_fraction"] == pytest.approx(2 / 3)
    assert m["min_factor_of_safety"] == pytest.approx(1.50)  # peak depth
    assert m["mean_probability_of_failure"] == 0.0


def test_postprocess_landlab_emits_4326_cog(tmp_path, monkeypatch):
    """A synthetic metric-CRS field COG reprojects to a VALID EPSG:4326 COG; the
    worker result block is preferred for the narration scalars."""
    from trid3nt_server.workflows import postprocess_landlab as PP

    field = np.full((8, 8), 0.1, dtype="float32")
    field[3:5, 3:5] = 0.95  # a high-susceptibility patch
    src = tmp_path / "landlab_field.tif"
    _write_synthetic_field_cog(src, values=field)

    captured: dict[str, Path] = {}

    def _capture_upload(local_cog, run_id, runs_bucket=None, *, dest_filename="landlab_susceptibility.tif"):  # noqa: ANN001
        import shutil

        keep = tmp_path / f"keep_{dest_filename}"
        shutil.copy(str(local_cog), str(keep))
        captured["cog"] = keep
        return f"s3://test-runs/{run_id}/{dest_filename}"

    monkeypatch.setattr(PP, "_upload_cog_to_runs_bucket", _capture_upload)

    # Worker result block carries the authoritative min_factor_of_safety the
    # probability field alone cannot derive.
    result = {
        "unstable_area_fraction": 0.0625,
        "min_factor_of_safety": 0.88,
        "mean_probability_of_failure": 0.153,
    }
    layers, metrics = PP.postprocess_landlab(
        src, run_id="run-pp", analysis="landslide_probability", result=result
    )

    assert len(layers) == 1
    layer = layers[0]
    assert isinstance(layer, LandlabSusceptibilityLayerURI)
    assert layer.role == "primary"
    assert layer.style_preset == "continuous_landslide_susceptibility"
    assert layer.layer_type == "raster"
    assert layer.uri.endswith("landlab_susceptibility.tif")
    # worker result block won (authoritative).
    assert layer.min_factor_of_safety == pytest.approx(0.88)
    assert layer.mean_probability_of_failure == pytest.approx(0.153)
    assert layer.bbox is not None
    assert metrics["crs"] == "EPSG:4326"

    # the produced COG is a valid 4326 COG (TiTiler-wedge / CRS round-trip guard).
    with rasterio.open(captured["cog"]) as ds:
        assert ds.crs is not None
        assert ds.crs.to_epsg() == 4326, ds.crs
        assert ds.count == 1
        assert abs(ds.bounds.left) <= 360.0


def test_postprocess_landlab_recomputes_when_result_absent(tmp_path, monkeypatch):
    """When the worker result block is absent, scalars are RECOMPUTED from the
    field (honest under-report, never invented)."""
    from trid3nt_server.workflows import postprocess_landlab as PP

    field = np.full((4, 4), 0.1, dtype="float32")
    field[0, 0] = 0.9  # 1 unstable cell of 16
    src = tmp_path / "landlab_field.tif"
    _write_synthetic_field_cog(src, values=field)

    monkeypatch.setattr(
        PP,
        "_upload_cog_to_runs_bucket",
        lambda c, r, b=None, *, dest_filename="x.tif": f"s3://t/{r}/{dest_filename}",
    )
    layers, _ = PP.postprocess_landlab(
        src, run_id="run-norez", analysis="landslide_probability", result=None
    )
    layer = layers[0]
    assert layer.unstable_area_fraction == pytest.approx(1 / 16)
    # recompute path leaves min_fos at 0 (no FoS field available).
    assert layer.min_factor_of_safety == 0.0


def test_postprocess_landlab_missing_cog_raises_typed(tmp_path):
    from trid3nt_server.workflows.postprocess_landlab import (
        PostprocessLandlabError,
        postprocess_landlab,
    )

    with pytest.raises(PostprocessLandlabError) as exc:
        postprocess_landlab(
            tmp_path / "nope.tif", run_id="x", analysis="landslide_probability"
        )
    assert exc.value.error_code == "LANDLAB_OUTPUT_READ_FAILED"


# ===========================================================================
# (5) Composer arg-assembly — every external call mocked.
# ===========================================================================
def test_model_landslide_scenario_chain_mocked(tmp_path, monkeypatch):
    """The composer drives fetch -> stage -> run_solver -> wait -> download ->
    postprocess -> publish with ALL external calls mocked and returns a
    LandlabSusceptibilityLayerURI carrying the narration scalars."""
    from trid3nt_server.tools.simulation import solver as _solver
    from trid3nt_server.workflows import model_landslide_scenario as M
    from trid3nt_server.workflows.run_landlab import LandlabStaging

    # A synthetic DEM so the DEM fetch is skipped (dem_path supplied).
    dem = tmp_path / "dem.tif"
    _write_synthetic_field_cog(dem, values=np.full((8, 8), 100.0, dtype="float32"))

    _RUN_ID = "compose-run"
    _WORKER_ID = "worker-run"

    # stage -> a staging carrying a deterministic manifest uri.
    def _fake_stage(run_args, *, dem_path, run_id):  # noqa: ANN001
        return LandlabStaging(
            run_id=run_id,
            manifest_uri="s3://cache/landlab_setup/run/manifest.json",
            dem_uri="s3://cache/landlab_setup/run/dem.tif",
            run_args=run_args,
            build_spec={"analysis": run_args.analysis},
        )

    monkeypatch.setattr(M, "stage_landlab_manifest", _fake_stage)

    # run_solver -> handle; wait_for_completion -> a 'complete' RunResult.
    class _Handle:
        run_id = _WORKER_ID
        handle_id = "h"
        solver = "landlab"

    class _RunResult:
        status = "complete"
        run_id = _WORKER_ID
        output_uri = f"s3://runs/{_WORKER_ID}/"
        error_code = None
        error_message = None

    monkeypatch.setattr(_solver, "run_solver", lambda **kw: _Handle())

    async def _fake_wait(handle):  # noqa: ANN001
        return _RunResult()

    monkeypatch.setattr(_solver, "wait_for_completion", _fake_wait)
    monkeypatch.setattr(_solver, "new_ulid", lambda: _RUN_ID)

    # download -> a local field COG + a worker result block.
    field_cog = tmp_path / "landlab_field.tif"
    field = np.full((8, 8), 0.1, dtype="float32")
    field[2:4, 2:4] = 0.9
    _write_synthetic_field_cog(field_cog, values=field)

    def _fake_download(run_result, run_id):  # noqa: ANN001
        # levers STEP 3: _download_batch_landlab_outputs now returns a 4-tuple
        # (the 4th is the secondary-field token->local-path map; empty here).
        return (
            str(field_cog),
            {
                "unstable_area_fraction": 0.0625,
                "min_factor_of_safety": 0.91,
                "mean_probability_of_failure": 0.2,
            },
            str(tmp_path / "batch-out"),
            {},
        )

    monkeypatch.setattr(M, "_download_batch_landlab_outputs", _fake_download)
    # postprocess upload stub.
    from trid3nt_server.workflows import postprocess_landlab as PP

    monkeypatch.setattr(
        PP,
        "_upload_cog_to_runs_bucket",
        lambda c, r, b=None, *, dest_filename="x.tif": f"s3://runs/{r}/{dest_filename}",
    )
    # publish_layer -> a renderable https URL (the render chokepoint).
    monkeypatch.setattr(
        M, "publish_layer", lambda **kw: "https://tiles.example/landlab/{z}/{x}/{y}.png"
    )

    primary = asyncio.run(
        M.model_landslide_scenario(
            LandlabRunArgs(
                bbox=(-122.5, 45.4, -122.4, 45.5), analysis="landslide_probability"
            ),
            dem_path=str(dem),
        )
    )

    assert isinstance(primary, LandlabSusceptibilityLayerURI)
    assert primary.uri.startswith("https://")
    assert primary.layer_id == f"landlab-susceptibility-{_RUN_ID}"
    # worker result block scalars surfaced (Invariant 1 — typed, not invented).
    assert primary.min_factor_of_safety == pytest.approx(0.91)
    assert primary.mean_probability_of_failure == pytest.approx(0.2)
    # bbox stamped to the floored AOI.
    assert primary.bbox is not None


def test_model_landslide_scenario_run_failure_raises_typed(tmp_path, monkeypatch):
    """A non-complete Batch solve surfaces a typed LANDLAB_RUN_FAILED."""
    from trid3nt_server.tools.simulation import solver as _solver
    from trid3nt_server.workflows import model_landslide_scenario as M
    from trid3nt_server.workflows.run_landlab import LandlabStaging, LandlabWorkflowError

    dem = tmp_path / "dem.tif"
    _write_synthetic_field_cog(dem, values=np.full((6, 6), 50.0, dtype="float32"))

    monkeypatch.setattr(
        M,
        "stage_landlab_manifest",
        lambda ra, *, dem_path, run_id: LandlabStaging(
            run_id=run_id, manifest_uri="s3://c/m.json", dem_uri="s3://c/dem.tif",
            run_args=ra, build_spec={},
        ),
    )
    monkeypatch.setattr(_solver, "new_ulid", lambda: "rid")

    class _Handle:
        run_id = "w"
        handle_id = "h"
        solver = "landlab"

    class _Failed:
        status = "failed"
        run_id = "w"
        output_uri = None
        error_code = "SOLVER_FAILED"
        error_message = "boom"

    monkeypatch.setattr(_solver, "run_solver", lambda **kw: _Handle())

    async def _fake_wait(handle):  # noqa: ANN001
        return _Failed()

    monkeypatch.setattr(_solver, "wait_for_completion", _fake_wait)

    with pytest.raises(LandlabWorkflowError) as exc:
        asyncio.run(
            M.model_landslide_scenario(
                LandlabRunArgs(bbox=(-122.5, 45.4, -122.4, 45.5)),
                dem_path=str(dem),
            )
        )
    assert exc.value.error_code == "LANDLAB_RUN_FAILED"


# ===========================================================================
# (6) REGRESSION — _download_batch_landlab_outputs must pick landlab_field.tif
#     specifically, never "any .tif in output_uris" (the live-proven bug: a
#     completion.json listing dem.tif BEFORE landlab_field.tif in output_uris
#     caused the composer to feed raw DEM elevations into the
#     probability-of-failure metrics, producing a degenerate
#     unstable_area_fraction=1.0 / mean_probability_of_failure=1.0 result).
# ===========================================================================
def test_download_batch_landlab_outputs_picks_field_cog_not_dem(tmp_path, monkeypatch):
    """completion.json's output_uris lists dem.tif FIRST (a real worker shape --
    the local-exec supervisor re-uploads the staged DEM alongside the worker
    outputs); _download_batch_landlab_outputs must still download
    landlab_field.tif, not whichever .tif key happens to sort/list first."""
    from trid3nt_server.tools.simulation import solver as _solver
    from trid3nt_server.workflows import model_landslide_scenario as M

    run_id = "field-select-rid"
    runs_bucket = "trid3nt-runs"

    completion = {
        "run_id": run_id,
        "status": "ok",
        "exit_code": 0,
        "output_uris": [
            f"s3://{runs_bucket}/{run_id}/dem.tif",
            f"s3://{runs_bucket}/{run_id}/landlab_field.tif",
            f"s3://{runs_bucket}/{run_id}/landlab_secondary_slope.tif",
        ],
        "result": None,  # local-exec: no worker result block (recompute fallback)
    }

    monkeypatch.setattr(
        _solver, "_get_runs_bucket", lambda: runs_bucket
    )
    monkeypatch.setattr(
        _solver, "_try_get_completion_s3", lambda bucket, rid: completion
    )

    downloaded_keys: list[str] = []

    class _FakeBody:
        def __init__(self, data: bytes) -> None:
            self._data = data
            self._consumed = False

        def read(self, *_args, **_kwargs) -> bytes:
            # shutil.copyfileobj loops read() -> write() until an EMPTY read
            # signals EOF; a stub that always returns non-empty bytes spins
            # forever (a real ENOSPC, not a mock artifact) — mirror a real
            # file-like object's one-shot-then-EOF behavior.
            if self._consumed:
                return b""
            self._consumed = True
            return self._data

    class _FakeS3:
        def get_object(self, *, Bucket: str, Key: str):  # noqa: N803
            downloaded_keys.append(Key)
            # Distinct byte payloads so a wrong pick is detectable.
            tag = Key.rsplit("/", 1)[-1].encode()
            return {"Body": _FakeBody(b"FAKE-BYTES-" + tag)}

    monkeypatch.setattr(_solver, "_get_s3_client", lambda: _FakeS3())

    local_field, result_block, out_dir, secondary = M._download_batch_landlab_outputs(
        run_result=None, run_id=run_id
    )

    # Only the field COG was downloaded (not dem.tif / the secondary COG).
    assert downloaded_keys == [f"{run_id}/landlab_field.tif"]
    assert Path(local_field).name == "landlab_field.tif"
    with open(local_field, "rb") as fh:
        assert fh.read() == b"FAKE-BYTES-landlab_field.tif"
    assert result_block == {}
    assert secondary == {}
