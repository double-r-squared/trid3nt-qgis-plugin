"""End-to-end MODULE tests for the SWAN (Simulating WAves Nearshore) spectral
wave engine (Phase 1), exercised in ISOLATION with run_solver / boto3 / network
MOCKED.

SWAN is NEW + BATCH-only + the ADDITIVE comparison engine (standalone wave field
vs SFINCS+SnapWave). These tests pin the agent-side MODULES the lane owns:

  1. **Contract round-trip + mode alias normalization** -- ``SwanRunArgs`` /
     ``WaveFieldLayerURI`` (no SWAN dep).
  2. **build_spec assembly** -- ``build_swan_build_spec`` maps run args onto the
     worker's deck_builder field dict (incl. the synthesized demo boundary).
  3. **Solver registration** -- ``'swan'`` is a first-class entry in
     ``SOLVER_WORKFLOW_REGISTRY`` and the bridge tool is registered with typed
     errors.
  4. **postprocess on a SYNTHETIC swan_out.mat** -- a hand-built SWAN Matlab BLOCK
     output reads + rasterizes + writes a VALID EPSG:4326 Hs COG (upload stubbed),
     yielding the EXACT postprocess_waves (layers, metrics) shape, AND the honesty
     floor (all-calm -> SWAN_OUTPUT_EMPTY).
  5. **Composer arg-assembly with run_solver MOCKED** -- the composer stages a
     manifest, dispatches via a mocked run_solver/wait_for_completion, downloads a
     mocked Batch output, postprocesses, and returns the peak WaveFieldLayerURI +
     emits frames out-of-band -- all without touching AWS or SWAN.

scipy + rasterio + numpy are required for (4)+(5); they are in the agent venv.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from trid3nt_contracts.swan_contracts import (
    SWAN_WAVE_HEIGHT_STYLE_PRESET,
    SwanRunArgs,
    SwanWaveBoundary,
    WaveFieldLayerURI,
)

_AOI = (-85.75, 29.55, -85.25, 30.20)

# The web parseFrameToken regex -- the frame NAMES must match it or the sequential
# group never forms (same guard as test_run_geoclaw_chain).
_WEB_STEP_TOKEN_RE = re.compile(r"\b(?:step|frame|idx|index)\s*\+?(\d{1,4})\b", re.I)


# ===========================================================================
# (1) Contract round-trip + mode alias normalization.
# ===========================================================================
def test_run_args_round_trip_and_mode_aliases():
    a = SwanRunArgs(bbox=_AOI, mode="peak")
    assert a.mode == "stationary"  # alias
    b = SwanRunArgs(bbox=_AOI, mode="transient")
    assert b.mode == "nonstationary"  # alias
    c = SwanRunArgs(bbox=_AOI, mode="time_series")
    assert c.mode == "nonstationary"  # alias
    # round-trip.
    a2 = SwanRunArgs(**a.model_dump())
    assert a2 == a


def test_run_args_rejects_bad_mode_and_freq_band():
    with pytest.raises(Exception):
        SwanRunArgs(bbox=_AOI, mode="not_a_mode")
    with pytest.raises(Exception):
        SwanRunArgs(bbox=_AOI, freq_low_hz=1.0, freq_high_hz=0.5)
    with pytest.raises(Exception):
        SwanRunArgs(bbox=_AOI, n_dir=4)  # < 12


def test_wave_field_layer_uri_round_trip():
    lyr = WaveFieldLayerURI(
        layer_id="swan-wave-height-peak-x",
        name="Peak wave height",
        layer_type="raster",
        uri="s3://b/k.tif",
        style_preset=SWAN_WAVE_HEIGHT_STYLE_PRESET,
        role="primary",
        units="meters",
        bbox=_AOI,
        max_hs_m=3.4,
        mean_tp_s=8.7,
        mean_dir_deg=182.0,
        wave_area_km2=12.3,
        mode="stationary",
    )
    assert WaveFieldLayerURI(**lyr.model_dump()) == lyr
    # reuses the shared SnapWave wave-height preset (no new style key).
    assert lyr.style_preset == "continuous_wave_height"


# ===========================================================================
# (2) build_spec assembly.
# ===========================================================================
def test_build_spec_synthesizes_demo_boundary():
    from trid3nt_server.workflows.run_swan import build_swan_build_spec

    args = SwanRunArgs(bbox=_AOI, mode="stationary")  # no explicit boundary
    spec = build_swan_build_spec(args, mesh_cells=(60, 80))
    assert spec["mode"] == "stationary"
    assert spec["bbox"] == list(_AOI)
    assert spec["bottom_file"] == "bottom.bot"
    assert spec["mx"] == 60 and spec["my"] == 80
    # demo boundary synthesized from the AOI geometry: this AOI is taller (N-S)
    # than wide (E-W), so the offshore-facing side is East (the height>=width
    # heuristic in synthesize_demo_wave_boundary).
    assert spec["boundary"]["side"] == "E"
    assert spec["boundary"]["hs_m"] > 0.0
    # no wind file unless wind_uri was set.
    assert "wind_file" not in spec
    assert spec["output_quantities"] == ["HSIGN", "RTP", "DIR"]


def test_build_spec_respects_explicit_boundary_and_wind():
    from trid3nt_server.workflows.run_swan import build_swan_build_spec

    args = SwanRunArgs(
        bbox=_AOI,
        mode="nonstationary",
        boundary=SwanWaveBoundary(hs_m=5.0, tp_s=12.0, dir_deg=200.0, side="E"),
        wind_uri="s3://cache/wind.dat",
    )
    spec = build_swan_build_spec(args, wind_dest="wind.dat")
    assert spec["mode"] == "nonstationary"
    assert spec["boundary"]["hs_m"] == 5.0
    assert spec["boundary"]["side"] == "E"
    assert spec["wind_file"] == "wind.dat"


# ===========================================================================
# (3) Solver registration + bridge tool registered.
# ===========================================================================
def test_swan_registered_in_solver_workflow_registry():
    from trid3nt_server.tools.solver import SOLVER_WORKFLOW_REGISTRY
    from trid3nt_server.workflows.run_swan import (
        SWAN_SOLVER_NAME,
        register_swan_solver,
    )

    register_swan_solver()  # idempotent
    assert SWAN_SOLVER_NAME in SOLVER_WORKFLOW_REGISTRY


def test_run_swan_waves_registered_in_tool_registry():
    import trid3nt_server.tools  # noqa: F401 -- fire eager imports
    from trid3nt_server.tools import TOOL_REGISTRY

    assert "run_swan_waves" in TOOL_REGISTRY


def test_run_swan_waves_typed_error_on_missing_bbox():
    import asyncio

    from trid3nt_server.tools.run_swan_tool import run_swan_waves

    out = asyncio.run(run_swan_waves(bbox=None))
    assert isinstance(out, dict)
    assert out["status"] == "error"
    assert out["error_code"] == "SWAN_PARAMS_INCOMPLETE"

    out2 = asyncio.run(run_swan_waves(bbox="garbage"))
    assert out2["status"] == "error"
    assert out2["error_code"] == "SWAN_PARAMS_INVALID"


# ===========================================================================
# (3b) REGRESSION: _fetch_bathy_for_swan REQUIRES real bathymetry.
# ===========================================================================
# Root cause of the live 2026-06-23 Mexico Beach all-dry no-op: fetch_topobathy
# can degrade to a LAND-ONLY 3DEP surface (bathymetry_present=False), which has no
# below-datum sea cells, so the SWAN bottom grid is entirely dry and SWAN no-ops
# (empty solve). The old fetch only checked ``.uri`` and SILENTLY fed that DEM. We
# now reject a bathymetry-absent result with a typed SWAN_NO_BATHYMETRY error.
def test_fetch_bathy_rejects_land_only_dem():
    from trid3nt_server.workflows.model_wave_scenario import (
        SwanComposerError,
        _fetch_bathy_for_swan,
    )

    bbox = (-85.55, 29.8, -85.25, 30.05)

    class _LandOnlyLayer:
        uri = "s3://cache/land-only.tif"
        bathymetry_present = False
        fallback_warning = "no CUDEM coverage; degraded to 3DEP land-only"

    with patch(
        "trid3nt_server.tools.fetch_topobathy.fetch_topobathy",
        lambda b: _LandOnlyLayer(),
    ):
        with pytest.raises(SwanComposerError) as ei:
            _fetch_bathy_for_swan(bbox)
    assert ei.value.error_code == "SWAN_NO_BATHYMETRY"
    assert "land-only" in str(ei.value).lower() or "below-datum" in str(ei.value).lower()


def test_fetch_bathy_accepts_real_bathymetry():
    from trid3nt_server.workflows.model_wave_scenario import _fetch_bathy_for_swan

    bbox = (-85.55, 29.8, -85.25, 30.05)

    class _SeamlessLayer:
        uri = "s3://cache/topobathy.tif"
        bathymetry_present = True
        fallback_warning = None

    with patch(
        "trid3nt_server.tools.fetch_topobathy.fetch_topobathy",
        lambda b: _SeamlessLayer(),
    ):
        uri = _fetch_bathy_for_swan(bbox)
    assert uri == "s3://cache/topobathy.tif"


def test_fetch_bathy_typed_error_when_topobathy_fails():
    from trid3nt_server.workflows.model_wave_scenario import (
        SwanComposerError,
        _fetch_bathy_for_swan,
    )

    bbox = (-85.55, 29.8, -85.25, 30.05)

    def _boom(_b):
        raise RuntimeError("CUDEM host unreachable")

    with patch(
        "trid3nt_server.tools.fetch_topobathy.fetch_topobathy", _boom
    ):
        with pytest.raises(SwanComposerError) as ei:
            _fetch_bathy_for_swan(bbox)
    assert ei.value.error_code == "SWAN_DEM_FETCH_FAILED"


# ===========================================================================
# (4) postprocess on a SYNTHETIC swan_out.mat (upload stubbed).
# ===========================================================================
def _synthetic_swan_mat(
    path: Path, mx: int, my: int, hs_fn, *, frames: int = 1, with_tp_dir: bool = True
) -> None:
    """Write a SWAN-style swan_out.mat with Hsig / RTp / Dir arrays.

    SWAN writes one variable per quantity (stationary) or per frame
    (nonstationary, with a frame suffix). ``hs_fn(i, j, frame) -> hs`` supplies Hs;
    Tp/Dir are constants when ``with_tp_dir``. Row 0 = south (SWAN idla=1).
    """
    from scipy.io import savemat

    mat: dict = {}
    for f in range(frames):
        hs = np.zeros((my, mx), dtype="float64")
        for j in range(my):
            for i in range(mx):
                hs[j, i] = float(hs_fn(i, j, f))
        suffix = "" if frames == 1 else f"_{f + 1:02d}"
        mat[f"Hsig{suffix}"] = hs
        if with_tp_dir:
            mat[f"RTp{suffix}"] = np.where(hs > 0.0, 9.0, -999.0)
            mat[f"Dir{suffix}"] = np.where(hs > 0.0, 180.0, -999.0)
    savemat(str(path), mat)


def test_read_swan_mat_fields_reads_hs_tp_dir(tmp_path: Path):
    from trid3nt_server.workflows.postprocess_swan import read_swan_mat_fields

    mat = tmp_path / "swan_out.mat"
    _synthetic_swan_mat(mat, 6, 4, lambda i, j, f: 2.0 if j >= 2 else 0.0)
    fields = read_swan_mat_fields(mat)
    assert len(fields["hs"]) == 1
    assert len(fields["tp"]) == 1
    assert len(fields["dir"]) == 1
    hs = fields["hs"][0]
    assert hs.shape == (4, 6)
    assert np.nanmax(hs) == pytest.approx(2.0)


def test_compute_swan_wave_metrics_on_synthetic_grid():
    from trid3nt_server.workflows.postprocess_swan import compute_swan_wave_metrics

    hs = np.full((8, 8), 0.0)
    hs[4:, :] = 2.5  # half wave-bearing
    tp = np.where(hs > 0, 10.0, np.nan)
    dr = np.where(hs > 0, 190.0, np.nan)
    m = compute_swan_wave_metrics(hs, bbox=_AOI, tp_grid=tp, dir_grid=dr)
    assert m["max_hs_m"] == pytest.approx(2.5)
    assert m["mean_tp_s"] == pytest.approx(10.0)
    assert m["mean_dir_deg"] == pytest.approx(190.0, abs=0.5)
    assert m["wave_cell_count"] > 0
    assert m["wave_area_km2"] > 0.0


def _fake_upload(local_cog, run_id, runs_bucket=None, *, dest_filename="x.tif"):
    # assert the COG is a valid EPSG:4326 raster before "uploading".
    import rasterio

    with rasterio.open(local_cog) as ds:
        assert str(ds.crs) == "EPSG:4326"
        assert ds.count == 1
    return f"s3://fake-runs/{run_id}/{dest_filename}"


def test_postprocess_swan_end_to_end_shape(tmp_path: Path):
    """A multi-frame synthetic run yields the EXACT (layers, metrics) shape:
    peak primary + contiguous 'Wave height step N' frames, all VALID COGs."""
    from trid3nt_server.workflows import postprocess_swan as ps

    mat = tmp_path / "swan_out.mat"
    # 5 frames; wave height rises then falls so the peak is the middle frame.
    amps = [0.5, 1.5, 3.0, 1.0, 0.2]

    def hs_fn(i, j, f):
        return amps[f] if (i + j) % 2 == 0 else 0.0

    _synthetic_swan_mat(mat, 10, 10, hs_fn, frames=5)

    with patch.object(ps, "_upload_cog_to_runs_bucket", _fake_upload):
        layers, metrics = ps.postprocess_swan(
            tmp_path, _AOI, run_id="RID123", mode="nonstationary"
        )

    # layers[0] = peak primary.
    peak = layers[0]
    assert isinstance(peak, WaveFieldLayerURI)
    assert peak.role == "primary"
    assert peak.name == "Peak wave height"
    assert peak.style_preset == "continuous_wave_height"
    assert peak.mode == "nonstationary"
    assert peak.max_hs_m == pytest.approx(3.0)  # the middle frame amplitude
    assert peak.uri.startswith("s3://fake-runs/RID123/swan_wave_height_peak.tif")
    assert metrics["max_hs_m"] == pytest.approx(3.0)

    # layers[1:] = contiguous 'Wave height step N' frames, distinct URIs.
    frames = layers[1:]
    assert len(frames) >= 2
    uris = set()
    for n, fr in enumerate(frames, start=1):
        assert fr.role == "context"
        assert fr.name == f"Wave height step {n}"
        assert _WEB_STEP_TOKEN_RE.search(fr.name)
        uris.add(fr.uri)
    assert len(uris) == len(frames)  # distinct keys -> no dedup collapse


# ===========================================================================
# (4b) RENDER FIX: the Hs COG must be upsampled so it carries internal OVERVIEWS.
# ===========================================================================
# Root cause of the live 2026-06-23 "SWAN solves but the wave layer never paints"
# (run 01KVSNNBKSHXAWPGGVD5DKV1C9): the SWAN mesh is coarse (a 101x101 BLOCK
# output), so the Hs COG was too small for the GDAL COG driver to build internal
# overviews. A no-overview COG makes TiTiler report a 1-level (min==max) tilejson
# zoom window, so the MapLibre raster source paints only inside that narrow band /
# times out cold -- the layer row appears in the panel but no raster lands on the
# map. The fix upsamples the masked Hs grid (nearest-neighbour, no data invented)
# so the COG crosses the overview-build threshold.
def test_upsample_for_cog_preserves_values_and_mask():
    from trid3nt_server.workflows.postprocess_swan import _upsample_for_cog

    # a tiny 4x4 grid with a calm/wave NaN edge.
    g = np.array(
        [
            [1.0, 2.0, np.nan, np.nan],
            [1.0, 2.0, np.nan, np.nan],
            [3.0, 4.0, np.nan, np.nan],
            [3.0, 4.0, np.nan, np.nan],
        ],
        dtype="float32",
    )
    up = _upsample_for_cog(g, min_dim_px=16)
    assert max(up.shape) >= 16
    # nearest-neighbour invents NO new values: the value set is unchanged.
    finite_in = set(np.round(g[np.isfinite(g)], 6).tolist())
    finite_out = set(np.round(up[np.isfinite(up)], 6).tolist())
    assert finite_out == finite_in
    # the NaN (calm) mask is preserved proportionally (no wave bled into calm).
    assert np.isnan(up).any()
    # no-op when already large enough.
    big = np.ones((20, 20), dtype="float32")
    assert _upsample_for_cog(big, min_dim_px=16).shape == (20, 20)


def test_written_hs_cog_has_overviews(tmp_path: Path):
    """A coarse SWAN grid must still produce a COG WITH internal overviews + a
    multi-level zoom span (the no-overview COG is what made the layer not paint)."""
    import rasterio

    from trid3nt_server.workflows.postprocess_swan import (
        _COG_MIN_DIM_PX,
        _write_hs_cog_4326,
    )

    # a coarse 100x100 wave field (matches the live SWAN deck mesh).
    hs = np.full((100, 100), 0.0, dtype="float32")
    hs[40:, :] = 3.0  # a wave-bearing band
    cog = _write_hs_cog_4326(hs, _AOI)
    try:
        with rasterio.open(cog) as ds:
            assert str(ds.crs) == "EPSG:4326"
            # upsampled past the overview-build threshold.
            assert max(ds.width, ds.height) >= _COG_MIN_DIM_PX
            # the decisive assertion: band-1 carries internal overviews, so
            # TiTiler reports a real multi-level tilejson zoom window.
            assert len(ds.overviews(1)) >= 1
    finally:
        cog.unlink(missing_ok=True)


def test_postprocess_swan_peak_cog_renderable_with_overviews(tmp_path: Path):
    """End-to-end: the published peak COG is a /tiles-renderable raster carrying
    overviews + the wave-height style preset (the full no-paint fix)."""
    import rasterio

    from trid3nt_server.workflows import postprocess_swan as ps

    mat = tmp_path / "swan_out.mat"
    _synthetic_swan_mat(mat, 100, 100, lambda i, j, f: 3.0 if j >= 40 else 0.0)

    captured: dict = {}

    def _verify_upload(local_cog, run_id, runs_bucket=None, *, dest_filename="x.tif"):
        with rasterio.open(local_cog) as ds:
            assert str(ds.crs) == "EPSG:4326"
            captured.setdefault("overviews", []).append(len(ds.overviews(1)))
            captured.setdefault("dims", []).append(max(ds.width, ds.height))
        return f"s3://fake-runs/{run_id}/{dest_filename}"

    with patch.object(ps, "_upload_cog_to_runs_bucket", _verify_upload):
        layers, _metrics = ps.postprocess_swan(
            tmp_path, _AOI, run_id="RIDOVR", mode="stationary"
        )

    peak = layers[0]
    # the layer carries the wave-height preset -> publish_layer resolves it to
    # &rescale=0,6&colormap_name=gnbu (the SWAN wave ramp), NOT a raw s3://.
    assert peak.style_preset == "continuous_wave_height"
    assert peak.uri.startswith("s3://fake-runs/RIDOVR/swan_wave_height_peak.tif")
    # every written COG carried overviews.
    assert captured["overviews"] and all(n >= 1 for n in captured["overviews"])
    assert all(d >= 512 for d in captured["dims"])


def test_swan_wave_height_preset_resolves_to_titiler_rescale_colormap():
    """The SWAN wave-height preset must resolve to a TiTiler /tiles URL with a
    valid rescale + colormap (gnbu over 0..6 m) -- never a washed-out empty style
    or a raw s3://. This is the publish-side half of the render contract."""
    from trid3nt_server.tools.publish_layer import _registry_style_params

    assert SWAN_WAVE_HEIGHT_STYLE_PRESET == "continuous_wave_height"
    params = _registry_style_params("continuous_wave_height")
    assert params is not None
    assert "rescale=0,6" in params
    assert "colormap_name=gnbu" in params


