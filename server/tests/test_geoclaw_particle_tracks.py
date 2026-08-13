"""Unit tests for the GeoClaw Lagrangian particle-track postprocess (ADR 0155).

Pins the CLAWPACK-FREE, pure-python reader + builders that turn Lagrangian
particle-gauge output into a drift-track product layer + chart:

  - ``parse_geoclaw_particle_tracks``: reads the ``# Lagrangian particle`` gauge
    files (cols [level, t, h, xg, yg, eta]) into drift tracks, IGNORING the
    stationary Eulerian coastal gauge.
  - ``build_geoclaw_particle_track_geojson``: one LineString feature per track.
  - ``build_particle_track_chart_spec``: cumulative-drift-vs-time multi-series.

No clawpack / numpy needed -- the fixtures are plain gauge text files.
"""

from __future__ import annotations

from pathlib import Path

from trid3nt_server.agent.workflows.geoclaw.postprocess_geoclaw import (
    build_geoclaw_particle_track_geojson,
    build_particle_track_chart_spec,
    parse_geoclaw_particle_tracks,
)

# A Lagrangian particle gauge: q[2,3] replaced by (x(t), y(t)); the particle
# drifts east+north over 3 samples.
_LAGRANGIAN_GAUGE = """\
# gauge_id=      100 location=( -1.2424000000e+02  4.1750000000e+01 ) num_var=  4
# Lagrangian particle, q[2,3] replaced by (x(t),y(t))
# level, time, q[1  2  3], eta, aux[]
# file format ascii, time series follow in this file
 1  0.0000000000e+00  1.5000000e+00  -124.2400000  41.7500000  1.5000000e+00
 1  1.0000000000e+02  1.6000000e+00  -124.2390000  41.7510000  1.6000000e+00
 1  2.0000000000e+02  1.7000000e+00  -124.2375000  41.7525000  1.7000000e+00
"""

# A stationary Eulerian coastal gauge -- must be IGNORED by the particle reader.
_EULERIAN_GAUGE = """\
# gauge_id=        1 location=( -1.2420000000e+02  4.1730000000e+01 ) num_var=  4
# Stationary gauge
# level, time, q[1  2  3], eta
 1  0.0000000000e+00  2.0000000e+00  0.1  0.05  2.0500000e+00
 1  1.0000000000e+02  2.1000000e+00  0.2  0.06  2.1600000e+00
"""


def _write_outputs(tmp_path: Path) -> Path:
    out = tmp_path / "_output"
    out.mkdir()
    (out / "gauge00001.txt").write_text(_EULERIAN_GAUGE, encoding="utf-8")
    (out / "gauge00100.txt").write_text(_LAGRANGIAN_GAUGE, encoding="utf-8")
    return tmp_path


def test_parse_reads_lagrangian_track_ignores_eulerian(tmp_path: Path):
    tracks = parse_geoclaw_particle_tracks(_write_outputs(tmp_path))
    # only the Lagrangian gauge yields a track (the Eulerian one is skipped).
    assert len(tracks) == 1
    tr = tracks[0]
    assert tr["gauge_id"] == 100
    assert tr["t"] == [0.0, 100.0, 200.0]
    # coords are (xg, yg) = (lon, lat), the advected positions.
    assert tr["coords"][0] == [-124.24, 41.75]
    assert tr["coords"][-1] == [-124.2375, 41.7525]
    assert tr["start"] == [-124.24, 41.75]
    assert tr["end"] == [-124.2375, 41.7525]
    # a positive drift length + duration over the 200 s window.
    assert tr["length_m"] > 0.0
    assert tr["duration_s"] == 200.0


def test_no_lagrangian_gauge_yields_no_tracks(tmp_path: Path):
    out = tmp_path / "_output"
    out.mkdir()
    (out / "gauge00001.txt").write_text(_EULERIAN_GAUGE, encoding="utf-8")
    assert parse_geoclaw_particle_tracks(tmp_path) == []


def test_track_geojson_is_one_linestring_per_track(tmp_path: Path):
    tracks = parse_geoclaw_particle_tracks(_write_outputs(tmp_path))
    fc, stats = build_geoclaw_particle_track_geojson(tracks)
    assert fc["type"] == "FeatureCollection"
    assert len(fc["features"]) == 1
    feat = fc["features"][0]
    assert feat["geometry"]["type"] == "LineString"
    assert len(feat["geometry"]["coordinates"]) == 3
    assert feat["properties"]["gauge_id"] == 100
    assert feat["properties"]["track_length_m"] > 0.0
    assert stats["track_count"] == 1
    assert fc["metadata"]["kind"] == "geoclaw_lagrangian_particle_tracks"
    assert fc["metadata"]["crs"] == "EPSG:4326"


def test_chart_spec_is_cumulative_drift_multi_series(tmp_path: Path):
    tracks = parse_geoclaw_particle_tracks(_write_outputs(tmp_path))
    spec = build_particle_track_chart_spec(tracks)
    assert spec is not None
    assert spec["encoding"]["x"]["field"] == "t_s"
    assert spec["encoding"]["y"]["field"] == "dist_m"
    assert spec["encoding"]["color"]["field"] == "particle"
    # first sample of a track has zero cumulative drift; distance grows monotonically.
    vals = [v for v in spec["data"]["values"] if v["particle"] == "particle 100"]
    assert vals[0]["dist_m"] == 0.0
    dists = [v["dist_m"] for v in vals]
    assert dists == sorted(dists)
    assert dists[-1] > 0.0


def test_chart_spec_none_when_no_tracks():
    assert build_particle_track_chart_spec([]) is None
    assert build_particle_track_chart_spec(None) is None
