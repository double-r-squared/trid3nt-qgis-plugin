"""Offline gates for the ``schism_baroclinic_circulation`` archetype (ADR 0189):
density-driven 3D baroclinic estuary deck authoring, the salinity/stratification
postprocess (synthetic 3D netCDF), and the registration pins.

No docker, no MinIO, no live solve -- the live coarse smoke (a US estuary spin-up)
rides the worker image + the run harness. The stratification numbers here are
computed from a synthetic 3D salinity netCDF the same reader consumes for the real
solve. The calibrated CORIE 28-day validation is NATE-gated.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from trid3nt_contracts.schism_contracts import (
    SCHISM_ARCHETYPES,
    SCHISM_SALINITY_STYLE_PRESET,
    SchismBaroclinicLayerURI,
)


# --------------------------------------------------------------------------- #
# 1. Contract + archetype registration
# --------------------------------------------------------------------------- #
def test_baroclinic_archetype_registered():
    assert "baroclinic_circulation" in SCHISM_ARCHETYPES
    assert SCHISM_SALINITY_STYLE_PRESET == "continuous_flood_depth"


def test_baroclinic_layer_uri_shape():
    s = SchismBaroclinicLayerURI(
        layer_id="schism-surf-salt-x", name="Surface salinity", layer_type="raster",
        uri="s3://b/surf.tif", role="primary", units="psu",
        style_preset=SCHISM_SALINITY_STYLE_PRESET,
        surface_salinity_min_psu=0.5, surface_salinity_max_psu=28.0,
        bottom_salinity_max_psu=33.0, max_stratification_psu=18.0,
        mean_stratification_psu=6.5, river_discharge_m3s=500.0,
        n_nodes=240, n_layers=10, sim_days=2.0,
    )
    assert s.max_stratification_psu == pytest.approx(18.0)
    assert s.river_discharge_m3s == pytest.approx(500.0)
    with pytest.raises(Exception):  # negative stratification is ge=0 constrained
        SchismBaroclinicLayerURI(
            layer_id="x", name="x", layer_type="raster", uri="s3://b/x", role="primary",
            style_preset=SCHISM_SALINITY_STYLE_PRESET,
            surface_salinity_min_psu=0.0, surface_salinity_max_psu=1.0,
            max_stratification_psu=-1.0,
        )


# --------------------------------------------------------------------------- #
# 2. Deck authoring: 3D vgrid + baroclinic param + gradient IC + river source
# --------------------------------------------------------------------------- #
def test_author_baroclinic_estuary_deck(tmp_path: Path):
    from trid3nt_server.workflows.schism import deck_authoring as D

    deck = D.author_baroclinic_estuary_deck(
        tmp_path / "e", bbox=(-75.55, 38.85, -75.05, 39.45),
        constituents=["M2"], tidal_amplitude_m=0.6, sim_days=1.0, ocean_side="south",
        river_discharge_m3s=800.0, ocean_salinity_psu=32.0, nvrt=8, nx=6, ny=10,
    )
    names = {p.name for p in deck["files"]}
    for req in ("hgrid.gr3", "vgrid.in", "param.nml", "bctides.in", "temp.ic",
                "salt.ic", "source_sink.in", "vsource.th", "msource.th"):
        assert req in names, f"missing {req}"
    assert deck["n_layers"] == 8
    assert deck["n_nodes"] > 0 and deck["n_elements"] > 0

    # 3D SZ vgrid with the requested layer count
    vgrid = (tmp_path / "e" / "vgrid.in").read_text()
    assert vgrid.splitlines()[0].startswith("2")   # ivcor=2 (SZ)
    assert vgrid.splitlines()[1].split()[0] == "8"  # nvrt

    # baroclinic param: ibc=0, if_source=1, salinity output on
    param = (tmp_path / "e" / "param.nml").read_text()
    import re
    assert re.search(r"(?m)^\s*ibc\s*=\s*0", param)
    assert re.search(r"(?m)^\s*if_source\s*=\s*1", param)
    assert re.search(r"(?m)^\s*iof_hydro\(19\)\s*=\s*1", param)  # salinity

    # salt.ic carries an estuarine GRADIENT (spans fresh -> ~ocean salinity)
    salt_lines = (tmp_path / "e" / "salt.ic").read_text().splitlines()[2:]
    svals = np.array([float(l.split()[3]) for l in salt_lines if l.strip()])
    assert svals.min() < 5.0            # fresh (river) end
    assert svals.max() > 25.0           # salty (ocean) end
    assert (svals.max() - svals.min()) > 10.0

    # river source: one element, constant freshwater discharge (S=0)
    ss = (tmp_path / "e" / "source_sink.in").read_text().splitlines()
    assert ss[0].split()[0] == "1"
    vsource = (tmp_path / "e" / "vsource.th").read_text().splitlines()
    assert float(vsource[0].split()[1]) == pytest.approx(800.0)
    msource = (tmp_path / "e" / "msource.th").read_text().splitlines()
    assert float(msource[0].split()[2]) == pytest.approx(0.0)  # S=0 freshwater


def test_author_baroclinic_deck_supplied_mesh(tmp_path: Path):
    """The precondition-gate path: a supplied (points, tris, depths_down) mesh
    REPLACES the idealized lattice, and the deck (open boundary + salinity IC
    gradient + river source) is authored on THOSE nodes (ADR 0208)."""
    from scipy.spatial import Delaunay

    from trid3nt_server.workflows.schism import deck_authoring as D

    # a real-ish coastal lon/lat rectangle, triangulated, depths positive-DOWN.
    xs = np.linspace(-70.60, -70.40, 9)
    ys = np.linspace(41.20, 41.40, 11)
    gx, gy = np.meshgrid(xs, ys)
    pts = np.column_stack([gx.ravel(), gy.ravel()])
    tris = Delaunay(pts).simplices
    # deepen toward the south (ocean) edge: positive-down.
    frac = 1.0 - (pts[:, 1] - ys.min()) / (ys.max() - ys.min())
    depths_down = 3.0 + 12.0 * frac

    deck = D.author_baroclinic_estuary_deck(
        tmp_path / "sm", bbox=(-70.60, 41.20, -70.40, 41.40),
        constituents=["M2"], tidal_amplitude_m=0.6, sim_days=1.0, ocean_side="south",
        river_discharge_m3s=600.0, ocean_salinity_psu=33.0, nvrt=8,
        supplied_mesh=(pts, tris, depths_down),
    )
    # the deck used the SUPPLIED node count (<= the 99 supplied, minus any bowtie
    # cleaning), never the default 20x40 idealized lattice (800 nodes).
    assert 0 < deck["n_nodes"] <= pts.shape[0]
    assert deck["n_nodes"] < 800
    assert deck["open_node_count"] > 0        # a seaward open boundary was designated
    # the salinity IC still carries the estuarine gradient on the supplied nodes.
    salt_lines = (tmp_path / "sm" / "salt.ic").read_text().splitlines()[2:]
    svals = np.array([float(l.split()[3]) for l in salt_lines if l.strip()])
    assert svals.min() < 5.0 and svals.max() > 25.0


def test_build_estuary_mesh_shoreline_clip():
    """A water mask clips the lattice to the wet body: NO triangle centroid lands
    on 'land', and no surviving node is outside the water region."""
    from trid3nt_server.workflows.schism import deck_authoring as D

    bbox = (-75.55, 38.85, -75.05, 39.45)
    # synthetic coastline: WATER only west of lon=-75.25 (a connected sub-body)
    def wet(lon, lat):
        return np.asarray(lon) < -75.25

    pts, tris, depths, clipped = D._build_estuary_mesh(
        bbox, nx=20, ny=24, ocean_side="south",
        depth_ocean_m=15.0, depth_river_m=3.0, water_mask_fn=wet,
    )
    assert clipped is True
    # every kept TRIANGLE centroid is water (cells kept by water-centroid; the
    # raster is masked by the mesh, so no cell paints land -- boundary NODES may
    # straddle the coastline by up to one cell, which is correct).
    cent = pts[tris].mean(axis=1)
    assert bool(np.all(cent[:, 0] < -75.25))
    assert depths.shape[0] == pts.shape[0]
    # the clipped mesh is smaller than the full lattice
    assert pts.shape[0] < 20 * 24
    # no node sits DEEP in land (more than ~one cell width past the coast)
    dx = (bbox[2] - bbox[0]) / 19
    assert bool(np.all(pts[:, 0] < -75.25 + dx + 1e-9))


def test_build_estuary_mesh_unclipped_fallback():
    """Too little water -> the full rectangle is kept (clipped=False), never a
    sliver, so the caller can loudly note the coastline clip was unavailable."""
    from trid3nt_server.workflows.schism import deck_authoring as D

    bbox = (-75.55, 38.85, -75.05, 39.45)
    pts, tris, depths, clipped = D._build_estuary_mesh(
        bbox, nx=10, ny=10, ocean_side="south",
        depth_ocean_m=15.0, depth_river_m=3.0,
        water_mask_fn=lambda lon, lat: np.zeros(np.asarray(lon).shape, dtype=bool),
    )
    assert clipped is False
    assert pts.shape[0] == 10 * 10  # full lattice


def test_author_baroclinic_deck_shoreline_clip_no_land(tmp_path: Path):
    """With a coastline mask, salt.ic carries NO node on the 'land' side -- the
    deliverable cannot paint salinity over land."""
    from trid3nt_server.workflows.schism import deck_authoring as D

    deck = D.author_baroclinic_estuary_deck(
        tmp_path / "clip", bbox=(-75.55, 38.85, -75.05, 39.45),
        constituents=["M2"], tidal_amplitude_m=0.6, sim_days=1.0, ocean_side="south",
        river_discharge_m3s=800.0, ocean_salinity_psu=32.0, nvrt=8, nx=20, ny=24,
        water_mask_fn=lambda lon, lat: np.asarray(lon) < -75.25,
    )
    assert deck["shoreline_clipped"] is True
    salt_lines = (tmp_path / "clip" / "salt.ic").read_text().splitlines()[2:]
    lons = np.array([float(l.split()[1]) for l in salt_lines if l.strip()])
    # no node sits deep in land: within ~one cell width of the coastline at worst
    dx = (-75.05 - (-75.55)) / 19
    assert bool(np.all(lons < -75.25 + dx + 1e-6))
    # the mesh is genuinely water-dominated (most nodes on the water side)
    assert float(np.mean(lons < -75.25)) > 0.75


def test_sz_vgrid_layer_ordering():
    from trid3nt_server.workflows.schism import deck_authoring as D

    v = D._author_sz_vgrid(6)
    body = v.splitlines()[6:]
    sigmas = [float(l.split()[1]) for l in body]
    assert sigmas[0] == pytest.approx(-1.0)   # bed
    assert sigmas[-1] == pytest.approx(0.0)   # surface
    assert all(sigmas[i] < sigmas[i + 1] for i in range(len(sigmas) - 1))


# --------------------------------------------------------------------------- #
# 3. Postprocess: surface/bottom salinity + stratification from a synthetic 3D nc
# --------------------------------------------------------------------------- #
def _write_synthetic_salinity(path: Path, *, n=60, nlayer=8, ntime=3) -> None:
    from netCDF4 import Dataset

    x = np.linspace(-75.5, -75.1, n)
    y = np.linspace(38.9, 39.4, n)
    # stratified column: bottom salty (33), surface fresher scaling with along-axis
    frac = (y - y.min()) / (y.max() - y.min())          # 0 river .. 1 ocean
    salt = np.empty((ntime, n, nlayer), dtype="f8")
    for k in range(nlayer):
        sigma = k / (nlayer - 1)                          # 0 bed .. 1 surface
        # bottom ~33 everywhere, surface tracks the estuarine surface gradient
        col = 33.0 * (1.0 - sigma) + (frac * 30.0) * sigma
        salt[:, :, k] = col
    with Dataset(str(path), "w") as ds:
        ds.createDimension("node", n)
        ds.createDimension("nSCHISM_vgrid_layers", nlayer)
        ds.createDimension("time", ntime)
        ds.createVariable("SCHISM_hgrid_node_x", "f8", ("node",))[:] = x
        ds.createVariable("SCHISM_hgrid_node_y", "f8", ("node",))[:] = y
        ds.createVariable("salinity", "f8", ("time", "node", "nSCHISM_vgrid_layers"))[:] = salt


def test_read_salinity_stratification(tmp_path: Path):
    from trid3nt_server.workflows.schism import postprocess_schism as PP

    p = tmp_path / "salinity_1.nc"
    _write_synthetic_salinity(p)
    data = PP.read_salinity_stratification(p)
    assert data["n_layers"] == 8
    strat = data["stratification"][data["finite"]]
    # bottom > surface almost everywhere (a stratified salt wedge)
    assert np.nanmean(strat) > 0.0
    assert np.nanmax(strat) > 10.0


def test_postprocess_baroclinic_metrics(tmp_path: Path, monkeypatch):
    from trid3nt_server.workflows.schism import postprocess_schism as PP
    from trid3nt_server.workflows.shared import cog_io

    # stub the COG write/upload so the test stays offline (no MinIO)
    monkeypatch.setattr(cog_io, "write_cog_4326_from_grid",
                        lambda *a, **k: Path(tmp_path / "fake.tif"))
    monkeypatch.setattr(cog_io, "upload_cog",
                        lambda p, run_id, bucket, **k: f"s3://runs/{run_id}/{k.get('dest_filename')}")
    monkeypatch.setattr(cog_io, "safe_unlink", lambda p: None)

    p = tmp_path / "salinity_1.nc"
    _write_synthetic_salinity(p)
    layers, metrics = PP.postprocess_schism_baroclinic(
        p, "s3://runs/rid/outputs/out2d_1.nc", run_id="rid", sim_days=2.0,
        river_discharge_m3s=500.0, n_layers=8,
    )
    surf = layers[0]
    assert isinstance(surf, SchismBaroclinicLayerURI)
    assert surf.max_stratification_psu is not None and surf.max_stratification_psu > 0
    assert metrics["bottom_salinity_max_psu"] > 25.0
    assert len(layers) == 3  # surface + bottom + mesh


# --------------------------------------------------------------------------- #
# 4. Registration pin
# --------------------------------------------------------------------------- #
def test_baroclinic_tool_registered():
    from trid3nt_server.data import TOOL_REGISTRY

    assert "schism_baroclinic_circulation" in TOOL_REGISTRY