def test_postprocess_swan_empty_output_raises(tmp_path: Path):
    from trid3nt_server.workflows.postprocess_swan import (
        PostprocessSwanError,
        postprocess_swan,
    )

    # no swan_out.mat at all -> SWAN_OUTPUT_EMPTY.
    with pytest.raises(PostprocessSwanError) as ei:
        postprocess_swan(tmp_path, _AOI, run_id="X", mode="stationary")
    assert ei.value.error_code == "SWAN_OUTPUT_EMPTY"


def test_postprocess_swan_all_calm_raises_honesty_floor(tmp_path: Path):
    """Honesty floor: a run whose Hs is everywhere below the calm threshold is NOT
    a usable wave field -- it raises SWAN_OUTPUT_EMPTY, never status ok."""
    from trid3nt_server.workflows.postprocess_swan import (
        PostprocessSwanError,
        postprocess_swan,
    )

    mat = tmp_path / "swan_out.mat"
    _synthetic_swan_mat(mat, 8, 8, lambda i, j, f: 0.0)  # all calm
    with pytest.raises(PostprocessSwanError) as ei:
        postprocess_swan(tmp_path, _AOI, run_id="X", mode="stationary")
    assert ei.value.error_code == "SWAN_OUTPUT_EMPTY"


# ===========================================================================
# (5) Composer arg-assembly with run_solver / wait_for_completion MOCKED.
# ===========================================================================
class _FakeHandle:
    run_id = "BATCHRID"
    workflow_name = "aws-batch"


