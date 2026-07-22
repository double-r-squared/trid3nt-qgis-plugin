"""Tests for the ``export_case_to_qgis`` tool (QGIS bridge v1).

No network / no S3: synthetic vector (geopandas -> GeoJSON file) + synthetic
raster (rasterio 10x10 GeoTIFF) in ``tmp_path``, passed through the explicit
``layers`` param as plain local paths.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

from trid3nt_server.tools.export_case_to_qgis import (
    ExportCaseError,
    ExportInputError,
    NoExportableLayersError,
    export_case_to_qgis,
)

# --------------------------------------------------------------------------- #
# Fixtures: tiny synthetic vector + raster
# --------------------------------------------------------------------------- #


@pytest.fixture()
def vector_path(tmp_path: Path) -> Path:
    import geopandas as gpd
    from shapely.geometry import Point, Polygon

    gdf = gpd.GeoDataFrame(
        {
            "name": ["a", "b"],
            "value": [1.5, 2.5],
            "geometry": [
                Point(-85.42, 29.94),
                Polygon([(-85.5, 29.9), (-85.4, 29.9), (-85.4, 30.0), (-85.5, 29.9)]),
            ],
        },
        crs="EPSG:4326",
    )
    path = tmp_path / "flood_extent.geojson"
    gdf.to_file(path, driver="GeoJSON")
    return path


@pytest.fixture()
def raster_path(tmp_path: Path) -> Path:
    import numpy as np
    import rasterio
    from rasterio.transform import from_bounds

    path = tmp_path / "depth.tif"
    data = np.linspace(0.0, 3.0, 100, dtype="float32").reshape(10, 10)
    transform = from_bounds(-85.5, 29.9, -85.4, 30.0, 10, 10)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=10,
        width=10,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=transform,
    ) as ds:
        ds.write(data, 1)
    return path


def _read_qgs(qgz_path: str) -> ET.Element:
    """Unzip the .qgz, parse the inner .qgs XML, return the root element."""
    with zipfile.ZipFile(qgz_path) as zf:
        qgs_names = [n for n in zf.namelist() if n.endswith(".qgs")]
        assert qgs_names, f".qgz holds no .qgs (contents: {zf.namelist()})"
        raw = zf.read(qgs_names[0])
    # Strip the DOCTYPE line -- ElementTree parses the rest.
    body = raw.split(b"\n", 1)[1] if raw.startswith(b"<!DOCTYPE") else raw
    return ET.fromstring(body)


# --------------------------------------------------------------------------- #
# Happy path: vector + raster via the explicit layers param
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_export_vector_and_raster_full_bundle(
    tmp_path: Path, vector_path: Path, raster_path: Path
) -> None:
    out_dir = tmp_path / "export"
    # Raster uri carries the TiTiler style params so the QML translation
    # (rescale=0,3 & colormap_name=Blues) is exercised end-to-end.
    result = await export_case_to_qgis(
        layers=[
            {
                "name": "Flood Extent",
                "layer_type": "vector",
                "uri": str(vector_path),
            },
            {
                "name": "Water Depth",
                "layer_type": "raster",
                "uri": f"{raster_path}?rescale=0,3&colormap_name=Blues",
            },
        ],
        output_dir=str(out_dir),
        project_name="Mexico Beach",
    )

    assert result["status"] == "ok"
    assert result["exported_vector_count"] == 1
    assert result["exported_raster_count"] == 1
    assert result["skipped"] == []
    assert result["output_dir"] == str(out_dir)

    # (a) GPKG holds the vector layer, readable via pyogrio.
    import pyogrio

    gpkg = result["gpkg_path"]
    assert gpkg and Path(gpkg).is_file()
    layer_names = [l[0] for l in pyogrio.list_layers(gpkg)]
    assert "Flood_Extent" in layer_names
    gdf = pyogrio.read_dataframe(gpkg, layer="Flood_Extent")
    assert len(gdf) == 2
    assert set(gdf["name"]) == {"a", "b"}

    # (b) GeoTIFF copied (byte-identical to the source COG) + its sidecar
    # .qml style (same stem, listed in the result JSON).
    tif = out_dir / "Water_Depth.tif"
    assert tif.is_file()
    assert tif.read_bytes() == raster_path.read_bytes()
    assert result["qml_paths"] == [str(out_dir / "Water_Depth.qml")]
    assert (out_dir / "Water_Depth.qml").is_file()

    # (c) .qgz unzips to a parseable .qgs with both maplayers + tree order.
    assert result["qgz_path"].endswith(".qgz")
    root = _read_qgs(result["qgz_path"])
    assert root.tag == "qgis"
    assert root.get("projectname") == "Mexico Beach"

    # Project CRS is EPSG:4326.
    authid = root.find("./projectCrs/spatialrefsys/authid")
    assert authid is not None and authid.text == "EPSG:4326"

    # Layer tree mirrors the case layer ORDER: vector first, raster second.
    tree_layers = root.findall("./layer-tree-group/layer-tree-layer")
    assert [t.get("name") for t in tree_layers] == ["Flood Extent", "Water Depth"]
    assert [t.get("providerKey") for t in tree_layers] == ["ogr", "gdal"]

    # Both maplayer nodes exist with the right providers + datasources.
    maplayers = root.findall("./projectlayers/maplayer")
    assert len(maplayers) == 2
    by_name = {ml.findtext("layername"): ml for ml in maplayers}
    vec_ml = by_name["Flood Extent"]
    ras_ml = by_name["Water Depth"]
    assert vec_ml.findtext("provider") == "ogr"
    assert vec_ml.findtext("datasource") == "./export.gpkg|layername=Flood_Extent"
    assert ras_ml.findtext("provider") == "gdal"
    assert ras_ml.findtext("datasource") == "./Water_Depth.tif"

    # Tree ids match projectlayers ids (QGIS joins the two by id).
    tree_ids = {t.get("id") for t in tree_layers}
    map_ids = {ml.findtext("id") for ml in maplayers}
    assert tree_ids == map_ids

    # Initial extent = union of layer bounds (covers the shared bbox).
    ext = root.find("./mapcanvas/extent")
    assert ext is not None
    xmin, ymin = float(ext.findtext("xmin")), float(ext.findtext("ymin"))
    xmax, ymax = float(ext.findtext("xmax")), float(ext.findtext("ymax"))
    assert xmin <= -85.5 and xmax >= -85.4
    assert ymin <= 29.9 and ymax >= 30.0


@pytest.mark.asyncio
async def test_raster_style_params_translate_to_pseudocolor(
    tmp_path: Path, raster_path: Path
) -> None:
    """rescale=0,3 & colormap_name=Blues -> singleband pseudocolor renderer
    with classification min 0 / max 3 and 5 Blues-sampled stops."""
    result = await export_case_to_qgis(
        layers=[
            {
                "name": "depth",
                "layer_type": "raster",
                "uri": f"{raster_path}?rescale=0,3&colormap_name=Blues",
            }
        ],
        output_dir=str(tmp_path / "styled"),
    )
    root = _read_qgs(result["qgz_path"])
    renderer = root.find("./projectlayers/maplayer/pipe/rasterrenderer")
    assert renderer is not None
    assert renderer.get("type") == "singlebandpseudocolor"
    assert float(renderer.get("classificationMin")) == 0.0
    assert float(renderer.get("classificationMax")) == 3.0

    items = renderer.findall("./rastershader/colorrampshader/item")
    assert len(items) == 5
    values = [float(i.get("value")) for i in items]
    assert values[0] == 0.0 and values[-1] == 3.0
    assert values == sorted(values)
    # Colors are the matplotlib Blues samples: light -> dark blue.
    from matplotlib import colormaps
    from matplotlib.colors import to_hex

    expected = [to_hex(colormaps["Blues"](i / 4)) for i in range(5)]
    assert [i.get("color") for i in items] == expected


@pytest.mark.asyncio
async def test_qml_sidecar_carries_ramp_and_zero_transparency(
    tmp_path: Path, raster_path: Path
) -> None:
    """Every exported raster gets a sidecar .qml (for the QGIS plugin's
    standalone-add path) with the SAME pseudocolor ramp the .qgz embeds, plus
    a 0-value transparency entry when the ramp starts at 0 (flood depth: dry
    cells transparent, never black)."""
    out_dir = tmp_path / "qml"
    result = await export_case_to_qgis(
        layers=[
            {
                "name": "depth",
                "layer_type": "raster",
                "uri": f"{raster_path}?rescale=0,3&colormap_name=Blues",
            }
        ],
        output_dir=str(out_dir),
    )
    assert result["qml_paths"] == [str(out_dir / "depth.qml")]
    raw = (out_dir / "depth.qml").read_bytes()
    body = raw.split(b"\n", 1)[1] if raw.startswith(b"<!DOCTYPE") else raw
    root = ET.fromstring(body)
    assert root.tag == "qgis"

    renderer = root.find("./pipe/rasterrenderer")
    assert renderer is not None
    assert renderer.get("type") == "singlebandpseudocolor"
    assert float(renderer.get("classificationMin")) == 0.0
    assert float(renderer.get("classificationMax")) == 3.0
    # nodata stays transparent (empty nodataColor = QGIS default transparent).
    assert renderer.get("nodataColor") == ""

    # The ramp: 5 Blues stops over 0..3, identical to the .qgz translation.
    items = renderer.findall("./rastershader/colorrampshader/item")
    assert len(items) == 5
    values = [float(i.get("value")) for i in items]
    assert values[0] == 0.0 and values[-1] == 3.0
    from matplotlib import colormaps
    from matplotlib.colors import to_hex

    expected = [to_hex(colormaps["Blues"](i / 4)) for i in range(5)]
    assert [i.get("color") for i in items] == expected

    # 0-depth cells are fully transparent (vmin == 0 ramp).
    entry = renderer.find("./rasterTransparency/singleValuePixelList/pixelListEntry")
    assert entry is not None
    assert entry.get("min") == "0" and entry.get("max") == "0"
    assert entry.get("percentTransparent") == "100"

    # The same transparency entry lands in the .qgz inline pipe (single seam).
    qgs_root = _read_qgs(result["qgz_path"])
    qgs_entry = qgs_root.find(
        "./projectlayers/maplayer/pipe/rasterrenderer/rasterTransparency"
        "/singleValuePixelList/pixelListEntry"
    )
    assert qgs_entry is not None
    assert qgs_entry.get("percentTransparent") == "100"


@pytest.mark.asyncio
async def test_qml_zero_transparency_only_for_zero_min_ramps(
    tmp_path: Path, raster_path: Path
) -> None:
    """A ramp that does NOT start at 0 (e.g. a DEM rescale=100,500) must not
    punch a transparency hole at value 0."""
    out_dir = tmp_path / "dem"
    result = await export_case_to_qgis(
        layers=[
            {
                "name": "dem",
                "layer_type": "raster",
                "uri": f"{raster_path}?rescale=100,500&colormap_name=terrain",
            }
        ],
        output_dir=str(out_dir),
    )
    raw = Path(result["qml_paths"][0]).read_bytes()
    body = raw.split(b"\n", 1)[1] if raw.startswith(b"<!DOCTYPE") else raw
    root = ET.fromstring(body)
    assert root.find(".//rasterTransparency") is None


@pytest.mark.asyncio
async def test_lowercase_titiler_colormap_resolves_case_insensitively(
    tmp_path: Path, raster_path: Path
) -> None:
    """TiTiler carries lowercase colormap names (ylgnbu); matplotlib registers
    YlGnBu. The translation must resolve case-insensitively instead of
    silently degrading every real flood-depth export to viridis."""
    result = await export_case_to_qgis(
        layers=[
            {
                "name": "depth",
                "layer_type": "raster",
                "uri": f"{raster_path}?rescale=0,3&colormap_name=ylgnbu",
            }
        ],
        output_dir=str(tmp_path / "lc"),
    )
    root = _read_qgs(result["qgz_path"])
    items = root.findall(
        "./projectlayers/maplayer/pipe/rasterrenderer/rastershader"
        "/colorrampshader/item"
    )
    from matplotlib import colormaps
    from matplotlib.colors import to_hex

    expected = [to_hex(colormaps["YlGnBu"](i / 4)) for i in range(5)]
    assert [i.get("color") for i in items] == expected


@pytest.mark.asyncio
async def test_raster_without_style_params_falls_back_to_viridis(
    tmp_path: Path, raster_path: Path
) -> None:
    result = await export_case_to_qgis(
        layers=[{"name": "plain", "layer_type": "raster", "uri": str(raster_path)}],
        output_dir=str(tmp_path / "plain"),
    )
    root = _read_qgs(result["qgz_path"])
    renderer = root.find("./projectlayers/maplayer/pipe/rasterrenderer")
    assert renderer is not None
    assert float(renderer.get("classificationMin")) == 0.0
    assert float(renderer.get("classificationMax")) == 1.0
    from matplotlib import colormaps
    from matplotlib.colors import to_hex

    items = renderer.findall("./rastershader/colorrampshader/item")
    assert [i.get("color") for i in items] == [
        to_hex(colormaps["viridis"](i / 4)) for i in range(5)
    ]


@pytest.mark.asyncio
async def test_titiler_tile_template_unwraps_url_param(
    tmp_path: Path, raster_path: Path
) -> None:
    """A /cog/tiles/ TEMPLATE uri resolves the raster from its percent-encoded
    url= query param (local-path COG here; no network)."""
    from urllib.parse import quote

    template = (
        "https://example.test/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png"
        f"?url={quote(str(raster_path), safe='')}&rescale=0,3&colormap_name=Blues"
    )
    result = await export_case_to_qgis(
        layers=[{"name": "tiled depth", "layer_type": "raster", "uri": template}],
        output_dir=str(tmp_path / "tiled"),
    )
    assert result["exported_raster_count"] == 1
    tif = Path(result["output_dir"]) / "tiled_depth.tif"
    assert tif.read_bytes() == raster_path.read_bytes()
    # Style params on the TEMPLATE still translate.
    root = _read_qgs(result["qgz_path"])
    renderer = root.find("./projectlayers/maplayer/pipe/rasterrenderer")
    assert float(renderer.get("classificationMax")) == 3.0


@pytest.mark.asyncio
async def test_inline_geojson_vector(tmp_path: Path) -> None:
    fc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"site": "gauge-1"},
                "geometry": {"type": "Point", "coordinates": [-85.45, 29.95]},
            }
        ],
    }
    result = await export_case_to_qgis(
        layers=[{"name": "gauges", "layer_type": "vector", "inline_geojson": fc}],
        output_dir=str(tmp_path / "inline"),
    )
    assert result["exported_vector_count"] == 1
    import pyogrio

    gdf = pyogrio.read_dataframe(result["gpkg_path"], layer="gauges")
    assert len(gdf) == 1 and gdf["site"].iat[0] == "gauge-1"


# --------------------------------------------------------------------------- #
# Error paths
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_exactly_one_of_case_id_or_layers_required(tmp_path: Path) -> None:
    with pytest.raises(ExportInputError) as exc_info:
        await export_case_to_qgis()
    assert exc_info.value.error_code == "INVALID_INPUT"

    with pytest.raises(ExportInputError):
        await export_case_to_qgis(
            case_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
            layers=[{"name": "x", "layer_type": "vector", "uri": "y"}],
        )


@pytest.mark.asyncio
async def test_unreadable_layer_is_a_skip_not_a_hard_fail(
    tmp_path: Path, vector_path: Path
) -> None:
    result = await export_case_to_qgis(
        layers=[
            {"name": "good", "layer_type": "vector", "uri": str(vector_path)},
            {
                "name": "ghost",
                "layer_type": "raster",
                "uri": str(tmp_path / "does_not_exist.tif"),
            },
        ],
        output_dir=str(tmp_path / "partial"),
    )
    assert result["status"] == "partial"
    assert result["exported_vector_count"] == 1
    assert result["exported_raster_count"] == 0
    assert len(result["skipped"]) == 1
    assert result["skipped"][0]["name"] == "ghost"
    assert result["skipped"][0]["reason"]
    # The project still opens with the surviving layer.
    root = _read_qgs(result["qgz_path"])
    assert len(root.findall("./projectlayers/maplayer")) == 1


@pytest.mark.asyncio
async def test_all_layers_skipped_raises_no_exportable_layers(tmp_path: Path) -> None:
    with pytest.raises(NoExportableLayersError) as exc_info:
        await export_case_to_qgis(
            layers=[
                {
                    "name": "ghost",
                    "layer_type": "raster",
                    "uri": str(tmp_path / "missing.tif"),
                }
            ],
            output_dir=str(tmp_path / "empty"),
        )
    assert exc_info.value.error_code == "NO_EXPORTABLE_LAYERS"


@pytest.mark.asyncio
async def test_empty_layers_list_raises(tmp_path: Path) -> None:
    with pytest.raises(NoExportableLayersError):
        await export_case_to_qgis(layers=[], output_dir=str(tmp_path / "none"))


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #


def test_tool_is_registered() -> None:
    from trid3nt_server.tools import TOOL_REGISTRY

    entry = TOOL_REGISTRY.get("export_case_to_qgis")
    assert entry is not None
    assert entry.metadata.cacheable is False
    assert entry.metadata.ttl_class == "live-no-cache"
    # Base error type is importable + typed (FR-AS-11).
    assert issubclass(NoExportableLayersError, ExportCaseError)
