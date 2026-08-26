"""Coastal tidal/surge worker unit tests (offline; no telemac binary).

Pins the LIQUID BOUNDARIES FILE grammar to what the in-image read_fic_frliq.f
reader accepts (first column T, SL(<i>) column, strictly-increasing time, a units
line, a trailing blank), the synthetic coastal mesher's single-open-boundary
topology, and the strict-unknown-field manifest gate.
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import telemac_coastal_build as C  # noqa: E402


def test_normalize_series_sorts_dedups_anchors_and_offsets():
    raw = [[3600, 1.0], [0.0001, 0.5], [3600, 1.2], [7200, 0.8]]
    ser = C._normalize_series(raw, datum_offset_m=-0.2)
    ts = [t for t, _ in ser]
    assert ts == sorted(ts) and len(set(ts)) == len(ts)   # strictly increasing
    assert ser[0][0] == 0.0                                # anchored at t=0
    assert ser[-2] == (3600.0, 1.2 - 0.2)                  # equal-time keep-last + offset


def test_normalize_series_rejects_too_few_points():
    with pytest.raises(C.CoastalInputError):
        C._normalize_series([[0, 1.0]], 0.0)


def test_liquid_boundaries_file_grammar(tmp_path):
    ser = C._normalize_series([[0, 0.4], [3600, 2.8], [7200, 0.6]], 0.0)
    path = str(tmp_path / "liq.txt")
    meta = C.write_liquid_boundaries_file(path, ser, duration_s=7200, boundary_index=1)
    lines = open(path).read().splitlines()
    data = [ln for ln in lines if ln and not ln.startswith("#")]
    assert data[0].split()[0] == "T"          # first column MUST be T (read_fic_frliq.f)
    assert data[0].split()[1] == "SL(1)"      # sl.f FCT='SL(1)' for boundary 1
    assert data[1] == "s m"                    # the skipped units line
    times = [float(r.split()[0]) for r in data[2:]]
    assert times == sorted(times) and len(set(times)) == len(times)   # strictly increasing
    assert times[-1] > 7200                    # flat-hold row brackets past DURATION
    assert lines[-1] == "" or lines[-2] == ""  # trailing blank line
    assert meta["liqbnd_col"] == "SL(1)" and meta["sl_max_m"] == 2.8


def test_synthetic_mesh_has_exactly_one_contiguous_open_boundary():
    cfg = C.CoastalConfig(bbox=(-85.0, 29.7, -84.9, 29.8), bathy_source="synthetic",
                          target_resolution_m=400.0, ocean_edge="S",
                          water_level_series=[[0, 0.4], [3600, 2.0]], duration_s=3600)
    mesh, meta = C.build_coastal_mesh(cfg, ".")
    assert meta["ocean_edge"] == "S"
    assert meta["n_ocean_nodes"] >= 2
    # ocean nodes form ONE contiguous run on the ring (=> one TELEMAC liquid boundary)
    flags = [1 if c == "ocean" else 0 for c in mesh["cls"]]
    runs = sum(1 for k in range(len(flags)) if flags[k] and not flags[k - 1])
    assert runs == 1, f"expected 1 contiguous open segment, got {runs}"
    # ocean nodes coded LIHBOR=5 (free surface imposed), land LIHBOR=2 (solid).
    for k, c in enumerate(mesh["cls"]):
        assert mesh["lihbor"][k] == (5 if c == "ocean" else 2)


def test_bbox_required():
    with pytest.raises(C.CoastalInputError):
        C.build_coastal_mesh(C.CoastalConfig(bbox=None, bathy_source="synthetic"), ".")


def test_manifest_strict_unknown_field_gate(tmp_path):
    import entrypoint as E
    with pytest.raises(E.CoastalManifestUnknownFieldsError):
        E._coastal_config(tmp_path, {"bbox": [-85, 29.7, -84.9, 29.8],
                                     "typo_knob": 1.0})
    cfg = E._coastal_config(tmp_path, {"bbox": [-85, 29.7, -84.9, 29.8],
                                       "bathy_source": "synthetic"})
    assert cfg.bbox == (-85.0, 29.7, -84.9, 29.8)
