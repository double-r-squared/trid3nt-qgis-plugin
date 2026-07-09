"""Tests for ``compose_case_report`` (markdown case situation report).

No network / no DynamoDB: fake persistence (the export_case_to_qgis seam),
tiny local raster/vector artifacts, report written into ``tmp_path``. The
exposure section is fed through ``compute_exposure_summary``'s session store
directly (no fetches).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from grace2_agent.tools import compute_exposure_summary as exposure_mod
from grace2_agent.tools.compose_case_report import (
    CaseReportInputError,
    CaseReportNotFoundError,
    compose_case_report,
)

_BBOX = (-85.5, 29.9, -85.4, 30.0)


def _write_raster(path: Path, data: np.ndarray, units: str | None = None) -> Path:
    import rasterio
    from rasterio.transform import from_bounds

    h, w = data.shape
    transform = from_bounds(*_BBOX, w, h)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=h,
        width=w,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=transform,
    ) as ds:
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


def _install_case(monkeypatch, layers, case_id="case-9", bbox=None, **extra):
    case = SimpleNamespace(
        case_id=case_id,
        title=extra.pop("title", "Hurricane Michael Surge"),
        bbox=bbox,
        loaded_layer_summaries=layers,
        primary_hazard=extra.pop("primary_hazard", None),
        created_at=extra.pop("created_at", "2026-06-20T12:00:00Z"),
        **extra,
    )
    import grace2_agent.telemetry as telemetry

    monkeypatch.setattr(telemetry, "get_persistence", lambda: FakePersistence(case))
    return case


@pytest.fixture()
def case_layers(tmp_path: Path) -> list[dict]:
    import geopandas as gpd
    from shapely.geometry import Point

    data = np.linspace(0.0, 3.0, 100, dtype="float32").reshape(10, 10)
    raster = _write_raster(tmp_path / "depth.tif", data, units="m")

    gdf = gpd.GeoDataFrame(
        {"val_struct": [100.0, 250.0]},
        geometry=[Point(-85.45, 29.95), Point(-85.44, 29.96)],
        crs="EPSG:4326",
    )
    vector = tmp_path / "assets.geojson"
    gdf.to_file(vector, driver="GeoJSON")

    return [
        {
            "layer_id": "depth-1",
            "name": "Flood depth",
            "layer_type": "raster",
            "uri": str(raster),
        },
        {
            "layer_id": "assets-1",
            "name": "Structures",
            "layer_type": "vector",
            "uri": str(vector),
        },
    ]


@pytest.fixture(autouse=True)
def clean_exposure_store():
    exposure_mod._SESSION_EXPOSURE.clear()
    yield
    exposure_mod._SESSION_EXPOSURE.clear()


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_report_written_with_layers_and_stats(
    monkeypatch, tmp_path: Path, case_layers
) -> None:
    _install_case(
        monkeypatch,
        case_layers,
        bbox=list(_BBOX),
        primary_hazard="coastal_flood",
    )
    result = await compose_case_report(
        case_id="case-9", output_dir=str(tmp_path / "report")
    )

    assert result["status"] == "ok"
    assert result["case_id"] == "case-9"
    assert result["layer_count"] == 2
    assert result["stats_computed_count"] == 2
    assert result["stats_unavailable_count"] == 0
    assert result["has_exposure_summary"] is False

    report = Path(result["report_path"])
    assert report.is_file()
    text = report.read_text(encoding="utf-8")
    # Title + date + case id.
    assert "# Situation report: Hurricane Michael Surge" in text
    assert "`case-9`" in text
    # AOI bbox.
    assert "-85.50000, 29.90000, -85.40000, 30.00000" in text
    # Layer rows with real stats (raster max is 3, mean 1.5; vector count 2).
    assert "**Flood depth** (raster)" in text
    assert "max 3" in text
    assert "**Structures** (vector)" in text
    assert "2 feature(s)" in text
    # Sim params section carries the denormalized hazard.
    assert "primary_hazard: coastal_flood" in text
    # Honest exposure absence.
    assert "No exposure summary was computed this session" in text
    # The result dict is LayerURI-free (plain scalars/strings only).
    from grace2_contracts.execution import LayerURI

    assert not any(isinstance(v, LayerURI) for v in result.values())


@pytest.mark.asyncio
async def test_exposure_section_from_session_store(
    monkeypatch, tmp_path: Path, case_layers
) -> None:
    _install_case(monkeypatch, case_layers, bbox=list(_BBOX))
    # Seed the session store the way compute_exposure_summary does (global
    # slot: no Case is bound to this test's "turn").
    exposure_mod._SESSION_EXPOSURE["__global__"] = {
        "population": 1234,
        "buildings": 56,
        "area_km2": 7.89,
        "threshold": 0.5,
        "errors": {},
        "hazard_layer_uri": "s3://runs/depth.tif",
    }
    result = await compose_case_report(
        case_id="case-9", output_dir=str(tmp_path / "report")
    )
    assert result["has_exposure_summary"] is True
    text = Path(result["report_path"]).read_text(encoding="utf-8")
    assert "Population exposed: 1234" in text
    assert "Buildings exposed: 56" in text
    assert "7.89 km^2" in text
    assert "value > 0.5" in text


@pytest.mark.asyncio
async def test_per_layer_stats_failure_is_honest_row(
    monkeypatch, tmp_path: Path, case_layers
) -> None:
    layers = [
        {
            "layer_id": "gone",
            "name": "Missing layer",
            "layer_type": "raster",
            "uri": "/nonexistent/gone.tif",
        }
    ] + case_layers
    _install_case(monkeypatch, layers)
    result = await compose_case_report(
        case_id="case-9", output_dir=str(tmp_path / "report")
    )
    assert result["status"] == "ok"
    assert result["stats_unavailable_count"] == 1
    assert result["stats_computed_count"] == 2
    text = Path(result["report_path"]).read_text(encoding="utf-8")
    assert "statistics unavailable" in text
    assert "Missing layer" in text


@pytest.mark.asyncio
async def test_empty_case_states_no_layers(monkeypatch, tmp_path: Path) -> None:
    _install_case(monkeypatch, [])
    result = await compose_case_report(
        case_id="case-9", output_dir=str(tmp_path / "report")
    )
    assert result["layer_count"] == 0
    text = Path(result["report_path"]).read_text(encoding="utf-8")
    assert "No layers are loaded on this case" in text
    assert "No AOI bbox is recorded" in text


@pytest.mark.asyncio
async def test_include_layer_stats_false_skips_staging(
    monkeypatch, tmp_path: Path
) -> None:
    # Layers with unreadable uris: with stats off the report must not touch them.
    layers = [
        {
            "layer_id": "l1",
            "name": "Some layer",
            "layer_type": "raster",
            "uri": "/nonexistent/a.tif",
        }
    ]
    _install_case(monkeypatch, layers)
    result = await compose_case_report(
        case_id="case-9",
        output_dir=str(tmp_path / "report"),
        include_layer_stats=False,
    )
    assert result["stats_computed_count"] == 0
    assert result["stats_unavailable_count"] == 0
    text = Path(result["report_path"]).read_text(encoding="utf-8")
    assert "**Some layer** (raster)" in text
    assert "statistics unavailable" not in text


@pytest.mark.asyncio
async def test_default_output_dir_uses_export_env(
    monkeypatch, tmp_path: Path, case_layers
) -> None:
    _install_case(monkeypatch, case_layers)
    monkeypatch.setenv("GRACE2_EXPORT_DIR", str(tmp_path / "exports"))
    result = await compose_case_report(case_id="case-9", include_layer_stats=False)
    assert result["report_path"].startswith(str(tmp_path / "exports"))
    assert Path(result["report_path"]).is_file()


# --------------------------------------------------------------------------- #
# Typed errors
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_unknown_case_typed_error(monkeypatch, case_layers) -> None:
    _install_case(monkeypatch, case_layers, case_id="case-9")
    with pytest.raises(CaseReportNotFoundError):
        await compose_case_report(case_id="case-other")


@pytest.mark.asyncio
async def test_no_case_typed_error(monkeypatch, case_layers) -> None:
    _install_case(monkeypatch, case_layers)
    # No case_id and no turn-bound Case.
    with pytest.raises(CaseReportInputError):
        await compose_case_report()


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #


def test_registered_in_tool_registry() -> None:
    from grace2_agent.tools import TOOL_REGISTRY

    entry = TOOL_REGISTRY.get("compose_case_report")
    assert entry is not None
    assert entry.metadata.cacheable is False
    assert entry.metadata.ttl_class == "live-no-cache"
