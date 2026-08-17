"""Offline tests for the graded-mesh input authoring (rog_refine.py).

No docker / no engine: these validate the host-side seed + breakline generation and
the invariant that makes the HEC mesh accept the seeds -- every INTERIOR Voronoi
(Delaunay) cell has <= 8 neighbours (HEC hard-rejects >8-sided cells). Self-contained
synthetic fixtures (a UTM box catchment + a diagonal channel), so the run is fast and
carries no external-data dependency.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from rog2025_pipeline import Rog2025Prep
from rog_refine import build_refined_inputs, RefineConfig

_EPSG = 32617
_OX, _OY = 276000.0, 3880000.0        # local-frame origin in the metric CRS
_W = _H = 3000.0                        # 3 km square test domain


def _prep() -> Rog2025Prep:
    cs = 60.0
    return Rog2025Prep(
        local_dem="", nx=int(_W / cs), ny=int(_H / cs), cell_size=cs,
        width_m=_W, height_m=_H, outlet_edge="s", utm_epsg=_EPSG,
        origin_x=_OX, origin_y=_OY, elev_min_m=600.0, elev_max_m=700.0, valid_frac=1.0)


def _fixtures(tmp: Path):
    """Write a box catchment + a diagonal channel as 4326 GeoJSON (geopandas reads it)."""
    from pyproj import Transformer
    inv = Transformer.from_crs(f"EPSG:{_EPSG}", "EPSG:4326", always_xy=True).transform

    def ll(ux, uy):
        lon, lat = inv(ux, uy)
        return [lon, lat]

    # catchment: an inset box of the domain
    b = [(_OX + 150, _OY + 150), (_OX + _W - 150, _OY + 150),
         (_OX + _W - 150, _OY + _H - 150), (_OX + 150, _OY + _H - 150), (_OX + 150, _OY + 150)]
    catch = {"type": "Feature", "properties": {},
             "geometry": {"type": "Polygon", "coordinates": [[ll(x, y) for x, y in b]]}}
    cpath = tmp / "catchment.geojson"
    cpath.write_text(json.dumps({"type": "FeatureCollection", "features": [catch]}))

    # channel: a diagonal + a branch (so there is a junction), as LineStrings
    line1 = [ll(_OX + 300, _OY + 300), ll(_OX + 1500, _OY + 1500), ll(_OX + 2600, _OY + 2600)]
    line2 = [ll(_OX + 1500, _OY + 1500), ll(_OX + 2500, _OY + 700)]
    fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {}, "geometry": {"type": "LineString", "coordinates": line1}},
        {"type": "Feature", "properties": {}, "geometry": {"type": "LineString", "coordinates": line2}},
    ]}
    fpath = tmp / "flowlines.geojson"
    fpath.write_text(json.dumps(fc))
    return str(cpath), str(fpath)


def _interior_max_degree(seeds: np.ndarray, W: float, H: float, margin: float) -> int:
    from scipy.spatial import Delaunay
    tri = Delaunay(seeds)
    indptr, indices = tri.vertex_neighbor_vertices
    interior = (seeds[:, 0] > margin) & (seeds[:, 0] < W - margin) & \
               (seeds[:, 1] > margin) & (seeds[:, 1] < H - margin)
    return max((indptr[i + 1] - indptr[i]) for i in np.nonzero(interior)[0])


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("rogref")
    catch, fl = _fixtures(tmp)
    cfg = RefineConfig(background_m=60.0, channel_m=18.0)
    r = build_refined_inputs(_prep(), catch, fl, tmp, cfg)
    seeds = np.frombuffer(Path(r.seeds_path).read_bytes(), dtype="<f8").reshape(-1, 2)
    return r, seeds


def test_seeds_written_and_readable(built):
    r, seeds = built
    assert seeds.shape[0] == r.n_seeds > 200
    assert Path(r.seeds_path).stat().st_size == r.n_seeds * 16      # 2 f64 / point


def test_seeds_strictly_inside_domain(built):
    _, seeds = built
    assert seeds[:, 0].min() > 0 and seeds[:, 0].max() < _W
    assert seeds[:, 1].min() > 0 and seeds[:, 1].max() < _H


def test_grading_is_bimodal_coarse_and_fine(built):
    """The realized spacing must carry BOTH a fine channel population and a coarse
    background one -- the paper's dynamic resolution, not a uniform mesh."""
    r, seeds = built
    from scipy.spatial import cKDTree
    nn = cKDTree(seeds).query(seeds, k=2)[0][:, 1]
    assert (nn < 30).sum() >= 20, "no fine (channel) cells"
    assert (nn > 45).sum() >= 20, "no coarse (background) cells"
    assert r.size_p5 < 30 < r.size_p95                              # spread across scales


def test_crowding_relief_bounds_interior_degree(built):
    """The crowding-relief pass keeps the interior Voronoi/Delaunay degree bounded on this
    gentle fixture (HEC rejects >8-sided cells; on this domain the relieved cloud stays
    within the cap -- on steeper real transitions HEC's own face-collapse + the driver's
    seed-drop backstop carry the residual, verified in-engine)."""
    _, seeds = built
    assert _interior_max_degree(seeds, _W, _H, margin=60.0) <= 8


def test_breaklines_are_local_polylines(built):
    r, _ = built
    bl = json.loads(Path(r.breaklines_path).read_text())
    assert r.n_breaklines == len(bl) >= 1
    for pl in bl:
        assert len(pl) >= 2
        for x, y in pl:
            assert 0.0 <= x <= _W and 0.0 <= y <= _H                # clipped to domain


def test_deterministic(built):
    r, _ = built
    tmp = Path(r.seeds_path).parent
    catch = str(tmp / "catchment.geojson")
    fl = str(tmp / "flowlines.geojson")
    r2 = build_refined_inputs(_prep(), catch, fl, tmp / "again",
                              RefineConfig(background_m=60.0, channel_m=18.0))
    assert r2.n_seeds == r.n_seeds                                  # seeded RNG -> stable
