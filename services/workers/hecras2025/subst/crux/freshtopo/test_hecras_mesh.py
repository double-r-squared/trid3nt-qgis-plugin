"""Offline tests for the standalone HEC-RAS RoG mesh authoring (hecras_mesh.py, ADR 0211).

No docker / no engine: these validate the PURE host surfaces -- the meshprobe spec the
driver reads, the local-frame reconstruction the consume path depends on, and the
realized-cell display (Voronoi of cell centers) that becomes the QGIS wireframe. The
container meshprobe realization + determinism are proven LIVE (byte-identical cell
centers over independent runs); here we lock the surfaces the server rides on.
"""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import numpy as np

from rog2025_pipeline import Rog2025Prep
from hecras_mesh import _meshprobe_spec, _voronoi_cells_lonlat_fgb


def _prep() -> Rog2025Prep:
    cs = 90.0
    return Rog2025Prep(
        local_dem="/x/local_dem.tif", nx=int(3000 / cs), ny=int(3000 / cs), cell_size=cs,
        width_m=3000.0, height_m=3000.0, outlet_edge="s", utm_epsg=32617,
        origin_x=276000.0, origin_y=3880000.0, elev_min_m=600.0, elev_max_m=700.0,
        valid_frac=1.0)


def test_meshprobe_spec_carries_parserog_fields():
    spec = _meshprobe_spec(_prep(), "/probe/rog_x/refine", "/probe/rog_x/meshprobe")
    # every field ParseRog(GetProperty) reads must be present, or the driver throws.
    for k in ("out_dir", "nx", "ny", "cell_size", "manning_n", "dt_s", "sim_seconds",
              "report_every", "outlet_edge", "outlet_slope", "outlet_stage",
              "outlet_bc", "diffusion", "precip_mm_hr", "refine_dir"):
        assert k in spec, k
    assert spec["refine_dir"] == "/probe/rog_x/refine"
    assert spec["nx"] == int(3000 / 90.0) and spec["cell_size"] == 90.0


def test_prep_json_reconstructs_frame_for_consume():
    """The consume path stores prep as a dict + rebuilds Rog2025Prep from it; the local
    frame (origin/size/epsg) must survive the round-trip exactly (the seeds live in it)."""
    prep = _prep()
    doc = asdict(prep)
    doc["local_dem"] = "local_dem.tif"          # relative, resolved on consume
    doc["channel_m_realized"] = 25.7            # extra field the consume path adds
    rebuilt = Rog2025Prep(**{k: doc[k] for k in Rog2025Prep.__dataclass_fields__ if k in doc})
    assert rebuilt.origin_x == prep.origin_x and rebuilt.origin_y == prep.origin_y
    assert rebuilt.utm_epsg == prep.utm_epsg
    assert rebuilt.width_m == prep.width_m and rebuilt.height_m == prep.height_m
    assert rebuilt.nx == prep.nx and rebuilt.cell_size == prep.cell_size


def test_voronoi_display_writes_cell_polygons(tmp_path: Path):
    """The realized-cell display = Voronoi of the meshprobe cell centers, clipped to the
    domain rectangle, reprojected to 4326 -- one polygon per interior cell center."""
    import geopandas as gpd

    prep = _prep()
    # a small blue-noise-ish scatter of cell centers in the local frame
    rng = np.random.default_rng(7)
    cc = rng.uniform(50.0, prep.width_m - 50.0, size=(120, 2))
    out = tmp_path / "cells.fgb"
    bbox = _voronoi_cells_lonlat_fgb(cc, prep, out)
    assert out.exists()
    gdf = gpd.read_file(out)
    assert len(gdf) > 80                        # ~one polygon per center (edge cells drop)
    assert "size_m" in gdf.columns and (gdf["size_m"] > 0).all()
    # bbox is lon/lat around the Coweeta UTM 17N test origin (western NC)
    assert -84.5 < bbox[0] < -82.5 and 34.0 < bbox[1] < 36.5
