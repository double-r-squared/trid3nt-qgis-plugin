"""ADR 0282 -- the SWMM emit-on-solve leg (host-exec writer + FRAME byte-equivalence).

M-class ruling OPTION (a): the seam owns the TEMPORAL FRAMES ONLY; the typed peak
``SWMMDepthLayerURI`` + its narration scalars stay composer-built. So the
byte-equivalence bar is measured on the FRAME render stream (name, layer_type,
style_preset, role, units, bbox, resolved rescale) -- the seam's ``frames_only``
frames are byte-identical to the legacy bespoke ``"Flood depth step N"`` context
frames, EXCEPT the internal ``layer_id`` stem, which the seam standardizes on the
physical quantity (``flood-depth-frame-*`` vs the legacy ``swmm-depth-frame-*``);
web grouping rides the ``name`` token so the stem swap renders identically.

Pure/offline: the entries are built by the same host-exec contracts writer
``postprocess_swmm`` uses (``build_entry``), no pyswmm solve needed here.
"""

from __future__ import annotations

import pytest

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.outputs_manifest import (
    append_entries,
    build_entry,
    parse_outputs_manifest,
)
from trid3nt_server.emission.outputs_seam import build_layers_from_outputs
from trid3nt_server.workflows.swmm.postprocess_swmm import (
    FLOOD_DEPTH_STYLE_PRESET,
    SWMM_DEPTH_QUANTITY,
)

RID = "01SWMMSEAM00000000000000000"
_BBOX = (-77.052, 38.802, -77.044, 38.808)


def _resolved_style(preset, uri):
    from trid3nt_server.emission.publish import (
        style_params_from_band_stats,
    )

    return style_params_from_band_stats(
        preset, is_categorical=False, is_rgba=False, p2=None, p98=None, layer_uri=uri
    )


def _render_row(layer, resolved):
    """RENDER-affecting fields only (layer_id excluded -- explained stem swap)."""
    return {
        "name": layer.name,
        "layer_type": layer.layer_type,
        "style_preset": layer.style_preset,
        "role": layer.role,
        "units": layer.units,
        "bbox": tuple(round(v, 6) for v in layer.bbox) if layer.bbox else None,
        "rescale": resolved,
    }


def _swmm_entries(n_frames: int) -> list[dict]:
    """The peak + N frame entries postprocess_swmm writes (quantity flood_depth)."""
    entries = [
        build_entry(
            kind="raster",
            quantity=SWMM_DEPTH_QUANTITY,
            name="Peak flood depth",
            uri=f"s3://trid3nt-runs/{RID}/swmm_depth_peak.tif",
            t=None,
            units="meters",
            bbox=list(_BBOX),
        )
    ]
    for i in range(1, n_frames + 1):
        entries.append(
            build_entry(
                kind="raster",
                quantity=SWMM_DEPTH_QUANTITY,
                name=f"Flood depth step {i}",
                uri=f"s3://trid3nt-runs/{RID}/swmm_depth_frame_{i:02d}.tif",
                t=float((i - 1) * 300),  # 5-min cadence, elapsed seconds
                units="meters",
                bbox=list(_BBOX),
            )
        )
    return entries


def _legacy_frame_stream(n_frames: int):
    """The OLD bespoke SWMM frame layers (pre-ADR-0282) render rows."""
    rows = []
    for i in range(1, n_frames + 1):
        uri = f"s3://trid3nt-runs/{RID}/swmm_depth_frame_{i:02d}.tif"
        lyr = LayerURI(
            layer_id=f"swmm-depth-frame-{i:02d}-{RID}",
            name=f"Flood depth step {i}",
            layer_type="raster",
            uri=uri,
            style_preset=FLOOD_DEPTH_STYLE_PRESET,
            role="context",
            units="meters",
            bbox=_BBOX,
        )
        rows.append(_render_row(lyr, _resolved_style(FLOOD_DEPTH_STYLE_PRESET, uri)))
    return rows


def test_quantity_resolves_to_flood_depth_preset():
    from trid3nt_server.emission.quantity_styles import resolve_style_preset

    preset, is_fallback = resolve_style_preset(SWMM_DEPTH_QUANTITY)
    assert preset == FLOOD_DEPTH_STYLE_PRESET and is_fallback is False


def test_frames_only_skips_the_peak():
    manifest = parse_outputs_manifest(
        append_entries(None, engine="swmm", run_id=RID, new=_swmm_entries(5))
    )
    seam = build_layers_from_outputs(manifest, run_id=RID, bbox=_BBOX, frames_only=True)
    # 5 frames, NO peak (the composer keeps its own typed peak -- no double reg).
    assert len(seam.layers) == 5
    assert all(l.role == "context" for l in seam.layers)
    assert not any(l.role == "primary" for l in seam.layers)


def test_never_omit_all_frames_published():
    # 60 frames (a fine cadence) -- every one is published (no 144-cap thinning,
    # and well past the retired 24/48 caps).
    n = 60
    manifest = parse_outputs_manifest(
        append_entries(None, engine="swmm", run_id=RID, new=_swmm_entries(n))
    )
    seam = build_layers_from_outputs(manifest, run_id=RID, bbox=_BBOX, frames_only=True)
    assert len(seam.layers) == n
    names = [l.name for l in seam.layers]
    assert names == [f"Flood depth step {i}" for i in range(1, n + 1)]


def test_frame_render_stream_byte_equivalent_to_legacy():
    n = 5
    manifest = parse_outputs_manifest(
        append_entries(None, engine="swmm", run_id=RID, new=_swmm_entries(n))
    )
    seam = build_layers_from_outputs(manifest, run_id=RID, bbox=_BBOX, frames_only=True)
    new_stream = [
        _render_row(l, _resolved_style(l.style_preset, l.uri)) for l in seam.layers
    ]
    old_stream = _legacy_frame_stream(n)
    assert new_stream == old_stream, (
        "frame render stream diverged:\nOLD=%s\nNEW=%s" % (old_stream, new_stream)
    )
    # The one EXPLAINED divergence: the layer_id stem (physical quantity vs the
    # engine-prefixed legacy stem). Grouping rides the name token, not the id.
    assert all(l.layer_id.startswith("flood-depth-frame-") for l in seam.layers)
    # Additive replay metadata: monotonic per-frame t + one temporal group.
    temporal = [f for f in seam.frames if f.t is not None]
    assert len(temporal) == n
    ts = [f.t for f in temporal]
    assert ts == sorted(ts) and len(set(ts)) == n
    assert all(f.group_id == f"flood-depth-{RID}" for f in temporal)


def test_report_step_cadence_maps_output_interval_min():
    from trid3nt_server.mesh.raster_cell_mesh import _report_step_hms

    assert _report_step_hms(None) == "00:05:00"  # legacy default, byte-identical
    assert _report_step_hms(15) == "00:15:00"
    assert _report_step_hms(2.5) == "00:02:30"
    assert _report_step_hms(0) == "00:05:00"  # non-positive -> legacy default
