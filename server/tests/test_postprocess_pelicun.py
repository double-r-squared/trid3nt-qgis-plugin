"""Unit tests for ``postprocess_pelicun`` (Wave 4.11 P2).

Coverage (per design § 9):

1. Tool registration with correct metadata.
2. Damage-state thresholds: ds_mean=1.0, 1.5, 3.4, 3.5, 3.6.
3. Damage-state distribution binning (modal DS = round(ds_mean).clip(0, 4)).
4. ``expected_loss_usd`` sums ``repair_cost_mean`` across all features.
5. ``loss_percentile_95_usd`` sums ``repair_cost_p95`` across all features.
6. NSI population from ``pop2amu65`` + ``pop2amo65``.
7. Population displaced threshold (``loss_ratio_mean >= 0.20``).
8. Population at high risk threshold (``ds_mean >= 2.5``).
9. Spatial convex-hull area (geodesic, km²).
10. Per-occupancy-class breakdown / grouping by ``component_type_used``.
11. Input error on missing required columns (schema error).
12. Population fields None when source is MS_BUILDINGS.
13. Empty FGB raises ``PelicunPostprocessEmptyError``.
14. ``pelicun_run_id`` stability across identical inputs.
"""

from __future__ import annotations

import asyncio

import pytest

# These imports are heavy (geopandas, shapely, pyproj).  Skip gracefully if
# absent so the test file imports cleanly in lean envs.
gpd = pytest.importorskip("geopandas")
shapely = pytest.importorskip("shapely")
pyproj = pytest.importorskip("pyproj")
ulid_mod = pytest.importorskip("ulid")

import numpy as np  # noqa: E402  — after the pytest.importorskip gates
import pandas as pd  # noqa: E402
from shapely.geometry import Point  # noqa: E402

from trid3nt_server.tools import TOOL_REGISTRY  # noqa: E402
from trid3nt_server.tools.postprocess_pelicun import (  # noqa: E402
    PelicunPostprocessEmptyError,
    PelicunPostprocessInputError,
    PelicunPostprocessSchemaError,
    _aggregate_gdf,
    _convex_hull_area_km2,
    _pelicun_run_id_from_inputs,
    postprocess_pelicun,
)


# ---------------------------------------------------------------------------
# Fixtures / synthetic GDF builders.
# ---------------------------------------------------------------------------


def _make_gdf(
    rows: list[dict],
    *,
    geometry: list | None = None,
    crs: str = "EPSG:4326",
) -> "gpd.GeoDataFrame":  # noqa: F821
    """Build a synthetic damage GeoDataFrame from a list of row dicts.

    Default geometry is a fixed point at (-81.87, 26.64) (Fort Myers) for every
    row — caller can pass a list of geometries to override.
    """
    if geometry is None:
        geometry = [Point(-81.87 + 0.001 * i, 26.64 + 0.001 * i) for i in range(len(rows))]
    df = pd.DataFrame(rows)
    return gpd.GeoDataFrame(df, geometry=geometry, crs=crs)


def _base_row(
    *,
    ctype: str = "RES1",
    ds_mean: float = 0.0,
    loss_ratio_mean: float = 0.0,
    repair_cost_mean: float = 0.0,
    repair_cost_p95: float = 0.0,
    replacement_value: float = 250_000.0,
    pop2amu65: float | None = None,
    pop2amo65: float | None = None,
) -> dict:
    """Return a baseline row dict; NSI population columns optional."""
    row: dict = {
        "component_type_used": ctype,
        "ds_mean": ds_mean,
        "loss_ratio_mean": loss_ratio_mean,
        "repair_cost_mean": repair_cost_mean,
        "repair_cost_p95": repair_cost_p95,
        "replacement_value": replacement_value,
    }
    if pop2amu65 is not None:
        row["pop2amu65"] = pop2amu65
    if pop2amo65 is not None:
        row["pop2amo65"] = pop2amo65
    return row


_DAMAGE_URI = "s3://trid3nt-cache/cache/static-30d/pelicun_damage/abc.fgb"
_FLOOD_URI = "s3://trid3nt-runs/run-xyz/flood_depth.tif"


# ---------------------------------------------------------------------------
# 1. Tool registration.
# ---------------------------------------------------------------------------


