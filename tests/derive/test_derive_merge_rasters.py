"""Unit tests for ``derive_merge_rasters``.

Covered: the primary winning every cell it measured and the fallback filling the
rest, the merged grid taking the finer cell over the union of both, the sidecar
naming which input painted each cell, an absent row passing the other surface
through, two different vertical datums refusing by name, an unstated datum
refusing, two disjoint surfaces, and the registration."""

from __future__ import annotations

import os
import tempfile

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from trid3nt_contracts.execution import LayerURI
from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.derive.derive_merge_rasters.derive_merge_rasters import (
    MergeRastersError,
    derive_merge_rasters,
)

_NODATA = float("nan")


def _write(values: np.ndarray, *, west: float, north: float, cell: float) -> str:
    handle, path = tempfile.mkstemp(suffix=".tif", prefix="trid3nt_merge_")
    os.close(handle)
    with rasterio.open(
        path, "w", driver="GTiff", height=values.shape[0], width=values.shape[1],
        count=1, dtype="float32", crs="EPSG:32610", nodata=_NODATA,
        transform=from_origin(west, north, cell, cell),
    ) as destination:
        destination.write(values.astype("float32"), 1)
    return path


def _layer(path: str, datum: str | None) -> LayerURI:
    return LayerURI(layer_id="x", name=os.path.basename(path), layer_type="raster",
                    uri=path, vertical_datum=datum)


def _survey(datum: str | None = "NAVD88") -> LayerURI:
    """A 1 m measurement over the middle 4 m of the terrain below."""
    return _layer(_write(np.full((4, 4), -5.0), west=500_004.0, north=4_000_000.0,
                         cell=1.0), datum)


def _terrain(datum: str | None = "NAVD88") -> LayerURI:
    """A 4 m surface over 16 m of ground, the survey inside it."""
    return _layer(_write(np.full((4, 4), 10.0), west=500_000.0, north=4_000_000.0,
                         cell=4.0), datum)


def test_registered() -> None:
    assert "derive_merge_rasters" in TOOL_REGISTRY


def test_the_primary_wins_where_it_measured_and_the_fallback_fills_the_rest(
        tmp_path) -> None:
    merged = derive_merge_rasters(primary=_survey(), fallback=_terrain(),
                                  _output_dir=str(tmp_path))
    with rasterio.open(merged.uri) as src:
        values = src.read(1)
        # The finer input sets the cell, over the union of both footprints.
        assert src.res == pytest.approx((1.0, 1.0))
        assert (src.width, src.height) == (16, 16)
    assert float(np.nanmin(values)) == pytest.approx(-5.0)
    assert float(np.nanmax(values)) == pytest.approx(10.0)
    assert merged.primary_fraction == pytest.approx(16 / 256.0, abs=1e-3)
    assert merged.fallback_fraction + merged.primary_fraction == pytest.approx(1.0)
    assert merged.resolution_m == pytest.approx(1.0)


def test_the_sidecar_says_which_input_painted_each_cell(tmp_path) -> None:
    merged = derive_merge_rasters(primary=_survey(), fallback=_terrain(),
                                  _output_dir=str(tmp_path))
    with rasterio.open(merged.uri) as surface, \
            rasterio.open(merged.provenance_uri) as source:
        values, won = surface.read(1), source.read(1)
        assert won.shape == values.shape
    assert set(np.unique(won).tolist()) == {0, 1}
    assert float(values[won == 0].max()) == pytest.approx(-5.0)
    assert float(values[won == 1].min()) == pytest.approx(10.0)


def test_an_absent_primary_passes_the_other_surface_through(tmp_path) -> None:
    terrain = _terrain()
    merged = derive_merge_rasters(primary=None, fallback=terrain,
                                  _output_dir=str(tmp_path))
    assert merged.uri == terrain.uri
    assert merged.primary_fraction == 0.0 and merged.fallback_fraction == 1.0
    assert "absent" in merged.notes[0]


def test_two_datums_refuse_by_name(tmp_path) -> None:
    with pytest.raises(MergeRastersError) as excinfo:
        derive_merge_rasters(primary=_survey("CRD"), fallback=_terrain("NAVD88"),
                             _output_dir=str(tmp_path))
    assert excinfo.value.error_code == "MERGE_RASTERS_DATUMS_DIFFER"
    assert "CRD" in str(excinfo.value) and "NAVD88" in str(excinfo.value)


def test_an_unstated_datum_refuses(tmp_path) -> None:
    with pytest.raises(MergeRastersError) as excinfo:
        derive_merge_rasters(primary=_survey(None), fallback=_terrain("NAVD88"),
                             _output_dir=str(tmp_path))
    assert excinfo.value.error_code == "MERGE_RASTERS_DATUM_UNSTATED"


def test_a_stated_resolution_is_the_merged_cell(tmp_path) -> None:
    merged = derive_merge_rasters(primary=_survey(), fallback=_terrain(),
                                  resolution_m=2.0, _output_dir=str(tmp_path))
    with rasterio.open(merged.uri) as src:
        assert src.res == pytest.approx((2.0, 2.0))


def test_neither_surface_refuses() -> None:
    with pytest.raises(MergeRastersError) as excinfo:
        derive_merge_rasters()
    assert excinfo.value.error_code == "MERGE_RASTERS_NO_SOURCE"


def test_a_grid_past_the_cell_ceiling_refuses(tmp_path) -> None:
    far = _layer(_write(np.full((4, 4), 1.0), west=900_000.0, north=4_400_000.0,
                        cell=4.0), "NAVD88")
    with pytest.raises(MergeRastersError) as excinfo:
        derive_merge_rasters(primary=_survey(), fallback=far,
                             _output_dir=str(tmp_path))
    assert excinfo.value.error_code == "MERGE_RASTERS_RESOLUTION_INVALID"


def test_the_corpus_is_retrievable() -> None:
    import pathlib

    import yaml

    from trid3nt_server.tools.search.tool_retrieval import retrieve_visible_tools

    package = pathlib.Path(__file__).resolve().parents[2] / "trid3nt_server" \
        / "tools" / "derive" / "derive_merge_rasters" / "corpus.yaml"
    queries = yaml.safe_load(package.read_text(encoding="utf-8"))["derive_merge_rasters"]
    assert any("derive_merge_rasters" in retrieve_visible_tools(q, None, 8)
               for q in queries), \
        "derive_merge_rasters surfaces in NO top-8 for any of its corpus queries"
