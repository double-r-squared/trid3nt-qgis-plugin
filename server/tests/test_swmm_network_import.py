"""Offline tests for the SWMM network-import family (ADR 0124, row #1).

Covers the pure engine core (parse -> build -> solve -> geojson) on a synthetic
municipal storm-drain network fixture, the labeled-degrade counts (invert fill,
topology snap, diameter-unit inference), the typed error paths, and the composer
input-loading helpers. No network access, no live fetch - the parser/builder/
solver run entirely on an in-memory fixture + a real headless swmm5_run.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from trid3nt_server.agent.mesh.swmm_network import (
    SWMMNetworkError,
    build_network_inp,
    network_to_geojson_4326,
    parse_network_features,
    run_network_deck,
)

pytest.importorskip("swmm_api")
pytest.importorskip("pyproj")

_LON0, _LAT0 = -95.37, 29.76  # Houston, TX
_D = 0.001


def _pt(dx, dy, props):
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [_LON0 + dx, _LAT0 + dy]},
        "properties": props,
    }


def _ln(a, b, props):
    return {
        "type": "Feature",
        "geometry": {
            "type": "LineString",
            "coordinates": [[_LON0 + a[0], _LAT0 + a[1]], [_LON0 + b[0], _LAT0 + b[1]]],
        },
        "properties": props,
    }


def _fixture_network():
    """A 5-node descending storm drain: MH1..MH4 -> OF1, conduits WITHOUT explicit
    from/to (topology recovered by endpoint snapping), diameters in inches, one
    node missing its invert (slope-walk fill), one missing its rim (default depth).
    """
    nodes = {
        "type": "FeatureCollection",
        "features": [
            _pt(0, 0, {"NodeID": "MH1", "InvertElev": 10.0, "RimElev": 13.0}),
            _pt(_D, 0, {"NodeID": "MH2", "InvertElev": 9.5, "RimElev": 12.5}),
            _pt(2 * _D, 0, {"NodeID": "MH3", "InvertElev": 9.0}),
            _pt(3 * _D, 0, {"NodeID": "MH4"}),  # missing invert
            _pt(4 * _D, 0, {"NodeID": "OF1", "Type": "Outfall", "InvertElev": 8.0}),
        ],
    }
    conds = {
        "type": "FeatureCollection",
        "features": [
            _ln((0, 0), (_D, 0), {"Diameter": 18}),
            _ln((_D, 0), (2 * _D, 0), {"Diameter": 24}),
            _ln((2 * _D, 0), (3 * _D, 0), {"Diameter": 24}),
            _ln((3 * _D, 0), (4 * _D, 0), {"Diameter": 30}),
        ],
    }
    return nodes, conds


def test_parse_topology_snap_invert_fill_and_diameter_units():
    nodes, conds = _fixture_network()
    parsed = parse_network_features(nodes, conds)
    assert len(parsed.junctions) == 4
    assert len(parsed.outfalls) == 1
    assert len(parsed.conduits) == 4
    # MH4 had no invert -> exactly one gap-fill.
    assert parsed.n_inverts_filled == 1
    # no from/to attrs -> both endpoints of every conduit snapped.
    assert parsed.n_topology_snapped == 8
    # diameters were inches (18/24/30) -> inferred inches, converted to metres.
    assert parsed.diameter_units_assumed == "inches"
    # every conduit diameter is now a sane metre value (18 in ~ 0.457 m).
    for c in parsed.conduits:
        assert 0.3 < c.diameter < 1.0
    # the tagged outfall is OF1.
    assert parsed.outfalls[0].name.startswith("OF1")


def test_outfall_inferred_when_untagged():
    """With no type=outfall tag, the lowest-invert downstream leaf becomes the outfall."""
    nodes, conds = _fixture_network()
    # strip the outfall tag; OF1 keeps the lowest invert (8.0) as a sink.
    for f in nodes["features"]:
        f["properties"].pop("Type", None)
    parsed = parse_network_features(nodes, conds)
    assert len(parsed.outfalls) == 1
    assert parsed.outfalls[0].invert == pytest.approx(8.0)


def test_build_solve_and_geojson_produces_real_scalars():
    nodes, conds = _fixture_network()
    parsed = parse_network_features(nodes, conds)
    with tempfile.TemporaryDirectory() as td:
        inp = os.path.join(td, "net.inp")
        build = build_network_inp(
            parsed, out_inp_path=inp, total_rain_depth_mm=120.0, storm_duration_hr=2.0
        )
        assert build.n_junctions == 4
        assert build.n_conduits == 4
        assert build.total_pipe_length_m > 0
        run = run_network_deck(build)
        # a real solve: some flow reaches the outfall, continuity within tolerance.
        assert run.peak_outfall_flow_cms > 0.0
        assert run.total_outfall_volume_m3 > 0.0
        assert abs(run.continuity_error_pct) <= 10.0
        # every node got a max-HGL readout.
        assert len(run.node_max_hgl) == 5

        gj = network_to_geojson_4326(build, run)
        pts = [f for f in gj["features"] if f["geometry"]["type"] == "Point"]
        lns = [f for f in gj["features"] if f["geometry"]["type"] == "LineString"]
        assert len(pts) == 5 and len(lns) == 4
        # node features carry role + hydraulic attributes.
        p0 = pts[0]["properties"]
        assert p0["role"] in ("junction", "outfall")
        assert "max_hgl_m" in p0 and "flooded" in p0
        # conduit features carry a surcharge flag.
        assert "surcharged" in lns[0]["properties"]


def test_empty_network_raises_typed():
    empty = {"type": "FeatureCollection", "features": []}
    with pytest.raises(SWMMNetworkError) as ei:
        parse_network_features(empty, empty)
    assert ei.value.error_code == "SWMM_NETWORK_EMPTY"


def test_degenerate_conduits_raise_disconnected():
    # every conduit is zero-length (start == end) -> from==to -> all dropped ->
    # no conduit connects two distinct nodes -> DISCONNECTED.
    nodes = {
        "type": "FeatureCollection",
        "features": [
            _pt(0, 0, {"NodeID": "A", "InvertElev": 10.0}),
            _pt(_D, 0, {"NodeID": "B", "InvertElev": 9.0}),
        ],
    }
    conds = {
        "type": "FeatureCollection",
        "features": [_ln((0, 0), (0, 0), {"Diameter": 12})],
    }
    with pytest.raises(SWMMNetworkError) as ei:
        parse_network_features(nodes, conds)
    assert ei.value.error_code == "SWMM_NETWORK_DISCONNECTED"


def test_explicit_topology_attrs_no_snap():
    """When conduits carry from/to node ids, no endpoint snapping is needed."""
    nodes, conds = _fixture_network()
    ids = ["MH1", "MH2", "MH3", "MH4", "OF1"]
    for k, f in enumerate(conds["features"]):
        f["properties"]["FromNode"] = ids[k]
        f["properties"]["ToNode"] = ids[k + 1]
    parsed = parse_network_features(nodes, conds)
    assert parsed.n_topology_snapped == 0
    assert len(parsed.conduits) == 4


def test_composer_input_helpers():
    from trid3nt_server.agent.workflows.swmm.network_import.network_import import (
        _bbox_from_fc,
        _source_label,
        _split_by_geometry,
    )

    nodes, conds = _fixture_network()
    combined = {
        "type": "FeatureCollection",
        "features": nodes["features"] + conds["features"],
    }
    pts, lns = _split_by_geometry(combined)
    assert len(pts["features"]) == 5
    assert len(lns["features"]) == 4

    bbox = _bbox_from_fc(nodes)
    assert bbox is not None and len(bbox) == 4
    assert bbox[0] < bbox[2] and bbox[1] < bbox[3]

    assert "FeatureServer" in _source_label(
        "https://x/arcgis/rest/services/y/FeatureServer/0"
    )
    assert _source_label("s3://b/k.geojson") == "user upload"


# --------------------------------------------------------------------------- #
# Row #2 - dual-drainage coupling (overland mesh + imported pipes -> one deck).
# --------------------------------------------------------------------------- #
def _write_synthetic_dem(path):
    import numpy as np
    import rasterio
    from rasterio.crs import CRS
    from rasterio.transform import from_origin

    N, CELL, EPSG, OX, OY = 20, 10.0, 32615, 500000.0, 3300000.0
    ii, jj = np.meshgrid(np.arange(N), np.arange(N), indexing="ij")
    dem = (30.0 - 0.02 * CELL * (ii + jj)).astype("float32")
    with rasterio.open(
        path, "w", driver="GTiff", dtype="float32", count=1, height=N, width=N,
        crs=CRS.from_epsg(EPSG), transform=from_origin(OX, OY, CELL, CELL), nodata=-9999.0,
    ) as d:
        d.write(dem, 1)
    return N, CELL, EPSG, OX, OY


def test_dual_drainage_coupling_solves_and_exchanges_flow(tmp_path):
    import dataclasses

    import numpy as np
    from rasterio.transform import from_origin, xy
    from rasterio.warp import transform as warp_transform

    from trid3nt_server.agent.mesh.raster_cell_mesh import build_swmm_mesh, run_swmm_deck
    from trid3nt_server.agent.mesh.swmm_network import (
        build_dual_drainage_inp,
        dual_drainage_network_to_geojson_4326,
        read_network_response,
    )

    demp = str(tmp_path / "dem.tif")
    N, CELL, EPSG, OX, OY = _write_synthetic_dem(demp)
    tr = from_origin(OX, OY, CELL, CELL)

    def cll(i, j):
        x, y = xy(tr, i, j)
        lo, la = warp_transform(f"EPSG:{EPSG}", "EPSG:4326", [x], [y])
        return lo[0], la[0]

    mesh = build_swmm_mesh(
        dem_path=demp, out_inp_path=str(tmp_path / "mesh.inp"),
        total_rain_depth_mm=100.0, storm_duration_hr=1.0,
        target_resolution_m=10.0, enable_autoscale=False,
    )
    pts = [cll(3, 3), cll(8, 8), cll(13, 13), cll(18, 18)]

    def pt(ll, p):
        return {"type": "Feature", "geometry": {"type": "Point", "coordinates": list(ll)}, "properties": p}

    def ln(a, b, p):
        return {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [list(a), list(b)]}, "properties": p}

    nodes = {"type": "FeatureCollection", "features": [
        pt(pts[0], {"NodeID": "P1"}), pt(pts[1], {"NodeID": "P2"}),
        pt(pts[2], {"NodeID": "P3"}), pt(pts[3], {"NodeID": "OF", "Type": "Outfall"})]}
    conds = {"type": "FeatureCollection", "features": [
        ln(pts[0], pts[1], {"Diameter": 24, "FromNode": "P1", "ToNode": "P2"}),
        ln(pts[1], pts[2], {"Diameter": 24, "FromNode": "P2", "ToNode": "P3"}),
        ln(pts[2], pts[3], {"Diameter": 30, "FromNode": "P3", "ToNode": "OF"})]}
    parsed = parse_network_features(nodes, conds, dem_path=demp)

    combined = str(tmp_path / "coupled.inp")
    dd = build_dual_drainage_inp(mesh, parsed, out_inp_path=combined)
    # the coupling wired one inlet per pipe junction that fell in a surface cell.
    assert dd.n_inlets >= 1
    assert dd.n_pipe_junctions == 3 and dd.n_pipe_outfalls == 1

    # the combined deck solves via the SAME run_swmm_deck (overland grid sampler
    # ignores the P_ pipe nodes) with a clean mass balance.
    cb = dataclasses.replace(mesh, inp_path=combined)
    run = run_swmm_deck(cb, mass_balance_tolerance_pct=10.0)
    assert abs(run.continuity_error_pct) <= 10.0
    assert run.max_depth_m >= 0.0

    # the pipe response reads ONLY the pipe nodes (filtered), and the pipe outfall
    # discharges captured surface flow - the dual-drainage exchange.
    resp = read_network_response(
        run.rpt_path, node_filter=set(dd.pipe_node_coords),
        conduit_filter={e[0] for e in dd.pipe_conduit_endpoints},
        outfall_filter=set(dd.pipe_outfall_names),
    )
    assert resp["peak_outfall_flow_cms"] > 0.0  # pipe captured + routed surface flow

    gj = dual_drainage_network_to_geojson_4326(dd, resp)
    assert any(f["geometry"]["type"] == "Point" for f in gj["features"])
    assert any(f["geometry"]["type"] == "LineString" for f in gj["features"])
