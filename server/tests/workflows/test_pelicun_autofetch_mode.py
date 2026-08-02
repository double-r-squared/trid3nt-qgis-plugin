"""Unit tests for the pelicun_damage_assessment AUTO-FETCH input mode (PELICUN fold).

The former ``pelicun_damage_with_buildings`` composer folded into the ONE
``pelicun_damage_assessment`` template as its bbox AUTO-FETCH input mode: pass a
``bbox`` and NO ``assets_uri`` and the tool fetches a building-density grid for
the area (``compute_building_density`` -> point FlatGeobuf) and runs Pelicun
against it — spatially-distributed damage over the real built-area grid.

Coverage (both modes offline; the explicit-inventory mode is covered in
``test_run_pelicun_damage_assessment.py``):

1. test_with_buildings_composer_gone_fold_registered — the folded composer name
   is UNREGISTERED; the single template remains.
2. test_autofetch_dispatches_building_density_then_pelicun_in_order — mocked
   happy path: compute_building_density -> density_cog_to_point_fgb -> the
   Pelicun assessment (read_through) runs LAST with the fetched point-FGB as
   ``assets_uri``.
3. test_autofetch_mocked_buildings_plus_flood_expected_damage_point_count — a
   small synthetic bbox produces approximately bbox_area/cell_size_m^2 damage
   points (each with ds_mean in [0, 4]).
4. test_live_fort_myers_buildings_pelicun (TRID3NT_TEST_LIVE_PELICUN_V2=1) —
   Fort Myers run produces a non-rectangular spatial distribution of damage
   points (geographic-correctness gate, codified lesson from job-0086).
"""

from __future__ import annotations

import math
import os
import tempfile
from types import SimpleNamespace
from typing import Any
from unittest import mock as _mock
from unittest.mock import MagicMock, patch

import pytest

# Force the template module to register before we inspect TOOL_REGISTRY.
import trid3nt_server.agent.workflows.pelicun.damage_assessment.damage_assessment  # noqa: F401
from trid3nt_server.agent.tools import TOOL_REGISTRY
from trid3nt_server.agent.workflows.pelicun.damage_assessment import (
    damage_assessment as _pelicun_mod,
)
from trid3nt_server.agent.workflows.pelicun.damage_assessment.damage_assessment import (
    pelicun_damage_assessment,
)
from trid3nt_contracts.execution import LayerURI


# ---------------------------------------------------------------------------
# Test bbox constants.
# ---------------------------------------------------------------------------

# Fort Myers, FL — the canonical demo area used throughout sprint-12 testing.
_FORT_MYERS_BBOX = (-81.95, 26.45, -81.75, 26.65)

# A small synthetic bbox (roughly 2 km x 2 km at 26N lat).
_SMALL_BBOX = (-81.9, 26.5, -81.88, 26.52)

# The job-0086 Y-flip-fixed flood COG — used for live verification.
_FORT_MYERS_FLOOD_COG = (
    "s3://trid3nt-runs/01KTJX71NKGDMXB9TN0DV75JWK/flood_depth_peak_0086.tif"
)


def _mock_buildings_uri(uri: str = "gs://test-cache/buildings.tif") -> LayerURI:
    return LayerURI(
        layer_id="building-density-test",
        name="Building Density (MS Global ML; 100 m cells)",
        layer_type="raster",  # type: ignore[arg-type]
        uri=uri,
        style_preset="building_density",
        role="context",
    )


# ---------------------------------------------------------------------------
# Test 1 — the folded composer name is gone; the single template remains.
# ---------------------------------------------------------------------------


def test_with_buildings_composer_gone_fold_registered() -> None:
    """PELICUN fold: pelicun_damage_with_buildings is UNREGISTERED (folded);
    the single pelicun_damage_assessment template remains cacheable."""
    assert "pelicun_damage_with_buildings" not in TOOL_REGISTRY, (
        "the with-buildings composer folded into the template's auto-fetch mode"
    )
    entry = TOOL_REGISTRY["pelicun_damage_assessment"]
    assert entry.metadata.source_class == "pelicun_damage"
    assert entry.fn is pelicun_damage_assessment


# ---------------------------------------------------------------------------
# Test 2 — orchestration order: building_density -> density_cog -> pelicun.
# ---------------------------------------------------------------------------


