"""SCHISM engine-landing test set (engine #12, ADR 0118).

Offline + deterministic: no docker, no S3. Covers the contract round-trip, the
mesh-emission row (layer_type="mesh" + crs_authid) through the live WS shape, the
gr3 + bathymetry bridge, deck-authoring determinism, the out2d postprocess read
vs a synthetic UGRID netCDF, the run_schism local-solver spec + classify_exit, and
the registry/solver registration pins.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from trid3nt_contracts import new_ulid
from trid3nt_contracts.schism_contracts import (
    SCHISM_CONSTITUENTS,
    SCHISM_ERROR_CODES,
    SCHISMRunArgs,
    SchismElevationLayerURI,
)
from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.collections import ProjectLayerSummary


# --------------------------------------------------------------------------- #
# 1. Contract round-trip
# --------------------------------------------------------------------------- #
def test_runargs_defaults_and_validation():
    a = SCHISMRunArgs()
    assert a.archetype == "tidal_hydro"
    assert a.mesh_source == "bundled_quarterannulus"
    assert a.constituents == ["M2"]
    b = SCHISMRunArgs(mesh_source="coastal_tin", constituents=["M2", "S2"],
                      tidal_amplitude_m=0.4, sim_days=4.0)
    assert b.constituents == ["M2", "S2"]
    with pytest.raises(Exception):
        SCHISMRunArgs(constituents=["ZZ"])
    with pytest.raises(Exception):
        SCHISMRunArgs(tidal_amplitude_m=99.0)


def test_elevation_layer_uri_shape():
    L = SchismElevationLayerURI(
        layer_id="schism-elev-x", name="n", layer_type="raster",
        uri="s3://b/k.tif", style_preset="continuous_flood_depth",
        elev_max_m=1.23, elev_min_m=-0.9, tidal_range_m=2.13, n_nodes=100,
        sim_days=5.0, mesh_source="coastal_tin", constituents=["M2"],
        crs_authid="EPSG:4326", analytical_rmse_m=0.0155,
    )
    d = L.model_dump(mode="json")
    assert d["layer_type"] == "raster"
    assert d["tidal_range_m"] == pytest.approx(2.13)
    assert d["analytical_rmse_m"] == pytest.approx(0.0155)
    assert d["crs_authid"] == "EPSG:4326"


def test_error_codes_present():
    for c in ("SCHISM_SOLVE_FAILED", "SCHISM_MESH_INVALID", "SCHISM_INPUT_INVALID"):
        assert c in SCHISM_ERROR_CODES


# --------------------------------------------------------------------------- #
# 2. The mesh-emission row (ADR 0118) through the live WS shape
# --------------------------------------------------------------------------- #
def test_layer_uri_accepts_mesh_type_and_crs():
    m = LayerURI(layer_id="m", name="SCHISM mesh", layer_type="mesh",
                 uri="s3://b/out2d.nc", style_preset="mesh_grid",
                 role="context", crs_authid="EPSG:4326")
    d = m.model_dump(mode="json")
    assert d["layer_type"] == "mesh"
    assert d["crs_authid"] == "EPSG:4326"


def test_project_layer_summary_carries_mesh_crs():
    s = ProjectLayerSummary(
        layer_id="m", name="mesh", layer_type="mesh", uri="s3://b/out2d.nc",
        style_preset="mesh_grid", visible=True, role="context", temporal=False,
        crs_authid="EPSG:4326",
    )
    row = s.model_dump(mode="json")  # the exact WS row the plugin reads as event.raw
    assert row["layer_type"] == "mesh"
    assert row["crs_authid"] == "EPSG:4326"


@pytest.mark.asyncio
async def test_add_loaded_layer_threads_mesh_row_end_to_end():
    """A mesh LayerURI -> add_loaded_layer -> session-state loaded_layers row
    carries layer_type='mesh' + crs_authid (the WS shape the plugin _add_mesh reads)."""
    from trid3nt_server.emission.pipeline_emitter import PipelineEmitter

    frames: list[dict] = []

    async def sink(text: str) -> None:
        frames.append(json.loads(text))

    em = PipelineEmitter(session_id=new_ulid(), sink=sink)
    mesh = LayerURI(layer_id="schism-mesh-1", name="SCHISM mesh (500 nodes)",
                    layer_type="mesh", uri="s3://b/out2d_1.nc", style_preset="mesh_grid",
                    role="context", crs_authid="EPSG:4326")
    await em.add_loaded_layer(mesh)
    sess = [f for f in frames if f["type"] == "session-state"]
    assert sess, "expected a session-state frame after add_loaded_layer"
    layers = sess[-1]["payload"]["loaded_layers"]
    row = next(r for r in layers if r["layer_id"] == "schism-mesh-1")
    assert row["layer_type"] == "mesh"
    assert row["crs_authid"] == "EPSG:4326"


# --------------------------------------------------------------------------- #
# 3. gr3 + bathymetry bridge
# --------------------------------------------------------------------------- #
def _square_mesh(nx=6, ny=6, x0=-95.0, y0=29.0, dx=0.02):
    xs, ys = np.meshgrid(
        x0 + np.arange(nx) * dx, y0 + np.arange(ny) * dx
    )
    pts = np.column_stack([xs.ravel(), ys.ravel()])
    cells = []
    for j in range(ny - 1):
        for i in range(nx - 1):
            a = j * nx + i
            cells.append([a, a + 1, a + nx + 1])
            cells.append([a, a + nx + 1, a + nx])
    return pts, np.asarray(cells, dtype=np.int64)


def test_bathymetry_sampling_and_gr3_bridge(tmp_path: Path):
    from trid3nt_server.workflows.schism import deck_authoring as D
    import rasterio
    from rasterio.transform import from_bounds

    pts, cells = _square_mesh()
    # a synthetic DEM: elevation ramps from -5 m (deep) to +2 m (land) N->S
    dem = tmp_path / "dem.tif"
    W = H = 32
    grid = np.linspace(2.0, -5.0, H)[:, None].repeat(W, axis=1).astype("float32")
    bbox = [pts[:, 0].min() - 0.01, pts[:, 1].min() - 0.01,
            pts[:, 0].max() + 0.01, pts[:, 1].max() + 0.01]
    tr = from_bounds(bbox[0], bbox[1], bbox[2], bbox[3], W, H)
    with rasterio.open(str(dem), "w", driver="GTiff", height=H, width=W, count=1,
                       dtype="float32", transform=tr, crs="EPSG:4326", nodata=-9999.0) as ds:
        ds.write(grid, 1)

    depths = D.sample_bathymetry_on_nodes(pts, dem, min_wet_depth_m=0.5)
    assert depths.shape[0] == pts.shape[0]
    assert np.all(depths >= 0.5)  # land nodes clamped to the wet floor
    assert depths.max() > 2.0  # the deep (-5 m elev) end -> +5 m depth

    bridge = D.load_gr3_bridge()
    gr3 = bridge.tin_to_hgrid(pts, cells, depth=depths, open_boundary_side="south")
    header = gr3.splitlines()[1].split()
    n_elem, n_nodes = int(header[0]), int(header[1])
    assert n_nodes == pts.shape[0]
    assert "open boundary nodes" in gr3
    assert "land boundary" in gr3.lower()


# --------------------------------------------------------------------------- #
# 4. Deck-authoring determinism
# --------------------------------------------------------------------------- #
def test_author_coastal_tin_deck_deterministic(tmp_path: Path):
    from trid3nt_server.workflows.schism import deck_authoring as D

    pts, cells = _square_mesh()
    depths = np.full(pts.shape[0], 8.0)

    def build(dst):
        return D.author_coastal_tin_deck(
            dst, points=pts, cells=cells, depths=depths, constituents=["M2", "S2"],
            tidal_amplitude_m=0.4, sim_days=4.0, open_boundary_side="south",
        )

    r1 = build(tmp_path / "a")
    r2 = build(tmp_path / "b")
    assert r1["n_nodes"] == r2["n_nodes"]
    for name in ("hgrid.gr3", "bctides.in", "param.nml", "vgrid.in", "station.in"):
        t1 = (tmp_path / "a" / name).read_text()
        t2 = (tmp_path / "b" / name).read_text()
        assert t1 == t2, f"{name} not deterministic"
    # bctides carries the two constituents + the open-node amplitude block
    bct = (tmp_path / "a" / "bctides.in").read_text()
    assert "M2" in bct and "S2" in bct
    assert "0.400000" in bct  # the uniform amplitude


def test_author_coastal_tin_deck_supplied_mesh(tmp_path: Path):
    """ADR 0212 precondition gate: a supplied (points, tris, depths_down) mesh
    REPLACES the internal TIN and the tidal deck (open boundary + bctides) is
    authored on THOSE nodes -- no points/cells/depths needed."""
    from trid3nt_server.workflows.schism import deck_authoring as D

    pts, cells = _square_mesh(nx=8, ny=8)
    depths_down = np.full(pts.shape[0], 8.0)

    deck = D.author_coastal_tin_deck(
        tmp_path / "sm", supplied_mesh=(pts, cells, depths_down),
        constituents=["M2"], tidal_amplitude_m=0.4, sim_days=4.0,
        open_boundary_side="south",
    )
    names = {p.name for p in deck["files"]}
    for req in ("hgrid.gr3", "vgrid.in", "param.nml", "bctides.in", "drag.gr3", "station.in"):
        assert req in names, f"missing {req}"
    assert 0 < deck["n_nodes"] <= pts.shape[0]
    assert deck["open_node_count"] > 0  # a seaward open boundary was designated
    # bctides carries the constituent + the uniform amplitude at the open nodes.
    bct = (tmp_path / "sm" / "bctides.in").read_text()
    assert "M2" in bct and "0.400000" in bct


def test_author_coastal_tin_deck_needs_geometry(tmp_path: Path):
    """Neither points/cells/depths nor supplied_mesh -> a typed honest error."""
    from trid3nt_server.workflows.schism import deck_authoring as D

    with pytest.raises(D.SchismDeckError):
        D.author_coastal_tin_deck(
            tmp_path / "x", constituents=["M2"], tidal_amplitude_m=0.4,
            sim_days=4.0, open_boundary_side="south")


def _schism_mesh_artifact(*, compatible: bool):
    """A MeshArtifact that is SCHISM-compatible (open boundary) or not (closed)."""
    from trid3nt_server.workflows.mesh.artifact import MeshArtifact

    obi = ({"open_boundary_side": "south", "open_node_count": 12}
           if compatible else {})
    return MeshArtifact(
        mesh_id="m-tin-1", name="Galveston coastal mesh", mode="coastal_water_edge",
        display_uri="s3://runs/m/mesh.2dm", slf_uri="s3://runs/m/mesh.slf",
        utm_epsg=32615, crs_authid="EPSG:32615", has_bathymetry=True,
        node_count=340, element_count=600, bbox=(-95.0, 29.0, -94.6, 29.4),
        engine_compat=(["telemac", "schism"] if compatible else ["telemac"]),
        gr3_uri=("s3://runs/m/hgrid.gr3" if compatible else None),
        open_boundary_info=obi, case_id="case-x")


@pytest.mark.asyncio
async def test_tidal_gate_decision_compatible_auto(monkeypatch):
    """A SCHISM-compatible case mesh -> gate accepts in AUTO (headless) mode."""
    from trid3nt_server.workflows.mesh import precondition_gate as G

    art = _schism_mesh_artifact(compatible=True)
    monkeypatch.setattr(G, "find_case_mesh_artifacts", lambda **k: [art])
    decision = await G.gate_supplied_mesh(
        tool_name="schism_tidal_hydro", engine="schism", input_mode="auto",
        case_id="case-x")
    assert decision.use is True
    assert decision.artifact is art
    assert (art.open_boundary_info or {}).get("open_boundary_side") == "south"


@pytest.mark.asyncio
async def test_tidal_gate_decision_incompatible_loud_skip(monkeypatch):
    """A case mesh with no open boundary -> NOT gated; loud-skip note, author fresh."""
    from trid3nt_server.workflows.mesh import precondition_gate as G

    art = _schism_mesh_artifact(compatible=False)
    monkeypatch.setattr(G, "find_case_mesh_artifacts", lambda **k: [art])
    decision = await G.gate_supplied_mesh(
        tool_name="schism_tidal_hydro", engine="schism", input_mode="auto",
        case_id="case-x")
    assert decision.use is False
    assert decision.artifact is None
    assert decision.note and "not compatible" in decision.note


@pytest.mark.asyncio
async def test_tidal_gate_decision_absent(monkeypatch):
    """No case mesh -> None decision (author the internal TIN as before)."""
    from trid3nt_server.workflows.mesh import precondition_gate as G

    monkeypatch.setattr(G, "find_case_mesh_artifacts", lambda **k: [])
    decision = await G.gate_supplied_mesh(
        tool_name="schism_tidal_hydro", engine="schism", input_mode="auto",
        case_id="case-x")
    assert decision.use is False
    assert decision.artifact is None
    assert decision.note is None


def test_tidal_parse_hgrid_roundtrip(tmp_path: Path):
    """The composer's gr3 parser round-trips tin_to_hgrid nodes/cells/depths."""
    from trid3nt_server.workflows.schism import deck_authoring as D
    from trid3nt_server.workflows.schism.tidal_hydro.tidal_hydro import (
        _parse_hgrid_nodes_cells,
    )

    pts, cells = _square_mesh(nx=7, ny=7)
    depths = np.full(pts.shape[0], 6.0)
    bridge = D.load_gr3_bridge()
    gr3 = bridge.tin_to_hgrid(pts, cells, depth=depths, open_boundary_side="south")
    p2, t2, d2 = _parse_hgrid_nodes_cells(gr3)
    assert p2.shape[0] == pts.shape[0]
    assert t2.shape[1] == 3 and int(t2.min()) == 0  # 0-based triangles
    assert np.allclose(d2, 6.0, atol=1e-6)