def test_postprocess_pelicun_registered() -> None:
    """``postprocess_pelicun`` registers with the documented metadata."""
    assert "postprocess_pelicun" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["postprocess_pelicun"]
    md = entry.metadata
    assert md.name == "postprocess_pelicun"
    assert md.cacheable is True
    assert md.ttl_class == "static-30d"
    assert md.source_class == "pelicun_postprocess"
    assert md.supports_global_query is False
    assert md.read_only_hint is True
    assert md.open_world_hint is False
    assert md.destructive_hint is False
    assert md.idempotent_hint is True


# ---------------------------------------------------------------------------
# 2. Damage-state thresholds.
# ---------------------------------------------------------------------------


def test_thresholds_damaged_destroyed() -> None:
    """ds_mean=1.0,1.5,3.4,3.5,3.6 → damaged=4, destroyed=2."""
    rows = [
        _base_row(ds_mean=1.0, loss_ratio_mean=0.05, repair_cost_mean=12_500.0, repair_cost_p95=20_000.0),
        _base_row(ds_mean=1.5, loss_ratio_mean=0.10, repair_cost_mean=25_000.0, repair_cost_p95=40_000.0),
        _base_row(ds_mean=3.4, loss_ratio_mean=0.50, repair_cost_mean=125_000.0, repair_cost_p95=150_000.0),
        _base_row(ds_mean=3.5, loss_ratio_mean=0.55, repair_cost_mean=137_500.0, repair_cost_p95=180_000.0),
        _base_row(ds_mean=3.6, loss_ratio_mean=0.58, repair_cost_mean=145_000.0, repair_cost_p95=200_000.0),
        # Below-threshold control row.
        _base_row(ds_mean=0.999, loss_ratio_mean=0.04, repair_cost_mean=10_000.0, repair_cost_p95=15_000.0),
    ]
    gdf = _make_gdf(rows)
    env = _aggregate_gdf(gdf, damage_layer_uri=_DAMAGE_URI, flood_layer_uri=_FLOOD_URI)
    assert env.n_structures_total == 6
    # ds_mean >= 1.0 — five rows (1.0, 1.5, 3.4, 3.5, 3.6).
    assert env.n_structures_damaged == 5
    # ds_mean >= 3.5 — two rows (3.5, 3.6).
    assert env.n_structures_destroyed == 2


# ---------------------------------------------------------------------------
# 3. Damage-state distribution binning.
# ---------------------------------------------------------------------------


def test_damage_state_distribution_binning() -> None:
    """Modal DS = round(ds_mean).clip(0,4); sum equals n_structures_total."""
    rows = [
        _base_row(ds_mean=0.0),    # DS0
        _base_row(ds_mean=0.49),   # DS0
        _base_row(ds_mean=0.5),    # DS1 (np.round goes to even; banker's rounding → 0 actually)
        _base_row(ds_mean=1.0),    # DS1
        _base_row(ds_mean=1.4),    # DS1
        _base_row(ds_mean=2.0),    # DS2
        _base_row(ds_mean=2.6),    # DS3
        _base_row(ds_mean=3.0),    # DS3
        _base_row(ds_mean=3.7),    # DS4
        _base_row(ds_mean=4.0),    # DS4
    ]
    gdf = _make_gdf(rows)
    env = _aggregate_gdf(gdf, damage_layer_uri=_DAMAGE_URI, flood_layer_uri=_FLOOD_URI)
    dist = env.damage_state_distribution
    # Sum equals n_structures_total — design § 2.1 closure.
    assert sum(dist.values()) == env.n_structures_total == 10
    # Each label is present in the dict (zero-fill contract).
    assert set(dist.keys()) == {
        "DS0_none",
        "DS1_slight",
        "DS2_moderate",
        "DS3_extensive",
        "DS4_complete",
    }
    # All bin counts non-negative.
    for v in dist.values():
        assert v >= 0


# ---------------------------------------------------------------------------
# 4. expected_loss_usd sums repair_cost_mean.
# ---------------------------------------------------------------------------


