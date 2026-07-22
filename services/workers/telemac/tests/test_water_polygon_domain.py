"""Offline regression: the water-polygon mesh domain must cover the WHOLE river.

Pins the 2026-07-18 NATE-marked defect on the Longview Columbia reach: the
corridor clip amputated the back-channel behind the Fisher Island chain and
the SW channel around Cottonwood Island (water with no mesh). The fixture is
the real NHDArea water (UTM 32610) + the proven reach centerline; the test
runs `_water_polygon_domain` fully offline and asserts:

  1. the domain resolves (no ribbon fallback),
  2. water coverage >= 0.90 of mapped water in the corridor,
  3. probe points in BOTH previously-amputated channels fall INSIDE the domain,
  4. cap edges exist on both end-transect lines (inflow/outflow resolvable).

Run: python -m pytest services/workers/telemac/tests/ -q
"""
import json
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from telemac_river_dye_build import ReachConfig, _dist_to_segment, _water_polygon_domain

FIXTURE = Path(__file__).with_name("fixtures_longview_water.json")

# Previously-amputated water (NATE's markup), EPSG:4326 -> UTM 32610 (pinned
# constants so the test needs no pyproj): back-channel behind the Fisher chain
# and the SW channel around Cottonwood Island.
PROBE_FISHER_BACKCHANNEL = (495677.2, 5113048.6)   # ~(-123.056, 46.1710)
PROBE_COTTONWOOD_SW = (497373.4, 5108269.9)        # ~(-123.034, 46.1280)

MESH_SIZE_M = 34.1


@pytest.fixture(scope="module")
def domain():
    fx = json.loads(FIXTURE.read_text())
    cl = np.asarray(fx["cl"], dtype=float)
    halfw = np.asarray(fx["halfw"], dtype=float)
    polys = [
        (np.asarray(p["ext"], dtype=float),
         [np.asarray(h, dtype=float) for h in p["holes"]])
        for p in fx["polys"]
    ]
    cfg = ReachConfig(name="longview_fixture", mesh_size_m=MESH_SIZE_M)
    cfg.bank_offsets = (halfw, halfw)
    cfg.water_polys_utm = polys
    out = _water_polygon_domain(cl, cfg, MESH_SIZE_M)
    assert out is not None, "water-polygon domain fell back to the ribbon"
    return out


def test_coverage_at_least_90_percent(domain):
    *_, coverage = domain
    assert coverage >= 0.90, (
        f"mesh domain covers only {coverage:.0%} of mapped water - "
        "part of the river would be unmeshed"
    )


def test_amputated_channels_are_inside_the_domain(domain):
    import shapely.geometry as sg

    ext_pts, holes, *_ = domain
    poly = sg.Polygon(ext_pts, holes=[h for h in holes if len(h) >= 4])
    for name, pt in (
        ("Fisher back-channel", PROBE_FISHER_BACKCHANNEL),
        ("Cottonwood SW channel", PROBE_COTTONWOOD_SW),
    ):
        assert poly.contains(sg.Point(pt)), (
            f"{name} probe {pt} is OUTSIDE the mesh domain - "
            "the 2026-07-18 lateral-clip regression is back"
        )


def test_cap_edges_resolve_on_both_ends(domain):
    ext_pts, _, cap_in, cap_out, _ = domain
    on_in = _dist_to_segment(ext_pts, *cap_in) < MESH_SIZE_M
    on_out = _dist_to_segment(ext_pts, *cap_out) < MESH_SIZE_M
    assert int(np.sum(on_in & np.roll(on_in, -1))) > 0, "no inflow cap edges"
    assert int(np.sum(on_out & np.roll(on_out, -1))) > 0, "no outflow cap edges"
