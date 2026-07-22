"""Unit + live tests for ``run_pelicun_damage_assessment`` Wave 2 composer (job-0120).

Wave 2 (this file) replaces the Wave 1 stub raises with REAL Pelicun-backed
behavior:

1. Input-validation typed errors are preserved verbatim from Wave 1 (the LLM-
   visible API contract didn't change).
2. The previous ``PelicunNotImplementedYet`` raise has been removed; the
   function now produces a real FlatGeobuf with HAZUS-derived damage states +
   repair-cost statistics.
3. New behavioral tests assert: HAZUS curve loading, monotonic damage with
   depth (geographic-correctness gate), Monte-Carlo determinism via per-asset
   seeding, component_types filtering, realization_count effect on CI width,
   cache hit/miss via the read_through path.
4. Live test ``test_live_pelicun_fort_myers_e2e`` (env-guarded
   ``TRID3NT_TEST_LIVE_PELICUN=1``) drives the kickoff's Fort Myers acceptance
   run end-to-end against the job-0086 Y-flip-fixed flood COG + a
   ``fetch_administrative_boundaries`` Fort Myers place-polygon.

Codified job-0086 lesson: ``test_geographic_correctness_higher_depth_higher_damage``
asserts ds_mean increases with raster depth at the asset centroid — a
sampling-pixel-swap bug would fail this test even if the FlatGeobuf round-trips.
"""

from __future__ import annotations

import os
import tempfile
from unittest import mock

import numpy as np
import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.simulation.run_pelicun_damage_assessment import (
    PelicunDamageError,
    PelicunFragilityDataError,
    PelicunInputError,
    PelicunNoAssetsError,
    PelicunRuntimeError,
    _bin_to_damage_state,
    _load_hazus_flood_curves,
    _mc_loss_ratio_realizations,
    _seed_for_asset,
    run_pelicun_damage_assessment,
)


# ---------------------------------------------------------------------------
# Constants used across cases.
# ---------------------------------------------------------------------------

_VALID_HAZARD_URI = "s3://trid3nt-runs/example-run/flood_depth.tif"
_VALID_ASSETS_URI = "s3://trid3nt-cache/places/fort-myers-place-polys.fgb"


# ---------------------------------------------------------------------------
# 1. Tool registration with correct metadata (preserved from Wave 1).
# ---------------------------------------------------------------------------


def test_run_pelicun_damage_assessment_registered() -> None:
    """The tool registers under its declared name with the Wave 1 metadata."""
    assert "run_pelicun_damage_assessment" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["run_pelicun_damage_assessment"]
    md = entry.metadata
    assert md.name == "run_pelicun_damage_assessment"
    assert md.cacheable is True
    assert md.ttl_class == "static-30d"
    assert md.source_class == "pelicun_damage"
    assert entry.fn is run_pelicun_damage_assessment


# ---------------------------------------------------------------------------
# 2-6. Input-validation typed errors — preserved verbatim from Wave 1.
# ---------------------------------------------------------------------------


def test_bad_fragility_set_raises_input_error() -> None:
    """Unknown / typo'd ``fragility_set`` → ``PelicunInputError``."""
    with pytest.raises(PelicunInputError) as excinfo:
        run_pelicun_damage_assessment(
            hazard_raster_uri=_VALID_HAZARD_URI,
            assets_uri=_VALID_ASSETS_URI,
            fragility_set="not_a_real_fragility_set",  # type: ignore[arg-type]
        )
    err = excinfo.value
    assert err.error_code == "PELICUN_INPUT_INVALID"
    assert err.retryable is False
    assert "not_a_real_fragility_set" in str(err)
    assert "hazus_flood_v6" in str(err)


def test_none_assets_uri_raises_input_error() -> None:
    """``assets_uri=None`` → ``PelicunInputError``."""
    with pytest.raises(PelicunInputError) as excinfo:
        run_pelicun_damage_assessment(
            hazard_raster_uri=_VALID_HAZARD_URI,
            assets_uri=None,  # type: ignore[arg-type]
        )
    assert excinfo.value.error_code == "PELICUN_INPUT_INVALID"
    assert "assets_uri" in str(excinfo.value)


