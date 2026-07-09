#!/usr/bin/env python3
"""Tests for the ELMFIRE deck builder (FIRE-2). Synthetic rasters, NO network.

Covers:
  * happy path: 15 co-registered rasters + namelist + manifest from synthetic
    EPSG:4326 sources (exercising the warp onto the EPSG:5070 30 m grid);
  * the same-grid HARD ASSERT fires on a deliberately misaligned raster
    (shifted geotransform, wrong dims, wrong CRS);
  * honest-failure norm: missing input / unreadable input / disjoint
    (no-coverage) input -> typed exceptions, never a defaulted raster;
  * namelist renders with the EXACT tutorial-01 key set the FIRE-1 proven
    container consumed;
  * weather rasters carry the scenario values as Float32;
  * manifest checksums are stable across two identical builds;
  * ignition outside the domain -> typed error;
  * the 10 m m/s -> 20 ft mph wind-units helper (design doc units trap).

Run:
    services/agent/.venv/bin/python -m pytest services/workers/elmfire/tests/
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.crs import CRS
from rasterio.transform import Affine, from_origin

HERE = Path(__file__).resolve().parent

# Import the deck builder by path (sfincs_deckbuilder test pattern) so the
# tests run regardless of how the package lands on sys.path.
_spec = importlib.util.spec_from_file_location(
    "elmfire_deck_builder", HERE.parent / "deck_builder.py"
)
db = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(db)  # type: ignore[union-attr]


# --------------------------------------------------------------------------- #
# Synthetic fixtures.
# --------------------------------------------------------------------------- #

#: Small AOI in the northern Sierra Nevada (CONUS, well inside EPSG:5070).
AOI_BBOX = [-120.60, 39.10, -120.55, 39.14]

#: Per-raster constant values for the synthetic sources (distinct so a
#: swapped file would be caught by value checks).
SRC_VALUES = {
    "fbfm40": 102,
    "cbh": 3,
    "cbd": 12,
    "cc": 40,
    "ch": 150,
    "dem": 500,
    "slp": 5,
    "asp": 180,
}

WEATHER = {
    "ws_mph_20ft": 15.0,
    "wd_deg": 0.0,
    "m1_pct": 3.0,
    "m10_pct": 4.0,
    "m100_pct": 5.0,
}

IGNITION = {"lon": -120.575, "lat": 39.12, "t_ign_s": 0.0}

DURATION_S = 7200.0


def _write_synthetic_source(
    path: Path,
    value: int,
    bbox=None,
    px: float = 0.00027,  # ~30 m in degrees
) -> Path:
    """Write a constant Int16 raster in EPSG:4326 covering ``bbox`` + margin."""
    bbox = bbox or AOI_BBOX
    margin = 0.01
    minx, miny, maxx, maxy = (
        bbox[0] - margin, bbox[1] - margin, bbox[2] + margin, bbox[3] + margin
    )
    width = int(round((maxx - minx) / px))
    height = int(round((maxy - miny) / px))
    transform = from_origin(minx, maxy, px, px)
    arr = np.full((height, width), value, dtype="int16")
    profile = {
        "driver": "GTiff",
        "width": width,
        "height": height,
        "count": 1,
        "dtype": "int16",
        "crs": "EPSG:4326",
        "transform": transform,
        "nodata": -32768,
    }
    with rasterio.open(path, "w", **profile) as ds:
        ds.write(arr, 1)
    return path


@pytest.fixture(scope="module")
def source_rasters(tmp_path_factory) -> dict[str, str]:
    src_dir = tmp_path_factory.mktemp("elmfire_srcs")
    return {
        name: str(_write_synthetic_source(src_dir / f"{name}_src.tif", value))
        for name, value in SRC_VALUES.items()
    }


def _make_spec(source_rasters: dict[str, str], **overrides) -> dict:
    spec = {
        "aoi": {"bbox": list(AOI_BBOX)},
        "ignitions": [dict(IGNITION)],
        "weather": dict(WEATHER),
        "duration_s": DURATION_S,
        "inputs": dict(source_rasters),
    }
    spec.update(overrides)
    return spec


@pytest.fixture(scope="module")
def built_deck(source_rasters, tmp_path_factory):
    """One shared happy-path deck build (module-scoped: builds are not free)."""
    deck_dir = tmp_path_factory.mktemp("deck")
    manifest = db.build_deck(_make_spec(source_rasters), deck_dir)
    return deck_dir, manifest


# --------------------------------------------------------------------------- #
# Happy path: alignment, dtypes, values.
# --------------------------------------------------------------------------- #

ALL_RASTERS = tuple(db.INT_RASTERS) + tuple(db.WEATHER_RASTERS) + ("adj", "phi")


def test_deck_has_all_fifteen_rasters(built_deck) -> None:
    deck_dir, _ = built_deck
    for name in ALL_RASTERS:
        assert (deck_dir / "inputs" / f"{name}.tif").is_file(), name
    assert len(ALL_RASTERS) == 15


def test_every_raster_shares_the_identical_grid(built_deck) -> None:
    """Same geotransform (exact float equality), CRS and dims on all 15."""
    deck_dir, manifest = built_deck
    grid = manifest["grid"]
    expected_transform = tuple(grid["transform"])
    expected_crs = CRS.from_epsg(grid["epsg"])
    for name in ALL_RASTERS:
        with rasterio.open(deck_dir / "inputs" / f"{name}.tif") as ds:
            assert tuple(ds.transform)[:6] == expected_transform, name
            assert ds.crs == expected_crs, name
            assert (ds.width, ds.height) == (grid["nx"], grid["ny"]), name


def test_grid_is_epsg5070_30m_snapped(built_deck) -> None:
    _, manifest = built_deck
    grid = manifest["grid"]
    assert grid["epsg"] == 5070
    assert grid["cellsize_m"] == 30.0
    # Corners snapped to whole 30 m multiples -> deterministic registration.
    assert grid["xll"] % 30.0 == 0.0
    assert grid["yll"] % 30.0 == 0.0
    assert grid["nx"] > 1 and grid["ny"] > 1


def test_fuel_rasters_are_int16_with_source_values(built_deck) -> None:
    deck_dir, _ = built_deck
    for name, value in SRC_VALUES.items():
        with rasterio.open(deck_dir / "inputs" / f"{name}.tif") as ds:
            assert ds.dtypes[0] == "int16", name
            assert ds.nodata == db.NODATA, name
            arr = ds.read(1)
            valid = arr[arr != db.NODATA]
            assert valid.size > 0, name
            assert (valid == value).all(), (
                f"{name}: expected constant {value}, got {np.unique(valid)}"
            )


def test_weather_rasters_are_float32_with_scenario_values(built_deck) -> None:
    deck_dir, _ = built_deck
    expected = {
        "ws": WEATHER["ws_mph_20ft"],
        "wd": WEATHER["wd_deg"],
        "m1": WEATHER["m1_pct"],
        "m10": WEATHER["m10_pct"],
        "m100": WEATHER["m100_pct"],
        "adj": db.ADJ_VALUE,
        "phi": db.PHI_VALUE,
    }
    for name, value in expected.items():
        with rasterio.open(deck_dir / "inputs" / f"{name}.tif") as ds:
            assert ds.dtypes[0] == "float32", name
            arr = ds.read(1)
            assert (arr == np.float32(value)).all(), (
                f"{name}: expected constant {value}, got {np.unique(arr)}"
            )


# --------------------------------------------------------------------------- #
# The same-grid HARD ASSERT fires on misalignment.
# --------------------------------------------------------------------------- #


def _rewrite_raster(path: Path, transform=None, crs=None, shape=None) -> None:
    with rasterio.open(path) as ds:
        arr = ds.read(1)
        profile = dict(ds.profile)
    if transform is not None:
        profile["transform"] = transform
    if crs is not None:
        profile["crs"] = crs
    if shape is not None:
        arr = arr[: shape[0], : shape[1]]
        profile["height"], profile["width"] = arr.shape
    with rasterio.open(path, "w", **profile) as ds:
        ds.write(arr, 1)


@pytest.mark.parametrize("corruption", ["shifted_transform", "wrong_crs", "wrong_dims"])
def test_grid_mismatch_assert_fires(source_rasters, tmp_path, corruption) -> None:
    """A deliberately misaligned raster trips ElmfireGridMismatchError."""
    deck_dir = tmp_path / "deck"
    manifest = db.build_deck(_make_spec(source_rasters), deck_dir)
    grid = manifest["grid"]
    victim = deck_dir / "inputs" / "cc.tif"
    if corruption == "shifted_transform":
        t = Affine(*grid["transform"])
        # Half-cell shift: the classic silent co-registration bug.
        _rewrite_raster(victim, transform=t * Affine.translation(0.5, 0.5))
    elif corruption == "wrong_crs":
        _rewrite_raster(victim, crs="EPSG:32610")
    else:
        _rewrite_raster(victim, shape=(grid["ny"] - 1, grid["nx"] - 1))
    with pytest.raises(db.ElmfireGridMismatchError, match="cc.tif"):
        db.verify_deck_grid(deck_dir / "inputs", grid)


def test_verify_deck_grid_passes_on_clean_deck(built_deck) -> None:
    deck_dir, manifest = built_deck
    verified = db.verify_deck_grid(deck_dir / "inputs", manifest["grid"])
    assert len(verified) == 15


# --------------------------------------------------------------------------- #
# Honest-failure norm: typed errors, never a defaulted raster.
# --------------------------------------------------------------------------- #


def test_missing_input_raises_typed_error(source_rasters, tmp_path) -> None:
    inputs = dict(source_rasters)
    inputs["cbh"] = str(tmp_path / "does_not_exist.tif")
    with pytest.raises(db.ElmfireInputMissingError, match="cbh"):
        db.build_deck(_make_spec(source_rasters, inputs=inputs), tmp_path / "deck")


def test_unreadable_input_raises_typed_error(source_rasters, tmp_path) -> None:
    bogus = tmp_path / "bogus.tif"
    bogus.write_text("this is not a raster")
    inputs = dict(source_rasters)
    inputs["dem"] = str(bogus)
    with pytest.raises(db.ElmfireInputUnreadableError, match="dem"):
        db.build_deck(_make_spec(source_rasters, inputs=inputs), tmp_path / "deck")


def test_disjoint_input_raises_coverage_error(source_rasters, tmp_path) -> None:
    """An input raster that does not overlap the AOI is a typed coverage error."""
    far_away = _write_synthetic_source(
        tmp_path / "far.tif", 102, bbox=[-90.0, 30.0, -89.95, 30.04]
    )
    inputs = dict(source_rasters)
    inputs["fbfm40"] = str(far_away)
    with pytest.raises(db.ElmfireCoverageError, match="fbfm40"):
        db.build_deck(_make_spec(source_rasters, inputs=inputs), tmp_path / "deck")


def test_missing_spec_field_raises_spec_error(source_rasters, tmp_path) -> None:
    spec = _make_spec(source_rasters)
    del spec["weather"]["m100_pct"]
    with pytest.raises(db.ElmfireSpecError, match="m100_pct"):
        db.build_deck(spec, tmp_path / "deck")


def test_ignition_outside_domain_raises_typed_error(source_rasters, tmp_path) -> None:
    spec = _make_spec(
        source_rasters, ignitions=[{"lon": -121.5, "lat": 40.0, "t_ign_s": 0.0}]
    )
    with pytest.raises(db.ElmfireIgnitionError, match="outside the domain"):
        db.build_deck(spec, tmp_path / "deck")


# --------------------------------------------------------------------------- #
# Namelist: EXACT tutorial-01 key set.
# --------------------------------------------------------------------------- #

#: Every namelist key the tutorial-01 elmfire.data.in carries (the deck the
#: FIRE-1 proven container consumed).
TUTORIAL_01_KEYS = [
    "FUELS_AND_TOPOGRAPHY_DIRECTORY",
    "ASP_FILENAME", "CBD_FILENAME", "CBH_FILENAME", "CC_FILENAME",
    "CH_FILENAME", "DEM_FILENAME", "FBFM_FILENAME", "SLP_FILENAME",
    "ADJ_FILENAME", "PHI_FILENAME",
    "DT_METEOROLOGY", "WEATHER_DIRECTORY",
    "WS_FILENAME", "WD_FILENAME", "M1_FILENAME", "M10_FILENAME",
    "M100_FILENAME",
    "LH_MOISTURE_CONTENT", "LW_MOISTURE_CONTENT",
    "OUTPUTS_DIRECTORY", "DTDUMP", "DUMP_FLIN", "DUMP_SPREAD_RATE",
    "DUMP_TIME_OF_ARRIVAL", "CONVERT_TO_GEOTIFF",
    "A_SRS", "COMPUTATIONAL_DOMAIN_CELLSIZE",
    "COMPUTATIONAL_DOMAIN_XLLCORNER", "COMPUTATIONAL_DOMAIN_YLLCORNER",
    "SIMULATION_DT", "SIMULATION_TSTOP",
    "NUM_IGNITIONS", "X_IGN(1)", "Y_IGN(1)", "T_IGN(1)",
    "WX_BILINEAR_INTERPOLATION", "WSMFEFF_LOW_MULT",
    "PATH_TO_GDAL", "SCRATCH",
]

TUTORIAL_01_GROUPS = [
    "&INPUTS", "&OUTPUTS", "&COMPUTATIONAL_DOMAIN", "&TIME_CONTROL",
    "&SIMULATOR", "&MISCELLANEOUS",
]


def test_namelist_contains_exact_tutorial_key_set(built_deck) -> None:
    deck_dir, _ = built_deck
    text = (deck_dir / "inputs" / "elmfire.data").read_text()
    for group in TUTORIAL_01_GROUPS:
        assert group in text, f"missing namelist group {group}"
    for key in TUTORIAL_01_KEYS:
        pattern = re.escape(key) + r"\s*="
        assert re.search(pattern, text), f"missing namelist key {key}"


def test_namelist_values_match_spec_and_grid(built_deck) -> None:
    deck_dir, manifest = built_deck
    grid = manifest["grid"]
    text = (deck_dir / "inputs" / "elmfire.data").read_text()

    def _val(key: str) -> str:
        m = re.search(re.escape(key) + r"\s*=\s*(.+)", text)
        assert m, key
        return m.group(1).strip()

    assert _val("A_SRS") == f"'EPSG: {grid['epsg']}'"
    assert float(_val("COMPUTATIONAL_DOMAIN_CELLSIZE")) == grid["cellsize_m"]
    assert float(_val("COMPUTATIONAL_DOMAIN_XLLCORNER")) == grid["xll"]
    assert float(_val("COMPUTATIONAL_DOMAIN_YLLCORNER")) == grid["yll"]
    assert float(_val("SIMULATION_TSTOP")) == DURATION_S
    assert int(_val("NUM_IGNITIONS")) == 1
    assert _val("CONVERT_TO_GEOTIFF") == ".FALSE."
    assert float(_val("LH_MOISTURE_CONTENT")) == 30.0
    assert float(_val("LW_MOISTURE_CONTENT")) == 60.0
    # Ignition coordinates are ABSOLUTE projected domain coords, in-domain.
    x_ign = float(_val("X_IGN(1)"))
    y_ign = float(_val("Y_IGN(1)"))
    assert grid["xll"] <= x_ign <= grid["xll"] + grid["nx"] * grid["cellsize_m"]
    assert grid["yll"] <= y_ign <= grid["yll"] + grid["ny"] * grid["cellsize_m"]


# --------------------------------------------------------------------------- #
# Manifest: checksums stable + complete.
# --------------------------------------------------------------------------- #


def test_manifest_covers_all_deck_files(built_deck) -> None:
    deck_dir, manifest = built_deck
    assert manifest["schema"] == db.MANIFEST_SCHEMA
    # 15 rasters + elmfire.data
    assert len(manifest["files"]) == 16
    assert "inputs/elmfire.data" in manifest["files"]
    for name in ALL_RASTERS:
        assert f"inputs/{name}.tif" in manifest["files"]
    for sha in manifest["files"].values():
        assert re.fullmatch(r"[0-9a-f]{64}", sha)
    # Manifest is also written to disk and round-trips.
    on_disk = json.loads((deck_dir / "deck_manifest.json").read_text())
    assert on_disk["files"] == manifest["files"]


def test_manifest_checksums_stable_across_rebuilds(source_rasters, tmp_path) -> None:
    """Two identical builds produce byte-identical deck files (golden-deck diff)."""
    m1 = db.build_deck(_make_spec(source_rasters), tmp_path / "deck_a")
    m2 = db.build_deck(_make_spec(source_rasters), tmp_path / "deck_b")
    assert m1["files"] == m2["files"]
    assert m1["grid"] == m2["grid"]


# --------------------------------------------------------------------------- #
# Units trap: 10 m m/s -> 20 ft mph.
# --------------------------------------------------------------------------- #


def test_wind_conversion_10m_ms_to_20ft_mph() -> None:
    # 10 m/s at 10 m -> 10 * 2.236936 * 0.87 = 19.461 mph at 20 ft.
    assert db.wind_10m_ms_to_20ft_mph(10.0) == pytest.approx(19.4613, abs=1e-3)
    assert db.wind_10m_ms_to_20ft_mph(0.0) == 0.0


# --------------------------------------------------------------------------- #
# Spec-validation edges.
# --------------------------------------------------------------------------- #


def test_too_many_ignitions_rejected(source_rasters) -> None:
    many = [dict(IGNITION) for _ in range(db.MAX_IGNITIONS + 1)]
    with pytest.raises(db.ElmfireSpecError, match="at most"):
        db.validate_deck_spec(_make_spec(source_rasters, ignitions=many))


def test_non_s3_uri_scheme_rejected(source_rasters, tmp_path) -> None:
    inputs = dict(source_rasters)
    inputs["asp"] = "https://example.com/asp.tif"
    with pytest.raises(db.ElmfireSpecError, match="unsupported URI scheme"):
        db.build_deck(_make_spec(source_rasters, inputs=inputs), tmp_path / "deck")
