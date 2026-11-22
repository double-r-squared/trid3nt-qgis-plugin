"""The coverage operations as derive tools, usable outside any slot: ``merge_surfaces`` lays ranked surfaces into one
with the share each reached, ``interpolate_surface`` fills a surface's holes within a stated reach and counts the rest.

The rasters are SYNTHETIC, written by the test (an isolated verification gate): constant elevations on a small grid.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from trid3nt import case
from trid3nt.tools import TOOL_REGISTRY
from trid3nt.tools.derive import DeriveError
from trid3nt.tools.derive._raster_layers import FILLED, UNMEASURED
from trid3nt.tools.derive.interpolate_surface import interpolate_surface
from trid3nt.tools.derive.merge_surfaces import merge_surfaces

_BOUNDS = (-122.68, 45.514, -122.66, 45.521)


def _raster(path: Path, values) -> str:
    import rasterio
    from rasterio.transform import from_bounds

    height, width = values.shape
    with rasterio.open(path, "w", driver="GTiff", width=width, height=height, count=1, dtype="float32",
                       nodata=float("nan"), crs="EPSG:4326", transform=from_bounds(*_BOUNDS, width, height)) as out:
        out.write(values.astype("float32"), 1)
    return str(path)


def _bands(c, layer):
    import rasterio

    with rasterio.open(c.path(layer.uri)) as src:
        return src.read(1), src.read(2)


def test_merge_lays_the_first_where_it_measured_and_states_each_share(tmp_path):
    west = np.full((20, 40), -4.0)
    west[:, 20:] = np.nan
    first = _raster(tmp_path / "west.tif", west)
    second = _raster(tmp_path / "all.tif", np.full((20, 40), -6.0))
    c = case(tmp_path / "ops")
    with c.bound():
        merged = merge_surfaces([first, second], name="bed")
        values, won = _bands(c, merged)
    assert merged.sources == ["input 1", "input 2", "filled"]
    assert np.allclose(values[won == 0], -4.0) and np.allclose(values[won == 1], -6.0)
    shares = dict(merged.coverage)
    assert shares["input 1"] == pytest.approx(0.5, abs=0.1) and shares["input 2"] == pytest.approx(0.5, abs=0.1)


def test_merge_refuses_two_surfaces_on_different_zeros(tmp_path):
    a = _raster(tmp_path / "a.tif", np.full((20, 40), -4.0))
    b = _raster(tmp_path / "b.tif", np.full((20, 40), -6.0))
    with case(tmp_path / "ops").bound(), pytest.raises(DeriveError) as refused:
        merge_surfaces([{"uri": a, "name": "a", "vertical_datum": "NAVD88"},
                        {"uri": b, "name": "b", "vertical_datum": "EGM2008"}])
    assert refused.value.error_code == "MERGE_DATUMS_DIFFER"


def test_interpolate_fills_within_its_reach_and_counts_what_lies_past_it(tmp_path):
    holed = np.full((20, 40), -4.0)
    holed[5:15, 5:9] = np.nan
    holed[:, 25:] = np.nan
    layer = _raster(tmp_path / "holed.tif", holed)
    c = case(tmp_path / "ops")
    with c.bound():
        filled = interpolate_surface(layer, max_distance=3)
        values, won = _bands(c, filled)
    assert (won[5:15, 5:9] == FILLED).all() and np.allclose(values[5:15, 5:9], -4.0)
    assert np.isnan(values[:, 32:]).all() and (won[:, 32:] == UNMEASURED).all()
    shares = dict(filled.coverage)
    assert shares["filled"] > 0 and 0 < filled.unmeasured_fraction < 1
    assert filled.sources == ["input 1", "filled"]


def test_interpolate_refuses_an_algorithm_it_does_not_have(tmp_path):
    layer = _raster(tmp_path / "flat.tif", np.full((20, 40), -4.0))
    with case(tmp_path / "ops").bound(), pytest.raises(DeriveError) as refused:
        interpolate_surface(layer, algorithm="kriging")
    assert refused.value.error_code == "INTERPOLATE_ARGUMENT"


@pytest.mark.parametrize("name", ["merge_surfaces", "interpolate_surface"])
def test_each_is_a_registered_derive_with_its_corpus(name):
    registered = TOOL_REGISTRY[name]
    assert registered.module == f"trid3nt.tools.derive.{name}.{name}"
    corpus = Path(__file__).resolve().parents[2] / "trid3nt" / "tools" / "derive" / name / "corpus.yaml"
    import yaml

    assert len(yaml.safe_load(corpus.read_text())[name]) >= 5
