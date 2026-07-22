"""Quadtree (face-indexed UGRID) COG-write tests for postprocess_flood (P1).

The cht_sfincs quadtree solve writes a FACE-INDEXED UGRID ``sfincs_map.nc``:
fields live on ``nmesh2d_face`` (one scalar per quadtree face) with per-face
coordinates ``mesh2d_face_x`` / ``mesh2d_face_y`` — NOT the regular ``(n, m)``
grid + 1D ``x``/``y`` coords the legacy ``_write_verified_cog`` ``from_bounds``
path assumes. Before P1 that path would FAIL on real quadtree output (which also
means the existing DEPTH animation likely never ran on a true quadtree solve).

P1 added:
- ``_is_quadtree_output(ds)`` — probe (``nmesh2d_face`` in dims OR
  ``mesh2d_face_x`` in variables).
- ``_rasterize_face_field(values_1d, face_x, face_y, ...)`` — grid per-face
  scalars onto a regular metric raster (scipy nearest-neighbour) in the deck's
  projected (UTM) CRS.
- ``_write_verified_cog`` branches a face-indexed dataset through the rasterizer.

These tests build a SYNTHETIC face-indexed dataset and assert the writer
produces a valid georeferenced 2D COG — proving DEPTH-on-quadtree works too.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("rasterio")
pytest.importorskip("xarray")
pytest.importorskip("scipy")
pytest.importorskip("pyproj")

from trid3nt_server.workflows.postprocess_flood import (  # noqa: E402
    NODATA_DEPTH_M,
    PostprocessError,
    _is_quadtree_output,
    _rasterize_face_field,
    _read_face_coords,
    _write_verified_cog,
)


# Mexico Beach UTM zone 16N (matches the coastal North Star deck CRS).
_UTM16N = "EPSG:32616"
# A bbox over the Mexico Beach panhandle (EPSG:4326).
_BBOX = (-85.45, 29.93, -85.38, 29.98)


def _epsg_to_wkt(epsg_str: str) -> str:
    try:
        import pyproj

        return pyproj.CRS.from_string(epsg_str).to_wkt()
    except Exception:
        return epsg_str


def _make_quadtree_ds(
    *,
    n_faces: int = 400,
    n_steps: int = 0,
    rising: bool = False,
    crs: str = _UTM16N,
):
    """Build a synthetic FACE-INDEXED UGRID xr.Dataset.

    - ``hm0(nmesh2d_face[, time])`` — a wave-height-like field (also serves as a
      generic per-face scalar).
    - ``zs(nmesh2d_face[, time])`` + ``zb(nmesh2d_face)`` — water-level + bed
      level so the DEPTH path (zs - zb) resolves on a quadtree dataset.
    - ``mesh2d_face_x`` / ``mesh2d_face_y`` — per-face centroids in UTM metres.
    - ``crs`` variable carrying the WKT (CF-convention).

    When ``n_steps > 0`` the time dim is added (dims (nmesh2d_face, time) to
    mirror the verified ncoutput.F90 ordering); ``rising`` makes the field grow
    with the time index. Face centroids are laid out over a UTM box derived from
    the AOI bbox so the rasterizer's bbox-reproject path is exercised.
    """
    import xarray as xr
    from pyproj import Transformer

    tf = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
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

    # Base per-face field: a smooth ramp 0..3 m across the domain (so masking +
    # aggregates are non-trivial), with a dry NW corner (values below threshold).
    base = np.linspace(0.0, 3.0, n_faces).astype("float32")

    data_vars: dict = {
        "crs": xr.DataArray(0, attrs={"crs_wkt": _epsg_to_wkt(crs)}),
        "mesh2d_face_x": xr.DataArray(face_x, dims=["nmesh2d_face"]),
        "mesh2d_face_y": xr.DataArray(face_y, dims=["nmesh2d_face"]),
    }

    if n_steps > 0:
        hm0 = np.zeros((n_faces, n_steps), dtype="float32")
        zs = np.zeros((n_faces, n_steps), dtype="float32")
        for t in range(n_steps):
            scale = (t / max(1, n_steps - 1)) if rising else 1.0
            hm0[:, t] = base * scale
            zs[:, t] = base * scale  # water level rising = base ramp
        data_vars["hm0"] = xr.DataArray(hm0, dims=["nmesh2d_face", "time"])
        data_vars["zs"] = xr.DataArray(zs, dims=["nmesh2d_face", "time"])
        data_vars["zb"] = xr.DataArray(
            np.zeros(n_faces, dtype="float32"), dims=["nmesh2d_face"]
        )
        coords = {"time": np.arange(n_steps)}
    else:
        data_vars["hm0"] = xr.DataArray(base, dims=["nmesh2d_face"])
        data_vars["zb"] = xr.DataArray(
            np.zeros(n_faces, dtype="float32"), dims=["nmesh2d_face"]
        )
        coords = {}

    return xr.Dataset(data_vars, coords=coords)


# --------------------------------------------------------------------------- #
# Probe
# --------------------------------------------------------------------------- #


def test_is_quadtree_output_detects_face_dim() -> None:
    ds = _make_quadtree_ds()
    assert _is_quadtree_output(ds) is True


def test_is_quadtree_output_false_for_regular_grid() -> None:
    import xarray as xr

    ds = xr.Dataset(
        {"hmax": xr.DataArray(np.zeros((1, 4, 5)), dims=["timemax", "n", "m"])},
        coords={
            "x": xr.DataArray(np.arange(5, dtype="float64"), dims=["m"]),
            "y": xr.DataArray(np.arange(4, dtype="float64"), dims=["n"]),
        },
    )
    assert _is_quadtree_output(ds) is False


def test_read_face_coords_returns_1d_arrays() -> None:
    ds = _make_quadtree_ds(n_faces=100)
    fx, fy = _read_face_coords(ds)
    assert fx.ndim == 1 and fy.ndim == 1
    assert fx.shape[0] == fy.shape[0] == 100


def test_read_face_coords_raises_when_absent() -> None:
    import xarray as xr

    ds = xr.Dataset({"hm0": xr.DataArray(np.zeros(3), dims=["nmesh2d_face"])})
    with pytest.raises(PostprocessError) as ei:
        _read_face_coords(ds)
    assert ei.value.error_code == "RUN_OUTPUT_UNEXPECTED_SHAPE"


# --------------------------------------------------------------------------- #
# Rasterizer
# --------------------------------------------------------------------------- #


def test_rasterize_face_field_produces_2d_grid() -> None:
    ds = _make_quadtree_ds(n_faces=400)
    fx, fy = _read_face_coords(ds)
    vals = ds["hm0"].values
    arr, transform = _rasterize_face_field(
        vals, fx, fy, crs=_UTM16N, bbox=_BBOX, resolution_m=30.0
    )
    assert arr.ndim == 2
    assert arr.shape[0] > 1 and arr.shape[1] > 1
    # The nearest-neighbour grid preserves the per-face value range (no invented
    # magnitudes beyond [min, max] of the source faces).
    finite = arr[np.isfinite(arr)]
    assert finite.size > 0
    assert finite.min() >= float(vals.min()) - 1e-4
    assert finite.max() <= float(vals.max()) + 1e-4
    # from_bounds transform: positive dx, negative dy (north-up).
    assert transform.a > 0
    assert transform.e < 0


def test_rasterize_face_field_length_mismatch_raises() -> None:
    with pytest.raises(PostprocessError) as ei:
        _rasterize_face_field(
            np.zeros(5), np.zeros(4), np.zeros(4), crs=_UTM16N, bbox=None
        )
    assert ei.value.error_code == "RUN_OUTPUT_UNEXPECTED_SHAPE"


# --------------------------------------------------------------------------- #
# _write_verified_cog on a face-indexed dataset → valid georeferenced COG
# --------------------------------------------------------------------------- #


def _assert_valid_projected_cog(cog_path: Path) -> None:
    import rasterio

    assert cog_path.exists()
    with rasterio.open(cog_path) as ds:
        assert ds.count == 1
        assert ds.width > 1 and ds.height > 1
        # Authored in the UTM (projected) CRS of the face coords.
        assert ds.crs is not None
        assert not ds.crs.is_geographic
        assert ds.crs.to_epsg() == 32616
        # Projected bounds are metric (|x| > 1000) — the CRS_TAG_MISMATCH guard
        # passes (projected tag, projected magnitudes).
        assert abs(ds.bounds.left) > 1000
        band = ds.read(1)
        assert band.dtype == np.dtype("float32")
        finite = band[np.isfinite(band)]
        assert finite.size > 0


def test_write_verified_cog_depth_on_quadtree(tmp_path: Path) -> None:
    """A face-indexed DEPTH field (zs.max(time) - zb) writes a valid COG — proves
    depth-on-quadtree works (the legacy from_bounds path would have failed)."""
    ds = _make_quadtree_ds(n_faces=400, n_steps=5, rising=True)
    depth = (ds["zs"].max(dim="time") - ds["zb"]).clip(min=0.0)
    cog, metrics = _write_verified_cog(
        depth.values,
        ds=ds,
        netcdf_path=tmp_path / "sfincs_map.nc",
        bbox=_BBOX,
    )
    try:
        _assert_valid_projected_cog(cog)
        assert metrics["units"] == "meters"
        assert metrics["crs"].endswith("32616")
        assert metrics["max_depth_m"] > NODATA_DEPTH_M
        assert metrics["flooded_cell_count"] > 0
    finally:
        cog.unlink(missing_ok=True)


def test_write_verified_cog_face_values_kwarg(tmp_path: Path) -> None:
    """Passing ``face_values`` explicitly routes any field through the rasterizer
    (the path postprocess_waves uses)."""
    ds = _make_quadtree_ds(n_faces=256)
    vals = ds["hm0"].values
    cog, metrics = _write_verified_cog(
        vals,
        ds=ds,
        netcdf_path=tmp_path / "sfincs_map.nc",
        face_values=vals,
        bbox=_BBOX,
        nodata_threshold_m=0.05,
    )
    try:
        _assert_valid_projected_cog(cog)
    finally:
        cog.unlink(missing_ok=True)


def test_write_verified_cog_quadtree_without_bbox(tmp_path: Path) -> None:
    """No bbox → the rasterizer bounds to the face extent (still valid COG)."""
    ds = _make_quadtree_ds(n_faces=225)
    vals = ds["hm0"].values
    cog, _metrics = _write_verified_cog(
        vals, ds=ds, netcdf_path=tmp_path / "sfincs_map.nc", face_values=vals
    )
    try:
        _assert_valid_projected_cog(cog)
    finally:
        cog.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# REAL cht_sfincs quadtree schema: NO mesh2d_face_x/_y — centroids computed
# from mesh2d_node_x/_y + mesh2d_face_nodes (start_index=1), crs var VALUE int
# --------------------------------------------------------------------------- #


def _make_real_schema_quadtree_ds(
    *,
    side: int = 20,
    n_steps: int = 0,
    rising: bool = False,
    crs_epsg: int = 32616,
):
    """Build a synthetic FACE-INDEXED UGRID xr.Dataset matching the REAL
    cht_sfincs ``sfincs_map.nc`` schema (verified against /tmp/mb_map.nc):

    - ``mesh2d_node_x`` / ``mesh2d_node_y`` (``nmesh2d_node``) — per-node coords.
    - ``mesh2d_face_nodes`` (``nmesh2d_face``, ``max_nmesh2d_face_nodes``) — each
      face's corner node indices, **1-based** (``start_index=1`` attr), float64,
      with a fill value in unused slots for non-quad faces.
    - ``crs`` — a SCALAR data variable whose VALUE is the bare int EPSG code
      (32616), ``attrs={'EPSG': '-'}`` (a useless placeholder, exactly as cht
      writes it). NO ``crs_wkt`` / ``epsg_code`` attr.
    - ``zs(time, nmesh2d_face)`` + ``zb(nmesh2d_face)`` and ``hm0`` on the faces.
    - NO ``mesh2d_face_x`` / ``mesh2d_face_y`` — the centroids must be COMPUTED.

    Builds a regular ``side x side`` grid of unit quad cells in UTM metres
    (Mexico Beach box). One face is given a TRIANGLE (a fill index in slot 4) to
    exercise the defensive non-quad masking. Returns ``(ds, expected_face_xy)``
    where ``expected_face_xy`` is the analytic per-face centroid array.
    """
    import xarray as xr
    from pyproj import Transformer

    tf = Transformer.from_crs("EPSG:4326", f"EPSG:{crs_epsg}", always_xy=True)
    x0, y0 = tf.transform(_BBOX[0], _BBOX[1])
    x1, y1 = tf.transform(_BBOX[2], _BBOX[3])
    minx, maxx = min(x0, x1), max(x0, x1)
    miny, maxy = min(y0, y1), max(y0, y1)

    # Node grid: (side+1) x (side+1) corner nodes over the UTM box.
    nx = np.linspace(minx, maxx, side + 1)
    ny = np.linspace(miny, maxy, side + 1)
    gx, gy = np.meshgrid(nx, ny)  # (side+1, side+1)
    node_x = gx.ravel().astype("float64")
    node_y = gy.ravel().astype("float64")
    n_node_cols = side + 1

    def node_id_1based(r: int, c: int) -> int:
        # 1-based node id (start_index=1).
        return r * n_node_cols + c + 1

    n_faces = side * side
    FILL = -999.0  # an out-of-range fill sentinel for unused slots.
    conn = np.full((n_faces, 4), FILL, dtype="float64")
    expected_x = np.zeros(n_faces, dtype="float64")
    expected_y = np.zeros(n_faces, dtype="float64")
    f = 0
    for r in range(side):
        for c in range(side):
            n_bl = node_id_1based(r, c)
            n_br = node_id_1based(r, c + 1)
            n_tr = node_id_1based(r + 1, c + 1)
            n_tl = node_id_1based(r + 1, c)
            ids = [n_bl, n_br, n_tr, n_tl]
            # Make face 0 a TRIANGLE (drop the 4th corner -> fill slot) to test
            # the defensive non-quad masking path (nanmean over 3 real nodes).
            if f == 0:
                conn[f, :3] = ids[:3]
                used = ids[:3]
            else:
                conn[f, :] = ids
                used = ids
            zerob = np.asarray(used, dtype=int) - 1
            expected_x[f] = node_x[zerob].mean()
            expected_y[f] = node_y[zerob].mean()
            f += 1

    data_vars: dict = {
        "mesh2d_node_x": xr.DataArray(node_x, dims=["nmesh2d_node"]),
        "mesh2d_node_y": xr.DataArray(node_y, dims=["nmesh2d_node"]),
        "mesh2d_face_nodes": xr.DataArray(
            conn,
            dims=["nmesh2d_face", "max_nmesh2d_face_nodes"],
            attrs={"cf_role": "face_node_connectivity", "start_index": 1},
        ),
        # crs VARIABLE VALUE is the bare int EPSG code; attrs is the cht
        # placeholder. NO crs_wkt / epsg_code.
        "crs": xr.DataArray(np.int32(crs_epsg), attrs={"EPSG": "-"}),
    }

    base = np.linspace(0.0, 3.0, n_faces).astype("float32")
    if n_steps > 0:
        zs = np.zeros((n_steps, n_faces), dtype="float32")
        hm0 = np.zeros((n_steps, n_faces), dtype="float32")
        for t in range(n_steps):
            scale = (t / max(1, n_steps - 1)) if rising else 1.0
            zs[t, :] = base * scale
            hm0[t, :] = base * scale
        data_vars["zs"] = xr.DataArray(zs, dims=["time", "nmesh2d_face"])
        data_vars["hm0"] = xr.DataArray(hm0, dims=["time", "nmesh2d_face"])
        coords = {"time": np.arange(n_steps)}
    else:
        data_vars["hm0"] = xr.DataArray(base, dims=["nmesh2d_face"])
        coords = {}
    data_vars["zb"] = xr.DataArray(
        np.zeros(n_faces, dtype="float32"), dims=["nmesh2d_face"]
    )

    ds = xr.Dataset(data_vars, coords=coords)
    return ds, np.column_stack([expected_x, expected_y])


def test_read_crs_from_real_schema_variable_value() -> None:
    """The cht crs var stores the bare int EPSG as its VALUE (attrs={'EPSG':'-'});
    the reader must return EPSG:32616, NOT the EPSG:3857 fallback."""
    from trid3nt_server.workflows.postprocess_flood import _read_crs_from_dataset

    ds, _ = _make_real_schema_quadtree_ds(side=8)
    assert _read_crs_from_dataset(ds) == "EPSG:32616"


def test_is_quadtree_output_true_for_real_schema() -> None:
    ds, _ = _make_real_schema_quadtree_ds(side=8)
    assert _is_quadtree_output(ds) is True


def test_read_face_coords_computed_from_nodes_no_face_xy() -> None:
    """With NO mesh2d_face_x/_y present, centroids are COMPUTED from node coords
    + face-node connectivity (1-based, fill-masked) — matching the analytic
    per-face centroids (incl. the triangle face 0)."""
    ds, expected = _make_real_schema_quadtree_ds(side=20)
    assert "mesh2d_face_x" not in ds.variables
    fx, fy = _read_face_coords(ds)
    n_faces = 20 * 20
    assert fx.shape[0] == fy.shape[0] == n_faces
    assert np.isfinite(fx).all() and np.isfinite(fy).all()
    np.testing.assert_allclose(fx, expected[:, 0], rtol=0, atol=1e-6)
    np.testing.assert_allclose(fy, expected[:, 1], rtol=0, atol=1e-6)
    # Centroids fall inside the UTM node bbox.
    nx = ds["mesh2d_node_x"].values
    ny = ds["mesh2d_node_y"].values
    assert nx.min() <= fx.min() and fx.max() <= nx.max()
    assert ny.min() <= fy.min() and fy.max() <= ny.max()


def test_write_verified_cog_depth_on_real_schema(tmp_path: Path) -> None:
    """End-to-end on the REAL schema: a DEPTH field (zs.max(time) - zb) writes a
    valid georeferenced COG tagged EPSG:32616 with centroids computed from the
    node coords + connectivity (the case the synthetic-fixture tests missed)."""
    ds, _ = _make_real_schema_quadtree_ds(side=24, n_steps=5, rising=True)
    depth = (ds["zs"].max(dim="time") - ds["zb"]).clip(min=0.0)
    cog, metrics = _write_verified_cog(
        depth.values,
        ds=ds,
        netcdf_path=tmp_path / "sfincs_map.nc",
        bbox=_BBOX,
    )
    try:
        _assert_valid_projected_cog(cog)
        assert metrics["crs"] == "EPSG:32616"
        assert metrics["units"] == "meters"
        assert metrics["max_depth_m"] > NODATA_DEPTH_M
        assert metrics["flooded_cell_count"] > 0
        # COG bounds reproject into the AOI bbox (EPSG:4326).
        import rasterio
        from pyproj import Transformer

        with rasterio.open(cog) as r:
            b = r.bounds
            tf = Transformer.from_crs(r.crs, "EPSG:4326", always_xy=True)
            lon0, lat0 = tf.transform(b.left, b.bottom)
            lon1, lat1 = tf.transform(b.right, b.top)
        assert _BBOX[0] - 0.05 <= lon0 <= _BBOX[2]
        assert _BBOX[1] - 0.05 <= lat0 <= _BBOX[3]
        assert _BBOX[0] <= lon1 <= _BBOX[2] + 0.05
        assert _BBOX[1] <= lat1 <= _BBOX[3] + 0.05
    finally:
        cog.unlink(missing_ok=True)


def test_read_face_coords_handles_fill_and_out_of_range(tmp_path: Path) -> None:
    """Defensive: fill / out-of-range / sub-start_index node slots are masked
    (NaN) and excluded from the centroid mean — never poison the result."""
    import xarray as xr

    node_x = np.array([0.0, 10.0, 10.0, 0.0], dtype="float64")
    node_y = np.array([0.0, 0.0, 10.0, 10.0], dtype="float64")
    # Two faces: a clean quad (nodes 1-4) and a triangle (nodes 1,2,3 + fill).
    conn = np.array(
        [[1.0, 2.0, 3.0, 4.0], [1.0, 2.0, 3.0, -999.0]], dtype="float64"
    )
    ds = xr.Dataset(
        {
            "mesh2d_node_x": xr.DataArray(node_x, dims=["nmesh2d_node"]),
            "mesh2d_node_y": xr.DataArray(node_y, dims=["nmesh2d_node"]),
            "mesh2d_face_nodes": xr.DataArray(
                conn,
                dims=["nmesh2d_face", "max_nmesh2d_face_nodes"],
                attrs={"start_index": 1},
            ),
        }
    )
    fx, fy = _read_face_coords(ds)
    assert fx.shape[0] == 2
    # Face 0 quad centroid = (5, 5); face 1 triangle centroid = mean of nodes 1-3.
    np.testing.assert_allclose(fx, [5.0, (0 + 10 + 10) / 3.0], atol=1e-9)
    np.testing.assert_allclose(fy, [5.0, (0 + 0 + 10) / 3.0], atol=1e-9)