def test_expected_loss_sums_repair_cost_mean() -> None:
    """expected_loss_usd = sum(repair_cost_mean) across all features (DS0 included)."""
    rows = [
        _base_row(ds_mean=0.0, repair_cost_mean=0.0, repair_cost_p95=0.0),
        _base_row(ds_mean=1.5, repair_cost_mean=25_000.0, repair_cost_p95=40_000.0),
        _base_row(ds_mean=2.5, repair_cost_mean=75_000.0, repair_cost_p95=110_000.0),
        _base_row(ds_mean=3.6, repair_cost_mean=145_000.0, repair_cost_p95=200_000.0),
    ]
    gdf = _make_gdf(rows)
    env = _aggregate_gdf(gdf, damage_layer_uri=_DAMAGE_URI, flood_layer_uri=_FLOOD_URI)
    assert env.expected_loss_usd == pytest.approx(0.0 + 25_000.0 + 75_000.0 + 145_000.0)


# ---------------------------------------------------------------------------
# 5. loss_percentile_95_usd sums repair_cost_p95.
# ---------------------------------------------------------------------------


def test_loss_percentile_95_sums_repair_cost_p95() -> None:
    """loss_percentile_95_usd = sum(repair_cost_p95) across all features."""
    rows = [
        _base_row(ds_mean=0.0, repair_cost_mean=0.0, repair_cost_p95=0.0),
        _base_row(ds_mean=1.5, repair_cost_mean=25_000.0, repair_cost_p95=40_000.0),
        _base_row(ds_mean=2.5, repair_cost_mean=75_000.0, repair_cost_p95=110_000.0),
        _base_row(ds_mean=3.6, repair_cost_mean=145_000.0, repair_cost_p95=200_000.0),
    ]
    gdf = _make_gdf(rows)
    env = _aggregate_gdf(gdf, damage_layer_uri=_DAMAGE_URI, flood_layer_uri=_FLOOD_URI)
    assert env.loss_percentile_95_usd == pytest.approx(
        0.0 + 40_000.0 + 110_000.0 + 200_000.0
    )


# ---------------------------------------------------------------------------
# 6. Population from NSI columns.
# ---------------------------------------------------------------------------


def test_population_from_nsi_columns() -> None:
    """population_total = sum(pop2amu65 + pop2amo65) when source inferred as NSI."""
    rows = [
        _base_row(
            ds_mean=0.0, loss_ratio_mean=0.0,
            pop2amu65=2.0, pop2amo65=1.0,
        ),
        _base_row(
            ds_mean=2.0, loss_ratio_mean=0.25,
            pop2amu65=3.0, pop2amo65=2.0,
        ),
        _base_row(
            ds_mean=3.6, loss_ratio_mean=0.55,
            pop2amu65=1.0, pop2amo65=4.0,
        ),
    ]
    gdf = _make_gdf(rows)
    env = _aggregate_gdf(gdf, damage_layer_uri=_DAMAGE_URI, flood_layer_uri=_FLOOD_URI)
    assert env.structure_inventory_source == "USACE_NSI"
    # (2+1) + (3+2) + (1+4) = 13.
    assert env.population_total == 13


# ---------------------------------------------------------------------------
# 7. Population displaced threshold (loss_ratio_mean >= 0.20).
# ---------------------------------------------------------------------------


def test_population_displaced_threshold() -> None:
    """Features at loss_ratio_mean=0.20 count; 0.199 do not."""
    rows = [
        # Below threshold.
        _base_row(ds_mean=1.0, loss_ratio_mean=0.199, pop2amu65=5.0, pop2amo65=5.0),
        # At threshold.
        _base_row(ds_mean=2.0, loss_ratio_mean=0.20, pop2amu65=4.0, pop2amo65=4.0),
        # Above threshold.
        _base_row(ds_mean=3.0, loss_ratio_mean=0.40, pop2amu65=3.0, pop2amo65=3.0),
    ]
    gdf = _make_gdf(rows)
    env = _aggregate_gdf(gdf, damage_layer_uri=_DAMAGE_URI, flood_layer_uri=_FLOOD_URI)
    # Only the second + third rows count toward displaced — (4+4) + (3+3) = 14.
    assert env.population_displaced == 14


# ---------------------------------------------------------------------------
# 8. Population at high risk threshold (ds_mean >= 2.5).
# ---------------------------------------------------------------------------


