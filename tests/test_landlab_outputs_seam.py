"""ADR 0282 -- the Landlab overland-flow emit-on-solve leg (FRAME byte-equivalence).

M-class ruling OPTION (a): the seam owns the TEMPORAL FRAMES ONLY; the typed peak
``LandlabOverlandTimeseriesLayerURI`` + its scalars stay composer-built. The
byte-equivalence bar is the FRAME render stream (name, layer_type, style_preset,
role, units, bbox, resolved rescale): the seam ``frames_only`` frames are
byte-identical to the legacy bespoke ``"Overland depth step N"`` context frames,
EXCEPT the internal ``layer_id`` stem (``flood-depth-frame-*`` vs the legacy
``landlab-overland-depth-frame-*``); grouping rides the ``name`` token.

Frames carry the worker's REAL snapshot elapsed seconds (``max_cell_series``).
Pure/offline -- entries built by the same host-exec writer the postprocess uses.
"""

from __future__ import annotations

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.outputs_manifest import (
    append_entries,
    build_entry,
    parse_outputs_manifest,
)
from trid3nt_server.emission.outputs_seam import build_layers_from_outputs
from trid3nt_server.workflows.landlab.postprocess_landlab import (
    OVERLAND_QUANTITY,
    OVERLAND_STYLE_PRESET,
    _overland_frame_t,
)

RID = "01LANDLABOVERLAND000000000"
_BBOX = (-105.30, 40.00, -105.26, 40.03)


def _resolved_style(preset, uri):
    from trid3nt_server.tools.publish_layer.publish_layer import (
        style_params_from_band_stats,
    )

    return style_params_from_band_stats(
        preset, is_categorical=False, is_rgba=False, p2=None, p98=None, layer_uri=uri
    )


def _render_row(layer, resolved):
    return {
        "name": layer.name,
        "layer_type": layer.layer_type,
        "style_preset": layer.style_preset,
        "role": layer.role,
        "units": layer.units,
        "bbox": tuple(round(v, 6) for v in layer.bbox) if layer.bbox else None,
        "rescale": resolved,
    }


def _overland_entries(n_frames: int, *, times_s):
    entries = [
        build_entry(
            kind="raster",
            quantity=OVERLAND_QUANTITY,
            name="Peak overland depth",
            uri=f"s3://trid3nt-runs/{RID}/landlab_overland_peak.tif",
            t=None,
            units="meters",
            bbox=list(_BBOX),
        )
    ]
    for i in range(1, n_frames + 1):
        entries.append(
            build_entry(
                kind="raster",
                quantity=OVERLAND_QUANTITY,
                name=f"Overland depth step {i}",
                uri=f"s3://trid3nt-runs/{RID}/landlab_overland_depth_frame_{i:02d}.tif",
                t=float(times_s[i - 1]),
                units="meters",
                bbox=list(_BBOX),
            )
        )
    return entries


def _legacy_frame_stream(n_frames: int):
    rows = []
    for i in range(1, n_frames + 1):
        uri = f"s3://trid3nt-runs/{RID}/landlab_overland_depth_frame_{i:02d}.tif"
        lyr = LayerURI(
            layer_id=f"landlab-overland-depth-frame-{i:02d}-{RID}",
            name=f"Overland depth step {i}",
            layer_type="raster",
            uri=uri,
            style_preset=OVERLAND_STYLE_PRESET,
            role="context",
            units="meters",
            bbox=_BBOX,
        )
        rows.append(_render_row(lyr, _resolved_style(OVERLAND_STYLE_PRESET, uri)))
    return rows


def test_overland_quantity_shares_flood_depth_preset():
    from trid3nt_server.emission.quantity_styles import resolve_style_preset

    preset, is_fallback = resolve_style_preset(OVERLAND_QUANTITY)
    assert preset == OVERLAND_STYLE_PRESET and is_fallback is False


def test_frame_t_reads_worker_snapshot_elapsed_seconds():
    series = [{"time_s": 0.0}, {"time_s": 120.5}, {"time_s": 240.0}]
    assert _overland_frame_t(series, 1) == 0.0
    assert _overland_frame_t(series, 2) == 120.5
    assert _overland_frame_t(series, 3) == 240.0
    # ordinal fallback when the series is short/absent (distinct + monotonic).
    assert _overland_frame_t([], 4) == 3.0


def test_frames_only_skips_peak_and_never_omits():
    n = 40
    times = [i * 150.0 for i in range(n)]
    manifest = parse_outputs_manifest(
        append_entries(None, engine="landlab", run_id=RID, new=_overland_entries(n, times_s=times))
    )
    seam = build_layers_from_outputs(manifest, run_id=RID, bbox=_BBOX, frames_only=True)
    assert len(seam.layers) == n  # never-omit (past the retired 48-snapshot cap)
    assert all(l.role == "context" for l in seam.layers)
    assert [l.name for l in seam.layers] == [f"Overland depth step {i}" for i in range(1, n + 1)]


def test_frame_render_stream_byte_equivalent_to_legacy():
    n = 6
    times = [i * 300.0 for i in range(n)]
    manifest = parse_outputs_manifest(
        append_entries(None, engine="landlab", run_id=RID, new=_overland_entries(n, times_s=times))
    )
    seam = build_layers_from_outputs(manifest, run_id=RID, bbox=_BBOX, frames_only=True)
    new_stream = [_render_row(l, _resolved_style(l.style_preset, l.uri)) for l in seam.layers]
    assert new_stream == _legacy_frame_stream(n)
    assert all(l.layer_id.startswith("flood-depth-frame-") for l in seam.layers)
    temporal = [f for f in seam.frames if f.t is not None]
    assert [f.t for f in temporal] == times
    assert all(f.group_id == f"flood-depth-{RID}" for f in temporal)
