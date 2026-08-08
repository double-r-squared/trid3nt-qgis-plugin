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

from services.workers.telemac import entrypoint as E  # noqa: E402


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
    """ADR 0158: an unknown reach key errors loudly instead of being dropped
    with a log warning (the ADR 0148 lesson -- a WARNING line is invisible in
    practice; two registered knob templates ran as no-ops that way)."""
    with pytest.raises(E.TelemacManifestUnknownFieldsError, match="bogus"):
        E._reach_config(tmp_path, {
            "bogus": 123, "another_unknown": "x", "distance_km": 7.0,
        })


def test_parser_version_is_reach_3():
    """ADR 0196: the RoG field additions bump the strict-parser version stamp."""
    assert E._PARSER_VERSION == "telemac-reach-3"


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


def test_reach_config_rejects_unknown_rog_key_names_v3(tmp_path):
    """A bogus reach key raises naming the CURRENT parser version (telemac-reach-3)."""
    with pytest.raises(E.TelemacManifestUnknownFieldsError, match="telemac-reach-3"):
        E._reach_config(tmp_path, {"bogus_rog_field": 1, "mode": "rain_on_grid"})


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