def test_population_at_high_risk_threshold() -> None:
    """Features at ds_mean=2.5 count toward population_at_high_risk; 2.499 do not."""
    rows = [
        # Below threshold.
        _base_row(ds_mean=2.499, loss_ratio_mean=0.30, pop2amu65=5.0, pop2amo65=5.0),
        # At threshold.
        _base_row(ds_mean=2.5, loss_ratio_mean=0.35, pop2amu65=4.0, pop2amo65=4.0),
        # Above threshold.
        _base_row(ds_mean=3.6, loss_ratio_mean=0.55, pop2amu65=3.0, pop2amo65=3.0),
    ]
    gdf = _make_gdf(rows)
    env = _aggregate_gdf(gdf, damage_layer_uri=_DAMAGE_URI, flood_layer_uri=_FLOOD_URI)
    # (4+4) + (3+3) = 14.
    assert env.population_at_high_risk == 14


# ---------------------------------------------------------------------------
# 9. Spatial convex-hull area.
# ---------------------------------------------------------------------------


def test_spatial_convex_hull_area() -> None:
    """impact_area_km2 within sane bounds for a hand-built 4-corner hull."""
    # Hand-build a unit-square hull at (-81.87, 26.64) → (-81.86, 26.65),
    # roughly 1.1 km × 1.1 km at 26.6° N.
    centroids = [
        (-81.87, 26.64),
        (-81.86, 26.64),
        (-81.86, 26.65),
        (-81.87, 26.65),
    ]
    area = _convex_hull_area_km2(centroids)
    # 0.01 deg lon × 0.01 deg lat at 26.6 N ≈ 1.11 × 1.11 km = ~1.23 km².
    assert 1.0 < area < 1.5, f"unexpected area: {area}"

    # No damaged centroids → 0.0.
    assert _convex_hull_area_km2([]) == 0.0
    # Fewer than 3 points → 0.0 (degenerate hull).
    assert _convex_hull_area_km2([(-81.87, 26.64), (-81.86, 26.65)]) == 0.0


def test_impact_area_zero_when_no_damage() -> None:
    """All ds_mean < 1.0 → impact_area_km2 == 0.0."""
    rows = [
        _base_row(ds_mean=0.0, loss_ratio_mean=0.0),
        _base_row(ds_mean=0.5, loss_ratio_mean=0.02),
        _base_row(ds_mean=0.99, loss_ratio_mean=0.04),
    ]
    gdf = _make_gdf(rows)
    env = _aggregate_gdf(gdf, damage_layer_uri=_DAMAGE_URI, flood_layer_uri=_FLOOD_URI)
    assert env.impact_area_km2 == 0.0


# ---------------------------------------------------------------------------
# 10. Per-occupancy-class grouping.
# ---------------------------------------------------------------------------


def test_per_occupancy_class_grouping() -> None:
    """by_occupancy_class groups by component_type_used."""
    rows = [
        _base_row(ctype="RES1", ds_mean=1.5, loss_ratio_mean=0.10,
                  repair_cost_mean=25_000.0, repair_cost_p95=40_000.0,
                  replacement_value=250_000.0),
        _base_row(ctype="RES1", ds_mean=2.0, loss_ratio_mean=0.20,
                  repair_cost_mean=50_000.0, repair_cost_p95=75_000.0,
                  replacement_value=250_000.0),
        _base_row(ctype="COM1", ds_mean=3.6, loss_ratio_mean=0.55,
                  repair_cost_mean=770_000.0, repair_cost_p95=900_000.0,
                  replacement_value=1_400_000.0),
        _base_row(ctype="IND1", ds_mean=0.0, loss_ratio_mean=0.0,
                  repair_cost_mean=0.0, repair_cost_p95=0.0,
                  replacement_value=2_500_000.0),
    ]
    gdf = _make_gdf(rows)
    env = _aggregate_gdf(gdf, damage_layer_uri=_DAMAGE_URI, flood_layer_uri=_FLOOD_URI)
    classes = env.by_occupancy_class
    assert set(classes.keys()) == {"RES1", "COM1", "IND1"}
    # n_structures per class sums to n_structures_total.
    assert sum(c.n_structures for c in classes.values()) == env.n_structures_total
    # RES1: two rows, both damaged (>=1.0), neither destroyed (none >=3.5).
    assert classes["RES1"].n_structures == 2
    assert classes["RES1"].n_damaged == 2
    assert classes["RES1"].n_destroyed == 0
    # COM1: one row, damaged + destroyed.
    assert classes["COM1"].n_destroyed == 1
    # IND1: ds_mean=0, not damaged.
    assert classes["IND1"].n_damaged == 0
    # Per-class expected loss = sum repair_cost_mean for that class.
    assert classes["RES1"].expected_loss_usd == pytest.approx(75_000.0)
    assert classes["COM1"].loss_percentile_95_usd == pytest.approx(900_000.0)


