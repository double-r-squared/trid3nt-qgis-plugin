"""ADR 0183: postprocess_modflow georef hardening (ADR 0180 finding).

``_write_reprojected_cog`` / ``_modflow_src_transform`` used to fall back to
``rasterio.Affine.identity()`` (null island) when ``_grid_georegistration_from_deck``
returned ``None`` on a flopy deck-load failure -- a silently misplaced raster
instead of an honest error, shared across every raster-emitting MODFLOW
template. Covers:

  * the two writers RAISE ``PostprocessMODFLOWError("MODFLOW_GEOREGISTRATION_MISSING")``
    on a missing georegistration, and the identity-affine fallback is GONE
    from their source (structural guard against regression);
  * happy-path transforms are unaffected (byte-identical geometry);
  * a simulated flopy deck-load failure (``_grid_georegistration_from_deck``
    returns ``None``, exactly what the real function does on any exception)
    propagates as the typed error through the RASTER-IS-THE-DELIVERABLE
    callers (plume / multi_species / drawdown);
  * the SCALAR-IS-THE-DELIVERABLE callers (budget_partition / asr) instead
    degrade gracefully to an unplaced fallback URI, honestly logged -- the
    same skip pattern ``modflow_mesh.emit_modflow_mesh_artifact`` already
    uses for its bonus UGRID artifact.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import rasterio

from trid3nt_contracts.modflow_contracts import ASRLayerURI, BudgetPartitionLayerURI

from trid3nt_server.agent.workflows.modflow import postprocess_modflow as pp

_GEO = {
    "xorigin": 500_000.0,
    "yorigin": 3_000_000.0,
    "delr": 25.0,
    "delc": 25.0,
    "nrow": 4,
    "ncol": 4,
}


def _grid() -> Any:
    return np.array(
        [[0.01, 0.02, 0.0, 0.0],
         [0.02, 0.05, 0.0, 0.0],
         [0.0, 0.0, 0.0, 0.0],
         [0.0, 0.0, 0.0, 0.0]],
        dtype="float64",
    )


# --------------------------------------------------------------------------- #
# Structural guard: the identity-affine fallback is GONE
# --------------------------------------------------------------------------- #


def test_identity_affine_fallback_is_gone_from_write_reprojected_cog() -> None:
    src = inspect.getsource(pp._write_reprojected_cog)
    assert "Affine.identity" not in src
    assert ".identity()" not in src


def test_identity_affine_fallback_is_gone_from_modflow_src_transform() -> None:
    src = inspect.getsource(pp._modflow_src_transform)
    assert "Affine.identity" not in src
    assert ".identity()" not in src


# --------------------------------------------------------------------------- #
# Unit: the two writers, direct calls
# --------------------------------------------------------------------------- #


def test_write_reprojected_cog_raises_on_missing_georegistration() -> None:
    with pytest.raises(pp.PostprocessMODFLOWError) as exc:
        pp._write_reprojected_cog(_grid(), "EPSG:32617", None)
    assert exc.value.error_code == "MODFLOW_GEOREGISTRATION_MISSING"
    assert exc.value.details.get("model_crs") == "EPSG:32617"


def test_write_reprojected_cog_happy_path_places_correctly(tmp_path) -> None:
    """Happy path: the COG lands at the deck origin, CRS EPSG:4326."""
    cog_path = pp._write_reprojected_cog(_grid(), "EPSG:32617", _GEO)
    try:
        with rasterio.open(cog_path) as ds:
            assert str(ds.crs) == "EPSG:4326"
            # The deck origin (500000, 3000000 in UTM 17N) reprojects to
            # roughly (-81, 27) -- nowhere near null island (0, 0).
            bounds = ds.bounds
            assert -85.0 < bounds.left < -75.0
            assert 20.0 < bounds.bottom < 35.0
    finally:
        pp.cog_io.safe_unlink(cog_path)


def test_modflow_src_transform_raises_on_missing_georegistration() -> None:
    with pytest.raises(pp.PostprocessMODFLOWError) as exc:
        pp._modflow_src_transform(None, nrow=4)
    assert exc.value.error_code == "MODFLOW_GEOREGISTRATION_MISSING"


def test_modflow_src_transform_happy_path_matches_manual_transform() -> None:
    transform = pp._modflow_src_transform(_GEO, nrow=_GEO["nrow"])
    expected = rasterio.transform.from_origin(
        _GEO["xorigin"],
        _GEO["yorigin"] + _GEO["nrow"] * _GEO["delc"],
        _GEO["delr"],
        _GEO["delc"],
    )
    assert transform == expected


# --------------------------------------------------------------------------- #
# End-to-end: a simulated flopy deck-load failure through the public
# entrypoints. _grid_georegistration_from_deck returning None is EXACTLY what
# the real function does on any load exception (see its own except clause) --
# stubbing the return is the fixture for "the deck failed to load".
# --------------------------------------------------------------------------- #


def test_postprocess_modflow_raises_on_deck_load_failure(monkeypatch, tmp_path) -> None:
    """RASTER-IS-THE-DELIVERABLE: the plume COG IS the finding -- hard fail."""
    monkeypatch.setattr(pp, "_resolve_ucn_path", lambda uri: Path("/tmp/x.ucn"))
    monkeypatch.setattr(pp, "_read_final_concentration", lambda p: _grid())
    monkeypatch.setattr(pp, "_grid_georegistration_from_deck", lambda d: None)
    upload_called = []
    monkeypatch.setattr(
        pp, "_upload_cog", lambda *a, **k: upload_called.append(1) or "file://x"
    )

    with pytest.raises(pp.PostprocessMODFLOWError) as exc:
        pp.postprocess_modflow(
            str(tmp_path),
            run_id="RUN1",
            model_crs="EPSG:32617",
            deck_dir=str(tmp_path),
            publish=False,
        )
    assert exc.value.error_code == "MODFLOW_GEOREGISTRATION_MISSING"
    # No COG was ever uploaded -- the misplaced raster never left the writer.
    assert not upload_called


def test_postprocess_multi_species_raises_on_deck_load_failure(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        pp, "_resolve_species_ucn_paths", lambda uri: [Path("/tmp/gwt_tce.ucn")]
    )
    monkeypatch.setattr(pp, "_read_final_concentration", lambda p: _grid())
    monkeypatch.setattr(pp, "_grid_georegistration_from_deck", lambda d: None)

    with pytest.raises(pp.PostprocessMODFLOWError) as exc:
        pp.postprocess_multi_species(
            str(tmp_path),
            run_id="RUN1",
            model_crs="EPSG:32617",
            deck_dir=str(tmp_path),
            publish=False,
        )
    assert exc.value.error_code == "MODFLOW_GEOREGISTRATION_MISSING"


def test_postprocess_drawdown_raises_on_deck_load_failure(monkeypatch, tmp_path) -> None:
    """The GWF-only archetype family (head-based, not UCN-based) is covered too."""
    monkeypatch.setattr(pp, "_resolve_gwf_hds_path", lambda uri: Path("/tmp/x.hds"))
    monkeypatch.setattr(pp, "_read_head_decline_grid", lambda p, invert=False: (_grid(), None))
    monkeypatch.setattr(pp, "_grid_georegistration_from_deck", lambda d: None)

    with pytest.raises(pp.PostprocessMODFLOWError) as exc:
        pp.postprocess_drawdown(
            str(tmp_path),
            run_id="RUN1",
            model_crs="EPSG:32617",
            deck_dir=str(tmp_path),
            publish=False,
        )
    assert exc.value.error_code == "MODFLOW_GEOREGISTRATION_MISSING"


# --------------------------------------------------------------------------- #
# End-to-end: SCALAR-IS-THE-DELIVERABLE callers degrade gracefully (the
# modflow_mesh "loud skip" pattern), rather than sinking a narrated scalar
# the call already computed.
# --------------------------------------------------------------------------- #


def test_postprocess_budget_partition_degrades_on_deck_load_failure(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(pp, "_resolve_gwf_cbc_path", lambda uri: Path("/tmp/x.cbc"))
    monkeypatch.setattr(
        pp, "_read_cbc_budget_partition", lambda p: {"WEL_OUT": -10.0, "CHD_IN": 10.0}
    )
    monkeypatch.setattr(pp, "_grid_georegistration_from_deck", lambda d: None)
    monkeypatch.setattr(pp, "_resolve_gwf_hds_path", lambda uri: Path("/tmp/x.hds"))
    monkeypatch.setattr(pp, "_read_head_grid", lambda p: _grid())

    result = pp.postprocess_budget_partition(
        str(tmp_path),
        run_id="RUN1",
        model_crs="EPSG:32617",
        deck_dir=str(tmp_path),
        publish=False,
    )
    assert isinstance(result, BudgetPartitionLayerURI)
    # The scalar deliverable survives the raster failure.
    assert result.budget_partition_m3_day == {"wel_out": -10.0, "chd_in": 10.0}
    # Honest degrade: unplaced fallback URI, no bbox -- never a misplaced COG.
    assert result.uri == str(tmp_path)
    assert result.bbox is None


def test_postprocess_asr_degrades_on_deck_load_failure(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(pp, "_resolve_gwf_hds_path", lambda uri: Path("/tmp/x.hds"))
    monkeypatch.setattr(pp, "_read_head_steps", lambda p: [_grid(), _grid() * 2])
    monkeypatch.setattr(pp, "_grid_georegistration_from_deck", lambda d: None)
    monkeypatch.setattr(pp, "_resolve_gwf_cbc_path", lambda uri: Path("/tmp/x.cbc"))
    monkeypatch.setattr(
        pp, "_read_cbc_term_signed_totals", lambda p, term: (0.0, 100.0, 80.0)
    )

    result = pp.postprocess_asr(
        str(tmp_path),
        run_id="RUN1",
        model_crs="EPSG:32617",
        deck_dir=str(tmp_path),
        publish=False,
    )
    assert isinstance(result, ASRLayerURI)
    # The scalar deliverables survive the raster failure.
    assert result.recovery_efficiency == pytest.approx(0.8)
    assert result.head_timeseries is not None
    # Honest degrade: unplaced fallback URI, no bbox -- never a misplaced COG.
    assert result.uri == str(tmp_path)
    assert result.bbox is None