def test_bctides_no_open_boundary_raises():
    from trid3nt_server.workflows.schism import deck_authoring as D

    with pytest.raises(D.SchismDeckError):
        D._author_bctides(0, ["M2"], 0.5)


def test_param_nml_substitution_one_stack():
    from trid3nt_server.workflows.schism import deck_authoring as D

    qa = D.quarterannulus_fixture_dir()
    text = D._substitute_param_nml((qa / "param.nml").read_text(), sim_days=3.0, dt_s=120.0)
    # rnday -> 3, dt -> 120., and ihfskip = ceil(3*86400/120) = 2160 (one stack)
    assert "rnday = 3" in text
    assert "dt = 120." in text
    assert "ihfskip = 2160" in text


# --------------------------------------------------------------------------- #
# 5. Postprocess: read a synthetic out2d UGRID
# --------------------------------------------------------------------------- #
def test_read_out2d_elevation_geographic(tmp_path: Path):
    from trid3nt_server.workflows.schism import postprocess_schism as PP
    from netCDF4 import Dataset

    nc = tmp_path / "out2d_1.nc"
    N, T = 25, 6
    xs = np.linspace(-95.0, -94.9, N)
    ys = np.linspace(29.0, 29.1, N)
    # a tidal elevation swinging +/-0.5 m in time, node-varying
    tvals = np.linspace(0, 2 * np.pi, T)
    elev = 0.5 * np.sin(tvals)[:, None] + 0.01 * np.arange(N)[None, :]
    with Dataset(str(nc), "w") as ds:
        ds.createDimension("node", N)
        ds.createDimension("time", T)
        ds.createVariable("SCHISM_hgrid_node_x", "f8", ("node",))[:] = xs
        ds.createVariable("SCHISM_hgrid_node_y", "f8", ("node",))[:] = ys
        ds.createVariable("elevation", "f8", ("time", "node"))[:] = elev
    out = PP.read_out2d_elevation(nc)
    assert out["is_geographic"] is True
    assert out["n_nodes"] == N
    assert out["n_times"] == T
    assert out["elev_max"].max() == pytest.approx(elev.max(), abs=1e-6)
    assert out["elev_min"].min() == pytest.approx(elev.min(), abs=1e-6)