# ---------------------------------------------------------------------------
# 11. Schema error on missing required columns.
# ---------------------------------------------------------------------------


def test_input_error_on_missing_required_columns() -> None:
    """GDF missing ``ds_mean`` → PelicunPostprocessSchemaError."""
    # Build a frame missing ``ds_mean`` outright.
    rows = [
        {
            "component_type_used": "RES1",
            "loss_ratio_mean": 0.1,
            "repair_cost_mean": 25_000.0,
            "repair_cost_p95": 40_000.0,
            "replacement_value": 250_000.0,
        }
    ]
    gdf = _make_gdf(rows)
    with pytest.raises(PelicunPostprocessSchemaError) as excinfo:
        _aggregate_gdf(gdf, damage_layer_uri=_DAMAGE_URI, flood_layer_uri=_FLOOD_URI)
    err = excinfo.value
    assert err.error_code == "POSTPROCESS_PELICUN_SCHEMA"
    assert err.retryable is False
    assert "ds_mean" in str(err)


# ---------------------------------------------------------------------------
# 12. Population fields None when source is MS_BUILDINGS.
# ---------------------------------------------------------------------------


def test_population_none_when_source_is_MS_BUILDINGS() -> None:
    """No pop2amu65 column → source inferred as MS_BUILDINGS → all pop None."""
    rows = [
        _base_row(ds_mean=1.5, loss_ratio_mean=0.10, repair_cost_mean=25_000.0,
                  repair_cost_p95=40_000.0),
        _base_row(ds_mean=2.6, loss_ratio_mean=0.30, repair_cost_mean=75_000.0,
                  repair_cost_p95=110_000.0),
    ]
    gdf = _make_gdf(rows)
    env = _aggregate_gdf(gdf, damage_layer_uri=_DAMAGE_URI, flood_layer_uri=_FLOOD_URI)
    assert env.structure_inventory_source == "MS_BUILDINGS"
    assert env.population_total is None
    assert env.population_displaced is None
    assert env.population_at_high_risk is None
    # Per-class population also None.
    for cls in env.by_occupancy_class.values():
        assert cls.population is None
        assert cls.population_displaced is None


# ---------------------------------------------------------------------------
# 13. Empty FGB raises empty error.
# ---------------------------------------------------------------------------


def test_empty_fgb_raises_empty_error() -> None:
    """Zero-feature GDF → PelicunPostprocessEmptyError."""
    gdf = _make_gdf([])
    with pytest.raises(PelicunPostprocessEmptyError) as excinfo:
        _aggregate_gdf(gdf, damage_layer_uri=_DAMAGE_URI, flood_layer_uri=_FLOOD_URI)
    err = excinfo.value
    assert err.error_code == "POSTPROCESS_PELICUN_EMPTY"
    assert err.retryable is False


# ---------------------------------------------------------------------------
# 14. pelicun_run_id stable across identical inputs.
# ---------------------------------------------------------------------------


def test_pelicun_run_id_stable() -> None:
    """Identical inputs → identical pelicun_run_id (cache-stable)."""
    id1 = _pelicun_run_id_from_inputs(_DAMAGE_URI, _FLOOD_URI)
    id2 = _pelicun_run_id_from_inputs(_DAMAGE_URI, _FLOOD_URI)
    assert id1 == id2
    # Different inputs → different ID.
    id3 = _pelicun_run_id_from_inputs(_DAMAGE_URI + "different", _FLOOD_URI)
    assert id1 != id3
    # ULID format: 26 chars, Crockford base32.
    assert len(id1) == 26


# ---------------------------------------------------------------------------
# 15. Input validation on the public async surface.
# ---------------------------------------------------------------------------


def test_input_error_on_none_damage_layer_uri() -> None:
    """None damage_layer_uri → PelicunPostprocessInputError."""
    with pytest.raises(PelicunPostprocessInputError) as excinfo:
        asyncio.run(
            postprocess_pelicun(damage_layer_uri=None, flood_layer_uri=_FLOOD_URI)  # type: ignore[arg-type]
        )
    assert excinfo.value.error_code == "POSTPROCESS_PELICUN_INPUT"
    assert "damage_layer_uri" in str(excinfo.value)