class _FakeRunResult:
    run_id = "BATCHRID"
    status = "complete"
    output_uri = "s3://runs/BATCHRID/"
    error_code = None
    error_message = None
    cancellation_reason = None
    batch_compute_meta = {"instance_type": "c7i.2xlarge"}


def test_composer_arg_assembly_and_dispatch(tmp_path: Path):
    """The composer stages a manifest, dispatches via a MOCKED run_solver, and
    returns the peak WaveFieldLayerURI -- no AWS, no SWAN. Asserts the run_solver
    call carries solver='swan' + the staged manifest_uri."""
    import asyncio

    from trid3nt_server.workflows import model_wave_scenario as comp
    from trid3nt_server.workflows.run_swan import SwanStaging

    run_args = SwanRunArgs(bbox=_AOI, mode="nonstationary", output_frames=4)

    captured: dict = {}

    def _fake_stage(ra, *, dem_uri, run_id=None, wind_uri=None, mesh_cells=(100, 100)):
        captured["dem_uri"] = dem_uri
        return SwanStaging(
            run_id="STAGERID",
            manifest_uri="s3://cache/swan_setup/STAGERID/manifest.json",
            build_spec={"mode": "nonstationary"},
            run_args=ra,
            bbox=tuple(ra.bbox),
            n_active_cells=10000,
        )

    def _fake_run_solver(*, solver, model_setup_uri, compute_class):
        captured["solver"] = solver
        captured["model_setup_uri"] = model_setup_uri
        captured["compute_class"] = compute_class
        return _FakeHandle()

    async def _fake_wait(handle):
        return _FakeRunResult()

    def _fake_download(run_id):
        return str(tmp_path)  # a dir with no .mat -> postprocess is mocked too

    def _fake_postprocess(out_dir, bbox, *, run_id, mode, **_kw):
        peak = WaveFieldLayerURI(
            layer_id=f"swan-wave-height-peak-{run_id}",
            name="Peak wave height",
            layer_type="raster",
            uri=f"s3://runs/{run_id}/swan_wave_height_peak.tif",
            style_preset=SWAN_WAVE_HEIGHT_STYLE_PRESET,
            role="primary",
            units="meters",
            bbox=tuple(bbox),
            max_hs_m=3.3,
            mean_tp_s=8.8,
            mean_dir_deg=181.0,
            wave_area_km2=4.2,
            mode=mode,
        )
        return [peak], {"max_hs_m": 3.3}

    def _fake_publish(raw_peak, run_id):
        return raw_peak.model_copy(update={"uri": "https://tiles/peak.png"})

    # The composer imports run_solver / wait_for_completion / EmitterBinding /
    # set_emitter_binding INSIDE the function (from ..tools.solver import ...), so
    # they must be patched at the SOURCE module, not on the composer module.
    from trid3nt_server.tools import solver as solver_mod

    with patch.object(comp, "_fetch_bathy_for_swan", lambda b: "s3://cache/topo.tif"), \
         patch.object(comp, "stage_swan_manifest", _fake_stage), \
         patch.object(solver_mod, "run_solver", _fake_run_solver), \
         patch.object(solver_mod, "wait_for_completion", _fake_wait), \
         patch.object(solver_mod, "set_emitter_binding", lambda *a, **k: None), \
         patch.object(comp, "mint_dispatch_and_sim_cards", _amock(None)), \
         patch.object(comp, "route_sim_terminal", _amock(None)), \
         patch.object(comp, "_download_batch_swan_outputs", _fake_download), \
         patch.object(comp, "postprocess_swan", _fake_postprocess), \
         patch.object(comp, "_publish_peak_layer", _fake_publish), \
         patch.object(comp, "current_emitter", lambda: None), \
         patch.object(comp, "drive_live_solve_progress", _amock(None)):
        peak = asyncio.run(comp.model_wave_scenario(run_args))

    assert isinstance(peak, WaveFieldLayerURI)
    assert peak.uri == "https://tiles/peak.png"
    assert peak.max_hs_m == pytest.approx(3.3)
    assert captured["solver"] == "swan"
    assert captured["model_setup_uri"].endswith("manifest.json")
    assert captured["dem_uri"] == "s3://cache/topo.tif"


