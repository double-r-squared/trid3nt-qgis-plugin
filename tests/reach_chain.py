"""The reach chain's two FETCHES, stood in for with geometry this module writes.

Offline. ``fetch_nhdplus_nldi_navigate`` (the navigated mainstem) and
``fetch_nhd_area_water`` (the mapped banks) are the only network reads in the
reach templates' domain chain; ``endpoints`` and ``section`` between them run for
real over the files written here, so a chain test measures the chain rather than
a stand-in for it.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

from trid3nt_contracts.execution import LayerURI

#: A straight west-to-east stretch and the mapped banks around it. The banks are
#: wider than the stretch on both ends, so the ``between`` cut has polygon to
#: remove and the section is a measurement rather than a pass-through.
CENTERLINE = {"type": "LineString",
              "coordinates": [[-124.16, 40.50], [-124.12, 40.50],
                              [-124.08, 40.50], [-124.04, 40.50]]}
WATER = {"type": "Polygon", "coordinates": [
    [[-124.20, 40.4970], [-124.00, 40.4970], [-124.00, 40.5030],
     [-124.20, 40.5030], [-124.20, 40.4970]]]}

#: Mapped water that covers only the WEST half of the stretch, and mapped water
#: that covers none of it. NHDArea maps a surface only where the channel is wide
#: enough to have two banks, so both are real answers about a real river rather
#: than fetch failures - and the coverage measurement is what tells them apart.
WATER_HALF = {"type": "Polygon", "coordinates": [
    [[-124.20, 40.4970], [-124.10, 40.4970], [-124.10, 40.5030],
     [-124.20, 40.5030], [-124.20, 40.4970]]]}
WATER_ELSEWHERE = {"type": "Polygon", "coordinates": [
    [[-123.50, 40.4970], [-123.40, 40.4970], [-123.40, 40.5030],
     [-123.50, 40.5030], [-123.50, 40.4970]]]}
#: Mapped water at BOTH ends of the stretch with an unmapped gap in the middle -
#: half the centreline covered, and both end transects still cut off real
#: polygon. The shape a partly-mapped reach takes when it can still be sectioned.
WATER_GAPPED = {"type": "MultiPolygon", "coordinates": [
    [[[-124.20, 40.4970], [-124.13, 40.4970], [-124.13, 40.5030],
      [-124.20, 40.5030], [-124.20, 40.4970]]],
    [[[-124.07, 40.4970], [-124.00, 40.4970], [-124.00, 40.5030],
      [-124.07, 40.5030], [-124.07, 40.4970]]]]}

CENTERLINE_BBOX = [-124.16, 40.50, -124.04, 40.50]

#: The BOUNDARY the stood-in accepted mesh declares, and the bed it carries
#: there. Its four nodes are the two western ones (the inflow cap) and the two
#: eastern ones (the outflow cap); the deck's outflow stage is the median bed
#: over each, so the reach falls 3 m from top to bottom.
MESH_ROLES = {"inflow": [0, 3], "outflow": [1, 2]}
MESH_NODE_BED = [900.0, 897.0, 897.0, 900.0]


def _write(tmp_path, name: str, geometry: dict[str, Any]) -> str:
    path = tmp_path / name
    path.write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {}, "geometry": geometry}]}))
    return str(path)


def _layer(uri: str, name: str, bbox: list[float] | None) -> LayerURI:
    return LayerURI(layer_id=name, name=name, layer_type="vector", uri=uri,
                    style_preset="nhd_waterbodies", role="context", bbox=bbox)


def install_reach_chain(monkeypatch, tmp_path, captured: dict | None = None,
                        water: dict[str, Any] | None = None) -> None:
    """Answer the chain's two fetches from local files, recording what was asked.

    The section tool writes its own artifact, so the output directory is pinned to
    ``tmp_path`` for the whole chain. ``water`` names WHAT the water fetch returns,
    so a caller can ask the chain a reach the mapped polygons only partly cover -
    or do not cover at all.
    """
    from trid3nt_server.tools import TOOL_REGISTRY

    seen = captured if captured is not None else {}
    centerline_uri = _write(tmp_path, "centerline.geojson", CENTERLINE)
    water_uri = _write(tmp_path, "water.geojson", water or WATER)

    def _navigate(*, seed_point=None, comid=None, direction="DM",
                  distance_km=50.0, **_kw):
        seen["navigate"] = {"seed_point": seed_point, "direction": direction,
                            "distance_km": distance_km}
        # EVERY navigate, not just the last: one reach has one centerline, and a
        # second acquisition beside the declared row is what put a derived release
        # 350 m outside the meshed domain.
        seen.setdefault("navigates", []).append(dict(seen["navigate"]))
        return _layer(centerline_uri, "centerline", list(CENTERLINE_BBOX))

    def _water(*, bbox, max_records=200, **_kw):
        seen["water_bbox"] = list(bbox)
        return _layer(water_uri, "water", list(bbox))

    for name, fn in (("fetch_nhdplus_nldi_navigate", _navigate),
                     ("fetch_nhd_area_water", _water)):
        monkeypatch.setitem(TOOL_REGISTRY, name,
                            dataclasses.replace(TOOL_REGISTRY[name], fn=fn))

    # endpoints and section persist their own artifacts to the runs bucket when a
    # chain names no output directory; offline there is no bucket, so the write
    # lands in the test's own directory and the uris stay readable.
    def _local_write(fc, prefix, seed, output_dir):
        path = tmp_path / f"{prefix}_{seed}.geojson"
        path.write_text(json.dumps(fc))
        return str(path)

    for module in ("endpoints.endpoints", "section.section"):
        monkeypatch.setattr(
            f"trid3nt_server.tools.processing.{module}._write_geojson",
            _local_write)

    # The ACCEPTED MESH a derived release is settled inside. The mesh session is
    # stood in for by these tests, so its display face is a uri nothing wrote;
    # what the deck needs from it is a triangulation to test containment against
    # and the bed its declared roles stand on, and here that is one that holds the
    # whole stretch - so a chain test measures the chain rather than a stand-in
    # mesh's extent. The deck reads the display face through its own binding and
    # the release containment through the module's, so it stands in at both.
    import numpy as np

    from trid3nt_server.workflows.mesh.shared import nodes as nodes_mod
    from trid3nt_server.workflows.telemac.authoring import deck as deck_mod

    span = 1.0e7

    def _accepted_nodes(_uri, utm_epsg=None):
        return (np.array([[-span, -span], [span, -span],
                          [span, span], [-span, span]]),
                np.array([[0, 1, 2], [0, 2, 3]]), np.array(MESH_NODE_BED), None)

    monkeypatch.setattr(nodes_mod, "read_accepted_mesh_nodes", _accepted_nodes)
    monkeypatch.setattr(deck_mod, "read_accepted_mesh_nodes", _accepted_nodes)