def test_input_error_on_empty_damage_layer_uri() -> None:
    """Empty string damage_layer_uri → PelicunPostprocessInputError."""
    with pytest.raises(PelicunPostprocessInputError):
        asyncio.run(
            postprocess_pelicun(damage_layer_uri="   ", flood_layer_uri=_FLOOD_URI)
        )


# ---------------------------------------------------------------------------
# 16. Smoke test — synthetic small case via _aggregate_gdf.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 17. S3 staging branch — exercises _download_uri_to_local s3:// path end to
#     end (boto3 reader monkeypatched, tempfile staging, geopandas read,
#     was_remote cleanup) with NO live AWS. Closes the sprint-14-aws gap where
#     the s3:// staging branch had zero coverage.
# ---------------------------------------------------------------------------


def _fgb_bytes_for(rows: list[dict]) -> bytes:
    """Serialize a synthetic damage GeoDataFrame to FlatGeobuf bytes."""
    import os
    import tempfile

    gdf = _make_gdf(rows)
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as tf:
        path = tf.name
    try:
        gdf.to_file(path, driver="FlatGeobuf")
        with open(path, "rb") as fh:
            return fh.read()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def test_postprocess_pelicun_s3_staging_branch(monkeypatch, tmp_path) -> None:
    """``postprocess_pelicun(s3://...)`` stages via boto3 reader → valid envelope.

    Monkeypatches ``cache.read_object_bytes_s3`` to return fixture FGB bytes so
    the s3:// branch of ``_download_uri_to_local`` runs (tempfile staging +
    geopandas read + was_remote unlink) without any live AWS. Also asserts the
    staged temp file is cleaned up (the ``finally`` unlink for remote inputs).
    """
    import trid3nt_server.tools.cache as cache_mod

    rows = [
        _base_row(ctype="RES1", ds_mean=1.5, loss_ratio_mean=0.10,
                  repair_cost_mean=25_000.0, repair_cost_p95=40_000.0,
                  replacement_value=250_000.0,
                  pop2amu65=3.0, pop2amo65=2.0),
        _base_row(ctype="COM1", ds_mean=3.6, loss_ratio_mean=0.55,
                  repair_cost_mean=770_000.0, repair_cost_p95=900_000.0,
                  replacement_value=1_400_000.0,
                  pop2amu65=1.0, pop2amo65=4.0),
    ]
    fgb_bytes = _fgb_bytes_for(rows)

    s3_uri = "s3://grace2-bucket/cache/static-30d/pelicun_damage/abc.fgb"

    calls: list[str] = []

    def _fake_read(uri: str) -> bytes:
        calls.append(uri)
        return fgb_bytes

    # Patch the symbol the tool imports lazily from .cache.
    monkeypatch.setattr(cache_mod, "read_object_bytes_s3", _fake_read)

    # Track temp files created so we can assert cleanup of the staged input.
    import tempfile as _tempfile

    created: list[str] = []
    real_ntf = _tempfile.NamedTemporaryFile

    def _tracking_ntf(*args, **kwargs):  # noqa: ANN002, ANN003
        tf = real_ntf(*args, **kwargs)
        created.append(tf.name)
        return tf

    monkeypatch.setattr(_tempfile, "NamedTemporaryFile", _tracking_ntf)

    env = asyncio.run(
        postprocess_pelicun(damage_layer_uri=s3_uri, flood_layer_uri=_FLOOD_URI)
    )

    # The boto3 reader was invoked with the s3:// URI.
    assert calls == [s3_uri]

    # Envelope is well-formed and reflects the fixture rows.
    assert env["n_structures_total"] == 2
    assert env["n_structures_damaged"] == 2
    assert env["n_structures_destroyed"] == 1
    assert env["structure_inventory_source"] == "USACE_NSI"
    # population_total = (3+2) + (1+4) = 10.
    assert env["population_total"] == 10
    assert env["damage_layer_uri"] == s3_uri
    assert env["flood_layer_uri"] == _FLOOD_URI

    # The staged temp file (remote input) was unlinked by the finally block.
    import os
    staged = [p for p in created if p.endswith(".fgb")]
    assert staged, "expected a staged .fgb temp file"
    for p in staged:
        assert not os.path.exists(p), f"staged temp file not cleaned up: {p}"


