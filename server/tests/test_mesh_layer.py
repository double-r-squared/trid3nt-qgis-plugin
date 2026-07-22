"""Tests for the SWMM computational-mesh -> mesh_grid vector layer (task #156).

Covers the PURE geometry function ``mesh_cells_to_feature_collection`` (no
swmm-api / no DEM) and ``make_swmm_mesh_layer_uri`` (via a tiny fake build +
monkeypatched ``_active_cells_from_deck``). No live deps.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass

import pytest

from trid3nt_server.workflows.mesh_layer import (
    make_sfincs_mesh_layer_uri,
    make_swmm_mesh_layer_uri,
    mesh_cells_to_feature_collection,
)

# A simple UTM-like transform: origin x=500000, y=4000000, pixel 10 m, north-up.
# transform = [a, b, c, d, e, f] mapping (col, row) -> (x, y); e (row scale) is
# negative because row 0 is north.
UTM_TRANSFORM = [10.0, 0.0, 500000.0, 0.0, -10.0, 4000000.0]
UTM_CRS = "EPSG:32617"  # UTM zone 17N


@dataclass
class _FakeBuild:
    """Minimal stand-in for ``swmm_mesh_builder.BuildResult`` for unit tests."""

    transform: list
    crs: str
    resolution_m: float
    grid_shape: tuple
    inp_path: str = "/nonexistent/deck.inp"


def test_full_2x2_grid_all_active() -> None:
    active = [(0, 0), (0, 1), (1, 0), (1, 1)]
    fc, meta = mesh_cells_to_feature_collection(
        active,
        UTM_TRANSFORM,
        UTM_CRS,
        resolution_m=10.0,
        grid_shape=(2, 2),
    )

    assert meta["decimated"] is False
    assert meta["block"] == 1
    assert meta["n_active"] == 4
    assert meta["n_cells"] == 4
    assert meta["effective_resolution_m"] == 10.0

    assert fc["type"] == "FeatureCollection"
    assert len(fc["features"]) == 4

    for feat in fc["features"]:
        assert feat["type"] == "Feature"
        assert feat["geometry"]["type"] == "Polygon"
        ring = feat["geometry"]["coordinates"][0]
        # Closed ring: 5 coords, first == last.
        assert len(ring) == 5
        assert ring[0] == ring[-1]
        for lon, lat in ring:
            assert math.isfinite(lon) and math.isfinite(lat)
            # UTM 17N -> western hemisphere: lon in [-180, 0], lat 0..90.
            assert -180.0 <= lon <= 0.0
            assert 0.0 <= lat <= 90.0

        props = feat["properties"]
        assert props["cell_size_m"] == 10.0
        assert props["resolution_label"] == "10 m"
        assert "row" in props and "col" in props
        # Not decimated -> no block / decimated props.
        assert "block" not in props
        assert "decimated" not in props


def test_decimation_caps_feature_count() -> None:
    # 300x300 grid, ALL cells active = 90000 > cap 6000 -> must aggregate.
    active = [(r, c) for r in range(300) for c in range(300)]
    fc, meta = mesh_cells_to_feature_collection(
        active,
        UTM_TRANSFORM,
        UTM_CRS,
        resolution_m=10.0,
        grid_shape=(300, 300),
        cap=6000,
    )

    assert meta["decimated"] is True
    assert meta["block"] >= 2
    assert meta["n_active"] == 90000
    assert meta["n_cells"] <= 6000
    assert len(fc["features"]) <= 6000
    assert len(fc["features"]) == meta["n_cells"]
    assert meta["effective_resolution_m"] == 10.0 * meta["block"]

    block = meta["block"]
    feat = fc["features"][0]
    props = feat["properties"]
    assert props["decimated"] is True
    assert props["block"] == block
    assert props["cell_size_m"] == round(10.0 * block, 2)
    assert props["resolution_label"] == f"{10.0 * block:.0f} m"


def test_empty_active_cells() -> None:
    fc, meta = mesh_cells_to_feature_collection(
        [],
        UTM_TRANSFORM,
        UTM_CRS,
        resolution_m=10.0,
        grid_shape=(2, 2),
    )
    assert meta["n_cells"] == 0
    assert meta["decimated"] is False
    assert fc["features"] == []


class _FakeS3:
    """Captures put_object calls so the mesh upload can be asserted in-test."""

    def __init__(self) -> None:
        self.puts: list[dict] = []

    def put_object(self, **kw):  # noqa: ANN003
        body = kw.get("Body")
        data = body.read() if hasattr(body, "read") else body
        self.puts.append(
            {"Bucket": kw["Bucket"], "Key": kw["Key"], "Body": data}
        )
        return {}


def test_make_layer_uri_uploads_to_s3_durable(monkeypatch) -> None:
    """DURABILITY FIX: make_swmm_mesh_layer_uri UPLOADS mesh.geojson to the runs
    bucket and returns an s3:// uri (NOT a local /tmp path that the deck-cleanup
    deletes). The uri is re-readable by the emitter on every reconnect/re-emit.
    """
    from trid3nt_server.tools.simulation import solver as solver_mod

    build = _FakeBuild(
        transform=UTM_TRANSFORM,
        crs=UTM_CRS,
        resolution_m=10.0,
        grid_shape=(2, 2),
    )
    # Patch the deck reader at the seam used by swmm_mesh_to_geojson.
    monkeypatch.setattr(
        "trid3nt_server.workflows.swmm_mesh_builder._active_cells_from_deck",
        lambda b: [(0, 0), (0, 1), (1, 0), (1, 1)],
    )

    fake_s3 = _FakeS3()
    monkeypatch.setenv("TRID3NT_RUNS_BUCKET", "test-runs-bucket")
    solver_mod.set_s3_client(fake_s3)
    try:
        layer = make_swmm_mesh_layer_uri(build, run_id="run-abc")
    finally:
        solver_mod.set_s3_client(None)

    assert layer is not None
    assert layer.layer_type == "vector"
    assert layer.style_preset == "mesh_grid"
    assert layer.role == "context"
    assert layer.bbox is None
    assert layer.layer_id == "swmm-mesh-run-abc"

    # The uri is a DURABLE s3:// path under the runs bucket / per-run prefix -
    # NOT a local /tmp deck-staging path that deck cleanup would delete.
    assert layer.uri == "s3://test-runs-bucket/run-abc/mesh.geojson"
    assert layer.uri.startswith("s3://")
    assert "/tmp/" not in layer.uri
    assert not layer.uri.startswith("file://")

    # Exactly one put_object of the FeatureCollection to the conventional key.
    assert len(fake_s3.puts) == 1
    put = fake_s3.puts[0]
    assert put["Bucket"] == "test-runs-bucket"
    assert put["Key"] == "run-abc/mesh.geojson"
    body = put["Body"]
    parsed = json.loads(body.decode("utf-8") if isinstance(body, bytes) else body)
    assert parsed["type"] == "FeatureCollection"
    assert len(parsed["features"]) == 4


def test_make_layer_uri_runs_bucket_override(monkeypatch) -> None:
    """An explicit runs_bucket arg overrides the solver default for the s3 key."""
    from trid3nt_server.tools.simulation import solver as solver_mod

    build = _FakeBuild(
        transform=UTM_TRANSFORM, crs=UTM_CRS, resolution_m=10.0, grid_shape=(2, 2)
    )
    monkeypatch.setattr(
        "trid3nt_server.workflows.swmm_mesh_builder._active_cells_from_deck",
        lambda b: [(0, 0), (0, 1), (1, 0), (1, 1)],
    )
    fake_s3 = _FakeS3()
    solver_mod.set_s3_client(fake_s3)
    try:
        layer = make_swmm_mesh_layer_uri(
            build, run_id="run-ov", runs_bucket="explicit-bucket"
        )
    finally:
        solver_mod.set_s3_client(None)

    assert layer is not None
    assert layer.uri == "s3://explicit-bucket/run-ov/mesh.geojson"
    assert fake_s3.puts[0]["Bucket"] == "explicit-bucket"


def test_make_layer_uri_returns_none_on_upload_failure(monkeypatch) -> None:
    """BEST-EFFORT: an S3 put failure returns None (mesh simply absent) - it must
    NEVER raise / break the solve, and must NOT return a local /tmp fallback."""
    from trid3nt_server.tools.simulation import solver as solver_mod

    build = _FakeBuild(
        transform=UTM_TRANSFORM, crs=UTM_CRS, resolution_m=10.0, grid_shape=(2, 2)
    )
    monkeypatch.setattr(
        "trid3nt_server.workflows.swmm_mesh_builder._active_cells_from_deck",
        lambda b: [(0, 0), (0, 1), (1, 0), (1, 1)],
    )

    class _BoomS3:
        def put_object(self, **kw):  # noqa: ANN003
            raise RuntimeError("S3 put boom (no creds / bad bucket)")

    monkeypatch.setenv("TRID3NT_RUNS_BUCKET", "test-runs-bucket")
    solver_mod.set_s3_client(_BoomS3())
    try:
        layer = make_swmm_mesh_layer_uri(build, run_id="run-boom")
    finally:
        solver_mod.set_s3_client(None)

    assert layer is None


def test_make_layer_uri_zero_features_returns_none(monkeypatch) -> None:
    """Zero active cells -> None, and NO S3 upload is attempted."""
    from trid3nt_server.tools.simulation import solver as solver_mod

    build = _FakeBuild(
        transform=UTM_TRANSFORM,
        crs=UTM_CRS,
        resolution_m=10.0,
        grid_shape=(2, 2),
    )
    monkeypatch.setattr(
        "trid3nt_server.workflows.swmm_mesh_builder._active_cells_from_deck",
        lambda b: [],
    )

    fake_s3 = _FakeS3()
    solver_mod.set_s3_client(fake_s3)
    try:
        layer = make_swmm_mesh_layer_uri(build, run_id="run-empty")
    finally:
        solver_mod.set_s3_client(None)

    assert layer is None
    # Nothing to render -> no upload attempted.
    assert fake_s3.puts == []


# --------------------------------------------------------------------------- #
# SFINCS quadtree mesh (task #160): THIN constructor over an already-built,
# already-EPSG:4326 mesh.geojson the worker wrote. No geometry build / reproject
# / file write here - just a LayerURI over the worker's s3:// output.
# --------------------------------------------------------------------------- #


def test_make_sfincs_mesh_layer_uri_basic() -> None:
    uri = "s3://my-runs/run-xyz/mesh.geojson"
    layer = make_sfincs_mesh_layer_uri(uri, run_id="run-xyz")

    assert layer is not None
    assert layer.layer_type == "vector"
    assert layer.style_preset == "mesh_grid"
    assert layer.role == "context"
    assert layer.bbox is None
    assert layer.layer_id == "sfincs-mesh-run-xyz"
    assert layer.uri == uri
    # No n_cells -> plain quadtree name (no cell count).
    assert layer.name == "Computational mesh (quadtree)"


def test_make_sfincs_mesh_layer_uri_blank_returns_none() -> None:
    assert make_sfincs_mesh_layer_uri("", run_id="run-blank") is None
    assert make_sfincs_mesh_layer_uri("   ", run_id="run-ws") is None
    assert make_sfincs_mesh_layer_uri(None, run_id="run-none") is None  # type: ignore[arg-type]


def test_make_sfincs_mesh_layer_uri_n_cells_names_it() -> None:
    uri = "s3://my-runs/run-q/mesh.geojson"
    layer = make_sfincs_mesh_layer_uri(uri, run_id="run-q", n_cells=12345)

    assert layer is not None
    assert layer.name == "Computational mesh (quadtree, 12345 cells)"
    assert layer.uri == uri
    assert layer.layer_id == "sfincs-mesh-run-q"