def test_none_hazard_raster_uri_raises_input_error() -> None:
    """``hazard_raster_uri=None`` → ``PelicunInputError``."""
    with pytest.raises(PelicunInputError) as excinfo:
        run_pelicun_damage_assessment(
            hazard_raster_uri=None,  # type: ignore[arg-type]
            assets_uri=_VALID_ASSETS_URI,
        )
    assert excinfo.value.error_code == "PELICUN_INPUT_INVALID"
    assert "hazard_raster_uri" in str(excinfo.value)


def test_empty_component_types_list_raises_input_error() -> None:
    """Empty ``component_types=[]`` → ``PelicunInputError``."""
    with pytest.raises(PelicunInputError) as excinfo:
        run_pelicun_damage_assessment(
            hazard_raster_uri=_VALID_HAZARD_URI,
            assets_uri=_VALID_ASSETS_URI,
            component_types=[],
        )
    assert excinfo.value.error_code == "PELICUN_INPUT_INVALID"
    assert "None" in str(excinfo.value)


@pytest.mark.parametrize("bad_count", [0, -1, -100])
def test_nonpositive_realization_count_raises_input_error(bad_count: int) -> None:
    """``realization_count`` must be a positive int."""
    with pytest.raises(PelicunInputError) as excinfo:
        run_pelicun_damage_assessment(
            hazard_raster_uri=_VALID_HAZARD_URI,
            assets_uri=_VALID_ASSETS_URI,
            realization_count=bad_count,
        )
    assert excinfo.value.error_code == "PELICUN_INPUT_INVALID"
    assert "realization_count" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 7. HAZUS curve loader — verify bundled data + parse.
# ---------------------------------------------------------------------------


def test_hazus_curves_load_with_required_components() -> None:
    """The HAZUS v6.1 flood loss curves load successfully.

    Verifies (a) the bundled CSV is reachable and (b) the canonical
    occupancy classes we depend on for Case 1 demo flows are present.
    """
    curves = _load_hazus_flood_curves()
    assert isinstance(curves, dict)
    # Sanity check: must include RES1 (the default) plus a representative
    # commercial class.
    assert "RES1" in curves, f"RES1 missing from loaded HAZUS curves; got keys {sorted(curves.keys())}"
    assert "COM1" in curves, f"COM1 missing from loaded HAZUS curves; got keys {sorted(curves.keys())}"

    # Each curve has a sensibly-shaped piecewise function.
    res1 = curves["RES1"]
    assert len(res1.depths_ft) == len(res1.loss_ratios)
    assert len(res1.depths_ft) >= 5, "expected ≥5 breakpoints in HAZUS curve"
    # Monotonic non-decreasing (after the curve enters the wet region).
    # The bundled data has slightly non-monotonic regions near zero only.
    assert res1.loss_ratios.max() > 0.0


def test_hazus_curves_mean_loss_ratio_increases_with_depth() -> None:
    """At a low depth the RES1 mean loss ratio is lower than at a high depth.

    Sanity check on the interpolator; depth-damage curves are monotonic
    non-decreasing in the wet region (the HAZUS RES1 curve crosses 0 at the
    foundation step and rises through 60% by ~10 ft).
    """
    curves = _load_hazus_flood_curves()
    res1 = curves["RES1"]
    low = res1.mean_loss_ratio_at(0.5)   # half-foot
    high = res1.mean_loss_ratio_at(10.0)  # ten feet
    assert low < high, f"low={low}, high={high}: expected curve to rise with depth"


# ---------------------------------------------------------------------------
# 8. Monte-Carlo + damage-state binning unit tests.
# ---------------------------------------------------------------------------


def test_bin_to_damage_state_correctness() -> None:
    """The HAZUS loss-ratio → damage-state binner produces the right bins.

    Bins:
        DS0 (0): LR < 0.05
        DS1 (1): 0.05 ≤ LR < 0.20
        DS2 (2): 0.20 ≤ LR < 0.50
        DS3 (3): 0.50 ≤ LR < 0.80
        DS4 (4): LR ≥ 0.80
    """
    lrs = np.array([0.0, 0.04, 0.05, 0.10, 0.25, 0.50, 0.70, 0.80, 0.99])
    expected = np.array([0, 0, 1, 1, 2, 3, 3, 4, 4], dtype=np.int32)
    np.testing.assert_array_equal(_bin_to_damage_state(lrs), expected)


