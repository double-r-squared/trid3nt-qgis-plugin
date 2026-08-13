"""Offline unit tests: ARTEMIS REAL surveyed-structure path (no solve, no network).

The real-marina demo (ADR 0237) meshes an ACTUAL breakwater as a thin solid
barrier: OSM man_made=breakwater/pier polylines [[lon,lat],...] are projected to
the mesh's LOCAL UTM frame (AOI SW-corner origin subtracted, matching the node
coordinates), and a proof-norm-#9 REMOVED control keeps the same bathy but drops
the solid barrier. This covers the pure geometry helpers + the strict-field
manifest gate; the physics-through-the-baked-binary pair is the in-image sandbox
(docs/proof/templates/artemis_real_breakwater_sandbox.py) + the live E2E.

Run: python3 -m pytest services/workers/telemac/tests/ -q
"""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import artemis_build as A  # noqa: E402
import entrypoint as E  # noqa: E402


def _tr(epsg=32616):
    from pyproj import Transformer
    return Transformer.from_crs(4326, epsg, always_xy=True)


def test_polylines_to_segments_local_frame_and_count():
    tr = _tr()
    # two OSM-style ways: a 3-vertex + a 2-vertex line near Marquette
    polylines = [
        [[-87.379, 46.5340], [-87.378, 46.5390], [-87.377, 46.5440]],
        [[-87.383, 46.5427], [-87.384, 46.5431]],
    ]
    x0m, y0m = tr.transform(-87.392, 46.528)   # AOI SW corner
    segs = A._polylines_to_segments(polylines, tr, x0m, y0m)
    # 2 + 1 = 3 consecutive segments
    assert segs.shape == (3, 4)
    # local frame: every coordinate is a small positive offset from the SW corner
    # (harbour-scale, well under 10 km), NOT a raw ~4e5 UTM easting.
    assert np.all(segs >= -1.0) and np.all(segs < 10000.0)


def test_polylines_to_segments_rejects_empty():
    tr = _tr()
    with pytest.raises(A.ArtemisInputError):
        A._polylines_to_segments([[[-87.38, 46.54]]], tr, 0.0, 0.0)  # single vertex


def test_dist_to_segments_is_min_over_segments():
    # a unit segment along +x at y=0 from (0,0)->(10,0) and one at y=100
    segs = np.array([[0.0, 0.0, 10.0, 0.0], [0.0, 100.0, 10.0, 100.0]])
    d = A._dist_to_segments(np.array([5.0, 5.0]), np.array([3.0, 97.0]), segs)
    assert np.allclose(d, [3.0, 3.0])              # nearest of the two lines
    # a point off the end projects to the endpoint, not the infinite line
    d2 = A._dist_to_segments(np.array([-4.0]), np.array([0.0]), segs)
    assert np.isclose(d2[0], 4.0)


def test_config_accepts_real_structure_fields():
    cfg = A.ArtemisConfig(
        wave_mode="diffraction", bathy_source="noaa_greatlakes",
        bbox=(-87.392, 46.528, -87.368, 46.55),
        breakwater_polylines=[[[-87.379, 46.534], [-87.377, 46.544]]],
        remove_structure=True)
    assert cfg.breakwater_polylines and cfg.remove_structure is True


def test_entrypoint_strict_field_gate_allows_real_structure(tmp_path):
    # the strict-field manifest gate (ADR 0158/0148) must ACCEPT the two new keys
    # (else a live real-structure run silently no-ops them).
    cfg = E._artemis_config(tmp_path, {
        "wave_mode": "diffraction", "bathy_source": "noaa_greatlakes",
        "bbox": [-87.392, 46.528, -87.368, 46.55],
        "breakwater_polylines": [[[-87.379, 46.534], [-87.377, 46.544]]],
        "remove_structure": False,
    })
    assert cfg.breakwater_polylines and cfg.remove_structure is False
    assert cfg.workdir == str(tmp_path)


def test_entrypoint_strict_field_gate_still_rejects_unknown(tmp_path):
    with pytest.raises(E.ArtemisManifestUnknownFieldsError):
        E._artemis_config(tmp_path, {"wave_mode": "diffraction", "bogus_knob": 1})


def test_parser_version_bumped_for_real_structure():
    # the image-provenance marker MUST move when ArtemisConfig gains fields (ADR 0148).
    assert E._ARTEMIS_PARSER_VERSION == "artemis-agitation-2"
