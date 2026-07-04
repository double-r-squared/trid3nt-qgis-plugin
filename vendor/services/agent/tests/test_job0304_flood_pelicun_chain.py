"""job-0304 regressions: string-bbox coercion + flood-handle data-URI resolution.

Live 2026-06-16 (AWS) the flood->Pelicun chain failed three ways:
  A) compute_impact_envelope rejected a STRING bbox (no coercion).
  B) the TiTiler tile template displaced the s3:// COG under the flood handle,
     so Pelicun resolved the handle to a non-openable template (or nothing).
These lock both fixes in.
"""
from __future__ import annotations

import inspect

from grace2_agent.tool_arg_normalizer import coerce_bbox_value, normalize_args
from grace2_agent.uri_registry import SessionUriRegistry, _is_tile_template


# --- A. bbox string coercion ------------------------------------------------ #

def test_coerce_bbox_value_string_forms():
    want = [-81.9126085, 26.5476424, -81.7511414, 26.689176]
    for s in (
        "[-81.9126085, 26.5476424, -81.7511414, 26.689176]",
        "-81.9126085,26.5476424,-81.7511414,26.689176",
        "-81.9126085, 26.5476424, -81.7511414, 26.689176",
        "(-81.9126085 26.5476424 -81.7511414 26.689176)",
    ):
        assert coerce_bbox_value(s) == want, s
    # list/tuple of strings or ints -> floats
    assert coerce_bbox_value(["-81.9", "26.5", "-81.7", "26.6"]) == [-81.9, 26.5, -81.7, 26.6]
    # non-bbox -> None (tool validator speaks)
    assert coerce_bbox_value("Fort Myers") is None
    assert coerce_bbox_value("1,2,3") is None
    assert coerce_bbox_value(None) is None


def test_normalize_args_coerces_bbox_for_a_bbox_tool():
    def tool(bbox, structure_inventory_source="USACE_NSI"):
        return bbox
    out = normalize_args(
        "compute_impact_envelope",
        {"bbox": "-81.9126085, 26.5476424, -81.7511414, 26.689176",
         "structure_inventory_source": "USACE_NSI"},
        tool,
    )
    assert out["bbox"] == [-81.9126085, 26.5476424, -81.7511414, 26.689176]
    # already-good list bbox passes through untouched
    out2 = normalize_args("t", {"bbox": [1.0, 2.0, 3.0, 4.0]}, lambda bbox: bbox)
    assert out2["bbox"] == [1.0, 2.0, 3.0, 4.0]


# --- B. tile template never displaces the data URI -------------------------- #

def test_tile_template_detected():
    tpl = ("https://d125yfbyjrpbre.cloudfront.net/cog/tiles/WebMercatorQuad/"
           "{z}/{x}/{y}.png?url=s3%3A%2F%2Fb%2Fk.tif&rescale=0,3")
    assert _is_tile_template(tpl) is True
    assert _is_tile_template("s3://b/k.tif") is False
    assert _is_tile_template("https://x/wms?LAYERS=a&service=WMS") is False


def test_flood_handle_resolves_to_cog_not_template():
    reg = SessionUriRegistry(session_id="s1")
    handle = "flood-depth-peak-01TEST"
    cog = "s3://grace2-hazard-runs-x/01TEST/flood_depth_peak.tif"
    tpl = ("https://cf/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png?url="
           "s3%3A%2F%2Fgrace2-hazard-runs-x%2F01TEST%2Fflood_depth_peak.tif&rescale=0,3")
    # publish_layer registers both faces (s3 data + tile template display)
    reg.record(handle, uri=cog, wms_url=tpl)
    # then the emitted LayerURI (uri=template) is walked by register_tool_result
    reg.register_tool_result(
        "run_model_flood_scenario",
        {"layers": [{"layer_id": handle, "uri": tpl}]},
    )
    # Pelicun resolves the handle -> the openable COG, never the template
    resolved = reg.resolve_params("run_pelicun_damage_assessment", {"hazard_raster_uri": handle})
    assert resolved["hazard_raster_uri"] == cog
    # passing the template itself also resolves back to the COG
    resolved2 = reg.resolve_params("compute_impact_envelope", {"flood_layer_uri": tpl})
    assert resolved2["flood_layer_uri"] == cog


def test_register_order_template_first_then_cog_still_keeps_cog():
    """Even if the template is walked BEFORE the data URI is recorded."""
    reg = SessionUriRegistry(session_id="s2")
    handle = "flood-depth-peak-02TEST"
    cog = "s3://b/02TEST/flood_depth_peak.tif"
    tpl = "https://cf/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png?url=s3%3A%2F%2Fb%2F02TEST%2Fx.tif"
    reg.record(handle, uri=tpl)          # template arrives first -> wms slot
    reg.record(handle, uri=cog)          # then the real COG -> data slot
    resolved = reg.resolve_params("run_pelicun_damage_assessment", {"hazard_raster_uri": handle})
    assert resolved["hazard_raster_uri"] == cog
