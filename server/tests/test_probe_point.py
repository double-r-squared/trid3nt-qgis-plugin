"""Tests for ``probe_point.probe_point_at`` (deterministic map-click probe
core behind ``POST /api/probe-point``).

No network / no DynamoDB: persistence is a fake monkeypatched onto
``grace2_agent.telemetry.get_persistence`` (the SAME seam
``test_query_point_hazard.py`` uses, since ``probe_point_at`` reuses
``query_point_hazard.layers_from_case``). Layers are tiny local GeoTIFFs
referenced from synthetic ``loaded_layer_summaries`` dicts -- real rasterio
reads over throwaway files, no S3/rasterio mocking needed.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from grace2_agent.tools.probe_point import (
    MAX_PROBE_LAYERS,
    ProbePointCaseNotFoundError,
    ProbePointInputError,
    probe_point_at,
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


class NoPersistence:
    """Simulates the persistence backend being unbound."""


def _install_case(monkeypatch, layers, case_id="case-1", bbox=None, title="Test Case"):
    case = SimpleNamespace(
        case_id=case_id,
        title=title,
        bbox=bbox,
        loaded_layer_summaries=layers,
    )
    import grace2_agent.telemetry as telemetry

    monkeypatch.setattr(telemetry, "get_persistence", lambda: FakePersistence(case))
    return case


def _flat_layer(path: Path, value: float, *, layer_id: str, name: str, units: str | None = None) -> dict:
    data = np.full((10, 10), value, dtype="float32")
    _write_raster(path, data, units=units)
    return {"layer_id": layer_id, "name": name, "layer_type": "raster", "uri": str(path)}


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_two_rasters_happy_path(monkeypatch, tmp_path: Path) -> None:
    layers = [
        _flat_layer(tmp_path / "a.tif", 1.5, layer_id="a-1", name="Layer A", units="m"),
        _flat_layer(tmp_path / "b.tif", 42.0, layer_id="b-1", name="Layer B"),
    ]
    _install_case(monkeypatch, layers)
    result = await probe_point_at("case-1", _PT_LON, _PT_LAT)
    assert result["status"] == "ok"
    assert result["point"] == {"lon": _PT_LON, "lat": _PT_LAT}
    assert result["case_id"] == "case-1"
    assert result["truncated"] is False
    assert len(result["results"]) == 2
    a, b = result["results"]
    assert a["name"] == "Layer A"
    assert a["value"] == pytest.approx(1.5)
    assert a["units"] == "m"
    assert "error" not in a
    assert b["name"] == "Layer B"
    assert b["value"] == pytest.approx(42.0)


@pytest.mark.asyncio
async def test_no_raster_layers_is_ok_empty_results(monkeypatch) -> None:
    _install_case(
        monkeypatch,
        [{"layer_id": "v", "name": "Vec", "layer_type": "vector", "uri": "x.fgb"}],
    )
    result = await probe_point_at("case-1", _PT_LON, _PT_LAT)
    assert result["status"] == "ok"
    assert result["results"] == []


@pytest.mark.asyncio
async def test_empty_case_is_ok_empty_results(monkeypatch) -> None:
    _install_case(monkeypatch, [])
    result = await probe_point_at("case-1", _PT_LON, _PT_LAT)
    assert result["status"] == "ok"
    assert result["results"] == []


# --------------------------------------------------------------------------- #
# Outside-bounds / nodata honesty
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_outside_bounds_is_honest_null(monkeypatch, tmp_path: Path) -> None:
    layers = [_flat_layer(tmp_path / "a.tif", 1.5, layer_id="a-1", name="Layer A")]
    _install_case(monkeypatch, layers)
    result = await probe_point_at("case-1", -80.0, 25.0)
    (entry,) = result["results"]
    assert entry["value"] is None
    assert entry["note"] == "point outside the layer extent"


@pytest.mark.asyncio
async def test_nodata_is_honest_null(monkeypatch, tmp_path: Path) -> None:
    data = np.full((10, 10), -9999.0, dtype="float32")
    path = _write_raster(tmp_path / "nd.tif", data, nodata=-9999.0)
    _install_case(
        monkeypatch,
        [{"layer_id": "nd-1", "name": "Nodata layer", "layer_type": "raster", "uri": str(path)}],
    )
    result = await probe_point_at("case-1", _PT_LON, _PT_LAT)
    (entry,) = result["results"]
    assert entry["value"] is None
    assert entry["note"] == "nodata at this point"


@pytest.mark.asyncio
async def test_unreadable_layer_is_per_layer_error(monkeypatch, tmp_path: Path) -> None:
    good = _flat_layer(tmp_path / "good.tif", 7.0, layer_id="g-1", name="Good")
    layers = [
        {"layer_id": "gone-1", "name": "Missing", "layer_type": "raster", "uri": "/nonexistent/gone.tif"},
        good,
    ]
    _install_case(monkeypatch, layers)
    result = await probe_point_at("case-1", _PT_LON, _PT_LAT)
    assert "error" in result["results"][0]
    assert result["results"][0]["value"] is None
    assert result["results"][1]["value"] == pytest.approx(7.0)


# --------------------------------------------------------------------------- #
# Series grouping
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_frame_sequence_groups_into_one_series_entry(
    monkeypatch, tmp_path: Path
) -> None:
    layers = [
        _flat_layer(tmp_path / "f1.tif", 0.02, layer_id="f-1", name="Flood depth step 1", units="m"),
        _flat_layer(tmp_path / "f2.tif", 0.15, layer_id="f-2", name="Flood depth step 2"),
        _flat_layer(tmp_path / "f3.tif", 0.31, layer_id="f-3", name="Flood depth step 3"),
    ]
    _install_case(monkeypatch, layers)
    result = await probe_point_at("case-1", _PT_LON, _PT_LAT)
    assert len(result["results"]) == 1
    (entry,) = result["results"]
    assert entry["name"] == "flood depth"
    assert entry["units"] == "m"
    assert entry["layer_ids"] == ["f-1", "f-2", "f-3"]
    values = [pt["value"] for pt in entry["series"]]
    assert values == [pytest.approx(0.02), pytest.approx(0.15), pytest.approx(0.31)]
    labels = [pt["label"] for pt in entry["series"]]
    assert labels == ["step 1", "step 2", "step 3"]


@pytest.mark.asyncio
async def test_series_and_single_layer_both_present(monkeypatch, tmp_path: Path) -> None:
    layers = [
        _flat_layer(tmp_path / "f1.tif", 0.1, layer_id="f-1", name="Flood depth step 1"),
        _flat_layer(tmp_path / "f2.tif", 0.2, layer_id="f-2", name="Flood depth step 2"),
        _flat_layer(tmp_path / "dem.tif", 12.0, layer_id="dem-1", name="Elevation"),
    ]
    _install_case(monkeypatch, layers)
    result = await probe_point_at("case-1", _PT_LON, _PT_LAT)
    assert len(result["results"]) == 2
    names = {r.get("name") for r in result["results"]}
    assert "flood depth" in names
    assert "Elevation" in names
    single = next(r for r in result["results"] if r.get("name") == "Elevation")
    assert single["value"] == pytest.approx(12.0)
    assert "series" not in single


# --------------------------------------------------------------------------- #
# Layer cap
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_layer_cap_truncates_and_flags(monkeypatch, tmp_path: Path) -> None:
    layers = [
        _flat_layer(
            tmp_path / f"layer{i}.tif", float(i), layer_id=f"l-{i}", name=f"Layer {i}"
        )
        for i in range(MAX_PROBE_LAYERS + 5)
    ]
    _install_case(monkeypatch, layers)
    result = await probe_point_at("case-1", _PT_LON, _PT_LAT)
    assert result["truncated"] is True
    assert len(result["results"]) == MAX_PROBE_LAYERS


# --------------------------------------------------------------------------- #
# Typed errors
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_missing_case_id_typed_error(monkeypatch, tmp_path: Path) -> None:
    layers = [_flat_layer(tmp_path / "a.tif", 1.0, layer_id="a-1", name="A")]
    _install_case(monkeypatch, layers)
    with pytest.raises(ProbePointInputError):
        await probe_point_at("", _PT_LON, _PT_LAT)


@pytest.mark.asyncio
async def test_invalid_lon_lat_typed_error(monkeypatch, tmp_path: Path) -> None:
    layers = [_flat_layer(tmp_path / "a.tif", 1.0, layer_id="a-1", name="A")]
    _install_case(monkeypatch, layers)
    with pytest.raises(ProbePointInputError):
        await probe_point_at("case-1", 999.0, _PT_LAT)


@pytest.mark.asyncio
async def test_case_not_found_typed_error(monkeypatch, tmp_path: Path) -> None:
    layers = [_flat_layer(tmp_path / "a.tif", 1.0, layer_id="a-1", name="A")]
    _install_case(monkeypatch, layers)
    with pytest.raises(ProbePointCaseNotFoundError):
        await probe_point_at("case-GONE", _PT_LON, _PT_LAT)


@pytest.mark.asyncio
async def test_persistence_unavailable_typed_error(monkeypatch) -> None:
    import grace2_agent.telemetry as telemetry

    monkeypatch.setattr(telemetry, "get_persistence", lambda: None)
    with pytest.raises(ProbePointCaseNotFoundError):
        await probe_point_at("case-1", _PT_LON, _PT_LAT)
