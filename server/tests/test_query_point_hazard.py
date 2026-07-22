"""Tests for ``query_point_hazard`` (sample every case raster at a point).

No network / no DynamoDB: persistence is a fake monkeypatched onto
``trid3nt_server.telemetry.get_persistence`` (the export_case_to_qgis seam) and
the geocoder seam (``_geocode_place``) is stubbed. Layers are tiny local
GeoTIFFs referenced from synthetic ``loaded_layer_summaries`` dicts.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from trid3nt_server.tools.processing import query_point_hazard as mod
from trid3nt_server.tools.processing.query_point_hazard import (
    NoCaseBoundError,
    NoCaseLayersError,
    PointHazardInputError,
    PointHazardUpstreamError,
    query_point_hazard,
)

_BBOX = (-85.5, 29.9, -85.4, 30.0)
# A point in the left half of the grid (column 1 of 10).
_PT_LON, _PT_LAT = -85.485, 29.95


def _write_raster(
    path: Path, data: np.ndarray, nodata: float | None = None, units: str | None = None
) -> Path:
    import rasterio
    from rasterio.transform import from_bounds

    h, w = data.shape
    transform = from_bounds(*_BBOX, w, h)
    kwargs = dict(
        driver="GTiff",
        height=h,
        width=w,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=transform,
    )
    if nodata is not None:
        kwargs["nodata"] = nodata
    with rasterio.open(path, "w", **kwargs) as ds:
        ds.write(data.astype("float32"), 1)
        if units:
            ds.update_tags(units=units)
    return path


class FakePersistence:
    def __init__(self, case) -> None:
        self._case = case

    async def get_case(self, case_id: str):
        if self._case is not None and self._case.case_id == case_id:
            return self._case
        return None


def _install_case(monkeypatch, layers, case_id="case-1", bbox=None, title="Test Case"):
    case = SimpleNamespace(
        case_id=case_id,
        title=title,
        bbox=bbox,
        loaded_layer_summaries=layers,
    )
    import trid3nt_server.telemetry as telemetry

    monkeypatch.setattr(telemetry, "get_persistence", lambda: FakePersistence(case))
    return case


@pytest.fixture()
def depth_layer(tmp_path: Path) -> dict:
    data = np.zeros((10, 10), dtype="float32")
    data[:, :5] = 1.75
    path = _write_raster(tmp_path / "depth.tif", data, units="m")
    return {
        "layer_id": "depth-1",
        "name": "Flood depth",
        "layer_type": "raster",
        "uri": str(path),
    }


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_samples_raster_value_at_point(monkeypatch, depth_layer) -> None:
    _install_case(monkeypatch, [depth_layer])
    result = await query_point_hazard(
        lon=_PT_LON, lat=_PT_LAT, case_id="case-1"
    )
    assert result["case_id"] == "case-1"
    assert result["case_title"] == "Test Case"
    assert result["sampled_count"] == 1
    (entry,) = result["results"]
    assert entry["name"] == "Flood depth"
    assert entry["value"] == pytest.approx(1.75)
    assert entry["units"] == "m"  # picked up from raster tags
    assert "error" not in entry


@pytest.mark.asyncio
async def test_multiple_layers_and_vector_skip(
    monkeypatch, depth_layer, tmp_path: Path
) -> None:
    data = np.full((10, 10), 42.0, dtype="float32")
    other = _write_raster(tmp_path / "other.tif", data)
    layers = [
        depth_layer,
        {
            "layer_id": "other-1",
            "name": "Other raster",
            "layer_type": "raster",
            "uri": str(other),
        },
        {
            "layer_id": "vec-1",
            "name": "Buildings",
            "layer_type": "vector",
            "uri": "unused.fgb",
        },
    ]
    _install_case(monkeypatch, layers)
    result = await query_point_hazard(lon=_PT_LON, lat=_PT_LAT, case_id="case-1")
    assert len(result["results"]) == 2
    assert result["results"][1]["value"] == pytest.approx(42.0)
    assert result["skipped_vector_layers"] == ["Buildings"]


@pytest.mark.asyncio
async def test_geocoded_place_path(monkeypatch, depth_layer) -> None:
    _install_case(monkeypatch, [depth_layer])
    monkeypatch.setattr(
        mod,
        "_geocode_place",
        lambda place: {
            "name": "Mexico Beach, FL",
            "longitude": _PT_LON,
            "latitude": _PT_LAT,
        },
    )
    result = await query_point_hazard(place="Mexico Beach", case_id="case-1")
    assert result["location"]["label"] == "Mexico Beach, FL"
    assert result["results"][0]["value"] == pytest.approx(1.75)


@pytest.mark.asyncio
async def test_point_outside_extent_is_honest_none(
    monkeypatch, depth_layer
) -> None:
    _install_case(monkeypatch, [depth_layer])
    result = await query_point_hazard(lon=-80.0, lat=25.0, case_id="case-1")
    (entry,) = result["results"]
    assert entry["value"] is None
    assert entry["note"] == "point outside the layer extent"
    assert result["sampled_count"] == 0


@pytest.mark.asyncio
async def test_nodata_at_point_is_honest_none(monkeypatch, tmp_path: Path) -> None:
    data = np.full((10, 10), -9999.0, dtype="float32")
    path = _write_raster(tmp_path / "nd.tif", data, nodata=-9999.0)
    _install_case(
        monkeypatch,
        [
            {
                "layer_id": "nd-1",
                "name": "Nodata layer",
                "layer_type": "raster",
                "uri": str(path),
            }
        ],
    )
    result = await query_point_hazard(lon=_PT_LON, lat=_PT_LAT, case_id="case-1")
    (entry,) = result["results"]
    assert entry["value"] is None
    assert entry["note"] == "nodata at this point"


@pytest.mark.asyncio
async def test_unreadable_layer_is_per_layer_error(monkeypatch, depth_layer) -> None:
    layers = [
        {
            "layer_id": "gone-1",
            "name": "Missing layer",
            "layer_type": "raster",
            "uri": "/nonexistent/gone.tif",
        },
        depth_layer,
    ]
    _install_case(monkeypatch, layers)
    result = await query_point_hazard(lon=_PT_LON, lat=_PT_LAT, case_id="case-1")
    assert "error" in result["results"][0]
    assert result["results"][0]["value"] is None
    # The readable layer still sampled -- no hard fail.
    assert result["results"][1]["value"] == pytest.approx(1.75)


# --------------------------------------------------------------------------- #
# Typed errors
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_no_case_layers_typed_error(monkeypatch) -> None:
    _install_case(monkeypatch, [])
    with pytest.raises(NoCaseLayersError):
        await query_point_hazard(lon=_PT_LON, lat=_PT_LAT, case_id="case-1")


@pytest.mark.asyncio
async def test_only_vector_layers_typed_error(monkeypatch) -> None:
    _install_case(
        monkeypatch,
        [{"layer_id": "v", "name": "Vec", "layer_type": "vector", "uri": "x.fgb"}],
    )
    with pytest.raises(NoCaseLayersError):
        await query_point_hazard(lon=_PT_LON, lat=_PT_LAT, case_id="case-1")


@pytest.mark.asyncio
async def test_no_location_typed_error(monkeypatch, depth_layer) -> None:
    _install_case(monkeypatch, [depth_layer])
    with pytest.raises(PointHazardInputError):
        await query_point_hazard(case_id="case-1")


@pytest.mark.asyncio
async def test_bad_latlon_typed_error(monkeypatch, depth_layer) -> None:
    _install_case(monkeypatch, [depth_layer])
    with pytest.raises(PointHazardInputError):
        await query_point_hazard(lon=999.0, lat=29.95, case_id="case-1")


@pytest.mark.asyncio
async def test_no_case_bound_typed_error(monkeypatch, depth_layer) -> None:
    _install_case(monkeypatch, [depth_layer])
    # No case_id and no turn-bound Case (ContextVar default is None).
    with pytest.raises(NoCaseBoundError):
        await query_point_hazard(lon=_PT_LON, lat=_PT_LAT)


@pytest.mark.asyncio
async def test_unknown_case_typed_error(monkeypatch, depth_layer) -> None:
    _install_case(monkeypatch, [depth_layer], case_id="case-1")
    with pytest.raises(PointHazardUpstreamError):
        await query_point_hazard(lon=_PT_LON, lat=_PT_LAT, case_id="case-other")


@pytest.mark.asyncio
async def test_geocode_failure_typed_error(monkeypatch, depth_layer) -> None:
    _install_case(monkeypatch, [depth_layer])

    def _boom(place):
        raise RuntimeError("nominatim down")

    monkeypatch.setattr(mod, "_geocode_place", _boom)
    with pytest.raises(PointHazardInputError):
        await query_point_hazard(place="somewhere", case_id="case-1")


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #


def test_registered_in_tool_registry() -> None:
    from trid3nt_server.tools import TOOL_REGISTRY

    entry = TOOL_REGISTRY.get("query_point_hazard")
    assert entry is not None
    assert entry.metadata.cacheable is False
    assert entry.metadata.ttl_class == "live-no-cache"
