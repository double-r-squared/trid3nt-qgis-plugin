"""P2 unit tests for the TELEMAC river-dye worker entrypoint.

TELEMAC-free: exercise the manifest -> ReachConfig mapping, the unknown-key
drop, the workdir pin, and the bad-manifest typed-error path -- WITHOUT gmsh /
telemac2d / the network. The live solve is covered by the container build-time
smoke + the through-the-seam dev proof.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")  # telemac_river_dye_build imports numpy at top

# The entrypoint imports its sibling ``telemac_river_dye_build`` off the script
# dir (as it does inside the container); replicate that here.
_WORKER_DIR = Path(__file__).parent
if str(_WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(_WORKER_DIR))

from workers.telemac import entrypoint as E  # noqa: E402


def test_reach_config_defaults_pin_workdir(tmp_path):
    cfg = E._reach_config(tmp_path, {})
    assert cfg.workdir == str(tmp_path)
    # proven P1 defaults survive
    assert cfg.name == "snake_river_twin_falls"
    assert cfg.dye_conc_mgl == 100.0


def test_reach_config_applies_overrides(tmp_path):
    cfg = E._reach_config(tmp_path, {
        "name": "colorado_reach",
        "seed_lon": -108.5, "seed_lat": 39.1,
        "distance_km": 4.0, "channel_width_m": 45.0,
        "dye_conc_mgl": 250.0, "duration_s": 1800.0,
    })
    assert cfg.name == "colorado_reach"
    assert cfg.seed_lon == -108.5 and cfg.seed_lat == 39.1
    assert cfg.distance_km == 4.0 and cfg.channel_width_m == 45.0
    assert cfg.dye_conc_mgl == 250.0 and cfg.duration_s == 1800.0
    assert cfg.workdir == str(tmp_path)


def test_reach_config_ignores_manifest_workdir_pin(tmp_path):
    # a manifest 'workdir' must not override the mounted-data-dir pin
    cfg = E._reach_config(tmp_path, {
        "workdir": "/etc/should-not-win",
        "distance_km": 7.0,
    })
    assert cfg.distance_km == 7.0
    assert cfg.workdir == str(tmp_path)


def test_reach_config_rejects_unknown_keys(tmp_path):
    """an unknown reach key errors loudly instead of being dropped
    with a log warning (the lesson -- a WARNING line is invisible in
    practice; two registered knob templates ran as no-ops that way)."""
    with pytest.raises(E.TelemacManifestUnknownFieldsError, match="bogus"):
        E._reach_config(tmp_path, {
            "bogus": 123, "another_unknown": "x", "distance_km": 7.0,
        })


def test_parser_version_is_reach_7():
    """the output_interval_min cadence lever bumps the parser stamp to reach-10."""
    assert E._PARSER_VERSION == "telemac-reach-10"


def test_reach_config_accepts_erodible_bed_fields(tmp_path):
    """the GAIA v2 erodible-bed knobs are known fields (bedload scour)."""
    cfg = E._reach_config(tmp_path, {
        "substance_class": "sediment", "erodible_bed": True,
        "bed_thickness_m": 4.0, "bedload_formula": 1,
        "morphological_factor": 20.0, "grain_size_um": 400.0,
    })
    assert cfg.erodible_bed is True and cfg.bed_thickness_m == 4.0
    assert cfg.bedload_formula == 1 and cfg.morphological_factor == 20.0


def test_reach_config_accepts_soil_store_fields(tmp_path):
    """the soil-store knobs are known fields (continuous SCS-CN store)."""
    cfg = E._reach_config(tmp_path, {
        "mode": "rain_on_grid", "watershed_slf": "watershed.slf",
        "rain_hyetograph_blocks": [[3600.0, 12.5], [7200.0, 0.0]],
        "soil_store": True, "soil_store_capacity_mm": 90.0,
        "soil_store_recovery_h": 72.0, "soil_store_init_mm": 30.0,
    })
    assert cfg.soil_store is True and cfg.soil_store_capacity_mm == 90.0
    assert cfg.soil_store_recovery_h == 72.0 and cfg.soil_store_init_mm == 30.0


def test_reach_config_accepts_hyetograph_blocks(tmp_path):
    """rain_hyetograph_blocks is a known field (time-varying native path)."""
    cfg = E._reach_config(tmp_path, {
        "mode": "rain_on_grid", "watershed_slf": "watershed.slf",
        "rain_hyetograph_blocks": [[3600.0, 12.5], [7200.0, 0.0]],
    })
    assert cfg.rain_hyetograph_blocks == [[3600.0, 12.5], [7200.0, 0.0]]


def test_reach_config_accepts_rog_fields(tmp_path):
    cfg = E._reach_config(tmp_path, {
        "mode": "rain_on_grid", "watershed_slf": "watershed.slf",
        "runoff_path": "native", "curve_number": 82.0, "amc_condition": 1,
        "rain_intensity_mm_per_hr": 40.0, "node_cn2_file": "cn.txt",
        "node_manning_file": "n.txt", "outlet_lonlat": (-83.4, 35.05),
        "observed_gauge_id": "02086500",
    })
    assert cfg.mode == "rain_on_grid" and cfg.runoff_path == "native"
    assert cfg.curve_number == 82.0 and cfg.amc_condition == 1
    assert cfg.observed_gauge_id == "02086500"


def test_reach_config_rejects_unknown_key_names_v7(tmp_path):
    """A bogus reach key raises naming the CURRENT parser version (telemac-reach-10)."""
    with pytest.raises(E.TelemacManifestUnknownFieldsError, match="telemac-reach-10"):
        E._reach_config(tmp_path, {"bogus_rog_field": 1, "mode": "rain_on_grid"})


def test_default_outputs_include_bed_cog():
    """the supervisor uploads the in-worker bed COG so the composer can
    surface the river bed bathymetry as a role=context input."""
    assert "bed_bathymetry.tif" in E.DEFAULT_OUTPUTS


def test_write_bed_cog_nonconstant_and_finite(tmp_path):
    """behavior: write_bed_cog rasterizes the solved bed to a small 4326
    COG whose valid pixels are FINITE and NON-CONSTANT (a real sloped bed, never a
    flat placeholder). Mirrors the in-image smoke assertion, offline on a
    synthetic sloped mesh so it runs without TELEMAC."""
    np = pytest.importorskip("numpy")
    pytest.importorskip("scipy")
    rasterio = pytest.importorskip("rasterio")
    pyproj = pytest.importorskip("pyproj")
    import telemac_river_dye_build as B

    # a synthetic UTM 10N reach: a 60 m-wide x ~1 km ribbon sloping downstream.
    tr = pyproj.Transformer.from_crs(4326, 32610, always_xy=True)
    n_along, n_across = 40, 5
    xs = np.linspace(500_000.0, 501_000.0, n_along)      # 1 km downstream (UTM x)
    ys = np.linspace(4_000_000.0, 4_000_060.0, n_across)  # 60 m across (UTM y)
    gx, gy = np.meshgrid(xs, ys)
    X = gx.ravel()
    Y = gy.ravel()
    # bed drops 5 m over the km (a real, non-constant slope)
    Z = 100.0 - 5.0 * (X - 500_000.0) / 1000.0

    class _Cfg:
        pass

    meta = B.write_bed_cog({"X": X, "Y": Y}, Z, _Cfg(),
                           tr, str(tmp_path / B.BED_COG_FILENAME))
    cog = tmp_path / B.BED_COG_FILENAME
    assert cog.exists(), "bed COG was not written"
    assert meta["bed_cog"] == B.BED_COG_FILENAME
    # finite + non-constant range (the sloped bed, not a flat sentinel)
    assert meta["bed_cog_max_m"] > meta["bed_cog_min_m"] + 1.0
    with rasterio.open(cog) as src:
        assert src.crs.to_epsg() == 4326
        band = src.read(1, masked=True)
        vals = band.compressed()
        assert vals.size > 0 and np.isfinite(vals).all()
        assert float(vals.max()) - float(vals.min()) > 1.0  # non-constant


def test_write_bed_cog_lonlat_nonconstant_and_finite(tmp_path):
    """S3: the shared wave-module bed writer rasterizes node lon/lat/z
    (a NOAA lake-datum bed) to a small 4326 COG whose valid pixels are FINITE and
    NON-CONSTANT (the real sloped lake bed, never a flat placeholder), dropping
    non-finite (off-lake / land) nodes. Offline on a synthetic sloped lake patch."""
    np = pytest.importorskip("numpy")
    pytest.importorskip("scipy")
    rasterio = pytest.importorskip("rasterio")
    import _bed_cog as BC

    # a synthetic lake AOI near Marquette: lon/lat grid, bed deepening offshore,
    # with a NaN land corner the writer must drop (not paint).
    lons = np.linspace(-87.40, -87.36, 30)
    lats = np.linspace(46.52, 46.56, 30)
    glon, glat = np.meshgrid(lons, lats)
    lon = glon.ravel()
    lat = glat.ravel()
    # lake-datum bed: -5 m at the north shore down to -40 m offshore (south).
    z = -5.0 - 35.0 * (lats.max() - glat.ravel()) / (lats.max() - lats.min())
    z[lat > 46.555] = np.nan  # a dry land strip -> non-finite, dropped

    meta = BC.write_bed_cog_lonlat(lon, lat, z, str(tmp_path / BC.BED_COG_FILENAME))
    cog = tmp_path / BC.BED_COG_FILENAME
    assert cog.exists(), "lake bed COG was not written"
    assert meta["bed_cog"] == BC.BED_COG_FILENAME
    assert meta["bed_cog_max_m"] > meta["bed_cog_min_m"] + 1.0  # non-constant slope
    with rasterio.open(cog) as src:
        assert src.crs.to_epsg() == 4326
        vals = src.read(1, masked=True).compressed()
        assert vals.size > 0 and np.isfinite(vals).all()
        assert float(vals.max()) - float(vals.min()) > 1.0
        # lake-datum bed is BELOW datum -> negative elevations surfaced faithfully.
        assert float(vals.min()) < 0.0


def test_write_bed_cog_lonlat_too_few_nodes_raises(tmp_path):
    """Fewer than 3 finite nodes cannot rasterize -> the writer raises (the caller
    wraps it best-effort so this never voids a solve)."""
    np = pytest.importorskip("numpy")
    pytest.importorskip("scipy")
    pytest.importorskip("rasterio")
    import _bed_cog as BC

    with pytest.raises(RuntimeError):
        BC.write_bed_cog_lonlat(
            np.array([-87.4, np.nan]), np.array([46.5, 46.5]),
            np.array([-10.0, -12.0]), str(tmp_path / BC.BED_COG_FILENAME))


def test_main_bad_manifest_writes_typed_error(tmp_path, monkeypatch):
    # A malformed manifest (JSON array, not object) -> exit 2 + typed metrics.
    (tmp_path / "manifest.json").write_text("[1, 2, 3]", encoding="utf-8")
    rc = E.main(["--data-dir", str(tmp_path), "--manifest", str(tmp_path / "manifest.json")])
    assert rc == 2
    metrics = json.loads((tmp_path / E.METRICS_FILENAME).read_text())
    assert metrics["status"] == "error"
    assert metrics["correct_end"] is False
    assert "manifest read failed" in metrics["error"]


def test_default_outputs_include_result_and_metrics():
    assert "r2d_river.slf" in E.DEFAULT_OUTPUTS
    assert E.METRICS_FILENAME in E.DEFAULT_OUTPUTS
    assert "river.slf" in E.DEFAULT_OUTPUTS
