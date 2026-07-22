"""End-to-end MODULE tests for the ELMFIRE wildfire-spread engine (FIRE-3),
exercised in ISOLATION with fetches / docker / S3 MOCKED (mirrors
test_run_geoclaw_chain.py).

  1. **Contract round-trip + preset normalization** — ``ElmfireRunArgs`` /
     ``FireSpreadLayerURI`` and the documented FUEL_MOISTURE_PRESETS mapping.
  2. **Deck-spec assembly** — ``build_elmfire_deck_spec`` maps the run args
     onto the FIRE-2 deck-builder spec shape (preset expansion, hours->s).
  3. **Solver registration + local docker spec** — ``'elmfire'`` in
     ``SOLVER_WORKFLOW_REGISTRY`` + ``LOCAL_SOLVER_SPEC_REGISTRY``; the
     ``docker run`` argv carries the FIRE-1 image, the --cpus cap, the
     mounted rundir, and the mkdir-outputs/scratch preamble.
  4. **Tool typed errors** — missing bbox / MISSING IGNITION (the
     never-fabricate rule) / bad params.
  5. **Postprocess unit tests on a SYNTHETIC ToA .bil** — CRS stamp (the
     FIRE-1 gdal_translate -a_srs step done in code), hourly burned-extent
     frames with the web ``step N`` token, unit conversions (ft->m, ft/min ->
     m/min), typed ELMFIRE_OUTPUT_EMPTY / ELMFIRE_NO_SPREAD.
  6. **Composer mocked E2E** — REAL deck build from synthetic fetched rasters,
     run_solver/wait_for_completion mocked, synthetic solver outputs, REAL
     postprocess (upload stubbed) -> the primary FireSpreadLayerURI + frame +
     aux COGs, no AWS, no docker.
  7. **Confirm gate** — model_fire_spread in SOLVER_CONFIRM_TOOLS and the
     fire confirm card carries cell count + runtime estimate.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from grace2_contracts.elmfire_contracts import (
    ELMFIRE_TOA_STYLE_PRESET,
    FUEL_MOISTURE_PRESETS,
    ElmfireRunArgs,
    FireSpreadLayerURI,
)

# A small AOI in the northern Sierra Nevada (CONUS, inside EPSG:5070).
_AOI = (-120.60, 39.10, -120.55, 39.14)
_IGN = (-120.575, 39.12)

# The web parseFrameToken regex — frame NAMES must match it or the sequential
# group never forms (same guard as test_run_geoclaw_chain).
_WEB_STEP_TOKEN_RE = re.compile(r"\b(?:step|frame|idx|index)\s*\+?(\d{1,4})\b", re.I)


# ===========================================================================
# (1) Contract round-trip + preset normalization.
# ===========================================================================
def test_run_args_round_trip_and_preset_aliases():
    a = ElmfireRunArgs(bbox=_AOI, ignition_lonlat=_IGN)
    assert a.fuel_moisture == "dry"
    assert a.fuel_moisture_values() == FUEL_MOISTURE_PRESETS["dry"]
    a2 = ElmfireRunArgs(**a.model_dump())
    assert a2 == a
    # LLM synonyms normalize on the FIRST attempt.
    for alias, canon in (("critical", "dry"), ("normal", "moderate"), ("wet", "moist")):
        assert (
            ElmfireRunArgs(
                bbox=_AOI, ignition_lonlat=_IGN, fuel_moisture=alias
            ).fuel_moisture
            == canon
        )


def test_fuel_moisture_mapping_is_the_documented_table():
    # The documented dry/moderate/moist -> M1/M10/M100 triples (+ live LH/LW).
    assert FUEL_MOISTURE_PRESETS["dry"]["m1_pct"] == 3.0
    assert FUEL_MOISTURE_PRESETS["dry"]["m10_pct"] == 4.0
    assert FUEL_MOISTURE_PRESETS["dry"]["m100_pct"] == 5.0
    assert FUEL_MOISTURE_PRESETS["moderate"]["m1_pct"] == 6.0
    assert FUEL_MOISTURE_PRESETS["moist"]["m100_pct"] == 14.0
    # The ladder ascends within every preset (m1 <= m10 <= m100).
    for preset, vals in FUEL_MOISTURE_PRESETS.items():
        assert vals["m1_pct"] <= vals["m10_pct"] <= vals["m100_pct"], preset


def test_run_args_rejects_bad_values():
    with pytest.raises(Exception):
        ElmfireRunArgs(bbox=_AOI, ignition_lonlat=_IGN, fuel_moisture="soggy")
    with pytest.raises(Exception):
        ElmfireRunArgs(bbox=_AOI, ignition_lonlat=(999.0, 0.0))
    with pytest.raises(Exception):
        ElmfireRunArgs(bbox=_AOI, ignition_lonlat=_IGN, duration_hours=0.0)
    with pytest.raises(Exception):
        ElmfireRunArgs(bbox=_AOI, ignition_lonlat=_IGN, wind_dir_deg=361.0)


def test_fire_layer_uri_round_trip():
    lyr = FireSpreadLayerURI(
        layer_id="fire-arrival-x",
        name="Fire arrival time",
        layer_type="raster",
        uri="s3://b/k.tif",
        style_preset=ELMFIRE_TOA_STYLE_PRESET,
        role="primary",
        units="hours",
        bbox=_AOI,
        burned_area_km2=1.5,
        fire_arrival_max_hr=5.5,
        max_flame_length_m=2.4,
        max_spread_rate_m_min=12.0,
        duration_hours=6.0,
        ignition_lonlat=_IGN,
    )
    assert FireSpreadLayerURI(**lyr.model_dump()) == lyr
    assert lyr.style_preset == "continuous_fire_arrival_hr"


# ===========================================================================
# (2) Deck-spec assembly.
# ===========================================================================
def test_build_elmfire_deck_spec_maps_args():
    from grace2_agent.workflows.run_elmfire import build_elmfire_deck_spec

    args = ElmfireRunArgs(
        bbox=_AOI,
        ignition_lonlat=_IGN,
        wind_speed_mph=22.0,
        wind_dir_deg=270.0,
        fuel_moisture="moderate",
        duration_hours=4.0,
    )
    inputs = {k: f"/tmp/{k}.tif" for k in (
        "fbfm40", "cbh", "cbd", "cc", "ch", "dem", "slp", "asp"
    )}
    spec = build_elmfire_deck_spec(args, inputs)
    assert spec["aoi"]["bbox"] == list(_AOI)
    assert spec["ignitions"] == [
        {"lon": _IGN[0], "lat": _IGN[1], "t_ign_s": 0.0}
    ]
    assert spec["weather"]["ws_mph_20ft"] == 22.0
    assert spec["weather"]["wd_deg"] == 270.0
    # The preset EXPANDS to the documented m1/m10/m100 (+ live lh/lw).
    assert spec["weather"]["m1_pct"] == 6.0
    assert spec["weather"]["m10_pct"] == 7.0
    assert spec["weather"]["m100_pct"] == 8.0
    assert spec["duration_s"] == 4.0 * 3600.0
    assert spec["inputs"] == inputs
    assert spec["grid"] == {"target_epsg": 5070, "cellsize_m": 30.0}
    # DTDUMP stays hourly so postprocess frame thresholds align with dumps.
    assert spec["time"]["dtdump_s"] == 3600.0


# ===========================================================================
# (3) Solver registration + local docker spec.
# ===========================================================================
def test_elmfire_registered_in_solver_registries():
    from grace2_agent.tools.solver import (
        LOCAL_SOLVER_SPEC_REGISTRY,
        SOLVER_WORKFLOW_REGISTRY,
    )
    from grace2_agent.workflows.run_elmfire import (
        ELMFIRE_SOLVER_NAME,
        register_elmfire_local_spec,
        register_elmfire_solver,
    )

    register_elmfire_solver()  # idempotent
    register_elmfire_local_spec()
    assert ELMFIRE_SOLVER_NAME in SOLVER_WORKFLOW_REGISTRY
    assert ELMFIRE_SOLVER_NAME in LOCAL_SOLVER_SPEC_REGISTRY


def test_elmfire_local_spec_docker_argv(monkeypatch, tmp_path):
    from grace2_agent.workflows.run_elmfire import elmfire_local_spec

    monkeypatch.setenv("GRACE2_ELMFIRE_CPUS", "3")
    monkeypatch.setenv(
        "GRACE2_ELMFIRE_DOCKER_HOST", "unix:///run/user/1000/docker.sock"
    )
    spec = elmfire_local_spec()
    assert spec.solver == "elmfire"
    assert spec.args_key == "elmfire_args"
    assert spec.exec_kind == "docker"
    # Rootless-docker DOCKER_HOST threads through env_overrides.
    assert spec.env_overrides == {
        "DOCKER_HOST": "unix:///run/user/1000/docker.sock"
    }
    argv = spec.build_argv("RUNID1", tmp_path, ["./inputs/elmfire.data"])
    assert argv[:3] == ["docker", "run", "--rm"]
    assert "--name" in argv and argv[argv.index("--name") + 1] == "RUNID1"
    assert "--cpus" in argv and argv[argv.index("--cpus") + 1] == "3"
    assert f"{tmp_path}:/deck" in argv  # the deck dir is mounted
    assert "trid3nt/elmfire:dev" in argv  # the FIRE-1 proven image
    # The inner command recreates the solver dirs and runs the pinned binary.
    inner = argv[-1]
    assert "mkdir -p outputs scratch" in inner
    assert "elmfire_2025.0526 ./inputs/elmfire.data" in inner


def test_batch_job_def_seam_constant():
    # FIRE-4 seam: the job-def NAME constant exists; nothing seeds the Batch
    # registry (the lane stays inert until GRACE2_AWS_BATCH_JOB_DEF_ELMFIRE).
    from grace2_agent.workflows.run_elmfire import ELMFIRE_BATCH_JOB_DEF_NAME

    assert ELMFIRE_BATCH_JOB_DEF_NAME == "grace2-elmfire"


# ===========================================================================
# (4) Tool typed errors (ignition REQUIRED — never fabricated).
# ===========================================================================
def test_tool_typed_error_on_missing_bbox():
    """Missing bbox is NO LONGER an error when ignition is present - the
    domain derives ~5 km around the ignition (contract model_post_init) and
    the run proceeds past validation (here into staging, which fails in the
    test env - any non-params error proves validation passed)."""
    import asyncio

    from grace2_agent.tools.model_fire_spread import model_fire_spread

    out = asyncio.run(
        model_fire_spread(
            bbox=None,
            ignition_lonlat=(-120.85, 39.02),
            duration_hours=1,
        )
    )
    assert out["status"] == "error"
    assert out["error_code"] not in (
        "FIRE_PARAMS_INVALID",
        "FIRE_PARAMS_INCOMPLETE",
        "FIRE_IGNITION_REQUIRED",
    )


def test_tool_typed_error_on_missing_ignition():
    from grace2_agent.tools.model_fire_spread import model_fire_spread

    out = asyncio.run(model_fire_spread(bbox=list(_AOI), ignition_lonlat=None))
    assert out["status"] == "error"
    assert out["error_code"] == "FIRE_IGNITION_REQUIRED"
    # The message points the LLM at the spatial-input pick machinery.
    assert "request_spatial_input" in out["error_message"]

    out2 = asyncio.run(
        model_fire_spread(bbox=list(_AOI), ignition_lonlat="not-a-point")
    )
    assert out2["error_code"] == "FIRE_PARAMS_INVALID"


def test_tool_typed_error_on_bad_preset():
    from grace2_agent.tools.model_fire_spread import model_fire_spread

    out = asyncio.run(
        model_fire_spread(
            bbox=list(_AOI), ignition_lonlat=list(_IGN), fuel_moisture="soggy"
        )
    )
    assert out["status"] == "error"
    assert out["error_code"] == "FIRE_PARAMS_INVALID"


# ===========================================================================
# (5) Postprocess on SYNTHETIC solver outputs.
# ===========================================================================
#: A small EPSG:5070-shaped grid: 40x40 cells at 30 m.
_NX = _NY = 40
_CELL = 30.0
_XLL, _YUP = -2_070_000.0, 2_110_000.0  # plausible CONUS Albers coords


def _write_bil(
    path: Path,
    arr: "np.ndarray",
    *,
    with_crs: bool = False,
) -> Path:
    """Write an ESRI BIL (.bil + .hdr) like ELMFIRE does — NO CRS by default
    (the FIRE-1 proof's gdal_translate -a_srs precondition)."""
    profile = {
        "driver": "EHdr",
        "width": arr.shape[1],
        "height": arr.shape[0],
        "count": 1,
        "dtype": "float32",
        "transform": from_origin(_XLL, _YUP, _CELL, _CELL),
        "nodata": -9999.0,
    }
    if with_crs:
        profile["crs"] = "EPSG:5070"
    with rasterio.open(path, "w", **profile) as ds:
        ds.write(arr.astype("float32"), 1)
    return path


def _toa_array(duration_s: float = 6 * 3600.0) -> "np.ndarray":
    """A radially-growing time-of-arrival field: the fire starts at the grid
    centre and reaches the corner at ~duration; a dry (never-burned) margin
    stays nodata."""
    toa = np.full((_NY, _NX), -9999.0, dtype="float64")
    cy, cx = _NY // 2, _NX // 2
    max_r = 14.0  # cells actually burned (margin beyond stays nodata)
    for j in range(_NY):
        for i in range(_NX):
            r = float(np.hypot(j - cy, i - cx))
            if r <= max_r:
                toa[j, i] = 30.0 + (r / max_r) * (duration_s - 30.0)
    return toa


def _fake_upload(local_cog, run_id, runs_bucket=None, *, dest_filename="x.tif"):
    # assert the COG is a valid single-band EPSG:4326 raster before "uploading".
    with rasterio.open(local_cog) as ds:
        assert str(ds.crs) == "EPSG:4326"
        assert ds.count == 1
    return f"s3://fake-runs/{run_id}/{dest_filename}"


def test_read_fire_raster_stamps_missing_crs(tmp_path: Path):
    """The CRS stamp: a CRS-less BIL read carries the deck EPSG (the in-code
    equivalent of FIRE-1's gdal_translate -a_srs); a CRS-carrying file keeps
    its own CRS (never silently overridden)."""
    from grace2_agent.workflows.postprocess_elmfire import read_fire_raster

    bare = _write_bil(tmp_path / "time_of_arrival_1.bil", _toa_array())
    arr, _t, crs, cellsize = read_fire_raster(bare, epsg=5070)
    assert crs == "EPSG:5070"
    assert cellsize == pytest.approx(_CELL)
    # nodata -9999 mapped to NaN.
    assert np.isnan(arr).any() and np.isfinite(arr).any()

    tagged = _write_bil(
        tmp_path / "time_of_arrival_2.bil", _toa_array(), with_crs=True
    )
    _a, _t2, crs2, _c2 = read_fire_raster(tagged, epsg=32610)
    assert "5070" in crs2  # the file's own CRS wins over the fallback epsg


def test_toa_frame_grids_threshold_per_hour():
    from grace2_agent.workflows.postprocess_elmfire import toa_frame_grids

    duration_s = 6 * 3600.0
    toa = _toa_array(duration_s)
    toa_nan = np.where(toa == -9999.0, np.nan, toa)
    frames = toa_frame_grids(toa_nan, duration_s)
    assert len(frames) == 6  # one per hour
    hours = [h for h, _g in frames]
    assert hours == [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    # Burned extent GROWS monotonically; values are arrival HOURS <= frame hour.
    prev = 0
    for h, g in frames:
        n = int(np.isfinite(g).sum())
        assert n >= prev
        prev = n
        assert np.nanmax(g) <= h + 1e-9
    # The final frame covers every burned cell.
    assert prev == int(np.isfinite(toa_nan).sum())


def test_postprocess_elmfire_end_to_end_shape(tmp_path: Path):
    """Synthetic ToA + flame-length + spread-rate BILs -> the (layers, metrics)
    shape: primary ToA COG + contiguous 'Burned area step N' frames + aux COGs
    with the ft->m conversions applied exactly once."""
    from grace2_agent.workflows import postprocess_elmfire as pe

    out = tmp_path / "outputs"
    out.mkdir()
    duration_s = 6 * 3600.0
    toa = _toa_array(duration_s)
    _write_bil(out / "time_of_arrival_0000001_0021600.bil", toa)
    burned = toa != -9999.0
    flame_ft = np.where(burned, 10.0, -9999.0)  # 10 ft everywhere burned
    _write_bil(out / "flame_length_0000001_0021600.bil", flame_ft)
    vs_ftmin = np.where(burned, 100.0, -9999.0)  # 100 ft/min
    _write_bil(out / "vs_0000001_0021600.bil", vs_ftmin)

    with patch.object(pe, "_upload_cog_to_runs_bucket", _fake_upload):
        layers, metrics = pe.postprocess_elmfire(
            tmp_path,
            _AOI,
            run_id="RIDF1",
            duration_s=duration_s,
            epsg=5070,
            ignition_lonlat=_IGN,
        )

    # layers[0] = the PRIMARY ToA layer with the typed scalars.
    primary = layers[0]
    assert isinstance(primary, FireSpreadLayerURI)
    assert primary.role == "primary"
    assert primary.name == "Fire arrival time"
    assert primary.style_preset == "continuous_fire_arrival_hr"
    assert primary.uri == "s3://fake-runs/RIDF1/elmfire_toa.tif"
    n_burned = int(burned.sum())
    expected_km2 = n_burned * (_CELL * _CELL) / 1e6
    assert primary.burned_area_km2 == pytest.approx(expected_km2)
    assert metrics["burned_area_km2"] == pytest.approx(expected_km2)
    assert metrics["burned_cell_count"] == n_burned
    assert primary.fire_arrival_max_hr == pytest.approx(6.0, abs=0.05)
    # Unit conversions applied ONCE: 10 ft -> 3.048 m; 100 ft/min -> 30.48 m/min.
    assert primary.max_flame_length_m == pytest.approx(10.0 * 0.3048)
    assert primary.max_spread_rate_m_min == pytest.approx(100.0 * 0.3048)
    assert primary.ignition_lonlat == _IGN

    # Contiguous 'Burned area step N' frames with distinct URIs (web contract).
    frames = [l for l in layers[1:] if "step" in l.name]
    assert len(frames) == 6
    uris = set()
    for n, fr in enumerate(frames, start=1):
        assert fr.name == f"Burned area step {n}"
        assert _WEB_STEP_TOKEN_RE.search(fr.name)
        assert fr.role == "context"
        uris.add(fr.uri)
    assert len(uris) == len(frames)
    # Frame burned area grows with the hour.
    frame_areas = [fr.burned_area_km2 for fr in frames]
    assert frame_areas == sorted(frame_areas)

    # Aux layers: flame length (m) + spread rate (m/min) as standalone COGs.
    aux_names = {l.name for l in layers[1:]} - {f.name for f in frames}
    assert aux_names == {"Flame length", "Spread rate"}
    aux = {l.name: l for l in layers[1:] if l.name in aux_names}
    assert aux["Flame length"].units == "m"
    assert aux["Flame length"].style_preset == "continuous_flame_length_m"
    assert aux["Spread rate"].units == "m/min"
    assert aux["Spread rate"].style_preset == "continuous_fire_spread_rate"


def test_postprocess_elmfire_empty_output_raises(tmp_path: Path):
    from grace2_agent.workflows.postprocess_elmfire import (
        PostprocessElmfireError,
        postprocess_elmfire,
    )

    with pytest.raises(PostprocessElmfireError) as ei:
        postprocess_elmfire(
            tmp_path, _AOI, run_id="X", duration_s=3600.0, epsg=5070
        )
    assert ei.value.error_code == "ELMFIRE_OUTPUT_EMPTY"


def test_postprocess_elmfire_zero_spread_is_typed(tmp_path: Path):
    """All-nodata ToA (nothing burned) -> the typed ELMFIRE_NO_SPREAD result,
    never a blank 'modeled ok' with empty layers (honesty floor)."""
    from grace2_agent.workflows.postprocess_elmfire import (
        PostprocessElmfireError,
        postprocess_elmfire,
    )

    out = tmp_path / "outputs"
    out.mkdir()
    _write_bil(
        out / "time_of_arrival_0000001_0021600.bil",
        np.full((_NY, _NX), -9999.0),
    )
    with pytest.raises(PostprocessElmfireError) as ei:
        postprocess_elmfire(
            tmp_path, _AOI, run_id="X", duration_s=6 * 3600.0, epsg=5070
        )
    assert ei.value.error_code == "ELMFIRE_NO_SPREAD"


# ===========================================================================
# (6) Composer mocked E2E: REAL deck build + REAL postprocess; fetches,
#     run_solver, wait_for_completion, downloads, publish + emitter mocked.
# ===========================================================================
def _write_synthetic_source(path: Path, value: int) -> str:
    """A constant Int16 EPSG:4326 source raster covering the AOI + margin
    (mirrors the FIRE-2 deck-builder test fixtures)."""
    px = 0.00027  # ~30 m
    margin = 0.01
    minx, miny = _AOI[0] - margin, _AOI[1] - margin
    maxx, maxy = _AOI[2] + margin, _AOI[3] + margin
    width = int(round((maxx - minx) / px))
    height = int(round((maxy - miny) / px))
    profile = {
        "driver": "GTiff",
        "width": width,
        "height": height,
        "count": 1,
        "dtype": "int16",
        "crs": "EPSG:4326",
        "transform": from_origin(minx, maxy, px, px),
        "nodata": -32768,
    }
    with rasterio.open(path, "w", **profile) as ds:
        ds.write(np.full((height, width), value, dtype="int16"), 1)
    return str(path)


class _FakeHandle:
    run_id = "FIRERUN1"
    workflow_name = "local-docker"


class _FakeRunResult:
    run_id = "FIRERUN1"
    status = "complete"
    output_uri = "s3://runs/FIRERUN1/"
    error_code = None
    error_message = None
    cancellation_reason = None
    batch_compute_meta: dict = {}


def _amock(ret):
    async def _inner(*a, **k):
        return ret
    return _inner


class _NullSubstep:
    """A no-op async context manager standing in for pipeline_emitter.substep."""

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def test_composer_mocked_end_to_end(tmp_path: Path, monkeypatch):
    """Fetches mocked (synthetic rasters), docker/solver mocked, synthetic
    solver outputs -> REAL deck build + REAL postprocess -> the primary
    FireSpreadLayerURI + frames + aux COGs as LayerURIs. No AWS, no docker."""
    from grace2_agent.tools import solver as solver_mod
    from grace2_agent.workflows import model_fire_spread_scenario as comp
    from grace2_agent.workflows import postprocess_elmfire as pe
    from grace2_agent.workflows.run_elmfire import load_deck_builder

    # Local backend so staging stays on the filesystem (file:// manifest).
    monkeypatch.setenv("GRACE2_SOLVER_BACKEND", "local-docker")

    src_dir = tmp_path / "srcs"
    src_dir.mkdir()
    values = {
        "fbfm40": 102, "cbh": 3, "cbd": 12, "cc": 40, "ch": 150,
        "dem": 500, "slp": 5, "asp": 180,
    }
    synthetic_inputs = {
        name: _write_synthetic_source(src_dir / f"{name}.tif", v)
        for name, v in values.items()
    }

    run_args = ElmfireRunArgs(
        bbox=_AOI,
        ignition_lonlat=_IGN,
        duration_hours=6.0,
        wind_speed_mph=15.0,
    )

    captured: dict = {}

    def _fake_fetch(bbox, **_kw):
        captured["fetch_bbox"] = tuple(bbox)
        return dict(synthetic_inputs)

    def _fake_run_solver(*, solver, model_setup_uri, compute_class):
        captured["solver"] = solver
        captured["model_setup_uri"] = model_setup_uri
        captured["compute_class"] = compute_class
        return _FakeHandle()

    def _fake_download(run_id):
        # Synthesize solver outputs on the DECK's real grid (deterministic:
        # compute_target_grid on the same bbox reproduces the deck grid).
        db = load_deck_builder()
        grid = db.compute_target_grid(list(_AOI))
        ny, nx = grid["ny"], grid["nx"]
        duration_s = 6 * 3600.0
        toa = np.full((ny, nx), -9999.0, dtype="float64")
        cy, cx = ny // 2, nx // 2
        max_r = min(ny, nx) / 3.0
        for j in range(ny):
            for i in range(nx):
                r = float(np.hypot(j - cy, i - cx))
                if r <= max_r:
                    toa[j, i] = 30.0 + (r / max_r) * (duration_s - 30.0)
        out_root = tmp_path / "solver_out"
        out = out_root / "outputs"
        out.mkdir(parents=True, exist_ok=True)
        transform = from_origin(
            grid["xll"], grid["yll"] + ny * grid["cellsize_m"],
            grid["cellsize_m"], grid["cellsize_m"],
        )
        profile = {
            "driver": "EHdr", "width": nx, "height": ny, "count": 1,
            "dtype": "float32", "transform": transform, "nodata": -9999.0,
        }
        with rasterio.open(
            out / "time_of_arrival_0000001_0021600.bil", "w", **profile
        ) as ds:
            ds.write(toa.astype("float32"), 1)
        burned = toa != -9999.0
        with rasterio.open(
            out / "flame_length_0000001_0021600.bil", "w", **profile
        ) as ds:
            ds.write(
                np.where(burned, 8.0, -9999.0).astype("float32"), 1
            )
        with rasterio.open(
            out / "vs_0000001_0021600.bil", "w", **profile
        ) as ds:
            ds.write(
                np.where(burned, 60.0, -9999.0).astype("float32"), 1
            )
        return str(out_root), True

    published: list[str] = []

    def _fake_publish(*, layer_uri, layer_id, style_preset=None, **_kw):
        published.append(layer_id)
        return f"https://tiles.example/{layer_id}"

    emitted_layers: list = []

    class _FakeEmitter:
        async def emit_map_command(self, *a, **k):
            return None

        async def add_loaded_layer(self, layer):
            emitted_layers.append(layer)

    with patch.object(comp, "fetch_elmfire_inputs", _fake_fetch), \
         patch.object(comp, "begin_substeps", lambda *a, **k: None), \
         patch.object(comp, "substep", _NullSubstep), \
         patch.object(solver_mod, "run_solver", _fake_run_solver), \
         patch.object(solver_mod, "wait_for_completion", _amock(_FakeRunResult())), \
         patch.object(solver_mod, "set_emitter_binding", lambda *a, **k: None), \
         patch.object(comp, "mint_dispatch_and_sim_cards", _amock(None)), \
         patch.object(comp, "route_sim_terminal", _amock(None)), \
         patch.object(comp, "drive_live_solve_progress", _amock(None)), \
         patch.object(comp, "_download_elmfire_outputs", _fake_download), \
         patch.object(pe, "_upload_cog_to_runs_bucket", _fake_upload), \
         patch.object(comp, "publish_layer", _fake_publish), \
         patch.object(comp, "current_emitter", lambda: _FakeEmitter()):
        primary = asyncio.run(comp.model_fire_spread_scenario(run_args))

    # Dispatch went through the generic seam with the staged manifest.
    assert captured["solver"] == "elmfire"
    assert captured["model_setup_uri"].startswith("file://")
    assert captured["model_setup_uri"].endswith("manifest.json")
    assert captured["fetch_bbox"] == _AOI

    # The primary is a published FireSpreadLayerURI with typed scalars.
    assert isinstance(primary, FireSpreadLayerURI)
    assert primary.role == "primary"
    assert primary.uri.startswith("https://tiles.example/")
    assert primary.burned_area_km2 > 0.0
    assert primary.fire_arrival_max_hr == pytest.approx(6.0, abs=0.05)
    assert primary.max_flame_length_m == pytest.approx(8.0 * 0.3048)
    assert primary.max_spread_rate_m_min == pytest.approx(60.0 * 0.3048)

    # Frames + aux layers were published + emitted out-of-band: 6 hourly
    # burned-extent frames with the web 'step N' token + 2 aux layers.
    frame_names = [
        l.name for l in emitted_layers if _WEB_STEP_TOKEN_RE.search(l.name or "")
    ]
    assert frame_names == [f"Burned area step {n}" for n in range(1, 7)]
    emitted_names = {l.name for l in emitted_layers}
    assert {"Flame length", "Spread rate"} <= emitted_names
    # Every emitted layer carries a published (renderable) URI.
    assert all(l.uri.startswith("https://tiles.example/") for l in emitted_layers)


# ===========================================================================
# (7) Confirm gate: the tool is gated + the card carries cells + runtime.
# ===========================================================================
def test_model_fire_spread_in_solver_confirm_tools():
    from grace2_agent.server import SOLVER_CONFIRM_TOOLS

    assert "model_fire_spread" in SOLVER_CONFIRM_TOOLS


def test_fire_confirm_envelope_carries_cells_and_runtime():
    from grace2_agent.server import _build_fire_confirm_envelope

    env = _build_fire_confirm_envelope(
        {
            "bbox": list(_AOI),
            "ignition_lonlat": list(_IGN),
            "wind_speed_mph": 20.0,
            "wind_dir_deg": 270.0,
            "fuel_moisture": "dry",
            "duration_hours": 6.0,
        }
    )
    assert env.tool_name == "model_fire_spread"
    assert env.options == ["proceed", "cancel"]
    assert env.tool_args["estimated_cells"] > 0
    assert env.tool_args["estimated_runtime_s"] >= 15
    # The preset is expanded so the user sees the actual moisture percentages.
    assert env.tool_args["fuel_moisture_pct"]["m1_pct"] == 3.0
    assert "ELMFIRE" in env.recommendation
    assert "Confirm to start" in env.recommendation


class TestArgShapeCoercion:
    """Live-observed small-model arg shapes (2026-07-08): ignition as a
    "lon,lat" string and bbox point-collapsed onto the ignition. Both must
    coerce instead of failing the run."""

    def test_ignition_string_coerces(self):
        from grace2_contracts.elmfire_contracts import ElmfireRunArgs

        args = ElmfireRunArgs(
            bbox=(-121.0, 38.9, -120.7, 39.1),
            ignition_lonlat="-120.85,39.02",
        )
        assert args.ignition_lonlat == (-120.85, 39.02)

    def test_ignition_dict_coerces(self):
        from grace2_contracts.elmfire_contracts import ElmfireRunArgs

        args = ElmfireRunArgs(
            bbox=(-121.0, 38.9, -120.7, 39.1),
            ignition_lonlat={"lon": -120.85, "lat": 39.02},
        )
        assert args.ignition_lonlat == (-120.85, 39.02)

    def test_point_bbox_derives_domain_from_ignition(self):
        from grace2_contracts.elmfire_contracts import (
            DEFAULT_FIRE_DOMAIN_HALFWIDTH_DEG as D,
            ElmfireRunArgs,
        )

        args = ElmfireRunArgs(
            bbox="-120.85,39.02", ignition_lonlat="-120.85,39.02"
        )
        assert args.bbox == (
            -120.85 - D, 39.02 - D, -120.85 + D, 39.02 + D,
        )

    def test_missing_bbox_derives_domain(self):
        from grace2_contracts.elmfire_contracts import ElmfireRunArgs

        args = ElmfireRunArgs(ignition_lonlat=(-120.85, 39.02))
        assert args.bbox is not None
        lo_lon, lo_lat, hi_lon, hi_lat = args.bbox
        assert lo_lon < -120.85 < hi_lon and lo_lat < 39.02 < hi_lat

    def test_bbox_string_four_coerces(self):
        from grace2_contracts.elmfire_contracts import ElmfireRunArgs

        args = ElmfireRunArgs(
            bbox="-121.0, 38.9, -120.7, 39.1",
            ignition_lonlat=(-120.85, 39.02),
        )
        assert args.bbox == (-121.0, 38.9, -120.7, 39.1)

    def test_garbage_ignition_still_fails_honestly(self):
        import pytest
        from pydantic import ValidationError
        from grace2_contracts.elmfire_contracts import ElmfireRunArgs

        with pytest.raises(ValidationError):
            ElmfireRunArgs(
                bbox=(-121.0, 38.9, -120.7, 39.1),
                ignition_lonlat="somewhere in California",
            )

    def test_lonlon_latlat_bbox_reorders(self):
        from grace2_contracts.elmfire_contracts import ElmfireRunArgs

        args = ElmfireRunArgs(
            bbox="-121.0,-120.7,38.9,39.1", ignition_lonlat=(-120.85, 39.02)
        )
        assert args.bbox == (-121.0, 38.9, -120.7, 39.1)

    def test_incoherent_bbox_derives_from_ignition(self):
        from grace2_contracts.elmfire_contracts import ElmfireRunArgs

        args = ElmfireRunArgs(
            bbox=(999.0, 5.0, -3.0, 1.0), ignition_lonlat=(-120.85, 39.02)
        )
        lo_lon, lo_lat, hi_lon, hi_lat = args.bbox
        assert lo_lon < -120.85 < hi_lon and lo_lat < 39.02 < hi_lat

    def test_bracketed_bbox_string_coerces(self):
        from grace2_contracts.elmfire_contracts import ElmfireRunArgs

        args = ElmfireRunArgs(
            bbox="[-121.0, 38.9, -120.7, 39.1]",
            ignition_lonlat=(-120.85, 39.02),
        )
        assert args.bbox == (-121.0, 38.9, -120.7, 39.1)

    def test_latlon_point_pairs_derive(self):
        from grace2_contracts.elmfire_contracts import ElmfireRunArgs

        args = ElmfireRunArgs(
            bbox="39.02,-120.85,39.02,-120.85",
            ignition_lonlat=(-120.85, 39.02),
        )
        lo_lon, lo_lat, hi_lon, hi_lat = args.bbox
        assert lo_lon < -120.85 < hi_lon and lo_lat < 39.02 < hi_lat

    def test_tool_wrapper_accepts_string_shapes(self):
        """The wrapper must DELEGATE shape handling to ElmfireRunArgs - its
        old manual pre-validation rejected string ignition/bbox (live
        2026-07-08: list("lon,lat") explodes into characters). Passing
        validation means any failure comes from LATER stages (staging in the
        test env) - the only forbidden outcomes are the params errors."""
        import asyncio

        from grace2_agent.tools.model_fire_spread import model_fire_spread

        result = asyncio.run(
            model_fire_spread(
                bbox="-120.85,39.02",
                ignition_lonlat="-120.85,39.02",
                duration_hours=1,
            )
        )
        assert result.get("error_code") not in (
            "FIRE_PARAMS_INVALID",
            "FIRE_PARAMS_INCOMPLETE",
            "FIRE_IGNITION_REQUIRED",
        )
