"""DISV / gridgen generator tests (ADR 0099, mesh wave M2).

The gridgen binary is NOT in the modflow worker image, so these tests exercise
everything up to the binary boundary: the availability probe, the
refinement-feature translation (pure geometry), the quadtree-level math, and the
honest STOP (GridgenUnavailableError naming the image-rebuild condition) -- both
directly and through the capture_zone deck seam.
"""
from __future__ import annotations

import pytest

import gwt_adapter as ga


def _to_local_identity(lon, lat):
    """A trivial (lon,lat)->(x,y) reprojection for geometry-only assertions."""
    return (float(lon), float(lat))


def _region(target_size_m, ring):
    return {
        "polygon": {
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [ring]},
            "properties": {},
        },
        "target_size_m": target_size_m,
        "bbox": (
            min(p[0] for p in ring),
            min(p[1] for p in ring),
            max(p[0] for p in ring),
            max(p[1] for p in ring),
        ),
    }


# --- level math -------------------------------------------------------------


def test_refine_level_halving_ladder() -> None:
    assert ga._refine_level_for(100.0, 100.0) == 0   # equal -> no refine
    assert ga._refine_level_for(100.0, 50.0) == 1    # one halving
    assert ga._refine_level_for(100.0, 25.0) == 2    # two halvings
    assert ga._refine_level_for(100.0, 12.5) == 3
    assert ga._refine_level_for(100.0, 200.0) == 0   # coarser target -> 0


def test_refine_level_clamped_to_max() -> None:
    assert ga._refine_level_for(100.0, 0.01, max_level=5) == 5


# --- refinement-feature translation (no binary) -----------------------------


def test_refinement_features_compute_level_and_reproject() -> None:
    ring = [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0], [0.0, 0.0]]
    feats = ga._disv_refinement_features(
        [_region(25.0, ring)], base_size_m=100.0, to_local_xy=_to_local_identity
    )
    assert len(feats) == 1
    rings, level = feats[0]
    assert level == 2  # 100 -> 25 = two halvings
    assert rings == [ring]  # identity reprojection preserves the ring


def test_refinement_features_drop_level_zero_regions() -> None:
    ring = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]
    # target coarser than base -> level 0 -> dropped
    feats = ga._disv_refinement_features(
        [_region(500.0, ring)], base_size_m=100.0, to_local_xy=_to_local_identity
    )
    assert feats == []


def test_refinement_features_default_half_base_when_no_target() -> None:
    ring = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]
    feats = ga._disv_refinement_features(
        [_region(None, ring)], base_size_m=100.0, to_local_xy=_to_local_identity
    )
    assert feats[0][1] == 1  # default target = base/2 -> level 1


# --- availability probe + honest STOP ---------------------------------------


def test_gridgen_unavailable_in_this_image() -> None:
    # The image ships no gridgen binary -- the probe must report absence.
    assert ga.gridgen_available("gridgen-does-not-exist") is False


def test_build_disv_gridprops_stops_without_binary(tmp_path) -> None:
    with pytest.raises(ga.GridgenUnavailableError) as exc:
        ga._build_disv_gridprops(
            nlay=1,
            nrow=5,
            ncol=5,
            delr=100.0,
            delc=100.0,
            top=10.0,
            botm=0.0,
            refinement_features=[],
            model_ws=tmp_path,
            exe_name="gridgen-does-not-exist",
        )
    assert exc.value.error_code == "MODFLOW_GRIDGEN_UNAVAILABLE"
    assert "Dockerfile" in str(exc.value)


def test_gridgen_error_names_image_condition() -> None:
    err = ga.GridgenUnavailableError()
    assert err.error_code == "MODFLOW_GRIDGEN_UNAVAILABLE"
    assert "gridgen" in str(err)


# --- capture_zone deck seam: refine_regions -> honest STOP -------------------


def test_capture_zone_with_refine_regions_stops_at_binary(tmp_path) -> None:
    """Drawn refine_region on capture_zone attempts DISV -> STOPs at the binary
    (no partial deck), proving the opt-in seam is wired end-to-end."""
    ring = [
        [-81.875, 26.635],
        [-81.865, 26.635],
        [-81.865, 26.645],
        [-81.875, 26.645],
        [-81.875, 26.635],
    ]
    with pytest.raises(ga.GridgenUnavailableError):
        ga.build_modflow_deck(
            workdir=tmp_path,
            archetype="capture_zone",
            spill_location_latlon=(26.64, -81.87),
            contaminant="x",
            release_rate_kg_s=0.0,
            duration_days=0.0,
            aquifer_k_ms=1e-3,
            porosity=0.25,
            refine_regions=[_region(25.0, ring)],
        )


def test_capture_zone_default_still_dis(tmp_path) -> None:
    """No refine_regions -> the uniform DIS grid is built (byte-identical path)."""
    deck = ga.build_modflow_deck(
        workdir=tmp_path,
        archetype="capture_zone",
        spill_location_latlon=(26.64, -81.87),
        contaminant="x",
        release_rate_kg_s=0.0,
        duration_days=0.0,
        aquifer_k_ms=1e-3,
        porosity=0.25,
    )
    assert deck.archetype == "capture_zone"
    # DIS file present, no DISV.
    dis_files = [f for f in deck.files if f.endswith(".dis")]
    disv_files = [f for f in deck.files if f.endswith(".disv")]
    assert dis_files and not disv_files