def _amock(ret):
    """Build an async no-op returning ``ret`` (for the emitter helpers)."""
    async def _inner(*a, **k):
        return ret
    return _inner


# --------------------------------------------------------------------------- #
# Boundary side/direction consistency (the live 2026-06-23 empty-raster bug).
# --------------------------------------------------------------------------- #
def test_coerce_boundary_snaps_net_outgoing_dir_inward():
    """side=S with dir=0 (waves from the north) is net-OUTGOING through the
    southern open-water boundary -> SWAN injects ~no energy and paints an empty
    raster. The coercion snaps it to the side-inward bearing (180 deg)."""
    from trid3nt_server.workflows.run_swan import _coerce_boundary_inward

    b = SwanWaveBoundary(hs_m=8.0, tp_s=12.0, dir_deg=0.0, spread_deg=25.0, side="S")
    c = _coerce_boundary_inward(b)
    assert c.dir_deg == pytest.approx(180.0)
    assert c.side == "S"
    # other fields preserved.
    assert c.hs_m == pytest.approx(8.0)
    assert c.tp_s == pytest.approx(12.0)


def test_coerce_boundary_keeps_sane_oblique_dir():
    """A direction within 90 deg of the side-inward bearing is a legitimate
    oblique sea-state and is left untouched."""
    from trid3nt_server.workflows.run_swan import _coerce_boundary_inward

    b = SwanWaveBoundary(hs_m=3.0, tp_s=9.0, dir_deg=160.0, spread_deg=25.0, side="S")
    assert _coerce_boundary_inward(b).dir_deg == pytest.approx(160.0)


