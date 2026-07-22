"""SNAP-TO-AOI INDEPENDENT OF GEOLOCATE (NATE 2026-06-24).

When the user gives coordinates DIRECTLY the model (correctly) skips
``geocode_location``, so the geocode-only zoom-to emit never fired and the
camera did not snap to the AOI ("if we need a place we are snapped there first
and foremost"). The fix generalizes the snap: ANY tool result that SETS an
AOI/bbox (a top-level ``bbox`` or ``aoi_bbox``) snaps the camera, deduped
against the turn's last zoom-to so a chain of bbox-bearing tools over the SAME
AOI does not re-snap.

These tests pin the pure decision helper ``_aoi_zoom_to_bbox`` (the inline emit
+ accumulator append in the turn loop is a thin wrapper over it).
"""

from __future__ import annotations

from grace2_agent.server import _aoi_zoom_to_bbox

_BBOX = [-82.70, 27.70, -82.30, 28.10]
_OTHER = [-100.0, 30.0, -99.0, 31.0]


def _zoom(bbox: list) -> dict:
    return {"command": "zoom-to", "args": {"bbox": list(bbox)}}


def test_top_level_bbox_snaps_with_no_prior_zoom_to() -> None:
    # A direct-coords fetch result carrying a bbox snaps the camera (no geocode).
    aoi = _aoi_zoom_to_bbox({"bbox": _BBOX}, [])
    assert aoi is not None
    assert list(aoi) == _BBOX


def test_aoi_bbox_fallback_when_no_top_level_bbox() -> None:
    # The request_spatial_input / draw result shape carries aoi_bbox.
    aoi = _aoi_zoom_to_bbox({"aoi_bbox": _BBOX, "barriers": {}}, [])
    assert aoi is not None
    assert list(aoi) == _BBOX


def test_top_level_bbox_preferred_over_aoi_bbox() -> None:
    aoi = _aoi_zoom_to_bbox({"bbox": _BBOX, "aoi_bbox": _OTHER}, [])
    assert list(aoi) == _BBOX  # the explicit bbox wins.


def test_dedupes_against_the_turns_last_zoom_to() -> None:
    # A second bbox-bearing tool over the SAME AOI must NOT re-snap.
    assert _aoi_zoom_to_bbox({"bbox": _BBOX}, [_zoom(_BBOX)]) is None


def test_new_extent_still_snaps_even_with_a_prior_zoom_to() -> None:
    # A DIFFERENT AOI later in the turn does re-snap (the extent changed).
    aoi = _aoi_zoom_to_bbox({"bbox": _OTHER}, [_zoom(_BBOX)])
    assert list(aoi) == _OTHER


def test_non_dict_result_is_ignored() -> None:
    assert _aoi_zoom_to_bbox("a string result", []) is None
    assert _aoi_zoom_to_bbox(None, []) is None
    assert _aoi_zoom_to_bbox(["bbox", _BBOX], []) is None


def test_malformed_bbox_is_rejected_no_snap() -> None:
    assert _aoi_zoom_to_bbox({"bbox": [1, 2, 3]}, []) is None  # wrong length
    assert _aoi_zoom_to_bbox({"bbox": "not-a-bbox"}, []) is None
    assert _aoi_zoom_to_bbox({"bbox": [1, 2, float("nan"), 4]}, []) is None
    assert _aoi_zoom_to_bbox({"no_bbox_here": True}, []) is None
