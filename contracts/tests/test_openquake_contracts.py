"""Validation + round-trip tests for the OpenQuake PSHA contracts (sprint-17,
``trid3nt_contracts.openquake_contracts``).

The OpenQuake analogue of the MODFLOW/SWMM contract tests. Covers:
- ``OpenQuakeRunArgs`` JSON round-trip (idempotent serialize/deserialize) +
  defaults (PGA / 10% / 50 yr / 5 km / BooreAtkinson2008).
- the IMT normalizer (``pga`` -> ``PGA``, ``sa(1.0)`` -> ``SA(1.0)``) + the
  structural IMT validator (rejects junk).
- the magnitude-range validator (max must exceed min) + the PoE bounds.
- ``SeismicHazardLayerURI`` round-trip + that it is a ``LayerURI`` (so it maps
  onto ``map-command load-layer`` with no translation) carrying the narration
  scalars.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.openquake_contracts import (
    DEFAULT_GMPE,
    DEFAULT_IMT,
    OpenQuakeRunArgs,
    SeismicHazardLayerURI,
)


# ===========================================================================
# OpenQuakeRunArgs
# ===========================================================================
def test_run_args_defaults_and_round_trip():
    args = OpenQuakeRunArgs(bbox=(-122.5, 37.5, -121.5, 38.5))
    assert args.imt == DEFAULT_IMT == "PGA"
    assert args.poe == pytest.approx(0.10)
    assert args.investigation_time_years == pytest.approx(50.0)
    assert args.site_grid_spacing_km == pytest.approx(5.0)
    assert args.gmpe == DEFAULT_GMPE

    # JSON round-trip is idempotent.
    dumped = args.model_dump_json()
    reloaded = OpenQuakeRunArgs.model_validate_json(dumped)
    assert reloaded == args
    assert reloaded.bbox == (-122.5, 37.5, -121.5, 38.5)


def test_imt_normalizer_and_validator():
    # Lowercase normalizes on the FIRST attempt.
    assert OpenQuakeRunArgs(bbox=(-1, -1, 1, 1), imt="pga").imt == "PGA"
    assert OpenQuakeRunArgs(bbox=(-1, -1, 1, 1), imt="sa(1.0)").imt == "SA(1.0)"
    assert OpenQuakeRunArgs(bbox=(-1, -1, 1, 1), imt="PGV").imt == "PGV"

    # A junk IMT raises an honest validation error.
    with pytest.raises(ValidationError):
        OpenQuakeRunArgs(bbox=(-1, -1, 1, 1), imt="banana")


def test_poe_bounds_and_magnitude_range():
    # PoE must be in (0, 1).
    with pytest.raises(ValidationError):
        OpenQuakeRunArgs(bbox=(-1, -1, 1, 1), poe=0.0)
    with pytest.raises(ValidationError):
        OpenQuakeRunArgs(bbox=(-1, -1, 1, 1), poe=1.0)

    # max_magnitude must exceed min_magnitude.
    with pytest.raises(ValidationError):
        OpenQuakeRunArgs(
            bbox=(-1, -1, 1, 1), min_magnitude=7.0, max_magnitude=6.0
        )
    # A valid range passes.
    ok = OpenQuakeRunArgs(
        bbox=(-1, -1, 1, 1), min_magnitude=4.5, max_magnitude=8.0
    )
    assert ok.max_magnitude > ok.min_magnitude


def test_bbox_lon_first_validation():
    # The shared BBox type range-validates; an out-of-range lon raises.
    with pytest.raises(ValidationError):
        OpenQuakeRunArgs(bbox=(-200.0, 0.0, 10.0, 1.0))


# ===========================================================================
# SeismicHazardLayerURI
# ===========================================================================
def test_hazard_layer_is_layer_uri_round_trip():
    layer = SeismicHazardLayerURI(
        layer_id="seismic-hazard-01ABC",
        name="Seismic hazard (PGA, 475-yr return period)",
        layer_type="raster",
        uri="s3://runs/01ABC/seismic_hazard_4326.tif",
        style_preset="continuous_seismic_pga",
        role="primary",
        units="g",
        bbox=(-122.5, 37.5, -121.5, 38.5),
        imt="PGA",
        poe=0.10,
        investigation_time_years=50.0,
        return_period_years=475.0,
        max_hazard_value=0.62,
        hazard_area_km2=1234.5,
        n_sites=400,
    )
    # It IS a LayerURI (so map-command load-layer needs no translation).
    assert isinstance(layer, LayerURI)
    assert layer.max_hazard_value == pytest.approx(0.62)
    assert layer.return_period_years == pytest.approx(475.0)
    assert layer.n_sites == 400

    dumped = layer.model_dump_json()
    reloaded = SeismicHazardLayerURI.model_validate_json(dumped)
    assert reloaded == layer


def test_hazard_layer_rejects_negative_scalars():
    with pytest.raises(ValidationError):
        SeismicHazardLayerURI(
            layer_id="x",
            name="x",
            layer_type="raster",
            uri="s3://x",
            style_preset="continuous_seismic_pga",
            return_period_years=475.0,
            max_hazard_value=-0.1,  # invalid: ge=0
            hazard_area_km2=0.0,
        )
