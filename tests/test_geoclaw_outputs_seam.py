"""ADR 0281 -- the GeoClaw emit-on-solve leg: producer + byte-equivalence bar.

The migration bar (outputs-manifest-schema.md Section 7.1): the emitted
layer-event stream from the NEW ``outputs.json`` + seam path is byte-identical --
field for field -- to the OLD ``register_manifest_layers`` path for the SAME
solved GeoClaw output, EXCEPT the internal ``layer_id`` stem, which the seam
standardizes on the physical quantity (``flood-depth-*``) rather than the
register path's engine-prefixed stem (``geoclaw-depth-*``). Every RENDER-affecting
field (name, style_preset, role, units, bbox, resolved rescale, stashed legend)
is identical; the layer_id is an internal idempotence key (grouping rides the
``name`` token), so the stem swap is an explained, non-rendering divergence.

Proven on synthetic fort.q/fort.t frames (no clawpack binary) so it is a durable
offline regression.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

rasterio = pytest.importorskip("rasterio")

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from workers._geoclaw_postprocess import postprocess as gpp  # noqa: E402
from workers._raster_postprocess import outputs_manifest as om  # noqa: E402
from trid3nt_contracts.outputs_manifest import parse_outputs_manifest  # noqa: E402
from trid3nt_contracts.publish_manifest import parse_publish_manifest  # noqa: E402
from trid3nt_server.emission.outputs_seam import build_layers_from_outputs  # noqa: E402
from trid3nt_server.workflows.shared.register_published_manifest import (  # noqa: E402
    register_manifest_layers,
)

RID = "01GEOCLAWSEAM0000000000000"
_BBOX = (-87.5, 29.5, -85.5, 31.0)
_BUILD_SPEC = {"bbox": list(_BBOX), "scenario": "dam_break", "mask_ocean": False}


def _fort_q_frame(*, h_value: float, mx: int = 6, my: int = 5) -> str:
    lines = [
        "1      grid_number",
        "1      AMR_level",
        f"{mx}      mx",
        f"{my}      my",
        f"{_BBOX[0]:.4f}      xlow",
        f"{_BBOX[1]:.4f}      ylow",
        f"{(_BBOX[2] - _BBOX[0]) / mx:.6f}      dx",
        f"{(_BBOX[3] - _BBOX[1]) / my:.6f}      dy",
    ]
    for _ in range(mx * my):
        lines.append(f"{h_value:.6f} 0.000000 0.000000 {h_value:.6f}")
    return "\n".join(lines) + "\n"


def _fort_t(t_seconds: float) -> str:
    return f"{t_seconds:.16E}    time\n4    meqn\n1    ngrids\n"


def _resolved_style(preset, band_stats, uri):
    from trid3nt_server.emission.publish import (
        style_params_from_band_stats,
    )
    bs = band_stats or {}
    return style_params_from_band_stats(
        preset, is_categorical=bool(bs.get("is_categorical")),
        is_rgba=bool(bs.get("is_rgba")), p2=bs.get("p2"), p98=bs.get("p98"),
        layer_uri=uri,
    )


def _stashed_legend(uri):
    from trid3nt_server.emission import publish as pl
    lg = pl._LAST_LEGEND_BY_URI.get(uri)
    if lg is None:
        return None
    return (lg.kind, getattr(lg, "colormap", None), getattr(lg, "vmin", None),
            getattr(lg, "vmax", None), getattr(lg, "units", None))


def _render_row(layer, resolved):
    """The RENDER-affecting fields only -- layer_id is excluded (the seam
    standardizes the stem on the physical quantity; see the module docstring)."""
    return {
        "name": layer.name, "layer_type": layer.layer_type,
        "style_preset": layer.style_preset, "role": layer.role,
        "units": layer.units,
        "bbox": tuple(round(v, 6) for v in layer.bbox) if layer.bbox else None,
        "rescale": resolved, "stashed_legend": _stashed_legend(layer.uri),
    }


def _make_run(scratch: Path) -> gpp.GeoClawPostprocessResult:
    # 3 increasing-depth frames -> peak (max energy) + a 2-frame animation group.
    for no, h in ((1, 1.0), (2, 2.0), (3, 3.0)):
        (scratch / f"fort.q{no:04d}").write_text(_fort_q_frame(h_value=h))
        (scratch / f"fort.t{no:04d}").write_text(_fort_t(float((no - 1) * 60)))
    return gpp.run_geoclaw_postprocess(
        RID, scratch, _BUILD_SPEC, lambda rel: f"s3://trid3nt-runs/{RID}/{rel}"
    )


def test_geoclaw_select_frame_indices_never_omits():
    # ADR 0281: the bespoke post-hoc subsample-to-cap thinning is deleted -- every
    # solver-written frame is kept (deck-side output_frames is the sole control).
    assert gpp._select_frame_indices(0) == []
    assert gpp._select_frame_indices(3) == [0, 1, 2]
    assert gpp._select_frame_indices(500) == list(range(500))


def test_geoclaw_producer_emits_outputs_entries(tmp_path):
    res = _make_run(tmp_path)
    assert res.status == "ok", res.error_message
    # peak + 3 frames = 4 outputs entries; publish_manifest keeps the peak alone.
    assert len(res.manifest["layers"]) == 1
    assert len(res.outputs_entries) >= 3
    peak_e = res.outputs_entries[0]
    assert peak_e["quantity"] == "flood_depth" and "t" not in peak_e
    frame_es = res.outputs_entries[1:]
    # frames carry monotonically increasing physical t from fort.t (0, 60, 120).
    ts = [e["t"] for e in frame_es]
    assert all("t" in e for e in frame_es)
    assert ts == sorted(ts) and len(set(ts)) == len(ts)


def test_geoclaw_byte_equivalence_seam_vs_register(tmp_path):
    """The seam's PEAK row matches the register path's identity fields, its RANGE
    spans the whole run, and every seam FRAME is painted on that one range.

    Two explained divergences. The layer_id stem swap (geoclaw-depth ->
    flood-depth, engine-prefixed to physical-quantity). And the RANGE: a
    data-driven scale is scoped to the RUN, so the seam - which sees the peak and
    every frame - spans them all, while publish_manifest holds the peak alone and
    can only ever range over that one raster. The seam's range therefore CONTAINS
    the register path's."""
    res = _make_run(tmp_path)
    assert res.status == "ok", res.error_message

    # OLD register path.
    pm = parse_publish_manifest(json.dumps(res.manifest))
    old = register_manifest_layers(pm, run_id=RID, bbox=None)
    old_stream = []
    for e, lyr in zip(pm.layers, old.layers):
        bs = {"is_categorical": e.band_stats.is_categorical,
              "is_rgba": e.band_stats.is_rgba, "p2": e.band_stats.p2,
              "p98": e.band_stats.p98}
        old_stream.append(_render_row(lyr, _resolved_style(e.style_preset, bs, e.cog_uri)))

    # NEW seam path.
    manifest = parse_outputs_manifest(
        om.append_entries(None, engine="geoclaw", run_id=RID, new=res.outputs_entries)
    )
    new = build_layers_from_outputs(manifest, run_id=RID, bbox=None)
    # Both sides resolve WITH the entry's band stats: under policy=data a preset's
    # range comes from those statistics, so omitting them on one side would compare
    # a raster against itself-without-its-own-data rather than path against path.
    stats_by_uri = {e.uri: {"is_categorical": e.band_stats.is_categorical,
                            "is_rgba": e.band_stats.is_rgba,
                            "p2": e.band_stats.p2, "p98": e.band_stats.p98}
                    for e in manifest.entries if e.band_stats is not None}
    new_stream = [
        _render_row(lyr, _resolved_style(lyr.style_preset,
                                         stats_by_uri.get(lyr.uri), lyr.uri))
        for lyr in new.layers
    ]

    new_peak = [r for r in new_stream if r["role"] == "primary"]
    assert len(new_peak) == len(old_stream) == 1
    for field in ("name", "layer_type", "style_preset", "role", "units", "bbox",
                  "rescale"):
        assert new_peak[0][field] == old_stream[0][field], (
            "peak render row diverged on %s:\nOLD=%s\nNEW=%s"
            % (field, old_stream, new_peak))

    seam_kind, seam_cmap, seam_lo, seam_hi, seam_units = new_peak[0]["stashed_legend"]
    reg_kind, reg_cmap, reg_lo, reg_hi, reg_units = old_stream[0]["stashed_legend"]
    assert (seam_kind, seam_cmap, seam_units) == (reg_kind, reg_cmap, reg_units)
    assert seam_lo <= reg_lo and seam_hi >= reg_hi, (
        "the seam's run range %s does not contain the register path's peak range %s"
        % ((seam_lo, seam_hi), (reg_lo, reg_hi)))

    # THE RULE: one range for the whole run - every frame on the peak's legend.
    frame_rows = [r for r in new_stream if r["role"] != "primary"]
    assert frame_rows
    for row in frame_rows:
        for field in ("style_preset", "units", "bbox", "stashed_legend"):
            assert row[field] == new_peak[0][field], field

    # The one EXPLAINED divergence: layer_id stem. OLD is engine-prefixed
    # (geoclaw-depth-*), NEW is physical-quantity (flood-depth-*).
    assert all(l.layer_id.startswith("geoclaw-depth-") for l in old.layers)
    assert all(l.layer_id.startswith("flood-depth-") for l in new.layers)
    # NEW additionally carries per-frame t + group_id (additive replay metadata).
    temporal = [f for f in new.frames if f.t is not None]
    assert temporal and all(f.group_id == f"flood-depth-{RID}" for f in temporal)