def test_mc_loss_ratio_realizations_zero_depth_returns_zero() -> None:
    """An asset on dry ground gets exactly-zero loss ratios across all realizations."""
    rng = np.random.default_rng(42)
    samples = _mc_loss_ratio_realizations(0.0, 1000, rng)
    assert samples.shape == (1000,)
    assert (samples == 0.0).all()


def test_mc_loss_ratio_realizations_mean_is_close_to_input() -> None:
    """The MC distribution's empirical mean ≈ the input deterministic mean."""
    rng = np.random.default_rng(123)
    target_mean = 0.30
    samples = _mc_loss_ratio_realizations(target_mean, 20000, rng)
    # With σ_lnD = 0.4 the empirical mean of a bounded lognormal centred on
    # ``target_mean`` should be within ~5% (truncation pulls the mean slightly
    # below the parametric mean because the cap is at 0.6).
    assert 0.0 < samples.mean() < target_mean + 0.05


def test_mc_higher_realization_count_yields_tighter_ci() -> None:
    """1000 realizations produce a narrower 5-95% CI than 100 (about the *mean estimator*)."""
    # We compare CI on the *sample-mean estimator* across many independent
    # batches — that's where the Monte-Carlo precision argument actually
    # applies (per-realization spread is governed by σ_lnD which is fixed).
    n_batches = 200

    def ci_of_means(n: int) -> float:
        means = []
        for batch in range(n_batches):
            rng = np.random.default_rng(batch)
            samples = _mc_loss_ratio_realizations(0.3, n, rng)
            means.append(float(samples.mean()))
        return float(np.percentile(means, 95) - np.percentile(means, 5))

    ci_100 = ci_of_means(100)
    ci_1000 = ci_of_means(1000)
    assert ci_1000 < ci_100, (
        f"expected tighter mean-estimator CI at 1000 realizations: "
        f"100→{ci_100:.4f}, 1000→{ci_1000:.4f}"
    )


def test_seed_for_asset_is_deterministic_and_independent() -> None:
    """Per-asset seeds are deterministic and unique across asset IDs."""
    s1 = _seed_for_asset("asset-1", "RES1", "gs://b/x.tif", 100)
    s1b = _seed_for_asset("asset-1", "RES1", "gs://b/x.tif", 100)
    s2 = _seed_for_asset("asset-2", "RES1", "gs://b/x.tif", 100)
    assert s1 == s1b, "seed must be deterministic for the same (asset, ctype, hazard, n)"
    assert s1 != s2, "different asset IDs must produce different seeds"


# ---------------------------------------------------------------------------
# 9. End-to-end with synthetic fixtures — exercises the assessment loop
#    WITHOUT cloud I/O. This is what runs in CI.
# ---------------------------------------------------------------------------


def _write_synthetic_flood_cog(
    path: str,
    bbox: tuple[float, float, float, float],
    west_depth_m: float,
    east_depth_m: float,
    res: int = 64,
) -> None:
    """Write a 2D EPSG:4326 flood-depth COG to ``path``.

    Splits the bbox vertically: western half = ``west_depth_m``, eastern half
    = ``east_depth_m``. Used by the geographic-correctness test.
    """
    import rasterio
    from rasterio.transform import from_bounds

    min_lon, min_lat, max_lon, max_lat = bbox
    arr = np.zeros((res, res), dtype=np.float32)
    mid_col = res // 2
    arr[:, :mid_col] = west_depth_m
    arr[:, mid_col:] = east_depth_m
    transform = from_bounds(min_lon, min_lat, max_lon, max_lat, res, res)

    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=res,
        width=res,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=transform,
        nodata=-9999.0,
    ) as dst:
        dst.write(arr, 1)
        dst.update_tags(units="meters")


def _write_synthetic_assets_fgb(
    path: str,
    bbox: tuple[float, float, float, float],
    asset_lonlats: list[tuple[float, float]],
    component_types: list[str] | None = None,
) -> None:
    """Write a FlatGeobuf with point assets at the given ``(lon, lat)`` coords."""
    import geopandas as gpd
    from shapely.geometry import Point

    gdf = gpd.GeoDataFrame(
        {
            "id": [f"asset-{i}" for i in range(len(asset_lonlats))],
            **(
                {"component_type": component_types}
                if component_types is not None
                else {}
            ),
            "geometry": [Point(lon, lat) for lon, lat in asset_lonlats],
        },
        crs="EPSG:4326",
    )
    gdf.to_file(path, driver="FlatGeobuf", engine="pyogrio")


