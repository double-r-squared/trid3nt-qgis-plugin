"""Tests for ``extract_timeseries_at_point`` (point series over frame stacks).

No network / no DynamoDB: fake persistence (the export_case_to_qgis seam),
tiny local GeoTIFF frames whose names carry the web LayerPanel frame tokens.
Also unit-tests the ``parse_frame_token`` port against the web patterns.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from trid3nt_server.tools.extract_timeseries_at_point import (
    NoFrameSequenceError,
    TimeseriesInputError,
    detect_frame_sequences,
    extract_timeseries_at_point,
    parse_frame_token,
)

_BBOX = (-85.5, 29.9, -85.4, 30.0)
_PT_LON, _PT_LAT = -85.485, 29.95


def _write_raster(path: Path, fill: float, nodata: float | None = None) -> Path:
    import rasterio
    from rasterio.transform import from_bounds

    transform = from_bounds(*_BBOX, 10, 10)
    kwargs = dict(
        driver="GTiff",
        height=10,
        width=10,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=transform,
    )
    if nodata is not None:
        kwargs["nodata"] = nodata
    with rasterio.open(path, "w", **kwargs) as ds:
        ds.write(np.full((10, 10), fill, dtype="float32"), 1)
    return path


class FakePersistence:
    def __init__(self, case) -> None:
        self._case = case

    async def get_case(self, case_id: str):
        if self._case is not None and self._case.case_id == case_id:
            return self._case
        return None


def _install_case(monkeypatch, layers, case_id="case-1", title="Anim Case"):
    case = SimpleNamespace(
        case_id=case_id, title=title, bbox=None, loaded_layer_summaries=layers
    )
    import trid3nt_server.telemetry as telemetry

    monkeypatch.setattr(telemetry, "get_persistence", lambda: FakePersistence(case))
    return case


def _frame_layers(tmp_path: Path, values=(0.5, 1.5, 2.5)) -> list[dict]:
    layers = []
    for i, v in enumerate(values, start=1):
        path = _write_raster(tmp_path / f"frame{i}.tif", v)
        layers.append(
            {
                "layer_id": f"depth-step-{i}",
                "name": f"Flood depth step {i}",
                "layer_type": "raster",
                "uri": str(path),
            }
        )
    return layers


# --------------------------------------------------------------------------- #
# Frame-token parsing (web LayerPanel port)
# --------------------------------------------------------------------------- #


def test_parse_step_token() -> None:
    token = parse_frame_token("Flood depth step 3")
    assert token is not None
    assert token["value"] == 3
    assert token["label"] == "step 3"
    assert token["stem"] == "flood depth"


def test_parse_forecast_lead_hour() -> None:
    token = parse_frame_token("HRRR reflectivity F+06h")
    assert token is not None
    assert token["value"] == 6
    assert token["label"] == "F+06h"


def test_parse_iso_valid_time_preferred_as_label() -> None:
    token = parse_frame_token("GOES Fire Temperature step 2 2026-06-22T18:05:00Z")
    assert token is not None
    assert token["value"] == 2
    assert token["label"] == "2026-06-22 18:05Z"
    # The ISO time is stripped from the stem so siblings share it.
    assert "2026" not in token["stem"]


def test_parse_no_token_returns_none() -> None:
    assert parse_frame_token("Flood depth (peak)") is None
    assert parse_frame_token("") is None


def test_detect_sequences_requires_two_members() -> None:
    layers = [
        {"name": "Depth step 1", "layer_type": "raster", "uri": "a.tif"},
        {"name": "Lonely step 5", "layer_type": "raster", "uri": "b.tif"},
        {"name": "Depth step 2", "layer_type": "raster", "uri": "c.tif"},
    ]
    seqs = detect_frame_sequences(layers)
    assert list(seqs) == ["depth"]
    assert [m["value"] for m in seqs["depth"]] == [1, 2]


def test_detect_sequences_ignores_vectors_and_duplicates() -> None:
    layers = [
        {"name": "Depth step 1", "layer_type": "vector", "uri": "a.fgb"},
        {"name": "Depth step 2", "layer_type": "raster", "uri": "b.tif"},
        # Duplicate token value breaks strict monotonicity -> no group.
        {"name": "Depth step 2", "layer_type": "raster", "uri": "c.tif"},
    ]
    assert detect_frame_sequences(layers) == {}


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_ordered_series_at_point(monkeypatch, tmp_path: Path) -> None:
    _install_case(monkeypatch, _frame_layers(tmp_path))
    result = await extract_timeseries_at_point(
        lon=_PT_LON, lat=_PT_LAT, case_id="case-1"
    )
    assert result["sequence"] == "flood depth"
    assert result["frame_count"] == 3
    assert result["sampled_count"] == 3
    labels = [e["label"] for e in result["series"]]
    values = [e["value"] for e in result["series"]]
    assert labels == ["step 1", "step 2", "step 3"]
    assert values == pytest.approx([0.5, 1.5, 2.5])
    assert result["available_sequences"] == ["flood depth"]


@pytest.mark.asyncio
async def test_frames_out_of_case_order_are_sorted(
    monkeypatch, tmp_path: Path
) -> None:
    layers = _frame_layers(tmp_path)
    layers.reverse()  # case order 3,2,1 -- series must still be 1,2,3
    _install_case(monkeypatch, layers)
    result = await extract_timeseries_at_point(
        lon=_PT_LON, lat=_PT_LAT, case_id="case-1"
    )
    assert [e["index"] for e in result["series"]] == [1, 2, 3]
    assert [e["value"] for e in result["series"]] == pytest.approx([0.5, 1.5, 2.5])


@pytest.mark.asyncio
async def test_layer_filter_selects_sequence(monkeypatch, tmp_path: Path) -> None:
    depth = _frame_layers(tmp_path)
    smoke = []
    for i, v in enumerate((7.0, 8.0), start=1):
        path = _write_raster(tmp_path / f"smoke{i}.tif", v)
        smoke.append(
            {
                "layer_id": f"smoke-{i}",
                "name": f"Smoke forecast F+{i:02d}h",
                "layer_type": "raster",
                "uri": str(path),
            }
        )
    _install_case(monkeypatch, depth + smoke)
    result = await extract_timeseries_at_point(
        lon=_PT_LON, lat=_PT_LAT, layer="smoke forecast", case_id="case-1"
    )
    assert result["sequence"] == "smoke forecast"
    assert [e["value"] for e in result["series"]] == pytest.approx([7.0, 8.0])
    assert sorted(result["available_sequences"]) == ["flood depth", "smoke forecast"]


@pytest.mark.asyncio
async def test_default_picks_largest_sequence(monkeypatch, tmp_path: Path) -> None:
    depth = _frame_layers(tmp_path)  # 3 frames
    pair = []
    for i, v in enumerate((7.0, 8.0), start=1):
        path = _write_raster(tmp_path / f"p{i}.tif", v)
        pair.append(
            {
                "layer_id": f"p-{i}",
                "name": f"Pair frame {i}",
                "layer_type": "raster",
                "uri": str(path),
            }
        )
    _install_case(monkeypatch, depth + pair)
    result = await extract_timeseries_at_point(
        lon=_PT_LON, lat=_PT_LAT, case_id="case-1"
    )
    assert result["sequence"] == "flood depth"  # largest group wins


@pytest.mark.asyncio
async def test_unreadable_frame_is_honest_entry(monkeypatch, tmp_path: Path) -> None:
    layers = _frame_layers(tmp_path, values=(0.5, 1.5))
    layers[1]["uri"] = "/nonexistent/frame2.tif"
    _install_case(monkeypatch, layers)
    result = await extract_timeseries_at_point(
        lon=_PT_LON, lat=_PT_LAT, case_id="case-1"
    )
    assert result["frame_count"] == 2
    assert result["series"][0]["value"] == pytest.approx(0.5)
    assert result["series"][1]["value"] is None
    assert "error" in result["series"][1]
    assert result["sampled_count"] == 1


# --------------------------------------------------------------------------- #
# Typed errors
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_no_sequence_typed_error(monkeypatch, tmp_path: Path) -> None:
    path = _write_raster(tmp_path / "single.tif", 1.0)
    _install_case(
        monkeypatch,
        [
            {
                "layer_id": "single",
                "name": "Flood depth (peak)",
                "layer_type": "raster",
                "uri": str(path),
            }
        ],
    )
    with pytest.raises(NoFrameSequenceError):
        await extract_timeseries_at_point(
            lon=_PT_LON, lat=_PT_LAT, case_id="case-1"
        )


@pytest.mark.asyncio
async def test_empty_case_typed_error(monkeypatch) -> None:
    _install_case(monkeypatch, [])
    with pytest.raises(NoFrameSequenceError):
        await extract_timeseries_at_point(
            lon=_PT_LON, lat=_PT_LAT, case_id="case-1"
        )


@pytest.mark.asyncio
async def test_filter_miss_typed_error(monkeypatch, tmp_path: Path) -> None:
    _install_case(monkeypatch, _frame_layers(tmp_path))
    with pytest.raises(NoFrameSequenceError) as exc_info:
        await extract_timeseries_at_point(
            lon=_PT_LON, lat=_PT_LAT, layer="wind speed", case_id="case-1"
        )
    # The honest miss lists what IS available.
    assert "flood depth" in str(exc_info.value)


@pytest.mark.asyncio
async def test_no_location_typed_error(monkeypatch, tmp_path: Path) -> None:
    _install_case(monkeypatch, _frame_layers(tmp_path))
    with pytest.raises(TimeseriesInputError):
        await extract_timeseries_at_point(case_id="case-1")


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #


def test_registered_in_tool_registry() -> None:
    from trid3nt_server.tools import TOOL_REGISTRY

    entry = TOOL_REGISTRY.get("extract_timeseries_at_point")
    assert entry is not None
    assert entry.metadata.cacheable is False
    assert entry.metadata.ttl_class == "live-no-cache"