def test_read_out2d_empty_raises(tmp_path: Path):
    from trid3nt_server.workflows.schism import postprocess_schism as PP
    from netCDF4 import Dataset

    nc = tmp_path / "bad.nc"
    with Dataset(str(nc), "w") as ds:
        ds.createDimension("node", 3)
        ds.createVariable("SCHISM_hgrid_node_x", "f8", ("node",))[:] = [0, 1, 2]
    with pytest.raises(PP.PostprocessSchismError):
        PP.read_out2d_elevation(nc)


def test_verify_against_analytical(tmp_path: Path):
    from trid3nt_server.workflows.schism import postprocess_schism as PP

    # staout: time[s], elev[m] -- a 5-day M2-ish signal; analytical = near-identical
    t = np.linspace(0, 5 * 86400, 400)
    z = 0.44 * np.sin(2 * np.pi * t / (12.42 * 3600))
    staout = tmp_path / "staout_1"
    np.savetxt(staout, np.column_stack([t, z]))
    ana = tmp_path / "ana.dat"
    np.savetxt(ana, np.column_stack([t / 86400, z * 1.001]))  # tiny offset
    v = PP.verify_against_analytical(staout, ana, spinup_days=3.0)
    assert v is not None
    assert v["rmse_m"] < 0.03
    assert v["correlation"] > 0.99


