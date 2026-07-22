"""Tests for postprocess_waves (sprint-17 SnapWave wave-animation, P2).

``postprocess_waves`` reads a quadtree+SnapWave ``sfincs_map.nc``, selects the
significant-wave-height field (``hm0`` -> fallback ``hm0ig``, dims
(nmesh2d_face, time) on the quadtree path), rasterizes each frame via the
quadtree-aware ``_write_verified_cog`` (P1), uploads the COGs, and returns:

- ``layers[0]`` = PEAK (max-over-time) wave height, role="primary",
  name="Peak wave height", style="continuous_wave_height".
- ``layers[1:]`` = per-frame "Wave height step N", role="context", each at a
  DISTINCT object key (distinct upload filename).

The "Wave height step N" name stem is a SEPARATE web scrubber group from "Flood
depth step N". hm0 is already a HEIGHT (no zs-zb arithmetic), masked below
NODATA_WAVE_M = 0.05 m.

The S3 upload is mocked: ``_upload_cog_to_runs_bucket`` is patched to capture the
dest_filename + return a synthetic s3:// URI, so no network / bucket is touched.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("rasterio")
pytest.importorskip("xarray")
pytest.importorskip("scipy")
pytest.importorskip("pyproj")

from grace2_agent.workflows import postprocess_waves as pw  # noqa: E402
from grace2_agent.workflows.postprocess_waves import (  # noqa: E402
    NODATA_WAVE_M,
    WAVE_HEIGHT_STYLE_PRESET,
    PostprocessError,
    postprocess_waves,
)

_UTM16N = "EPSG:32616"
_BBOX = (-85.45, 29.93, -85.38, 29.98)


def _epsg_to_wkt(epsg_str: str) -> str:
    import pyproj

    return pyproj.CRS.from_string(epsg_str).to_wkt()


def _make_snapwave_nc(
    tmp_path: Path,
    *,
    n_faces: int = 400,
    n_steps: int = 6,
    var: str = "hm0",
    rising: bool = True,
    filename: str = "sfincs_map.nc",
) -> Path:
    """Write a synthetic face-indexed SnapWave NetCDF with ``var(nmesh2d_face,
    time)`` rising over time + per-face centroids in UTM."""
    import xarray as xr
    from pyproj import Transformer

    tf = Transformer.from_crs("EPSG:4326", _UTM16N, always_xy=True)
    x0, y0 = tf.transform(_BBOX[0], _BBOX[1])
    x1, y1 = tf.transform(_BBOX[2], _BBOX[3])
    minx, maxx = min(x0, x1), max(x0, x1)
    miny, maxy = min(y0, y1), max(y0, y1)

    side = int(np.sqrt(n_faces))
    n_faces = side * side
    xs = np.linspace(minx + 50, maxx - 50, side)
    ys = np.linspace(miny + 50, maxy - 50, side)
    gx, gy = np.meshgrid(xs, ys)
    face_x = gx.ravel().astype("float64")
    face_y = gy.ravel().astype("float64")

    base = np.linspace(0.0, 4.0, n_faces).astype("float32")
    field = np.zeros((n_faces, n_steps), dtype="float32")
    for t in range(n_steps):
        scale = (t / max(1, n_steps - 1)) if rising else 1.0
        field[:, t] = base * scale

    ds = xr.Dataset(
        {
            "crs": xr.DataArray(0, attrs={"crs_wkt": _epsg_to_wkt(_UTM16N)}),
            var: xr.DataArray(field, dims=["nmesh2d_face", "time"]),
            "mesh2d_face_x": xr.DataArray(face_x, dims=["nmesh2d_face"]),
            "mesh2d_face_y": xr.DataArray(face_y, dims=["nmesh2d_face"]),
        },
        coords={"time": np.arange(n_steps)},
    )
    out = tmp_path / filename
    ds.to_netcdf(str(out))
    return out


@pytest.fixture()
def _mock_upload(monkeypatch: pytest.MonkeyPatch):
    """Patch the S3 upload to capture dest_filenames + return synthetic URIs."""
    captured: list[str] = []

    def _fake_upload(local_cog, run_id, runs_bucket=None, *, dest_filename):  # noqa: ANN001
        captured.append(dest_filename)
        return f"s3://test-runs/{run_id}/{dest_filename}"

    monkeypatch.setattr(pw, "_upload_cog_to_runs_bucket", _fake_upload)
    return captured


# --------------------------------------------------------------------------- #
# Peak + frames shape, naming, distinct keys
# --------------------------------------------------------------------------- #


def test_waves_emits_peak_and_frames(tmp_path: Path, _mock_upload) -> None:
    nc = _make_snapwave_nc(tmp_path, n_faces=400, n_steps=6, rising=True)
    layers, metrics = postprocess_waves(str(nc), run_id="RUN1", bbox=_BBOX)

    peak = [l for l in layers if l.role == "primary"]
    frames = [l for l in layers if l.role != "primary"]

    # Exactly one primary peak.
    assert len(peak) == 1
    p = peak[0]
    assert p.name == "Peak wave height"
    assert p.layer_id == "wave-height-peak-RUN1"
    assert p.role == "primary"
    assert p.style_preset == WAVE_HEIGHT_STYLE_PRESET
    assert p.units == "meters"
    assert p.uri.endswith("wave_height_peak.tif")

    # N frames named "Wave height step N", role=context.
    assert len(frames) >= 2
    for i, f in enumerate(frames, start=1):
        assert f.name == f"Wave height step {i}"
        assert f.layer_id == f"wave-height-frame-{i:02d}-RUN1"
        assert f.role == "context"
        assert f.style_preset == WAVE_HEIGHT_STYLE_PRESET
        assert f.uri.endswith(f"wave_height_frame_{i:02d}.tif")

    # Distinct object keys (no dedup collision).
    uris = [l.uri for l in layers]
    assert len(uris) == len(set(uris))

    # Metrics carry wave-height aggregates + crs/units.
    assert metrics["units"] == "meters"
    assert metrics["max_depth_m"] > NODATA_WAVE_M


def test_waves_frame_count_matches_distinct_filenames(
    tmp_path: Path, _mock_upload
) -> None:
    nc = _make_snapwave_nc(tmp_path, n_faces=256, n_steps=5)
    layers, _ = postprocess_waves(str(nc), run_id="RUNX", bbox=_BBOX)
    frames = [l for l in layers if l.role != "primary"]
    # captured upload filenames = 1 peak + N frames, all distinct.
    assert len(_mock_upload) == len(layers)
    assert len(set(_mock_upload)) == len(_mock_upload)
    assert "wave_height_peak.tif" in _mock_upload
    assert len(frames) >= 2


# --------------------------------------------------------------------------- #
# Style group separation from depth
# --------------------------------------------------------------------------- #


def test_wave_name_stem_distinct_from_flood_depth(tmp_path: Path, _mock_upload) -> None:
    """The wave frame name stem must differ from "Flood depth step N" so the web
    detectSequentialGroups keys them into SEPARATE scrubber groups."""
    nc = _make_snapwave_nc(tmp_path, n_steps=4)
    layers, _ = postprocess_waves(str(nc), run_id="R", bbox=_BBOX)
    for l in layers:
        if l.role != "primary":
            assert l.name.startswith("Wave height step")
            assert "Flood depth" not in l.name


def test_wave_style_preset_constant() -> None:
    assert WAVE_HEIGHT_STYLE_PRESET == "continuous_wave_height"


# --------------------------------------------------------------------------- #
# Fallback variable + honest empty
# --------------------------------------------------------------------------- #


def test_waves_falls_back_to_hm0ig(tmp_path: Path, _mock_upload) -> None:
    """When hm0 is absent but hm0ig is present, the infragravity field is used."""
    nc = _make_snapwave_nc(tmp_path, var="hm0ig", n_steps=4)
    layers, _ = postprocess_waves(str(nc), run_id="IG", bbox=_BBOX)
    assert any(l.role == "primary" for l in layers)


def test_waves_no_wave_field_raises_empty(tmp_path: Path, _mock_upload) -> None:
    """A run with NO SnapWave field raises RUN_OUTPUT_EMPTY (the honest "not a
    SnapWave run" signal the caller degrades on)."""
    import xarray as xr
    from pyproj import Transformer

    tf = Transformer.from_crs("EPSG:4326", _UTM16N, always_xy=True)
    x0, y0 = tf.transform(_BBOX[0], _BBOX[1])
    ds = xr.Dataset(
        {
            "crs": xr.DataArray(0, attrs={"crs_wkt": _epsg_to_wkt(_UTM16N)}),
            "zb": xr.DataArray(np.zeros(4, dtype="float32"), dims=["nmesh2d_face"]),
            "mesh2d_face_x": xr.DataArray(
                np.array([x0, x0 + 30, x0, x0 + 30], dtype="float64"),
                dims=["nmesh2d_face"],
            ),
            "mesh2d_face_y": xr.DataArray(
                np.array([y0, y0, y0 + 30, y0 + 30], dtype="float64"),
                dims=["nmesh2d_face"],
            ),
        }
    )
    nc = tmp_path / "sfincs_map.nc"
    ds.to_netcdf(str(nc))
    with pytest.raises(PostprocessError) as ei:
        postprocess_waves(str(nc), run_id="NOWAVE", bbox=_BBOX)
    assert ei.value.error_code == "RUN_OUTPUT_EMPTY"


def test_waves_single_step_drops_frames(tmp_path: Path, _mock_upload) -> None:
    """A single-time-step wave field yields ONLY the peak (no lone-frame group)."""
    nc = _make_snapwave_nc(tmp_path, n_steps=1)
    layers, _ = postprocess_waves(str(nc), run_id="ONE", bbox=_BBOX)
    assert len(layers) == 1
    assert layers[0].role == "primary"
