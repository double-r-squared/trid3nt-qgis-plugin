"""Tests for ``workflows.modflow_mesh`` (MDAL phase 2, additive).

Two layers of coverage:

1. ``build_modflow_ugrid_mesh_netcdf`` -- the PURE builder (synthetic DIS
   georegistration + numpy grids in, a CF-1.8/UGRID-1.0 NetCDF out). No S3, no
   flopy, no QGIS -- structural asserts via ``netCDF4`` directly, mirroring
   how a NON-xarray consumer (MDAL) will actually read the file.
2. ``emit_modflow_mesh_artifact`` -- the orchestration seam (resolve outputs
   -> read timeseries -> build -> upload), exercised with a real tiny
   flopy-readable HDS/UCN binary (the SAME hand-rolled-binary technique
   ``test_modflow_local_backend._write_synthetic_ucn`` uses) and a fake S3
   client so no network / no real solver run is needed.

MDAL acceptance itself (QgsMeshLayer validity, dataset-group/timestep counts,
CRS resolution) is proven separately by the live offscreen-QGIS validation
script -- not repeated here (this suite has no qgis dependency).
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import numpy as np
import pytest

pytest.importorskip("xarray")
pytest.importorskip("netCDF4")
flopy = pytest.importorskip("flopy")

from trid3nt_server.workflows import modflow_mesh as mesh_mod
from trid3nt_server.workflows.modflow_mesh import (
    ModflowMeshError,
    build_modflow_ugrid_mesh_netcdf,
    emit_modflow_mesh_artifact,
)

# --------------------------------------------------------------------------- #
# Synthetic geo + grids
# --------------------------------------------------------------------------- #

_GEO = {"xorigin": 500000.0, "yorigin": 3000000.0, "delr": 50.0, "delc": 50.0, "nrow": 3, "ncol": 4}


def _head_grid(fill: float, nrow: int = 3, ncol: int = 4) -> np.ndarray:
    g = np.full((nrow, ncol), fill, dtype="float64")
    g[0, 0] = np.nan  # one dry/inactive cell -- NaN must survive the round trip
    return g


# --------------------------------------------------------------------------- #
# 1. Pure builder -- structural asserts via netCDF4
# --------------------------------------------------------------------------- #


def test_builder_head_only_mesh_structure(tmp_path: Path) -> None:
    import netCDF4

    out = build_modflow_ugrid_mesh_netcdf(
        geo=_GEO,
        model_crs="EPSG:32617",
        head_times=[1.0, 2.0],
        head_grids=[_head_grid(10.0), _head_grid(11.0)],
        out_path=tmp_path / "mesh.nc",
    )
    assert out.is_file()

    ds = netCDF4.Dataset(str(out))
    try:
        nrow, ncol = _GEO["nrow"], _GEO["ncol"]
        n_nodes = (nrow + 1) * (ncol + 1)
        n_faces = nrow * ncol

        assert ds.dimensions["nmesh2d_node"].size == n_nodes
        assert ds.dimensions["nmesh2d_face"].size == n_faces
        assert ds.dimensions["max_nmesh2d_face_nodes"].size == 4
        assert ds.dimensions["time"].size == 2

        mesh_var = ds.variables["mesh2d"]
        assert mesh_var.cf_role == "mesh_topology"
        assert mesh_var.topology_dimension == 2
        assert mesh_var.node_coordinates == "mesh2d_node_x mesh2d_node_y"
        assert mesh_var.face_node_connectivity == "mesh2d_face_nodes"

        face_nodes = ds.variables["mesh2d_face_nodes"][:]
        assert face_nodes.shape == (n_faces, 4)
        assert int(face_nodes.min()) >= 0
        assert int(face_nodes.max()) < n_nodes
        # every face's 4 corners must be DISTINCT node indices.
        for row in face_nodes:
            assert len(set(row.tolist())) == 4

        node_x = ds.variables["mesh2d_node_x"][:]
        node_y = ds.variables["mesh2d_node_y"][:]
        assert node_x.min() == pytest.approx(_GEO["xorigin"])
        assert node_x.max() == pytest.approx(_GEO["xorigin"] + ncol * _GEO["delr"])
        assert node_y.min() == pytest.approx(_GEO["yorigin"])
        assert node_y.max() == pytest.approx(_GEO["yorigin"] + nrow * _GEO["delc"])

        crs_var = ds.variables["crs"]
        assert crs_var.epsg_code == "EPSG:32617"

        head = ds.variables["head"]
        assert head.dimensions == ("time", "nmesh2d_face")
        assert head.shape == (2, n_faces)
        assert head.mesh == "mesh2d"
        assert head.location == "face"
        arr = head[:]
        # face 0 (row0,col0) carries the NaN cell -- must survive as masked/NaN.
        assert np.isnan(arr[0, 0]) or np.ma.is_masked(arr[0, 0])
        assert float(arr[0, 5]) == pytest.approx(10.0, abs=1e-3)
        assert float(arr[1, 5]) == pytest.approx(11.0, abs=1e-3)

        # No concentration variable when none was passed -- honest omission.
        assert "concentration" not in ds.variables

        assert "UGRID" in ds.Conventions
        time_var = ds.variables["time"]
        assert time_var.standard_name == "time"
        assert time_var.axis == "T"
    finally:
        ds.close()


def test_builder_with_concentration_adds_variable(tmp_path: Path) -> None:
    import netCDF4

    out = build_modflow_ugrid_mesh_netcdf(
        geo=_GEO,
        model_crs="EPSG:32617",
        head_times=[1.0],
        head_grids=[_head_grid(5.0)],
        conc_times=[1.0, 2.0, 3.0],
        conc_grids=[_head_grid(0.1), _head_grid(0.2), _head_grid(0.3)],
        out_path=tmp_path / "mesh.nc",
    )
    ds = netCDF4.Dataset(str(out))
    try:
        assert ds.dimensions["time"].size == 3
        conc = ds.variables["concentration"]
        assert conc.shape == (3, _GEO["nrow"] * _GEO["ncol"])
        assert conc.units == "mg/L"
        assert conc.mesh == "mesh2d"
        assert conc.location == "face"
        # head shares the SAME "time" dim (only 1 real step here).
        assert ds.variables["head"].dimensions == ("time", "nmesh2d_face")
    finally:
        ds.close()


def test_builder_shared_time_axis_is_union_with_nan_gaps(tmp_path: Path) -> None:
    """The real spill deck saves HEAD only 'LAST' but CONCENTRATION 'ALL' --
    mismatched time series. The builder must union them onto ONE 'time' axis
    (MDAL requires a single literally-named 'time' dim) and NaN-fill head at
    the times it was NOT saved, never drop/misalign a step."""
    import netCDF4

    out = build_modflow_ugrid_mesh_netcdf(
        geo=_GEO,
        model_crs="EPSG:32617",
        head_times=[3.0],  # only the LAST of 3 periods
        head_grids=[_head_grid(9.0)],
        conc_times=[1.0, 2.0, 3.0],
        conc_grids=[_head_grid(0.1), _head_grid(0.2), _head_grid(0.3)],
        out_path=tmp_path / "mesh.nc",
    )
    ds = netCDF4.Dataset(str(out))
    try:
        assert ds.dimensions["time"].size == 3
        np.testing.assert_allclose(ds.variables["time"][:], [1.0, 2.0, 3.0])
        head = np.ma.filled(ds.variables["head"][:], np.nan)
        assert np.isnan(head[0, 5]) and np.isnan(head[1, 5])
        assert float(head[2, 5]) == pytest.approx(9.0, abs=1e-3)
        conc = np.ma.filled(ds.variables["concentration"][:], np.nan)
        assert not np.any(np.isnan(conc[:, 5]))
    finally:
        ds.close()


def test_builder_face_ordering_matches_row_major_flatten(tmp_path: Path) -> None:
    """A distinctive per-cell value at (row, col) must land at face_id =
    row*ncol + col -- the SAME row-major flatten every reader (head_grid.reshape
    ordering) assumes, with NO reordering between the (nrow, ncol) grid and the
    face dimension."""
    import netCDF4

    nrow, ncol = _GEO["nrow"], _GEO["ncol"]
    grid = np.arange(nrow * ncol, dtype="float64").reshape(nrow, ncol)
    out = build_modflow_ugrid_mesh_netcdf(
        geo=_GEO, model_crs="EPSG:32617", head_times=[1.0], head_grids=[grid], out_path=tmp_path / "mesh.nc"
    )
    ds = netCDF4.Dataset(str(out))
    try:
        head = ds.variables["head"][0, :]
        np.testing.assert_allclose(np.asarray(head), np.arange(nrow * ncol, dtype="float32"))
    finally:
        ds.close()


def test_builder_rejects_mismatched_grid_shape() -> None:
    with pytest.raises(ModflowMeshError) as exc_info:
        build_modflow_ugrid_mesh_netcdf(
            geo=_GEO,
            model_crs="EPSG:32617",
            head_times=[1.0],
            head_grids=[np.zeros((2, 2))],  # wrong shape for nrow=3, ncol=4
        )
    assert exc_info.value.error_code == "MESH_WRITE_FAILED"


def test_builder_rejects_empty_head_series() -> None:
    with pytest.raises(ModflowMeshError) as exc_info:
        build_modflow_ugrid_mesh_netcdf(geo=_GEO, model_crs="EPSG:32617", head_times=[], head_grids=[])
    assert exc_info.value.error_code == "MESH_WRITE_FAILED"


# --------------------------------------------------------------------------- #
# 2. emit_modflow_mesh_artifact orchestration (real flopy-readable binaries,
#    fake S3 -- no network, no mf6 solver run).
# --------------------------------------------------------------------------- #


def _write_binary_headfile(path: Path, text: str, grids: list[np.ndarray], times: list[float]) -> None:
    """Write a minimal flopy-``HeadFile``-readable binary array file with N
    saved records -- one per ``(totim, grid)`` pair. Mirrors
    ``test_modflow_local_backend._write_synthetic_ucn``'s proven record layout,
    generalized to several stress-period/timestep records so
    ``HeadFile.get_times()`` returns N entries."""
    nrow, ncol = grids[0].shape
    with path.open("wb") as f:
        for kstp, (totim, grid) in enumerate(zip(times, grids), start=1):
            np.array([kstp, 1], dtype="<i4").tofile(f)
            np.array([totim, totim], dtype="<f8").tofile(f)
            f.write(text.ljust(16)[:16].encode("ascii"))
            np.array([ncol, nrow, 1], dtype="<i4").tofile(f)
            np.nan_to_num(grid, nan=1e30).astype("<f8").tofile(f)


class _FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.put_calls: list[tuple[str, str]] = []

    def put_object(self, Bucket: str, Key: str, Body: Any, **_kw: Any) -> dict:
        data = Body.read() if hasattr(Body, "read") else bytes(Body)
        self.objects[(Bucket, Key)] = data
        self.put_calls.append((Bucket, Key))
        return {}


@pytest.fixture()
def synthetic_run(tmp_path: Path) -> Path:
    """A run-outputs dir carrying a real flopy-readable ``gwf_model.hds`` (2
    steps) + ``gwt_model.ucn`` (3 steps)."""
    run_dir = tmp_path / "run_outputs"
    run_dir.mkdir()
    grids = [_head_grid(10.0), _head_grid(12.0)]
    _write_binary_headfile(run_dir / "gwf_model.hds", "HEAD", grids, [1.0, 2.0])
    conc_grids = [_head_grid(0.1), _head_grid(0.2), _head_grid(0.3)]
    _write_binary_headfile(run_dir / "gwt_model.ucn", "CONCENTRATION", conc_grids, [1.0, 2.0, 3.0])
    return run_dir


def test_emit_uploads_mesh_with_head_and_concentration(
    monkeypatch: pytest.MonkeyPatch, synthetic_run: Path
) -> None:
    monkeypatch.setattr(mesh_mod, "_grid_georegistration_from_deck", lambda deck_dir: dict(_GEO))
    fake = _FakeS3Client()
    monkeypatch.setattr("trid3nt_server.tools.solver._get_s3_client", lambda: fake, raising=False)
    monkeypatch.setenv("TRID3NT_STORAGE_BACKEND", "s3")
    monkeypatch.setenv("TRID3NT_RUNS_BUCKET", "trid3nt-runs")

    uri = emit_modflow_mesh_artifact(
        str(synthetic_run),
        run_id="01RUNMESHTEST01",
        model_crs="EPSG:32617",
        deck_dir="/fake/deck",
    )

    assert uri == "s3://trid3nt-runs/01RUNMESHTEST01/modflow_mesh.nc"
    assert fake.put_calls == [("trid3nt-runs", "01RUNMESHTEST01/modflow_mesh.nc")]

    # The uploaded bytes are a real readable NetCDF with both quantities.
    import netCDF4

    raw = fake.objects[("trid3nt-runs", "01RUNMESHTEST01/modflow_mesh.nc")]
    with netCDF4.Dataset("inmem", memory=raw) as ds:
        # head has 2 saved steps, concentration 3 -- shared "time" axis is
        # their UNION (MDAL requires one literally-named "time" dim); head is
        # NaN at the one time slot it did not save.
        assert ds.dimensions["time"].size == 3
        assert ds.variables["concentration"].shape[0] == 3
        assert ds.variables["head"].shape[0] == 3
        assert ds.variables["crs"].epsg_code == "EPSG:32617"


def test_emit_head_only_when_no_ucn(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    run_dir = tmp_path / "run_outputs_gwf_only"
    run_dir.mkdir()
    _write_binary_headfile(run_dir / "gwf_model.hds", "HEAD", [_head_grid(1.0)], [1.0])
    # No gwt_model.ucn -- a GWF-only archetype deck.

    monkeypatch.setattr(mesh_mod, "_grid_georegistration_from_deck", lambda deck_dir: dict(_GEO))
    fake = _FakeS3Client()
    monkeypatch.setattr("trid3nt_server.tools.solver._get_s3_client", lambda: fake, raising=False)
    monkeypatch.setenv("TRID3NT_STORAGE_BACKEND", "s3")
    monkeypatch.setenv("TRID3NT_RUNS_BUCKET", "trid3nt-runs")

    uri = emit_modflow_mesh_artifact(
        str(run_dir), run_id="01RUNGWFONLY01", model_crs="EPSG:32617", deck_dir="/fake/deck"
    )
    assert uri == "s3://trid3nt-runs/01RUNGWFONLY01/modflow_mesh.nc"

    import netCDF4

    raw = fake.objects[("trid3nt-runs", "01RUNGWFONLY01/modflow_mesh.nc")]
    with netCDF4.Dataset("inmem", memory=raw) as ds:
        assert "concentration" not in ds.variables


def test_emit_local_file_fallback_does_not_delete_returned_path(
    monkeypatch: pytest.MonkeyPatch, synthetic_run: Path
) -> None:
    """A local-dev/offline degrade of the upload can return
    ``file://<the same temp path>`` the builder just wrote (the SAME
    convention the COG path already relies on -- its local temp file is
    likewise never unlinked on a file:// degrade; ``storage_scheme()`` is
    hardcoded to ``"s3"`` post-GCP-decommission today, so this exercises the
    defensive branch directly via a monkeypatched ``upload_cog`` rather than
    the -- currently unreachable in production -- gs-fallback path). A prior
    version of this function unlinked the temp file unconditionally in its
    ``finally``, which silently deleted the file out from under the URI it
    had just returned to the caller."""
    monkeypatch.setattr(mesh_mod, "_grid_georegistration_from_deck", lambda deck_dir: dict(_GEO))

    captured: dict[str, Path] = {}

    def _fake_upload_cog(local_cog: Path, run_id: str, runs_bucket: Any, **kw: Any) -> str:
        captured["path"] = local_cog
        return f"file://{local_cog}"

    monkeypatch.setattr(mesh_mod.cog_io, "upload_cog", _fake_upload_cog)

    uri = emit_modflow_mesh_artifact(
        str(synthetic_run), run_id="01RUNLOCALFALLBACK1", model_crs="EPSG:32617", deck_dir="/fake/deck"
    )

    assert uri is not None and uri.startswith("file://")
    assert uri == f"file://{captured['path']}"
    assert captured["path"].is_file(), "the returned file:// URI must still point at a real file"


def test_emit_returns_none_without_georegistration(monkeypatch: pytest.MonkeyPatch, synthetic_run: Path) -> None:
    monkeypatch.setattr(mesh_mod, "_grid_georegistration_from_deck", lambda deck_dir: None)
    uri = emit_modflow_mesh_artifact(
        str(synthetic_run), run_id="01RUNNODECK0001", model_crs="EPSG:32617", deck_dir=None
    )
    assert uri is None


def test_emit_returns_none_on_missing_head_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    empty_dir = tmp_path / "empty_run"
    empty_dir.mkdir()
    monkeypatch.setattr(mesh_mod, "_grid_georegistration_from_deck", lambda deck_dir: dict(_GEO))
    uri = emit_modflow_mesh_artifact(
        str(empty_dir), run_id="01RUNNOHEAD0001", model_crs="EPSG:32617", deck_dir="/fake/deck"
    )
    assert uri is None