def test_autofetch_dispatches_building_density_then_pelicun_in_order() -> None:
    """AUTO-FETCH mode order: compute_building_density -> density_cog_to_point_fgb
    -> the Pelicun assessment (read_through) LAST, with the fetched point-FGB as
    ``assets_uri``."""
    call_order: list[str] = []

    buildings_layer = _mock_buildings_uri("gs://test-cache/buildings-test.tif")
    fake_fgb_path = "/tmp/fake_density_pts.fgb"

    def _fake_building_density(**kwargs: Any) -> LayerURI:
        call_order.append("compute_building_density")
        return buildings_layer

    def _fake_density_cog_to_point_fgb(cog_uri: str) -> str:
        call_order.append("density_cog_to_point_fgb")
        assert cog_uri == buildings_layer.uri, (
            f"cog_uri mismatch: expected {buildings_layer.uri!r}, got {cog_uri!r}"
        )
        return fake_fgb_path

    result_layer = LayerURI(
        layer_id="pelicun-damage-test",
        name="Pelicun damage assessment (hazus_flood_v6)",
        layer_type="vector",  # type: ignore[arg-type]
        uri="gs://test-cache/damage.fgb",
        style_preset="pelicun_damage_state",
        role="primary",
    )

    def _fake_read_through(metadata, params, ext, fetch_fn, **kw):
        # The Pelicun step runs LAST, keyed on the fetched point-FGB path.
        call_order.append("read_through")
        assert params["assets_uri"] == fake_fgb_path, (
            f"assets_uri mismatch: expected {fake_fgb_path!r}, "
            f"got {params['assets_uri']!r}"
        )
        return SimpleNamespace(uri=result_layer.uri, data=b"", hit=False)

    mock_registry = {"compute_building_density": MagicMock(fn=_fake_building_density)}

    with (
        patch.object(_pelicun_mod, "TOOL_REGISTRY", mock_registry),
        patch.object(
            _pelicun_mod, "density_cog_to_point_fgb",
            side_effect=_fake_density_cog_to_point_fgb,
        ),
        patch.object(_pelicun_mod, "read_through", _fake_read_through),
        # Suppress os.unlink for the fake temp path.
        patch("os.unlink"),
    ):
        result = pelicun_damage_assessment(
            hazard_raster_uri="gs://test/flood.tif",
            bbox=_SMALL_BBOX,
            cell_size_m=100.0,
        )

    assert call_order == [
        "compute_building_density",
        "density_cog_to_point_fgb",
        "read_through",
    ], f"unexpected call order: {call_order}"
    assert result.uri == result_layer.uri


# ---------------------------------------------------------------------------
# Test 3 — mocked buildings + flood -> expected number of damage points.
# ---------------------------------------------------------------------------


