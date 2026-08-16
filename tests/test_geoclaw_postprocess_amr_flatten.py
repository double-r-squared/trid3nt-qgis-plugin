"""AMR flatten + overland-mask regression for the GeoClaw depth-COG postprocess.

Guards the fix for the NATE-flagged defect where the published depth COG rendered
ONE uniform-value rectangle: a coarse AMR patch cell smeared across the footprint
of a finer patch that resolved the ground as dry, surviving the overland mask.

Fixture: a synthetic 2-frame run. Frame 1 (peak) has a COARSE level-1 patch that
is uniformly wet over the whole AOI PLUS a finer level-2 patch over the eastern
(land) half whose cells carry VARYING depths and some DRY cells. Asserts:

  * finest-wins flattening: inside the level-2 footprint the output takes the
    level-2 depth where wet and NaN where dry -- the coarse uniform value never
    survives under a finer patch (wet OR dry).
  * overland mask honored: the western half (topo <= sea_level) is masked to NaN.
  * the NATE gate: the emitted wet field is NOT depth-uniform when the fixture's
    land depths vary.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from trid3nt_server.agent.workflows.geoclaw import postprocess_geoclaw as PP


def _write_fort_q(path: Path, patches: list[dict]) -> None:
    """Write a synthetic fort.q frame. Each patch dict: level, mx, my, xlow, ylow,
    dx, dy, h (an (my, mx) array, row 0 = ylow=south)."""
    lines: list[str] = []
    for gi, p in enumerate(patches, start=1):
        h = np.asarray(p["h"], dtype=float)
        my, mx = h.shape
        lines += [
            f"    {gi}    grid_number",
            f"    {p['level']}    AMR_level",
            f"    {mx}    mx",
            f"    {my}    my",
            f"    {p['xlow']:.6e}    xlow",
            f"    {p['ylow']:.6e}    ylow",
            f"    {p['dx']:.6e}    dx",
            f"    {p['dy']:.6e}    dy",
            "",
        ]
        for j in range(my):  # south -> north
            for i in range(mx):
                lines.append(f"    {h[j, i]:.6e}")
            lines.append("")  # blank line terminates a j-row block
    path.write_text("\n".join(lines) + "\n")


def _fixture_run(tmp_path: Path) -> Path:
    out = tmp_path / "_output"
    out.mkdir()
    # Frame 0 = still-water init: coarse patch, western half wet (ocean), east dry.
    _write_fort_q(
        out / "fort.q0000",
        [
            {
                "level": 1, "mx": 2, "my": 2, "xlow": 0.0, "ylow": 0.0,
                "dx": 0.5, "dy": 0.5,
                # cols: 0=west (wet 0.5), 1=east (dry)
                "h": [[0.5, 0.0], [0.5, 0.0]],
            }
        ],
    )
    # Frame 1 = peak: coarse uniformly-wet L1 (the smear source) + fine L2 over the
    # EAST (land) half with varying + dry cells.
    _write_fort_q(
        out / "fort.q0001",
        [
            {
                "level": 1, "mx": 2, "my": 2, "xlow": 0.0, "ylow": 0.0,
                "dx": 0.5, "dy": 0.5,
                "h": [[0.7, 0.7], [0.7, 0.7]],  # uniform coarse water column
            },
            {
                # fine patch over east half [0.5, 1.0] x [0, 1]
                "level": 2, "mx": 2, "my": 2, "xlow": 0.5, "ylow": 0.0,
                "dx": 0.25, "dy": 0.5,
                # row0=south: [SW=0.3, SE=dry], row1=north: [NW=0.9, NE=dry]
                "h": [[0.3, 0.0], [0.9, 0.0]],
            },
        ],
    )
    return tmp_path


@pytest.fixture
def _no_io(monkeypatch):
    """Capture the grids written to COGs and stub out S3 upload / unlink / fgmax."""
    captured: list[np.ndarray] = []

    def _fake_write(grid, bbox):
        captured.append(np.asarray(grid, dtype="float64").copy())
        return Path("/tmp/_fake_geoclaw_cog.tif")

    monkeypatch.setattr(PP, "_write_depth_cog_4326", _fake_write)
    monkeypatch.setattr(
        PP, "_upload_cog_to_runs_bucket",
        lambda *a, **k: "s3://fake-runs/fake.tif",
    )
    monkeypatch.setattr(PP, "_safe_unlink", lambda p: None)
    monkeypatch.setattr(PP, "read_fgmax_output", lambda *a, **k: None)
    return captured


def test_finest_wins_and_overland_mask_not_uniform(tmp_path, _no_io):
    run_dir = _fixture_run(tmp_path)
    bbox = (0.0, 0.0, 1.0, 1.0)
    shape = (8, 8)
    # topo: west half (cols 0-3) <= sea_level (ocean), east half (cols 4-7) land.
    topo = np.full(shape, 2.0)
    topo[:, :4] = -1.0

    layers, metrics = PP.postprocess_geoclaw(
        run_dir, bbox, run_id="TST", scenario="tsunami",
        grid_shape=shape, topo_grid=topo, mask_ocean=True, sea_level_m=0.0,
    )
    assert layers, "expected at least the peak layer"
    peak = _no_io[0]  # first COG written is the peak grid

    west = peak[:, :4]
    east = peak[:, 4:]

    # Overland mask: the western (topo <= 0) half is entirely NaN.
    assert np.all(np.isnan(west)), "ocean (topo<=sea_level) half must be masked"

    # Finest-wins: the coarse uniform 0.7 never survives under the finer L2 patch.
    assert not np.any(np.isclose(east[np.isfinite(east)], 0.7)), (
        "coarse smear value 0.7 leaked through a finer patch"
    )
    # Finer DRY erases coarse wet: the eastern-most columns (L2 dry) are NaN.
    assert np.all(np.isnan(peak[:, 6:])), "finer dry cells must erase coarse wet"
    # Finer WET wins: 0.3 (south) and 0.9 (north) present on land.
    east_wet = east[np.isfinite(east)]
    assert east_wet.size > 0
    assert np.any(np.isclose(east_wet, 0.3)) and np.any(np.isclose(east_wet, 0.9))

    # NATE gate: the emitted overland wet field is NOT depth-uniform.
    assert np.unique(np.round(east_wet, 6)).size >= 2, (
        "overland depth must vary across land cells (no uniform rectangle)"
    )
    # And the metrics reflect a varied field (mean strictly below the max).
    assert metrics["mean_depth_m"] < metrics["max_depth_m"]


def test_mesh_geojson_structure_levels_and_decimation():
    """The AMR grid-line mesh: per-level patches present, honest decimation flag,
    correct CRS, and a valid LineString FeatureCollection."""
    from trid3nt_server.agent.workflows.geoclaw.postprocess_geoclaw import (
        build_geoclaw_mesh_geojson,
    )

    class _P:
        def __init__(self, level, mx, my, xlow, ylow, dx, dy):
            self.level, self.mx, self.my = level, mx, my
            self.xlow, self.ylow, self.dx, self.dy = xlow, ylow, dx, dy
            self.h = None  # mesh builder never touches depth

    # L1 coarse full patch (4x4 <= full-cell threshold) + L2 fine LARGE patch
    # (200x200 > threshold) forcing the decimation branch.
    coarse = _P(1, 4, 4, -124.24, 41.73, 0.02, 0.0125)
    fine = _P(2, 200, 200, -124.21, 41.745, 0.00015, 0.000125)
    fc, stats = build_geoclaw_mesh_geojson([coarse, fine], frame_no=5)

    # FeatureCollection shape + CRS honesty.
    assert fc["type"] == "FeatureCollection"
    assert fc["metadata"]["crs"] == "EPSG:4326"
    assert fc["metadata"]["frame_no"] == 5
    assert fc["metadata"]["level_histogram"] == {"1": 1, "2": 1}
    assert fc["metadata"]["max_level"] == 2
    assert stats["patch_count"] == 2

    feats = {f["properties"]["level"]: f for f in fc["features"]}
    assert set(feats) == {1, 2}
    for f in fc["features"]:
        assert f["geometry"]["type"] == "MultiLineString"
        for seg in f["geometry"]["coordinates"]:
            assert len(seg) == 2 and len(seg[0]) == 2  # a 2-point LineString, lon/lat

    # L1 coarse: FULL grid, NOT decimated -> mx+1 + my+1 = 5+5 = 10 lines.
    assert feats[1]["properties"]["decimated"] is False
    assert feats[1]["properties"]["sample_stride_x"] == 1
    assert feats[1]["properties"]["n_grid_lines"] == 10

    # L2 fine: DECIMATED honestly (stride > 1) and bounded (~<= 2*(sample+boundary)).
    assert feats[2]["properties"]["decimated"] is True
    assert feats[2]["properties"]["sample_stride_x"] > 1
    assert feats[2]["properties"]["n_grid_lines"] < 2 * 200  # far fewer than every edge
    assert stats["decimated_patch_count"] == 1
    assert "sampled" in fc["metadata"]["decimation_policy"]


def test_build_geoclaw_mesh_layer_from_fort_q(tmp_path, monkeypatch):
    """End-to-end: parse a synthetic multi-patch fort.q frame, build + 'upload'
    the mesh, assert the emitted LayerURI envelope (vector / mesh_grid / context /
    crs_authid) mirrors the hecras mesh row."""
    from trid3nt_server.agent.workflows.geoclaw import postprocess_geoclaw as PPmod

    out = tmp_path / "_output"
    out.mkdir()
    # Two frames; the LAST (final) frame is the one the mesh reads.
    _write_fort_q(
        out / "fort.q0000",
        [{"level": 1, "mx": 2, "my": 2, "xlow": -124.24, "ylow": 41.73,
          "dx": 0.04, "dy": 0.025, "h": [[0.1, 0.1], [0.1, 0.1]]}],
    )
    _write_fort_q(
        out / "fort.q0001",
        [
            {"level": 1, "mx": 2, "my": 2, "xlow": -124.24, "ylow": 41.73,
             "dx": 0.04, "dy": 0.025, "h": [[0.1, 0.1], [0.1, 0.1]]},
            {"level": 2, "mx": 4, "my": 4, "xlow": -124.21, "ylow": 41.745,
             "dx": 0.0075, "dy": 0.00625,
             "h": [[0.2] * 4 for _ in range(4)]},
        ],
    )

    captured: dict = {}

    class _FakeS3:
        def put_object(self, **kw):
            captured.update(kw)

    monkeypatch.setattr(
        "trid3nt_server.agent.tools.simulation.solver.solver._get_s3_client",
        lambda: _FakeS3(),
    )
    monkeypatch.setattr(
        "trid3nt_server.agent.tools.simulation.solver.solver._get_runs_bucket",
        lambda: "trid3nt-runs",
    )

    layer = PPmod.build_geoclaw_mesh_layer(tmp_path, run_id="TSTMESH")
    assert layer is not None
    assert layer.layer_type == "vector"
    assert layer.style_preset == "mesh_grid"
    assert layer.role == "context"
    assert layer.bbox is None
    assert layer.crs_authid == "EPSG:4326"
    assert layer.uri == "s3://trid3nt-runs/TSTMESH/mesh.geojson"
    assert layer.layer_id == "geoclaw-mesh-TSTMESH"

    # The uploaded body is a valid FeatureCollection built from the FINAL frame
    # (2 patches, L1 + L2), correct content type.
    import json as _json

    assert captured["ContentType"] == "application/geo+json"
    fc = _json.loads(captured["Body"].decode("utf-8"))
    assert fc["type"] == "FeatureCollection"
    assert fc["metadata"]["frame_no"] == 1
    assert {f["properties"]["level"] for f in fc["features"]} == {1, 2}


def test_rasterize_finer_dry_erases_coarse_wet():
    """Pure rasterize: a finer DRY patch cell erases the coarser WET value beneath
    it (no coarse-cell smear), and the field is not uniform."""
    from trid3nt_server.agent.workflows.geoclaw.postprocess_geoclaw import (
        rasterize_frame_to_grid,
    )

    class _P:
        def __init__(self, level, mx, my, xlow, ylow, dx, dy, h):
            self.level, self.mx, self.my = level, mx, my
            self.xlow, self.ylow, self.dx, self.dy = xlow, ylow, dx, dy
            self.h = np.asarray(h, dtype=float)

    coarse = _P(1, 2, 2, 0.0, 0.0, 0.5, 0.5, [[0.7, 0.7], [0.7, 0.7]])
    fine = _P(2, 2, 2, 0.5, 0.0, 0.25, 0.5, [[0.3, 0.0], [0.9, 0.0]])
    grid = rasterize_frame_to_grid([coarse, fine], (0.0, 0.0, 1.0, 1.0), (8, 8))

    east = grid[:, 4:]
    # coarse 0.7 does not survive anywhere under the finer patch footprint
    assert not np.any(np.isclose(east[np.isfinite(east)], 0.7))
    # finer dry (eastern columns) -> NaN, not the coarse 0.7
    assert np.all(np.isnan(grid[:, 6:]))
    # west half (no finer patch) keeps the coarse value
    assert np.allclose(grid[:, :4], 0.7)
    # not uniform overall
    finite = grid[np.isfinite(grid)]
    assert np.unique(np.round(finite, 6)).size >= 3
