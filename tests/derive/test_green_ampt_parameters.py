"""``green_ampt_parameters`` over recorded SoilGrids rasters, offline.

Cells are hand-picked from the recorded windows (row, column, sand/silt/clay percent read off the files) and each
class and parameter is checked against the printed row of Table 2 of Rawls, Brakensiek and Miller (1983), J. Hydraulic
Engineering 109(1): 62-70, with the class cut by the USDA Soil Survey Manual (2017) chapter 3 definitions. The
class-boundary, silt and refusal tests use hand-made fractions and are banner-labeled SYNTHETIC: they verify the
mapping only and are never a product.
"""

from __future__ import annotations

import asyncio
import pathlib

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from trid3nt_contracts.execution import LayerURI
from trid3nt_server.tools.derive import DeriveError
from trid3nt_server.tools.derive.green_ampt_parameters.green_ampt_parameters import (
    CLASSES, NODATA, _NAMES, classes, derived, green_ampt_parameters)

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
    return LayerURI(layer_id=f"{window}-{fraction}", name=fraction, layer_type="raster", units="percent",
                    uri=str(_FIXTURES / f"{window}_{fraction}.tif"))


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


# SYNTHETIC - a silt cell has no row in the source table, so it stays nodata and is counted on the notes.
def test_a_silt_cell_stays_nodata(tmp_path):
    from trid3nt_server.inputs.run_notes import bind_notes, drain_notes
    token = bind_notes()
    try:
        grids = {}
        for name, value in (("sand", [[5.0, 40.0]]), ("silt", [[90.0, 40.0]]), ("clay", [[5.0, 20.0]])):
            path = tmp_path / f"{name}.tif"
            with rasterio.open(path, "w", driver="GTiff", height=1, width=2, count=1, dtype="float32", crs="EPSG:4326",
                               transform=from_origin(0, 1, 0.5, 1), nodata=-9999.0) as dst:
                dst.write(np.array(value, dtype="float32"), 1)
            grids[name] = LayerURI(layer_id=name, name=name, layer_type="raster", units="percent", uri=str(path))
        out = derived(grids["sand"], grids["silt"], grids["clay"], _output_dir=str(tmp_path))
    finally:
        notes = drain_notes(token)
    values = rasterio.open(out[0].uri).read(1)
    assert values[0, 0] == NODATA and values[0, 1] == pytest.approx(0.34)
    assert any("1 cells are the texture class silt" in n for n in notes)
