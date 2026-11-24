"""Headless views of recorded products."""

import json

import matplotlib
import pytest

matplotlib.use("Agg")

from trid3nt import view
from trid3nt.errors import DeclarationError
from trid3nt.sdk.model import Result


def test_views_draw_raster_vector_mesh_and_recorded_chart(tmp_path):
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    with rasterio.open(tmp_path / "grid.tif", "w", driver="GTiff", height=2, width=2,
                       count=1, dtype="float32", crs="EPSG:4326",
                       transform=from_origin(0, 2, 1, 1)) as raster:
        raster.write(np.ones((2, 2), dtype="float32"), 1)
    (tmp_path / "point.geojson").write_text(json.dumps({
        "type": "FeatureCollection", "features": [{
            "type": "Feature", "properties": {},
            "geometry": {"type": "Point", "coordinates": [0, 0]}}]}))
    (tmp_path / "mesh.2dm").write_text("MESH2D\nE3T 1 1 2 3 1\nND 1 0 0 0\nND 2 1 0 0\nND 3 0 1 0\n")
    (tmp_path / "mesh.dat").write_text('DATASET\nBEGSCL\nNAME "Depth"\nTS 0 0\n1\n2\n3\nENDDS\n')
    spec_dir = tmp_path / "runs" / "sample"
    spec_dir.mkdir(parents=True)
    (spec_dir / "chart_spec.json").write_text(json.dumps({"line": {"vega_lite_spec": {
        "mark": "line", "encoding": {"x": {"field": "time"}, "y": {"field": "value"}},
        "data": {"values": [{"time": 0, "value": 3}, {"time": 1, "value": 4}]}}}}))
    record = {"run_id": "sample", "outputs": [
        {"layer_type": "raster", "uri": "grid.tif"},
        {"layer_type": "vector", "uri": "point.geojson"},
        {"layer_type": "mesh", "uri": "mesh.2dm", "dataset_uris": ["mesh.dat"],
         "style": {"kind": "mesh", "dataset_group": "Depth"}}]}
    result = Result("sample", None, record, tmp_path)
    assert result.charts == json.loads((spec_dir / "chart_spec.json").read_text())
    assert Result("absent", None, root=tmp_path).charts == {}
    raster, vector, mesh, chart = result.show(mesh=True)
    assert raster.axes[0].images and "EPSG:4326" in raster.axes[0].get_xlabel()
    assert len(raster.axes) == 2
    assert vector.axes[0].collections
    assert mesh.axes[0].collections and mesh.axes[0].lines
    assert list(chart.axes[0].lines[0].get_ydata()) == [3, 4]


def test_view_names_unsupported_layer_kind():
    with pytest.raises(DeclarationError, match="point-cloud") as error:
        view({"layer_type": "point-cloud"})
    assert error.value.error_code == "VIEW_LAYER_UNSUPPORTED"