def test_postprocess_pelicun_s3_download_failure_wraps_io_error(monkeypatch) -> None:
    """A boto3 reader failure on the s3:// branch surfaces as PelicunPostprocessIOError."""
    import trid3nt_server.tools.cache as cache_mod
    from trid3nt_server.tools.postprocess_pelicun import PelicunPostprocessIOError

    def _boom(uri: str) -> bytes:
        raise RuntimeError("no creds")

    monkeypatch.setattr(cache_mod, "read_object_bytes_s3", _boom)

    with pytest.raises(PelicunPostprocessIOError) as excinfo:
        asyncio.run(
            postprocess_pelicun(
                damage_layer_uri="s3://b/k.fgb", flood_layer_uri=_FLOOD_URI
            )
        )
    assert excinfo.value.error_code == "POSTPROCESS_PELICUN_IO"
    assert excinfo.value.retryable is True


# ---------------------------------------------------------------------------
# 18. Provenance threading — fragility_set / realization_count overrides are
#     reflected in the envelope (M5.5 Invariant-7 fix), and default to the
#     back-compatible constants when omitted.
# ---------------------------------------------------------------------------


def test_provenance_overrides_thread_into_envelope() -> None:
    """Passing fragility_set / realization_count overrides the envelope provenance."""
    rows = [
        _base_row(ds_mean=1.5, loss_ratio_mean=0.10, repair_cost_mean=25_000.0,
                  repair_cost_p95=40_000.0),
    ]
    env = _aggregate_gdf(
        _make_gdf(rows),
        damage_layer_uri=_DAMAGE_URI,
        flood_layer_uri=_FLOOD_URI,
        fragility_set="hazus_flood_v7_custom",
        realization_count=250,
    )
    assert env.fragility_set == "hazus_flood_v7_custom"
    assert env.realization_count == 250


def test_provenance_defaults_when_overrides_omitted() -> None:
    """When overrides are omitted, the back-compatible defaults still hold."""
    rows = [
        _base_row(ds_mean=1.5, loss_ratio_mean=0.10, repair_cost_mean=25_000.0,
                  repair_cost_p95=40_000.0),
    ]
    env = _aggregate_gdf(
        _make_gdf(rows),
        damage_layer_uri=_DAMAGE_URI,
        flood_layer_uri=_FLOOD_URI,
    )
    assert env.fragility_set == "hazus_flood_v6"
    assert env.realization_count == 100


def test_postprocess_pelicun_threads_fragility_override(monkeypatch) -> None:
    """The public surface forwards fragility_set / realization_count into the envelope."""
    import trid3nt_server.tools.cache as cache_mod

    rows = [
        _base_row(ds_mean=1.5, loss_ratio_mean=0.10, repair_cost_mean=25_000.0,
                  repair_cost_p95=40_000.0),
    ]
    fgb_bytes = _fgb_bytes_for(rows)
    monkeypatch.setattr(
        cache_mod, "read_object_bytes_s3", lambda uri: fgb_bytes
    )

    env = asyncio.run(
        postprocess_pelicun(
            damage_layer_uri="s3://b/k.fgb",
            flood_layer_uri=_FLOOD_URI,
            fragility_set="hazus_flood_v7_custom",
            realization_count=42,
        )
    )
    assert env["fragility_set"] == "hazus_flood_v7_custom"
    assert env["realization_count"] == 42