# --------------------------------------------------------------------------- #
# 6. run_schism local-solver spec + classify_exit
# --------------------------------------------------------------------------- #
def test_schism_local_spec_and_classify_exit(tmp_path: Path):
    from trid3nt_server.workflows.schism.run_schism import (
        schism_local_spec, _classify_exit, SCHISM_SOLVER_NAME,
    )

    spec = schism_local_spec()
    assert spec.solver == SCHISM_SOLVER_NAME
    argv = spec.build_argv("run1", tmp_path, [])
    assert argv[0] == "docker" and "-v" in argv and str(tmp_path) + ":/data" in argv

    # ok metrics -> ok
    (tmp_path / "schism_metrics.json").write_text(json.dumps({"status": "ok", "wall_s": 2.0}))
    status, code, err, extra = _classify_exit(tmp_path, 0)
    assert status == "ok" and err is None and extra.get("wall_s") == 2.0
    # incomplete metrics -> error even on exit 0
    (tmp_path / "schism_metrics.json").write_text(
        json.dumps({"status": "error", "error_code": "SCHISM_RUN_INCOMPLETE"})
    )
    status, code, err, extra = _classify_exit(tmp_path, 0)
    assert status == "error" and code != 0


# --------------------------------------------------------------------------- #
# 7. Registration pins
# --------------------------------------------------------------------------- #
def test_registered_and_solver_wired():
    from trid3nt_server.data import TOOL_REGISTRY
    import trid3nt_server.workflows  # noqa: F401 -- trigger solver reg
    from trid3nt_server.data.simulation.solver.solver import (
        SOLVER_WORKFLOW_REGISTRY, LOCAL_SOLVER_SPEC_REGISTRY,
    )

    assert "schism_tidal_hydro" in TOOL_REGISTRY
    assert SOLVER_WORKFLOW_REGISTRY.get("schism_tidal_hydro") == "local-docker"
    assert "schism_tidal_hydro" in LOCAL_SOLVER_SPEC_REGISTRY
    md = TOOL_REGISTRY["schism_tidal_hydro"].metadata
    assert md.engine == "schism" and md.tier == "template"
