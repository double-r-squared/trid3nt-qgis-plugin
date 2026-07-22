"""F96 (NATE 2026-06-17): extend layer REUSE to fetch_* tools.

PROBLEM (live, "South Florida protected areas" repeat): on a "resize the bbox to
encompass all protected areas" follow-up the agent RE-FETCHED WDPA — already
loaded — producing TWO identical choropleth layers. job-0333 added reuse only for
run_model_* (expensive SIMULATION) results; it did NOT cover fetch_* layers, so a
fit / resize / re-show follow-up re-fetched a layer already on the map.

These tests cover the fetcher-reuse machinery (scenario_reuse.py) and the enriched
``build_layers_present_note`` (adapter.py) that together let the agent SEE an
already-loaded FETCHED layer as reusable so a follow-up reuses its handle (via
compute_layer_bounds) instead of re-fetching:

  1. ``fetched_layer_kind`` / ``fetched_kind_for_tool`` classify a fetched layer
     and its producing tool into the same KIND token (and keep RESULTs out).
  2. ``bbox_encloses`` recognizes a fit / resize to the SAME or a TIGHTER box.
  3. ``find_reusable_fetched_layer`` returns an existing same-kind layer that
     covers the request (so the caller does NOT re-fetch), and refuses on a
     different kind / a genuinely larger area / a missing-AOI ambiguity.
  4. The enriched ``build_layers_present_note`` tags an already-loaded WDPA layer
     INPUT[wdpa] and carries the fetched-reuse / no-duplicate directive.
"""

from __future__ import annotations

from grace2_agent.adapter import build_layers_present_note
from grace2_agent.scenario_reuse import (
    FetchedLayerMatch,
    bbox_encloses,
    bbox_equivalent,
    fetched_kind_for_tool,
    fetched_layer_kind,
    find_reusable_fetched_layer,
)

# "South Florida"-ish AOI the WDPA layer was fetched at.
SOUTH_FL_BBOX = [-82.0, 25.0, -80.0, 27.0]
# A tighter box that fits inside it (a "resize to encompass the features" follow-up).
TIGHTER_BBOX = [-81.5, 25.5, -80.5, 26.5]
# A genuinely larger / different area that pokes outside the loaded extent.
LARGER_BBOX = [-84.0, 24.0, -79.0, 29.0]


def _wdpa_layer(bbox=SOUTH_FL_BBOX, layer_id="wdpa--82.0000-25.0000"):
    return {
        "layer_id": layer_id,
        "name": "Protected Areas — WDPA",
        "layer_type": "vector",
        "uri": "s3://grace2-cache/wdpa/south-fl.fgb",
        "role": "context",
        "bbox": bbox,
    }


# --------------------------------------------------------------------------- #
# fetched-layer kind classification
# --------------------------------------------------------------------------- #


def test_fetched_kind_for_tool_maps_fetchers() -> None:
    assert fetched_kind_for_tool("fetch_wdpa_protected_areas") == "wdpa"
    assert fetched_kind_for_tool("fetch_landcover") == "landcover"
    assert fetched_kind_for_tool("fetch_dem") == "dem"
    # Non-fetcher / unknown tools have no fetched kind.
    assert fetched_kind_for_tool("run_model_flood_scenario") is None
    assert fetched_kind_for_tool("compute_layer_bounds") is None


def test_fetched_layer_kind_classifies_fetched_layers() -> None:
    assert fetched_layer_kind("wdpa--82.0-25.0", "Protected Areas — WDPA") == "wdpa"
    assert fetched_layer_kind("nlcd-landcover-xyz", "NLCD Landcover") == "landcover"
    assert fetched_layer_kind("usgs-dem-123", "DEM") == "dem"
    assert fetched_layer_kind("admin-county-...", "Administrative Boundaries") == "admin"


def test_fetched_layer_kind_excludes_simulation_results() -> None:
    # A simulation RESULT is NOT a fetched kind — the two taxonomies stay disjoint.
    assert fetched_layer_kind("flood-depth-peak-abc", "Flood Depth (peak)") is None
    assert fetched_layer_kind("plume-r9", "Contaminant Plume") is None
    # An unrecognized layer is conservatively None (falls back to plain INPUT).
    assert fetched_layer_kind("mystery-layer-1", "Mystery") is None


# --------------------------------------------------------------------------- #
# bbox_encloses — fit / resize recognition
# --------------------------------------------------------------------------- #


def test_bbox_encloses_same_and_tighter() -> None:
    assert bbox_encloses(SOUTH_FL_BBOX, SOUTH_FL_BBOX)
    assert bbox_encloses(SOUTH_FL_BBOX, TIGHTER_BBOX)


def test_bbox_encloses_refuses_larger() -> None:
    assert not bbox_encloses(SOUTH_FL_BBOX, LARGER_BBOX)


# --------------------------------------------------------------------------- #
# find_reusable_fetched_layer — the core F96 reuse check
# --------------------------------------------------------------------------- #


