"""``green_ampt_parameters`` over recorded SoilGrids rasters, offline.

Cells are hand-picked from the recorded windows (row, column, sand/silt/clay percent read off the files) and each
class and parameter is checked against the printed row of Table 2 of Rawls, Brakensiek and Miller (1983), J. Hydraulic
Engineering 109(1): 62-70, with the class cut by the USDA Soil Survey Manual (2017) chapter 3 definitions on the
fractions as given. A silt cell is checked against Saxton and Rawls (2006) Table 3's printed silt row and the 1983 paper's
Eqs. 4 and 6 worked by hand. The class-boundary, silt and refusal tests use hand-made fractions and are banner-labeled
SYNTHETIC: they verify the mapping only and are never a product.
"""

from __future__ import annotations

import asyncio
import pathlib

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from trid3nt_contracts.execution import LayerURI
from trid3nt.tools.derive import DeriveError
from trid3nt.tools.derive.green_ampt_parameters.green_ampt_parameters import (
    CLASSES, NODATA, _NAMES, classes, derived, green_ampt_parameters, saxton_rawls)

_FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "green_ampt"

#: (window, row, column, sand, silt, clay as read off the recorded files, class, K cm/h, psi_f cm, porosity, effective
#: porosity) - the last four typed from the printed Table 2 row of the class.
_CELLS = [
    ("florida", 24, 9, 89.9, 5.2, 4.9, "sand", 11.78, 4.95, 0.437, 0.417),
    ("florida", 8, 14, 85.2, 6.0, 8.8, "loamy sand", 2.99, 6.13, 0.437, 0.401),
    ("florida", 3, 28, 74.2, 11.2, 14.6, "sandy loam", 1.09, 11.01, 0.453, 0.412),
    ("gulf", 4, 12, 48.0, 29.7, 22.3, "loam", 0.34, 8.89, 0.463, 0.434),
    ("baton_rouge", 27, 36, 6.5, 67.4, 26.0, "silt loam", 0.65, 16.68, 0.501, 0.486),
    ("florida", 1, 30, 59.5, 17.6, 22.9, "sandy clay loam", 0.15, 21.85, 0.398, 0.330),
    ("desmoines", 23, 21, 22.0, 44.3, 33.8, "clay loam", 0.10, 20.88, 0.464, 0.309),
    ("baton_rouge", 19, 15, 4.7, 61.8, 33.5, "silty clay loam", 0.10, 27.30, 0.471, 0.432),
    ("baton_rouge", 22, 22, 6.3, 52.5, 41.2, "silty clay", 0.05, 29.22, 0.479, 0.423),
]


def _layer(window: str, fraction: str) -> LayerURI:
    return LayerURI(layer_id=f"{window}-{fraction}", name=fraction, layer_type="raster",
                    units="g/kg" if fraction == "soc" else "percent", uri=str(_FIXTURES / f"{window}_{fraction}.tif"))


def _run(tmp_path, window: str) -> dict[str, np.ndarray]:
    layers = derived(*(_layer(window, f) for f in ("sand", "silt", "clay")), _output_dir=str(tmp_path))
    assert [layer.units for layer in layers] == ["cm/h", "cm", "cm3/cm3", "cm3/cm3"]
    return {layer.quantity: rasterio.open(layer.uri).read(1) for layer in layers}


@pytest.mark.parametrize("window,row,col,sand,silt,clay,name,k,psi,phi,phi_e", _CELLS)
def test_a_recorded_cell_takes_its_class_and_the_published_row(tmp_path, window, row, col, sand, silt, clay, name, k, psi,
                                                                phi, phi_e):
    source = [rasterio.open(_FIXTURES / f"{window}_{f}.tif").read(1)[row, col] for f in ("sand", "silt", "clay")]
    assert [round(float(v), 1) for v in source] == [sand, silt, clay]
    out = _run(tmp_path, window)
    assert out["green_ampt_hydraulic_conductivity"][row, col] == pytest.approx(k)
    assert out["green_ampt_wetting_front_suction"][row, col] == pytest.approx(psi)
    assert out["green_ampt_porosity"][row, col] == pytest.approx(phi)
    assert out["green_ampt_effective_porosity"][row, col] == pytest.approx(phi_e)


def test_cells_with_no_texture_stay_nodata(tmp_path):
    # Every textured cell of the recorded window falls in exactly one class on its fractions as given.
    out = _run(tmp_path, "baton_rouge")
    sand = rasterio.open(_FIXTURES / "baton_rouge_sand.tif").read(1)
    assert (sand == -9999).sum() == 40 * 40 - 1129
    for raster in out.values():
        assert ((raster == NODATA) == (sand == -9999)).all()