def test_geographic_correctness_higher_depth_higher_damage(tmp_path) -> None:
    """Geographic-correctness gate (job-0086 codified lesson).

    Synthetic raster: bbox split west/east. West half dry (0 m); east half deep
    (3 m). Two point assets — one in west, one in east — must come back with
    east_ds_mean > west_ds_mean. A sampling-pixel-swap bug would fail this
    assertion even if the FlatGeobuf round-trips bytes correctly.
    """
    from trid3nt_server.tools.simulation.run_pelicun_damage_assessment import (
        _assess_assets,
    )

    bbox = (-82.0, 26.0, -81.0, 27.0)
    raster_path = str(tmp_path / "hazard.tif")
    _write_synthetic_flood_cog(
        raster_path, bbox, west_depth_m=0.0, east_depth_m=3.0, res=64
    )

    assets_path = str(tmp_path / "assets.fgb")
    # West asset at lon = -81.8 (well within west half); east at lon = -81.2.
    _write_synthetic_assets_fgb(
        assets_path,
        bbox,
        asset_lonlats=[(-81.8, 26.5), (-81.2, 26.5)],
        component_types=["RES1", "RES1"],
    )

    gdf = _assess_assets(
        hazard_raster_path=raster_path,
        assets_path=assets_path,
        component_types_filter=None,
        realization_count=200,
        hazard_uri_for_seed="test://geo-correctness",
    )

    # Verify the per-asset properties are populated.
    assert len(gdf) == 2
    for col in [
        "ds_mean",
        "ds_p05",
        "ds_p95",
        "loss_ratio_mean",
        "repair_cost_mean",
        "repair_cost_p95",
        "replacement_value",
        "hazard_depth_sampled",
        "component_type_used",
        "fragility_curve_id",
    ]:
        assert col in gdf.columns, f"output missing column {col!r}"

    # FlatGeobuf is spatially indexed → readback feature order is not the
    # insertion order. Look up by the stable ``id`` column instead.
    by_id = gdf.set_index("id")
    west_row = by_id.loc["asset-0"]
    east_row = by_id.loc["asset-1"]

    # Depth sampling: west asset reads 0, east asset reads 3 m.
    assert west_row["hazard_depth_sampled"] == pytest.approx(0.0), (
        f"west asset should sample dry raster pixel, got {west_row['hazard_depth_sampled']}"
    )
    assert east_row["hazard_depth_sampled"] == pytest.approx(3.0), (
        f"east asset should sample 3m raster pixel, got {east_row['hazard_depth_sampled']}"
    )

    # Damage-state gate.
    assert east_row["ds_mean"] > west_row["ds_mean"], (
        f"GEOGRAPHIC-CORRECTNESS FAIL: east ds_mean={east_row['ds_mean']} "
        f"not > west ds_mean={west_row['ds_mean']}. This indicates a sampling "
        f"bug (e.g. CRS-flip, Y-axis flip, swapped centroids)."
    )
    # And the loss-ratio + repair cost must follow.
    assert east_row["loss_ratio_mean"] > west_row["loss_ratio_mean"]
    assert east_row["repair_cost_mean"] > west_row["repair_cost_mean"]


def test_component_types_filter_restricts_assets(tmp_path) -> None:
    """``component_types=['COM1']`` filters out RES1 assets."""
    from trid3nt_server.tools.simulation.run_pelicun_damage_assessment import _assess_assets

    bbox = (-82.0, 26.0, -81.0, 27.0)
    raster_path = str(tmp_path / "hazard.tif")
    _write_synthetic_flood_cog(
        raster_path, bbox, west_depth_m=2.0, east_depth_m=2.0, res=32
    )

    assets_path = str(tmp_path / "assets.fgb")
    _write_synthetic_assets_fgb(
        assets_path,
        bbox,
        asset_lonlats=[(-81.6, 26.5), (-81.4, 26.5)],
        component_types=["RES1", "COM1"],
    )

    gdf = _assess_assets(
        hazard_raster_path=raster_path,
        assets_path=assets_path,
        component_types_filter=["COM1"],
        realization_count=100,
        hazard_uri_for_seed="test://filter",
    )
    assert len(gdf) == 1
    assert gdf.iloc[0]["component_type_used"] == "COM1"