def test_coerce_boundary_per_side_inward_bearings():
    """Each side snaps an opposite-pointing direction to its inward bearing."""
    from trid3nt_server.workflows.run_swan import _coerce_boundary_inward

    cases = {"N": (180.0, 0.0), "E": (270.0, 90.0), "S": (0.0, 180.0), "W": (90.0, 270.0)}
    for side, (bad_dir, want) in cases.items():
        b = SwanWaveBoundary(hs_m=3.0, tp_s=9.0, dir_deg=bad_dir, spread_deg=25.0, side=side)  # type: ignore[arg-type]
        assert _coerce_boundary_inward(b).dir_deg == pytest.approx(want), side


def test_build_swan_build_spec_applies_inward_coercion():
    """The contradictory pair reaches the worker as a corrected, inward dir."""
    from trid3nt_server.workflows.run_swan import build_swan_build_spec

    b = SwanWaveBoundary(hs_m=8.0, tp_s=12.0, dir_deg=0.0, spread_deg=25.0, side="S")
    args = SwanRunArgs(bbox=(-85.55, 29.85, -85.3, 30.05), boundary=b)
    spec = build_swan_build_spec(args)
    assert spec["boundary"]["side"] == "S"
    assert spec["boundary"]["dir_deg"] == pytest.approx(180.0)