def test_the_derived_rasters_are_on_the_inputs_grid(tmp_path):
    layers = derived(*(_layer("gulf", f) for f in ("sand", "silt", "clay")), _output_dir=str(tmp_path))
    with rasterio.open(_FIXTURES / "gulf_sand.tif") as source, rasterio.open(layers[0].uri) as made:
        assert (made.shape, made.transform, made.crs) == (source.shape, source.transform, source.crs)
        assert made.nodata == NODATA and made.dtypes == ("float32",)
    west, south, east, north = layers[0].bbox
    assert (west, south, east, north) == pytest.approx((-97.3, 29.0, -97.2, 29.1), abs=1e-6)


def test_the_tool_returns_the_four_layers_and_names_its_source():
    layers = asyncio.run(green_ampt_parameters(
        sand=_layer("gulf", "sand"), silt=_layer("gulf", "silt"), clay=_layer("gulf", "clay")))
    assert [layer.quantity for layer in layers] == [
        "green_ampt_hydraulic_conductivity", "green_ampt_wetting_front_suction", "green_ampt_porosity",
        "green_ampt_effective_porosity"]
    assert "Rawls, Brakensiek and Miller (1983)" in layers[0].notes[0]


def test_a_missing_fraction_refuses_naming_it():
    with pytest.raises(DeriveError) as raised:
        derived(_layer("gulf", "sand"), None, _layer("gulf", "clay"))
    assert raised.value.error_code == "GREEN_AMPT_FRACTION_MISSING" and "silt" in str(raised.value)


def test_rasters_on_different_grids_refuse():
    with pytest.raises(DeriveError) as raised:
        derived(_layer("gulf", "sand"), _layer("florida", "silt"), _layer("gulf", "clay"))
    assert raised.value.error_code == "GREEN_AMPT_GRID_MISMATCH"


def test_a_raster_stating_other_units_refuses():
    fraction = _layer("gulf", "sand").model_copy(update={"units": "g/kg"})
    with pytest.raises(DeriveError) as raised:
        derived(fraction, _layer("gulf", "silt"), _layer("gulf", "clay"))
    assert raised.value.error_code == "GREEN_AMPT_UNITS"


def test_the_table_has_the_published_eleven_classes():
    assert [n for n in _NAMES if n not in CLASSES] == ["silt"] and len(CLASSES) == 11


# SYNTHETIC - hand-made fractions: verifies the class cut only, never a product.
def test_every_whole_percent_triple_is_one_class():
    triples = np.array([(s, t, 100 - s - t) for s in range(101) for t in range(101 - s)])
    found = classes(triples[:, 0], triples[:, 1], triples[:, 2])
    assert (found >= 0).all()
    assert {_NAMES[i] for i in set(found.tolist())} == set(_NAMES)


# SYNTHETIC - the manual's corner definitions for the classes the recorded windows do not reach.
@pytest.mark.parametrize("sand,silt,clay,name", [
    (20, 40, 40, "silty clay"), (30, 20, 50, "clay"), (50, 10, 40, "sandy clay"), (5, 90, 5, "silt"),
    (80, 10, 10, "sandy loam"), (90, 5, 5, "sand"), (40, 40, 20, "loam")])
def test_the_manuals_classes(sand, silt, clay, name):
    assert _NAMES[int(classes(np.array([sand]), np.array([silt]), np.array([clay]))[0])] == name


def _grids(tmp_path, cells: dict[str, list[float]], units: dict[str, str] | None = None) -> dict[str, LayerURI]:
    grids = {}
    for name, value in cells.items():
        path = tmp_path / f"{name}.tif"
        with rasterio.open(path, "w", driver="GTiff", height=1, width=len(value), count=1, dtype="float32",
                           crs="EPSG:4326", transform=from_origin(0, 1, 0.5, 1), nodata=-9999.0) as dst:
            dst.write(np.array([value], dtype="float32"), 1)
        grids[name] = LayerURI(layer_id=name, name=name, layer_type="raster", uri=str(path),
                               units=(units or {}).get(name, "percent"))
    return grids


def _derived_with_notes(tmp_path, grids: dict[str, LayerURI]) -> tuple[dict[str, np.ndarray], list[str], list[LayerURI]]:
    from trid3nt.inputs.run_notes import bind_notes, drain_notes
    token = bind_notes()
    try:
        out = derived(grids["sand"], grids["silt"], grids["clay"], grids.get("soc"), _output_dir=str(tmp_path))
    finally:
        notes = drain_notes(token)
    return {layer.quantity: rasterio.open(layer.uri).read(1) for layer in out}, notes, out


# SYNTHETIC - a cell on the loam / sandy clay loam / clay loam corner whose fractions sum to 99.8 is in no class
# (silt 27.9 < 28 is not loam, sand 45.0 is not > 45 for sandy clay loam, clay 26.9 < 27 is not clay loam, silt + 2 clay
# >= 30 with clay >= 20 is not sandy loam): it stays nodata and is counted, its neighbour taking the loam row.
def test_a_cell_in_no_class_on_its_raw_fractions_stays_nodata(tmp_path):
    grids = _grids(tmp_path, {"sand": [45.0, 45.0], "silt": [27.9, 28.0], "clay": [26.9, 26.9]})
    out, notes, _ = _derived_with_notes(tmp_path, grids)
    assert out["green_ampt_hydraulic_conductivity"][0, 0] == NODATA
    assert out["green_ampt_hydraulic_conductivity"][0, 1] == pytest.approx(0.34)
    assert any(n.startswith("1 cells fall in no USDA texture class") for n in notes)