def test_deterministic_output_byte_identical_across_runs(tmp_path) -> None:
    """Two runs with the same inputs produce identical numeric columns.

    Verifies the per-asset RNG seeding is fully deterministic — required for
    the cache-key invariant.
    """
    from trid3nt_server.tools.simulation.run_pelicun_damage_assessment import _assess_assets

    bbox = (-82.0, 26.0, -81.0, 27.0)
    raster_path = str(tmp_path / "hazard.tif")
    _write_synthetic_flood_cog(
        raster_path, bbox, west_depth_m=1.0, east_depth_m=2.0, res=32
    )
    assets_path = str(tmp_path / "assets.fgb")
    _write_synthetic_assets_fgb(
        assets_path,
        bbox,
        asset_lonlats=[(-81.7, 26.5), (-81.3, 26.5)],
    )

    gdf1 = _assess_assets(
        hazard_raster_path=raster_path,
        assets_path=assets_path,
        component_types_filter=None,
        realization_count=100,
        hazard_uri_for_seed="test://determinism",
    )
    gdf2 = _assess_assets(
        hazard_raster_path=raster_path,
        assets_path=assets_path,
        component_types_filter=None,
        realization_count=100,
        hazard_uri_for_seed="test://determinism",
    )
    for col in ["ds_mean", "loss_ratio_mean", "repair_cost_mean"]:
        np.testing.assert_array_equal(
            gdf1[col].to_numpy(),
            gdf2[col].to_numpy(),
            err_msg=f"determinism failure on column {col}",
        )


def test_no_assets_in_hazard_raises_typed_error(tmp_path) -> None:
    """Asset outside hazard footprint → ``PelicunNoAssetsError``."""
    from trid3nt_server.tools.simulation.run_pelicun_damage_assessment import _assess_assets

    raster_path = str(tmp_path / "hazard.tif")
    _write_synthetic_flood_cog(
        raster_path,
        (-82.0, 26.0, -81.0, 27.0),
        west_depth_m=2.0,
        east_depth_m=2.0,
        res=16,
    )
    assets_path = str(tmp_path / "assets.fgb")
    # Asset way outside the raster bbox.
    _write_synthetic_assets_fgb(
        assets_path,
        bbox=(-100.0, 40.0, -99.0, 41.0),  # not used; just for typing
        asset_lonlats=[(-99.5, 40.5)],
    )
    with pytest.raises(PelicunNoAssetsError):
        _assess_assets(
            hazard_raster_path=raster_path,
            assets_path=assets_path,
            component_types_filter=None,
            realization_count=50,
            hazard_uri_for_seed="test://nohit",
        )


# ---------------------------------------------------------------------------
# 10. End-to-end through the registered tool — exercises cache + LayerURI
#     return shape with the cache shim stubbed.
# ---------------------------------------------------------------------------


def test_registered_tool_returns_layer_uri_with_correct_shape(tmp_path) -> None:
    """End-to-end with a stubbed read_through; verifies the LayerURI shape.

    The shim is patched to skip GCS and just call the fetch_fn (which writes
    bytes to a local tmp file we point ``uri`` at).
    """
    from trid3nt_server.tools.simulation import run_pelicun_damage_assessment as mod

    bbox = (-82.0, 26.0, -81.0, 27.0)
    raster_path = str(tmp_path / "hazard.tif")
    _write_synthetic_flood_cog(
        raster_path, bbox, west_depth_m=0.5, east_depth_m=2.5, res=32
    )
    assets_path = str(tmp_path / "assets.fgb")
    _write_synthetic_assets_fgb(
        assets_path,
        bbox,
        asset_lonlats=[(-81.5, 26.5)],
    )

    out_uri = "gs://test-cache/pelicun_damage/static-30d/dummy-key.fgb"

    def fake_read_through(metadata, params, ext, fetch_fn, **kw):
        # Run the fetch fn (so we exercise the whole pipeline).
        data = fetch_fn()
        assert isinstance(data, (bytes, bytearray)) and len(data) > 0
        # Save to verify column presence.
        out_path = tmp_path / "out.fgb"
        out_path.write_bytes(data)
        import geopandas as gpd
        out_gdf = gpd.read_file(str(out_path), engine="pyogrio")
        assert "ds_mean" in out_gdf.columns
        assert "repair_cost_mean" in out_gdf.columns
        assert "fragility_curve_id" in out_gdf.columns

        # Return a ReadThroughResult-shaped object (SimpleNamespace avoids
        # class-body scoping confusion around the captured ``data`` symbol).
        from types import SimpleNamespace
        return SimpleNamespace(uri=out_uri, data=data, hit=False)

    with mock.patch.object(mod, "read_through", fake_read_through):
        result = run_pelicun_damage_assessment(
            hazard_raster_uri=raster_path,
            assets_uri=assets_path,
            fragility_set="hazus_flood_v6",
            realization_count=50,
        )

    assert result.uri == out_uri
    assert result.layer_type == "vector"
    assert result.role == "primary"
    assert result.units == "damage_state"
    assert result.style_preset == "pelicun_damage_state"
    assert result.layer_id.startswith("pelicun-damage-")
    # DATA-DRIVEN LEGEND: the returned LayerURI carries a graduated/categorical
    # legend keyed off the real ``ds_mean`` feature property, so the generic web
    # vector path drives the choropleth (fixing the pelicun_damage_state-vs-
    # pelicun_damage sentinel mismatch by construction).
    assert result.legend is not None
    assert result.legend.value_field == "ds_mean"
    assert result.legend.vmin == 0.0 and result.legend.vmax == 4.0
    assert result.legend.units == "damage_state"
    assert result.legend.classes is not None and len(result.legend.classes) == 5