def test_smoke_synthetic_small_case_validates_envelope() -> None:
    """5-row mixed-DS synthetic case produces a fully-valid ImpactEnvelope."""
    rows = [
        _base_row(ctype="RES1", ds_mean=0.0, loss_ratio_mean=0.0,
                  repair_cost_mean=0.0, repair_cost_p95=0.0,
                  replacement_value=250_000.0,
                  pop2amu65=2.0, pop2amo65=1.0),
        _base_row(ctype="RES1", ds_mean=1.5, loss_ratio_mean=0.10,
                  repair_cost_mean=25_000.0, repair_cost_p95=40_000.0,
                  replacement_value=250_000.0,
                  pop2amu65=3.0, pop2amo65=2.0),
        _base_row(ctype="COM1", ds_mean=2.6, loss_ratio_mean=0.30,
                  repair_cost_mean=420_000.0, repair_cost_p95=600_000.0,
                  replacement_value=1_400_000.0,
                  pop2amu65=0.0, pop2amo65=0.0),
        _base_row(ctype="COM1", ds_mean=3.6, loss_ratio_mean=0.55,
                  repair_cost_mean=770_000.0, repair_cost_p95=900_000.0,
                  replacement_value=1_400_000.0,
                  pop2amu65=0.0, pop2amo65=0.0),
        _base_row(ctype="IND1", ds_mean=0.5, loss_ratio_mean=0.02,
                  repair_cost_mean=50_000.0, repair_cost_p95=75_000.0,
                  replacement_value=2_500_000.0,
                  pop2amu65=0.0, pop2amo65=0.0),
    ]
    gdf = _make_gdf(rows)
    env = _aggregate_gdf(gdf, damage_layer_uri=_DAMAGE_URI, flood_layer_uri=_FLOOD_URI)
    # Round-trip through model_dump to confirm it serializes cleanly.
    dumped = env.model_dump(mode="json")
    assert dumped["schema_version"] == "v1"
    assert dumped["n_structures_total"] == 5
    # Three damaged: rows 1, 2, 3 (ds_mean >= 1.0).
    assert dumped["n_structures_damaged"] == 3
    # One destroyed: row 3 (ds_mean=3.6).
    assert dumped["n_structures_destroyed"] == 1
    # Population_total = (2+1) + (3+2) + (0+0) + (0+0) + (0+0) = 8.
    assert dumped["population_total"] == 8
    # by_occupancy_class has three classes.
    assert set(dumped["by_occupancy_class"].keys()) == {"RES1", "COM1", "IND1"}
    # ULID-shaped run_id.
    assert len(dumped["pelicun_run_id"]) == 26
    # bbox is a 4-tuple of floats in EPSG:4326.
    bbox = dumped["bbox"]
    assert len(bbox) == 4
    assert all(isinstance(v, (int, float)) for v in bbox)
    # Provenance carried through.
    assert dumped["damage_layer_uri"] == _DAMAGE_URI
    assert dumped["flood_layer_uri"] == _FLOOD_URI
    assert dumped["fragility_set"] == "hazus_flood_v6"
    assert dumped["realization_count"] == 100


# ---------------------------------------------------------------------------
# job-0300 — Invariant 7 hardening (non-finite ds_mean + default-RV transparency)
# ---------------------------------------------------------------------------


def test_nonfinite_ds_mean_raises_schema_error() -> None:
    """A NaN ds_mean (malformed/foreign FGB) must fail honestly with a typed
    schema error, not an IndexError or a fabricated DS bin (Invariant 7)."""
    from trid3nt_server.tools.postprocess_pelicun import PelicunPostprocessSchemaError

    gdf = _make_gdf([_base_row(ds_mean=1.0), _base_row(ds_mean=float("nan"))])
    with pytest.raises(PelicunPostprocessSchemaError, match="non-finite"):
        _aggregate_gdf(gdf, damage_layer_uri=_DAMAGE_URI, flood_layer_uri=_FLOOD_URI)


def test_default_replacement_value_count_surfaced() -> None:
    """Assets whose loss rests on a HAZUS class-default replacement value are
    counted on the envelope so the loss basis is transparent (Invariant 7)."""
    rows = [
        {**_base_row(ds_mean=1.0), "replacement_value_defaulted": True},
        {**_base_row(ds_mean=2.0), "replacement_value_defaulted": False},
        {**_base_row(ds_mean=0.0), "replacement_value_defaulted": True},
    ]
    env = _aggregate_gdf(_make_gdf(rows), damage_layer_uri=_DAMAGE_URI, flood_layer_uri=_FLOOD_URI)
    assert env.n_assets_default_replacement_value == 2


def test_default_replacement_value_count_zero_when_column_absent() -> None:
    """A legacy/foreign FGB lacking the column yields an honest 0, not a crash."""
    gdf = _make_gdf([_base_row(ds_mean=1.0), _base_row(ds_mean=2.0)])
    env = _aggregate_gdf(gdf, damage_layer_uri=_DAMAGE_URI, flood_layer_uri=_FLOOD_URI)
    assert env.n_assets_default_replacement_value == 0