# SYNTHETIC - fractions far from summing to 100 (46 + 40 + 40) meet both sandy clay (clay >= 35, sand > 45) and silty
# clay (clay >= 40, silt >= 40): such a cell takes neither.
def test_a_cell_in_two_classes_takes_neither():
    assert int(classes(np.array([46.0]), np.array([40.0]), np.array([40.0]))[0]) == -2


# Saxton and Rawls (2006) Table 3, the printed silt row: sand 10, clay 5 %w at 2.5 %w organic matter -> wilting point
# 6, field capacity 30, saturation 48 %v, Ks 22.0 mm/h. Read from the article (SSSAJ 70: 1577).
def test_the_saxton_rawls_equations_reproduce_the_printed_silt_row():
    ks, theta_s, _psi_e, _lam = saxton_rawls(0.10, 0.05, 2.5)
    assert float(ks) == pytest.approx(22.0, abs=0.05) and float(theta_s) == pytest.approx(0.48, abs=0.005)


# SYNTHETIC fractions (sand 10, silt 85, clay 5: the class silt) with organic carbon 14.5 g/kg, i.e. organic matter
# 1.45 % x 1.724 = 2.50 %. By hand from Table 1 (S 0.10, C 0.05, OM 2.5): theta1500t 0.067915 -> theta1500 0.057423;
# theta33t 0.311535 -> theta33 0.304541; theta(S-33)t 0.151705 -> theta(S-33) 0.141189; psi_et 13.1561 -> psi_e
# 14.4311 kPa; theta_S = 0.304541 + 0.141189 - 0.0097 + 0.043 = 0.479031 (Eq. 5); B = ln(1500/33)/ln(0.304541/0.057423)
# = 2.28770, lambda 0.437120; Ks = 1930 x (0.479031 - 0.304541)^(3 - 0.437120) = 21.994 mm/h. Rawls et al. (1983) Eq. 6:
# K = 21.994/2 mm/h = 1.0997 cm/h; Eq. 4 with psi_b = 14.4311 kPa = 147.157 cm of water: (2 x 0.437120 + 3)/
# (2 x 0.437120 + 2) x 147.157/2 = 99.18 cm.
def test_a_silt_cell_takes_saxton_and_rawls(tmp_path):
    grids = _grids(tmp_path, {"sand": [10.0, 40.0], "silt": [85.0, 40.0], "clay": [5.0, 20.0], "soc": [2.5 / 1.724 * 10, 9.0]},
                   units={"soc": "g/kg"})
    out, notes, layers = _derived_with_notes(tmp_path, grids)
    assert out["green_ampt_hydraulic_conductivity"][0, 0] == pytest.approx(1.0997, abs=1e-4)
    assert out["green_ampt_wetting_front_suction"][0, 0] == pytest.approx(99.18, abs=0.01)
    assert out["green_ampt_porosity"][0, 0] == pytest.approx(0.47903, abs=1e-5)
    assert out["green_ampt_effective_porosity"][0, 0] == NODATA
    assert out["green_ampt_hydraulic_conductivity"][0, 1] == pytest.approx(0.34)
    assert any(n.startswith("1 cells are the texture class silt") and "Saxton and Rawls (2006)" in n for n in notes)
    assert "Saxton and Rawls (2006)" in layers[0].notes[1]


# SYNTHETIC - a silt cell with no organic carbon raster given stays nodata, counted on the notes.
def test_a_silt_cell_without_organic_carbon_stays_nodata(tmp_path):
    grids = _grids(tmp_path, {"sand": [5.0, 40.0], "silt": [90.0, 40.0], "clay": [5.0, 20.0]})
    out, notes, _ = _derived_with_notes(tmp_path, grids)
    assert out["green_ampt_hydraulic_conductivity"][0, 0] == NODATA
    assert out["green_ampt_hydraulic_conductivity"][0, 1] == pytest.approx(0.34)
    assert any("no organic carbon raster was given" in n for n in notes)


# SYNTHETIC - organic matter past the 8 %w Saxton and Rawls fitted to (60 g/kg carbon = 10.3 %) leaves the silt cell nodata.
def test_a_silt_cell_past_the_fitted_organic_matter_stays_nodata(tmp_path):
    grids = _grids(tmp_path, {"sand": [5.0], "silt": [90.0], "clay": [5.0], "soc": [60.0]}, units={"soc": "g/kg"})
    out, notes, _ = _derived_with_notes(tmp_path, grids)
    assert out["green_ampt_hydraulic_conductivity"][0, 0] == NODATA
    assert any("above the 8 %w" in n for n in notes)