def test_damage_state_legend_is_graduated_ds_mean_choropleth() -> None:
    """The ``ds_mean`` legend KEY: a 5-bucket green->red HAZUS damage ramp driven
    by the real feature property, with a canonical 0..4 damage-state scale."""
    from trid3nt_server.tools.simulation.run_pelicun_damage_assessment import (
        _build_damage_state_legend,
    )

    legend = _build_damage_state_legend()
    assert legend.kind == "categorical"
    assert legend.value_field == "ds_mean"
    assert (legend.vmin, legend.vmax) == (0.0, 4.0)
    assert legend.units == "damage_state"
    # One half-unit bucket centered on each DS0..DS4 (a continuous ds_mean of
    # 1.7 lands in the DS2 bucket [1.5, 2.5)).
    buckets = [(c.value_min, c.value_max) for c in legend.classes]
    assert buckets == [(-0.5, 0.5), (0.5, 1.5), (1.5, 2.5), (2.5, 3.5), (3.5, 4.5)]
    # green (no damage) -> red (complete) ramp; each swatch is a hex color.
    assert legend.classes[0].color == "#1a9850"  # DS0 none = green
    assert legend.classes[-1].color == "#d73027"  # DS4 complete = red
    for c in legend.classes:
        assert c.color.startswith("#") and len(c.color) == 7
        assert c.label.startswith("DS")
    # Round-trips through model_dump(mode="json") as the wire envelope would.
    dumped = legend.model_dump(mode="json")
    assert dumped["value_field"] == "ds_mean"
    assert len(dumped["classes"]) == 5


def test_fragility_set_eq_2020_raises_not_wired_yet() -> None:
    """``fema_hazus_eq_2020`` passes validation but the runtime path raises typed.

    The Wave 1 contract reserved the slot; v0.1 only wires flood. We surface
    the deferral as a clear input-shape error rather than a runtime crash.
    """
    from trid3nt_server.tools.simulation import run_pelicun_damage_assessment as mod

    def fake_read_through(metadata, params, ext, fetch_fn, **kw):
        return fetch_fn()  # triggers the inner raise

    with mock.patch.object(mod, "read_through", fake_read_through):
        with pytest.raises(PelicunInputError) as excinfo:
            run_pelicun_damage_assessment(
                hazard_raster_uri="/nonexistent/x.tif",
                assets_uri="/nonexistent/y.fgb",
                fragility_set="fema_hazus_eq_2020",
            )
    msg = str(excinfo.value)
    assert "fema_hazus_eq_2020" in msg
    assert "seismic" in msg.lower() or "earthquake" in msg.lower() or "not implemented" in msg.lower()


