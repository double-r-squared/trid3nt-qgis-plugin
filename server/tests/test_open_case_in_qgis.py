"""Tests for the ``hydrate_case_layers`` seam (QGIS case-layer serving).

No network / no S3: synthetic vector (geopandas -> GeoJSON file) + synthetic
raster (rasterio 10x10 GeoTIFF) in ``tmp_path``, passed through the explicit
``layers`` param as plain local paths.

Two seams under test:
- ``build_case_layers_manifest`` -- the PRIMARY path: pass the case's persisted
  layers straight through as a manifest (no materialization).
- ``hydrate_case_layers`` -- the REMOTE-mode fallback: materialize the layers
  into a GeoPackage + GeoTIFF + ``.qml`` style sidecars. NO ``.qgz`` project is
  produced (standalone project export is covered by native QGIS).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from trid3nt_server.cases.hydrate_case_layers import (
    _HYDRATE_CASE_LAYERS_METADATA,
    HydrateCaseError,
    HydrateInputError,
    NoExportableLayersError,
    build_case_layers_manifest,
    hydrate_case_layers,
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


def _read_qml(qml_path: str) -> ET.Element:
    """Parse a ``.qml`` style sidecar, stripping the DOCTYPE line."""
    raw = Path(qml_path).read_bytes()
    body = raw.split(b"\n", 1)[1] if raw.startswith(b"<!DOCTYPE") else raw
    return ET.fromstring(body)


# --------------------------------------------------------------------------- #
# PRIMARY path: the case-layers manifest (persistence passthrough, no geo deps)
# --------------------------------------------------------------------------- #


class _FakeCase:
    def __init__(self, layers, bbox, title):
        self.loaded_layer_summaries = layers
        self.bbox = bbox
        self.title = title


class _FakePersistence:
    def __init__(self, case):
        self._case = case

    async def get_case(self, case_id):  # noqa: D401 -- test double
        return self._case


@pytest.mark.asyncio
async def test_manifest_passes_persisted_layers_through(monkeypatch) -> None:
    """The manifest returns each persisted layer VERBATIM under ``loaded_layers``
    plus case_id/title/bbox -- no materialization, no gpkg/tif."""
    layers = [
        {
            "layer_id": "L1",
            "name": "Water Depth",
            "layer_type": "raster",
            "uri": "s3://runs/01RUN/depth.tif",
            "style_preset": "continuous_flood_depth",
        },
        {
            "layer_id": "L2",
            "name": "Flood Extent",
            "layer_type": "vector",
            "uri": "s3://runs/case-data/01CASE/L2.fgb",
        },
    ]
    case = _FakeCase(layers, [-85.5, 29.9, -85.4, 30.0], "Mexico Beach")
    import trid3nt_server.telemetry as tel

    monkeypatch.setattr(tel, "get_persistence", lambda: _FakePersistence(case))
    manifest = await build_case_layers_manifest("01CASE")
    assert manifest["case_id"] == "01CASE"
    assert manifest["title"] == "Mexico Beach"
    assert manifest["bbox"] == [-85.5, 29.9, -85.4, 30.0]
    assert manifest["loaded_layers"] == layers  # verbatim passthrough
    assert [l["layer_id"] for l in manifest["loaded_layers"]] == ["L1", "L2"]


@pytest.mark.asyncio
async def test_manifest_empty_case_is_honest_not_an_error(monkeypatch) -> None:
    """A case with no layers yields an empty ``loaded_layers`` list, never an
    error (the plugin notes "no layers yet")."""
    case = _FakeCase([], None, "Empty case")
    import trid3nt_server.telemetry as tel

    monkeypatch.setattr(tel, "get_persistence", lambda: _FakePersistence(case))
    manifest = await build_case_layers_manifest("01EMPTY")
    assert manifest["loaded_layers"] == []
    assert manifest["bbox"] is None


@pytest.mark.asyncio
async def test_manifest_missing_case_raises_case_not_found(monkeypatch) -> None:
    import trid3nt_server.telemetry as tel

    monkeypatch.setattr(tel, "get_persistence", lambda: _FakePersistence(None))
    from trid3nt_server.cases.hydrate_case_layers import CaseNotFoundError

    with pytest.raises(CaseNotFoundError) as exc_info:
        await build_case_layers_manifest("01GONE")
    assert exc_info.value.error_code == "CASE_NOT_FOUND"


# --------------------------------------------------------------------------- #
# REMOTE-mode fallback: materialize vector + raster via the explicit layers param
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_materialize_vector_and_raster_bundle(
    tmp_path: Path, vector_path: Path, raster_path: Path
) -> None:
    out_dir = tmp_path / "export"
    # Raster uri carries the TiTiler style params so the QML translation
    # (rescale=0,3 & colormap_name=Blues) is exercised end-to-end.
    result = await hydrate_case_layers(
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
    # NO QGIS project is produced any more.
    assert "qgz_path" not in result

    # (a) GPKG holds the vector layer, readable via pyogrio.
    import pyogrio

    gpkg = result["gpkg_path"]
    assert gpkg and Path(gpkg).is_file()
    layer_names = [l[0] for l in pyogrio.list_layers(gpkg)]
    assert "Flood_Extent" in layer_names
    gdf = pyogrio.read_dataframe(gpkg, layer="Flood_Extent")
    assert len(gdf) == 2
    assert set(gdf["name"]) == {"a", "b"}

    # (b) GeoTIFF copied (byte-identical to the source COG) + its sidecar .qml
    # style (same stem, listed in the result JSON).
    tif = out_dir / "Water_Depth.tif"
    assert tif.is_file()
    assert tif.read_bytes() == raster_path.read_bytes()
    assert result["qml_paths"] == [str(out_dir / "Water_Depth.qml")]
    assert (out_dir / "Water_Depth.qml").is_file()


@pytest.mark.asyncio
async def test_raster_style_params_translate_to_pseudocolor(
    tmp_path: Path, raster_path: Path
) -> None:
    """rescale=0,3 & colormap_name=Blues -> the .qml sidecar carries a singleband
    pseudocolor renderer with classification min 0 / max 3 and 5 Blues stops."""
    result = await hydrate_case_layers(
        layers=[
            {
                "name": "depth",
                "layer_type": "raster",
                "uri": f"{raster_path}?rescale=0,3&colormap_name=Blues",
            }
        ],
        output_dir=str(tmp_path / "styled"),
    )
    root = _read_qml(result["qml_paths"][0])
    renderer = root.find("./pipe/rasterrenderer")
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
    """Every materialized raster gets a sidecar .qml (for the QGIS plugin's
    standalone-add path) with the pseudocolor ramp, plus a 0-value transparency
    entry when the ramp starts at 0 (flood depth: dry cells transparent, never
    black)."""
    out_dir = tmp_path / "qml"
    result = await hydrate_case_layers(
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
    root = _read_qml(result["qml_paths"][0])
    assert root.tag == "qgis"

    renderer = root.find("./pipe/rasterrenderer")
    assert renderer is not None
    assert renderer.get("type") == "singlebandpseudocolor"
    assert float(renderer.get("classificationMin")) == 0.0
    assert float(renderer.get("classificationMax")) == 3.0
    # nodata stays transparent (empty nodataColor = QGIS default transparent).
    assert renderer.get("nodataColor") == ""

    # The ramp: 5 Blues stops over 0..3.
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


@pytest.mark.asyncio
async def test_qml_zero_transparency_only_for_zero_min_ramps(
    tmp_path: Path, raster_path: Path
) -> None:
    """A ramp that does NOT start at 0 (e.g. a DEM rescale=100,500) must not
    punch a transparency hole at value 0."""
    out_dir = tmp_path / "dem"
    result = await hydrate_case_layers(
        layers=[
            {
                "name": "dem",
                "layer_type": "raster",
                "uri": f"{raster_path}?rescale=100,500&colormap_name=terrain",
            }
        ],
        output_dir=str(out_dir),
    )
    root = _read_qml(result["qml_paths"][0])
    assert root.find(".//rasterTransparency") is None


@pytest.mark.asyncio
async def test_lowercase_titiler_colormap_resolves_case_insensitively(
    tmp_path: Path, raster_path: Path
) -> None:
    """TiTiler carries lowercase colormap names (ylgnbu); matplotlib registers
    YlGnBu. The translation must resolve case-insensitively instead of silently
    degrading every real flood-depth export to viridis."""
    result = await hydrate_case_layers(
        layers=[
            {
                "name": "depth",
                "layer_type": "raster",
                "uri": f"{raster_path}?rescale=0,3&colormap_name=ylgnbu",
            }
        ],
        output_dir=str(tmp_path / "lc"),
    )
    root = _read_qml(result["qml_paths"][0])
    items = root.findall(
        "./pipe/rasterrenderer/rastershader/colorrampshader/item"
    )
    from matplotlib import colormaps
    from matplotlib.colors import to_hex

    expected = [to_hex(colormaps["YlGnBu"](i / 4)) for i in range(5)]
    assert [i.get("color") for i in items] == expected


@pytest.mark.asyncio
async def test_raster_without_style_params_falls_back_to_viridis(
    tmp_path: Path, raster_path: Path
) -> None:
    result = await hydrate_case_layers(
        layers=[{"name": "plain", "layer_type": "raster", "uri": str(raster_path)}],
        output_dir=str(tmp_path / "plain"),
    )
    root = _read_qml(result["qml_paths"][0])
    renderer = root.find("./pipe/rasterrenderer")
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
    result = await hydrate_case_layers(
        layers=[{"name": "tiled depth", "layer_type": "raster", "uri": template}],
        output_dir=str(tmp_path / "tiled"),
    )
    assert result["exported_raster_count"] == 1
    tif = Path(result["output_dir"]) / "tiled_depth.tif"
    assert tif.read_bytes() == raster_path.read_bytes()
    # Style params on the TEMPLATE still translate into the sidecar.
    root = _read_qml(result["qml_paths"][0])
    renderer = root.find("./pipe/rasterrenderer")
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
    result = await hydrate_case_layers(
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
    with pytest.raises(HydrateInputError) as exc_info:
        await hydrate_case_layers()
    assert exc_info.value.error_code == "INVALID_INPUT"

    with pytest.raises(HydrateInputError):
        await hydrate_case_layers(
            case_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
            layers=[{"name": "x", "layer_type": "vector", "uri": "y"}],
        )


@pytest.mark.asyncio
async def test_unreadable_layer_is_a_skip_not_a_hard_fail(
    tmp_path: Path, vector_path: Path
) -> None:
    result = await hydrate_case_layers(
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


@pytest.mark.asyncio
async def test_all_layers_skipped_raises_no_exportable_layers(tmp_path: Path) -> None:
    with pytest.raises(NoExportableLayersError) as exc_info:
        await hydrate_case_layers(
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
        await hydrate_case_layers(layers=[], output_dir=str(tmp_path / "none"))


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #


def test_seam_is_not_registered() -> None:
    # DEREGISTERED: hydrate_case_layers is not an LLM-visible tool -- it serves
    # the /api/export-qgis HTTP route directly (and build_case_layers_manifest
    # serves /api/case-layers). The registry must NOT carry it.
    from trid3nt_server.agent.tools import TOOL_REGISTRY

    assert TOOL_REGISTRY.get("hydrate_case_layers") is None
    assert TOOL_REGISTRY.get("open_case_in_qgis") is None
    # The metadata object is still importable + carries the route's ttl/cacheable
    # semantics even though the seam is not registered.
    assert _HYDRATE_CASE_LAYERS_METADATA.cacheable is False
    assert _HYDRATE_CASE_LAYERS_METADATA.ttl_class == "live-no-cache"
    # Base error type is importable + typed (FR-AS-11).
    assert issubclass(NoExportableLayersError, HydrateCaseError)