def test_refetch_of_loaded_wdpa_reuses_existing_layer() -> None:
    # The headline case: WDPA is already loaded; a "resize the bbox to encompass
    # all protected areas" follow-up (a fit, no bbox of its own) must REUSE the
    # existing layer, NOT re-fetch.
    match = find_reusable_fetched_layer(
        "fetch_wdpa_protected_areas",
        {},  # fit/resize follow-up carries no bbox — targets the loaded layer
        [_wdpa_layer()],
        case_bbox=SOUTH_FL_BBOX,
    )
    assert isinstance(match, FetchedLayerMatch)
    assert match.kind == "wdpa"
    assert match.layer_id == "wdpa--82.0000-25.0000"
    assert match.uri == "s3://grace2-cache/wdpa/south-fl.fgb"


def test_refetch_with_tighter_bbox_reuses_existing_layer() -> None:
    # An explicit tighter bbox (resize to a sub-window of the loaded extent) is
    # still answered by the existing layer.
    match = find_reusable_fetched_layer(
        "fetch_wdpa_protected_areas",
        {"bbox": TIGHTER_BBOX},
        [_wdpa_layer()],
    )
    assert match is not None
    assert match.layer_id == "wdpa--82.0000-25.0000"


def test_refetch_with_larger_bbox_does_not_reuse() -> None:
    # A genuinely larger area pokes outside the loaded extent → real new data →
    # re-fetch (no match).
    match = find_reusable_fetched_layer(
        "fetch_wdpa_protected_areas",
        {"bbox": LARGER_BBOX},
        [_wdpa_layer()],
    )
    assert match is None


def test_refetch_of_different_kind_does_not_reuse() -> None:
    # A WDPA layer is loaded but the user fetches LANDCOVER — different kind, no
    # reuse.
    match = find_reusable_fetched_layer(
        "fetch_landcover",
        {"bbox": SOUTH_FL_BBOX},
        [_wdpa_layer()],
    )
    assert match is None


def test_no_loaded_layer_does_not_reuse() -> None:
    assert (
        find_reusable_fetched_layer(
            "fetch_wdpa_protected_areas", {"bbox": SOUTH_FL_BBOX}, []
        )
        is None
    )


def test_no_aoi_resolvable_does_not_reuse() -> None:
    # No bbox param AND no case_bbox → cannot compare without geocoding →
    # conservative re-fetch (no match).
    assert (
        find_reusable_fetched_layer(
            "fetch_wdpa_protected_areas", {}, [_wdpa_layer()]
        )
        is None
    )


def test_non_fetcher_tool_does_not_reuse() -> None:
    # An expensive-simulation tool routes through the scenario-reuse path, not
    # the fetched-layer path.
    assert (
        find_reusable_fetched_layer(
            "run_model_flood_scenario",
            {"bbox": SOUTH_FL_BBOX},
            [_wdpa_layer()],
        )
        is None
    )


def test_loaded_layer_without_bbox_reuses_on_case_aoi() -> None:
    # A persisted summary may carry no bbox; a same-kind layer in this Case
    # answers a fit/resize to the Case AOI.
    no_bbox = _wdpa_layer()
    no_bbox.pop("bbox")
    match = find_reusable_fetched_layer(
        "fetch_wdpa_protected_areas",
        {"bbox": SOUTH_FL_BBOX},
        [no_bbox],
        case_bbox=SOUTH_FL_BBOX,
    )
    assert match is not None
    assert match.layer_id == "wdpa--82.0000-25.0000"
    # ...but not when the request bbox differs from the Case AOI.
    assert (
        find_reusable_fetched_layer(
            "fetch_wdpa_protected_areas",
            {"bbox": LARGER_BBOX},
            [no_bbox],
            case_bbox=SOUTH_FL_BBOX,
        )
        is None
    )


def test_bbox_equivalent_still_works_for_fetch_paths() -> None:
    # Sanity: the shared quantizer still treats a jittered same-AOI as equal.
    assert bbox_equivalent(SOUTH_FL_BBOX, [-82.001, 25.001, -79.999, 27.001])


# --------------------------------------------------------------------------- #
# enriched layers-present note (F96)
# --------------------------------------------------------------------------- #


def test_layers_present_note_tags_fetched_wdpa_as_reusable_input_kind() -> None:
    note = build_layers_present_note([_wdpa_layer()], case_bbox=SOUTH_FL_BBOX)
    assert note is not None
    # The WDPA layer is tagged INPUT[wdpa] (a recognized reusable fetched kind)...
    assert "INPUT[wdpa]" in note
    # ...its reusable handle is surfaced...
    assert "handle=wdpa--82.0000-25.0000" in note
    # ...and the note carries the fetched-reuse / no-duplicate directive so a
    # fit/resize follow-up reuses it instead of re-fetching.
    assert "compute_layer_bounds" in note
    assert "FETCHED LAYER REUSE" in note


def test_layers_present_note_unknown_fetched_layer_stays_plain_input() -> None:
    # A layer whose kind we cannot recognize is a plain INPUT (no false kind tag)
    # — conservative.
    note = build_layers_present_note(
        [
            {
                "layer_id": "mystery-layer-1",
                "name": "Mystery",
                "layer_type": "vector",
                "uri": "s3://grace2-cache/x.fgb",
                "role": "context",
            }
        ],
        case_bbox=None,
    )
    assert note is not None
    # The mystery layer's own LINE is a plain INPUT (no false kind tag). The
    # directive text mentions INPUT[<kind>] as an example, so assert on the line.
    layer_line = next(
        ln for ln in note.splitlines() if ln.startswith("- Mystery")
    )
    assert ", INPUT," in layer_line
    assert "INPUT[" not in layer_line