def test_autofetch_mocked_buildings_plus_flood_expected_damage_point_count() -> None:
    """Mocked auto-fetch run: synthetic small bbox + 100 m cells -> approximately
    N = area/cell^2 damage points (each with ds_mean in [0, 4])."""
    try:
        import geopandas as gpd
        import numpy as np
        import rasterio
        from rasterio.transform import from_bounds
        from pyproj import Transformer
    except ImportError as exc:
        pytest.skip(f"geospatial dependencies not installed: {exc}")

    cell_size_m = 100.0
    min_lon, min_lat, max_lon, max_lat = _SMALL_BBOX

    # Build a tiny synthetic density COG with non-zero cells.
    transformer = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    sw_x, sw_y = transformer.transform(min_lon, min_lat)
    ne_x, ne_y = transformer.transform(max_lon, max_lat)
    width = max(1, int(math.ceil((ne_x - sw_x) / cell_size_m)))
    height = max(1, int(math.ceil((ne_y - sw_y) / cell_size_m)))

    arr = np.ones((height, width), dtype=np.float32) * 5.0  # 5 buildings per cell

    buildings_tmp = tempfile.NamedTemporaryFile(suffix=".tif", delete=False)
    buildings_path = buildings_tmp.name
    buildings_tmp.close()

    ne_x_snapped = sw_x + width * cell_size_m
    ne_y_snapped = sw_y + height * cell_size_m
    transform = from_bounds(sw_x, sw_y, ne_x_snapped, ne_y_snapped, width, height)

    with rasterio.open(
        buildings_path, "w", driver="GTiff", dtype="float32",
        width=width, height=height, count=1, crs="EPSG:3857", transform=transform,
    ) as dst:
        dst.write(arr, 1)

    # Build a tiny synthetic flood COG — uniform 1.5 m depth (moderate flood).
    flood_arr = np.full((height, width), 1.5, dtype=np.float32)
    flood_tmp = tempfile.NamedTemporaryFile(suffix=".tif", delete=False)
    flood_path = flood_tmp.name
    flood_tmp.close()

    lon_transform = from_bounds(min_lon, min_lat, max_lon, max_lat, width, height)
    with rasterio.open(
        flood_path, "w", driver="GTiff", dtype="float32",
        width=width, height=height, count=1, crs="EPSG:4326",
        transform=lon_transform, nodata=-9999.0,
    ) as dst:
        dst.write(flood_arr, 1)
        dst.update_tags(units="meters")

    buildings_layer = LayerURI(
        layer_id="buildings-test",
        name="Building Density test",
        layer_type="raster",  # type: ignore[arg-type]
        uri=buildings_path,
        style_preset="building_density",
        role="context",
    )

    # Stub read_through so the Pelicun fetch_fn runs locally (no GCS upload).
    out_fgb = tempfile.NamedTemporaryFile(suffix=".fgb", delete=False)
    out_fgb_path = out_fgb.name
    out_fgb.close()

    def _fake_read_through(metadata, params, ext, fetch_fn, **kw):
        data = fetch_fn()
        with open(out_fgb_path, "wb") as fh:
            fh.write(data)
        return SimpleNamespace(uri=out_fgb_path, data=data, hit=False)

    mock_registry = {
        "compute_building_density": MagicMock(fn=lambda **kw: buildings_layer),
    }

    try:
        with (
            patch.object(_pelicun_mod, "TOOL_REGISTRY", mock_registry),
            _mock.patch.object(_pelicun_mod, "read_through", _fake_read_through),
        ):
            result = pelicun_damage_assessment(
                hazard_raster_uri=flood_path,
                bbox=_SMALL_BBOX,
                cell_size_m=cell_size_m,
                realization_count=20,  # fast for unit tests
            )

        assert result.uri is not None, "damage URI must be non-None"
        damage_gdf = gpd.read_file(result.uri)
        n_cells = width * height
        # Tolerance: +/-2 cells (boundary effects at the bbox edge).
        assert abs(len(damage_gdf) - n_cells) <= 2, (
            f"expected ~{n_cells} damage points (+/-2), got {len(damage_gdf)}"
        )
        assert "ds_mean" in damage_gdf.columns, "ds_mean column missing"
        assert (damage_gdf["ds_mean"] >= 0).all(), "negative ds_mean found"
        assert (damage_gdf["ds_mean"] <= 4).all(), "ds_mean > 4 found"
    finally:
        for p in (buildings_path, flood_path, out_fgb_path):
            try:
                os.unlink(p)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Test 4 — live Fort Myers run (TRID3NT_TEST_LIVE_PELICUN_V2=1).
#
# Geographic-correctness gate (codified lesson from job-0086): the damage points
# must show a non-rectangular distribution — damage values vary spatially in a
# pattern that correlates with where buildings exist rather than uniformly
# rectangular CDP polygons.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("TRID3NT_TEST_LIVE_PELICUN_V2"),
    reason="set TRID3NT_TEST_LIVE_PELICUN_V2=1 to run the live Fort Myers test",
)
def test_live_fort_myers_buildings_pelicun() -> None:
    """Live: Fort Myers buildings (auto-fetch) -> Pelicun -> non-rectangular
    damage distribution."""
    try:
        import geopandas as gpd
        import numpy as np
    except ImportError as exc:
        pytest.skip(f"geospatial dependencies not installed: {exc}")

    # Auto-fetch mode: pass a bbox, no assets_uri.
    result = pelicun_damage_assessment(
        hazard_raster_uri=_FORT_MYERS_FLOOD_COG,
        bbox=_FORT_MYERS_BBOX,
        cell_size_m=100.0,
        fragility_set="hazus_flood_v6",
        realization_count=100,
    )

    assert result.uri is not None, "damage URI must be non-None"
    damage_gdf = gpd.read_file(result.uri)

    assert len(damage_gdf) > 0, "no damage features returned"
    assert "ds_mean" in damage_gdf.columns, "ds_mean column missing"
    assert "repair_cost_mean" in damage_gdf.columns, "repair_cost_mean column missing"

    ds = damage_gdf["ds_mean"].dropna().values
    assert (ds >= 0).all() and (ds <= 4).all(), "ds_mean out of [0, 4]"

    ds_std = float(np.std(ds))
    assert ds_std > 0.05, (
        f"ds_mean spatial std={ds_std:.3f} is near-zero — suggests rectangular "
        "aggregation rather than real building-density grid."
    )
    print(f"\n[LIVE] Fort Myers buildings (auto-fetch) -> Pelicun:")
    print(f"  damage URI: {result.uri}")
    print(f"  features: {len(damage_gdf)}")
    print(f"  ds_mean std: {ds_std:.3f} (geographic-correctness gate: PASS)")