# --------------------------------------------------------------------------- #
# Boundary SIDE free-text normalization (the transient "failed" card quirk).
# --------------------------------------------------------------------------- #
def test_normalize_boundary_side_words_and_phrases():
    """Words / phrases the LLM emits must coerce to a single cardinal so the
    strict Literal["N","S","E","W"] contract does not fail the first attempt."""
    from trid3nt_server.tools.run_swan_tool import _normalize_boundary_side

    assert _normalize_boundary_side("S") == "S"
    assert _normalize_boundary_side("south") == "S"
    assert _normalize_boundary_side("SOUTH ") == "S"
    assert _normalize_boundary_side("from the south") == "S"
    assert _normalize_boundary_side("the southern edge") == "S"
    assert _normalize_boundary_side("south-facing") == "S"
    assert _normalize_boundary_side("EAST") == "E"
    assert _normalize_boundary_side("northern") == "N"
    assert _normalize_boundary_side("west") == "W"
    # unparseable -> None (caller drops it, demo default applies, no failure).
    assert _normalize_boundary_side("offshore") is None
    assert _normalize_boundary_side("") is None
    assert _normalize_boundary_side(None) is None


def test_normalized_side_builds_a_valid_boundary():
    """The normalized side feeds the strict SwanWaveBoundary contract cleanly --
    a multi-char input ('from the south') no longer trips validation."""
    from trid3nt_server.tools.run_swan_tool import _normalize_boundary_side

    side = _normalize_boundary_side("from the south")
    assert side == "S"
    # Constructing the strict-Literal boundary with the normalized value succeeds
    # (the pre-fix raw "from the south" would have raised here -> the failed card).
    b = SwanWaveBoundary(hs_m=8.0, tp_s=12.0, dir_deg=180.0, spread_deg=25.0, side=side)
    assert b.side == "S"