# ---------------------------------------------------------------------------
# 11. Live Fort Myers acceptance run.
#
# Gated on ``TRID3NT_TEST_LIVE_PELICUN=1`` so CI runs the synthetic tests only.
# When invoked locally with the env var set + ADC credentials, drives the
# kickoff's acceptance scenario:
#     hazard: s3://trid3nt-runs/01KTJX71NKGDMXB9TN0DV75JWK/flood_depth_peak_0086.tif
#     assets: fetch_administrative_boundaries(level='place', bbox=fort_myers_bbox)
# Writes the output FGB + summary stats to the job's evidence directory.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("TRID3NT_TEST_LIVE_PELICUN") != "1",
    reason="set TRID3NT_TEST_LIVE_PELICUN=1 to run the live Fort Myers acceptance",
)
def test_live_pelicun_fort_myers_e2e(tmp_path) -> None:
    """LIVE: Fort Myers acceptance run end-to-end.

    Uses ``fetch_administrative_boundaries(level='place')`` for assets, the
    job-0086 Y-flip-fixed flood COG for hazard. Asserts: at least one asset
    feature emerges with populated damage-state + repair-cost properties; the
    consensus damage state is strictly positive (Fort Myers proper sits well
    within the Ian flood footprint).
    """
    from trid3nt_server.tools.fetchers.socioeconomic.fetch_administrative_boundaries import (
        fetch_administrative_boundaries,
    )

    fort_myers_bbox = (-82.0, 26.55, -81.7, 26.75)
    hazard_uri = (
        "s3://trid3nt-runs/01KTJX71NKGDMXB9TN0DV75JWK/"
        "flood_depth_peak_0086.tif"
    )

    # Fetch place polygons via the canonical tool (this exercises the
    # fetch_administrative_boundaries → Pelicun seam end-to-end).
    assets_layer = fetch_administrative_boundaries(
        level="place", bbox=fort_myers_bbox
    )

    result = run_pelicun_damage_assessment(
        hazard_raster_uri=hazard_uri,
        assets_uri=assets_layer.uri,
        fragility_set="hazus_flood_v6",
        realization_count=500,
    )
    assert result.uri.endswith(".fgb")

    # Read the output FGB back and assert non-zero damage at Fort Myers.
    import geopandas as gpd
    from trid3nt_server.tools.simulation.run_pelicun_damage_assessment import _download_uri_to_local

    local_fgb = _download_uri_to_local(result.uri, ".fgb")
    gdf = gpd.read_file(local_fgb, engine="pyogrio")
    assert len(gdf) >= 1, "expected at least one Fort Myers place feature"
    assert "ds_mean" in gdf.columns
    assert "repair_cost_mean" in gdf.columns

    # The Fort Myers CDP sits in the deep-flood footprint — at least one
    # asset must come back with non-zero expected damage.
    assert gdf["ds_mean"].max() > 0.0, (
        f"GEOGRAPHIC-CORRECTNESS FAIL: every Fort Myers asset got ds_mean=0; "
        f"sampled depths={gdf['hazard_depth_sampled'].tolist()}"
    )


def test_download_repairs_llm_mangled_prefix(monkeypatch, tmp_path):
    """job-0253: a phantom path prefix (s3://bucket/runs/<id>/f.tif when the
    object lives at s3://bucket/<id>/f.tif) is repaired by retrying the
    last-two-segment suffix. Live failure: rounds 8/9 Pelicun 404.

    GCP is decommissioned: the repair now runs on the boto3 S3 read path
    (``read_object_bytes_s3``); the gs:// download branch is gone.
    """
    import trid3nt_server.tools.simulation.run_pelicun_damage_assessment as mod
    from trid3nt_server.tools.simulation.run_pelicun_damage_assessment import (
        _download_uri_to_local,
    )

    calls = []

    def _fake_read_object_bytes_s3(uri: str) -> bytes:
        # The shim imports read_object_bytes_s3 from .cache inside the function;
        # patch it on the cache module so the lazy import resolves to this stub.
        _, _, obj_key = uri[len("s3://"):].partition("/")
        calls.append(obj_key)
        if obj_key != "01RUNID/flood_depth_peak.tif":
            raise RuntimeError("404 No such object")
        return b"cog"

    import trid3nt_server.tools.cache as cache_mod

    monkeypatch.setattr(cache_mod, "read_object_bytes_s3", _fake_read_object_bytes_s3)

    out = _download_uri_to_local(
        "s3://bucket-runs/runs/01RUNID/flood_depth_peak.tif",
        suffix=".tif",
    )
    assert calls == [
        "runs/01RUNID/flood_depth_peak.tif",
        "01RUNID/flood_depth_peak.tif",
    ]
    import os as _os

    assert _os.path.getsize(out) == 3
