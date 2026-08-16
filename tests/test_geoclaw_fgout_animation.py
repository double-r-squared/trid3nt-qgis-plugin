"""fgout SMOOTH-animation promotion for the GeoClaw depth-COG postprocess (ADR 0187).

The fgout monitor (setrun ``FGoutGrid``, gated by ``fgout_frames > 0``) writes a
uniform single-resolution frame series (``fgout0001.q0001``, ``.q0002``, ...) at
EVENLY-SPACED times. output_format='ascii' lands each frame in the SAME fort.q
uniform-grid layout, so the postprocess reads them with the fort.q parser (no AMR
flatten, no clawpack import agent-side) and promotes them to the scrubber
animation series -- while the fort.q peak stays the peak.

Pins:
  * when fgout frames are present they BECOME the animation series (the emitted
    per-frame COGs carry the fgout values, not the fort.q values), and their
    COUNT follows the fgout series, not the fort.q frame count;
  * the PEAK layer still comes from the fort.q frames;
  * absent fgout -> the fort.q frames remain the animation source (regression);
  * the same ocean mask is applied to the fgout frames.

ASCII only.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from trid3nt_server.workflows.geoclaw import postprocess_geoclaw as PP


def _write_frame(path: Path, level: int, xlow: float, ylow: float,
                 dx: float, dy: float, h: np.ndarray) -> None:
    """Write a single-patch fort.q/fgout ascii frame (row 0 = ylow=south)."""
    h = np.asarray(h, dtype=float)
    my, mx = h.shape
    lines = [
        "    1    grid_number",
        f"    {level}    AMR_level",
        f"    {mx}    mx",
        f"    {my}    my",
        f"    {xlow:.6e}    xlow",
        f"    {ylow:.6e}    ylow",
        f"    {dx:.6e}    dx",
        f"    {dy:.6e}    dy",
        "",
    ]
    for j in range(my):  # south -> north
        for i in range(mx):
            lines.append(f"    {h[j, i]:.6e}")
        lines.append("")
    path.write_text("\n".join(lines) + "\n")


BBOX = (0.0, 0.0, 1.0, 1.0)


def _fort_q_run(out: Path) -> None:
    """Two fort.q frames, uniform depth 0.7 (the AMR peak source)."""
    for no in (0, 1):
        _write_frame(
            out / f"fort.q{no:04d}", level=1, xlow=0.0, ylow=0.0,
            dx=0.5, dy=0.5, h=np.full((2, 2), 0.7),
        )


@pytest.fixture
def _no_io(monkeypatch):
    """Capture the grids handed to the COG writer (peak first, then frames) and
    stub the S3 upload / unlink / fgmax so the test needs no MinIO."""
    captured: list[np.ndarray] = []

    monkeypatch.setattr(
        PP, "_write_depth_cog_4326",
        lambda grid, bbox: (captured.append(np.asarray(grid, "float64").copy())
                            or Path("/tmp/_fake_geoclaw_cog.tif")),
    )
    monkeypatch.setattr(
        PP, "_upload_cog_to_runs_bucket",
        lambda *a, **k: "s3://fake-runs/fake.tif",
    )
    monkeypatch.setattr(PP, "_safe_unlink", lambda p: None)
    monkeypatch.setattr(PP, "read_fgmax_output", lambda *a, **k: None)
    return captured


def test_fgout_frames_become_the_animation_series(tmp_path, _no_io):
    out = tmp_path / "_output"
    out.mkdir()
    _fort_q_run(out)
    # SIX fgout frames, each a DISTINCT uniform depth (1.0, 1.1, ... 1.5) with
    # AMR_level=0 (the real fgout header) -- values that never appear in fort.q.
    fgout_vals = [1.0, 1.1, 1.2, 1.3, 1.4, 1.5]
    for k, v in enumerate(fgout_vals, start=1):
        _write_frame(
            out / f"fgout0001.q{k:04d}", level=0, xlow=0.0, ylow=0.0,
            dx=0.5, dy=0.5, h=np.full((2, 2), v),
        )

    layers, metrics = PP.postprocess_geoclaw(
        tmp_path, BBOX, run_id="TSTFGOUT", scenario="tsunami",
        grid_shape=(4, 4), mask_ocean=False,
    )

    # PEAK comes from fort.q (0.7), NOT from any fgout frame.
    peak = _no_io[0]
    assert np.allclose(peak[np.isfinite(peak)], 0.7)
    assert np.isclose(metrics["max_depth_m"], 0.7)

    # The animation frames (captured[1:]) are the SIX fgout frames, not the two
    # fort.q frames -- both count and values follow the fgout series.
    frame_grids = _no_io[1:]
    assert len(frame_grids) == len(fgout_vals), (
        f"expected {len(fgout_vals)} fgout animation frames, got {len(frame_grids)}"
    )
    got_vals = sorted(round(float(np.nanmax(g)), 3) for g in frame_grids)
    assert got_vals == sorted(fgout_vals)
    # The fort.q value 0.7 never feeds the animation.
    assert not any(np.isclose(np.nanmax(g), 0.7) for g in frame_grids)
    # One peak layer + >= 2 frame layers (the animation group).
    assert layers[0].role == "primary"
    assert len(layers) == 1 + len(fgout_vals)


def test_absent_fgout_falls_back_to_fort_q_animation(tmp_path, _no_io):
    out = tmp_path / "_output"
    out.mkdir()
    # Three fort.q frames with distinct depths; NO fgout frames.
    for no, v in enumerate((0.5, 0.9, 0.6)):
        _write_frame(
            out / f"fort.q{no:04d}", level=1, xlow=0.0, ylow=0.0,
            dx=0.5, dy=0.5, h=np.full((2, 2), v),
        )

    layers, metrics = PP.postprocess_geoclaw(
        tmp_path, BBOX, run_id="TSTNOFGOUT", scenario="tsunami",
        grid_shape=(4, 4), mask_ocean=False,
    )
    # Peak = the max-total fort.q frame (0.9).
    assert np.isclose(metrics["max_depth_m"], 0.9)
    frame_grids = _no_io[1:]
    # The animation is the fort.q frames (values in {0.5, 0.9, 0.6}).
    got = sorted(round(float(np.nanmax(g)), 3) for g in frame_grids)
    assert got == sorted([0.5, 0.9, 0.6])


def test_discover_fgout_frames_orders_and_pairs_time_headers(tmp_path):
    out = tmp_path / "_output"
    out.mkdir()
    for k in (3, 1, 2):  # out of order on disk
        (out / f"fgout0001.q{k:04d}").write_text("stub")
        (out / f"fgout0001.t{k:04d}").write_text("0.0  time")
    (out / "fort.q0000").write_text("stub")  # not an fgout frame

    frames = PP._discover_fgout_frames(tmp_path)
    assert [f[0] for f in frames] == [1, 2, 3]  # ascending frame_no
    assert all(f[2] is not None for f in frames)  # each .t header paired
    assert all(f[1].name.startswith("fgout0001.q") for f in frames)


def test_fgout_frame_parses_with_fort_q_parser(tmp_path):
    """A real fgout ascii frame (AMR_level=0, 4 q-columns) parses via the fort.q
    parser: col0 = depth h."""
    out = tmp_path / "_output"
    out.mkdir()
    p = out / "fgout0001.q0001"
    # 2x2, four columns (h, hu, hv, eta); parser reads col0 = h.
    lines = [
        "    1    grid_number", "    0    AMR_level",
        "    2    mx", "    2    my",
        "    0.000000e+00    xlow", "    0.000000e+00    ylow",
        "    5.000000e-01    dx", "    5.000000e-01    dy", "",
    ]
    for _row in range(2):
        for _col in range(2):
            lines.append("    3.250000e+01    4.9e+00   -2.1e+00   -3.3e-01")
        lines.append("")
    p.write_text("\n".join(lines) + "\n")

    patches = PP.parse_fort_q_frame(p.read_text())
    assert len(patches) == 1
    assert patches[0].level == 0
    assert np.allclose(patches[0].h, 32.5)  # col0 depth
