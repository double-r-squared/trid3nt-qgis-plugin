"""Offline tests for the Malpasset observation-layer builder.

Hand-checks builder output against known observations.json values (inline
fixture -- self-contained, no case data required) and, when the real
observations.json is present, an integration pass over the full 17/3/9 set. No
network, no S3.
"""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import pytest

from trid3nt_server.cases import malpasset_obs as M


# A minimal, self-contained slice of observations.json (values transcribed
# VERBATIM from data/cases/malpasset/observations.json -- P1/P2/P17, one
# transformer, one gauge, plus a null-value row to exercise honest null-keeping).
FIXTURE = {
    "vertical_reference": {
        "quantity": "water_surface_elevation",
        "units": "m",
        "datum_note": "elevations in metres above NGF (Nivellement General de la France).",
    },
    "police_survey_points": {
        "count": 4,
        "points": [
            {"id": "P1", "x_m": 4913.1, "y_m": 4244.0, "bank": "Right", "ws_obs_m": 79.15},
            {"id": "P2", "x_m": 5159.7, "y_m": 4369.6, "bank": "Left", "ws_obs_m": 87.20},
            {"id": "P17", "x_m": 12333.7, "y_m": 2269.7, "bank": "Right", "ws_obs_m": 14.00},
            {"id": "PX", "x_m": 6000.0, "y_m": 3000.0, "bank": "Right", "ws_obs_m": None},
        ],
    },
    "transformers": {
        "count": 1,
        "points": [
            {"id": "B", "x_m": 11900.0, "y_m": 3250.0, "at_obs_s": 1240, "at_obs_s_alt_tuflow": 1204},
        ],
    },
    "physical_model_gauges": {
        "count": 1,
        "points": [
            {"id": "G6", "x_m": 4947.4, "y_m": 4289.7, "at_lab_s": 10.2, "ws_lab_m": 84.2},
        ],
    },
}

REAL_OBS = Path("data/cases/malpasset/observations.json")


def test_police_gdf_schema_and_values():
    gdf = M.build_police_gdf(FIXTURE)
    assert list(gdf["obs_id"]) == ["P1", "P2", "P17", "PX"]
    assert gdf.crs is not None and gdf.crs.to_epsg() == M.MALPASSET_MESH_EPSG
    # quantity + datum + units stamps on every feature.
    assert set(gdf["quantity"]) == {"water_surface_elevation"}
    assert set(gdf["vertical_datum"]) == {"NGF"}
    assert set(gdf["units"]) == {"m"}
    # observed value lands in elev_m (the pairing tool's auto-detected field).
    row = gdf[gdf["obs_id"] == "P1"].iloc[0]
    assert row["elev_m"] == pytest.approx(79.15)
    assert row["ws_obs_m"] == pytest.approx(79.15)
    assert row["bank"] == "Right"
    # geometry is the RAW local-frame metre coordinate (no reprojection).
    assert row.geometry.x == pytest.approx(4913.1)
    assert row.geometry.y == pytest.approx(4244.0)
    p17 = gdf[gdf["obs_id"] == "P17"].iloc[0]
    assert p17["elev_m"] == pytest.approx(14.00)
    assert p17.geometry.x == pytest.approx(12333.7)


def test_police_null_value_kept_not_fabricated():
    gdf = M.build_police_gdf(FIXTURE)
    px = gdf[gdf["obs_id"] == "PX"].iloc[0]
    # an unreported ws_obs_m stays null, never guessed.
    assert px["elev_m"] is None or (isinstance(px["elev_m"], float) and px["elev_m"] != px["elev_m"])


def test_transformer_gdf_schema():
    gdf = M.build_transformer_gdf(FIXTURE)
    assert list(gdf["obs_id"]) == ["B"]
    assert set(gdf["quantity"]) == {"wave_arrival_time"}
    row = gdf.iloc[0]
    assert row["at_obs_s"] == pytest.approx(1240.0)
    assert row["at_obs_s_alt_tuflow"] == pytest.approx(1204.0)
    assert row.geometry.x == pytest.approx(11900.0)


def test_gauge_gdf_schema():
    gdf = M.build_gauge_gdf(FIXTURE)
    assert list(gdf["obs_id"]) == ["G6"]
    assert set(gdf["quantity"]) == {"water_surface_elevation"}
    row = gdf.iloc[0]
    assert row["elev_m"] == pytest.approx(84.2)
    assert row["ws_lab_m"] == pytest.approx(84.2)
    assert row["at_lab_s"] == pytest.approx(10.2)


def test_build_all_writes_fgbs(tmp_path):
    summary = M.build_malpasset_obs_layers(FIXTURE, tmp_path)
    assert summary["n_police"] == 4
    assert summary["n_transformers"] == 1
    assert summary["n_gauges"] == 1
    assert summary["transformer_scored"] is False
    assert "crs_caveat" in summary and "placeholder" in summary["crs_caveat"].lower()
    police = gpd.read_file(summary["police_fgb"])
    assert len(police) == 4
    assert police.crs.to_epsg() == M.MALPASSET_MESH_EPSG
    # round-trip preserves the quantity stamp the pairing tool keys off.
    assert set(police["quantity"]) == {"water_surface_elevation"}


@pytest.mark.skipif(not REAL_OBS.is_file(), reason="real observations.json not present")
def test_real_observations_full_set(tmp_path):
    summary = M.build_malpasset_obs_layers(REAL_OBS, tmp_path)
    assert summary["n_police"] == 17
    assert summary["n_transformers"] == 3
    assert summary["n_gauges"] == 9
    police = gpd.read_file(summary["police_fgb"])
    # hand-checked anchors from observations.json (Biscarini 2016 Table 3).
    p1 = police[police["obs_id"] == "P1"].iloc[0]
    assert p1["elev_m"] == pytest.approx(79.15)
    assert p1.geometry.x == pytest.approx(4913.1)
    p17 = police[police["obs_id"] == "P17"].iloc[0]
    assert p17["elev_m"] == pytest.approx(14.00)
    # monotone decrease down-valley (elevations, not depths).
    assert p1["elev_m"] > p17["elev_m"]
