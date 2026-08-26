"""ADR 0281 -- the SWAN emit-on-solve leg: producer + byte-equivalence bar.

The migration bar (outputs-manifest-schema.md Section 7.1): the emitted
layer-event stream from the NEW ``outputs.json`` + seam path is byte-identical --
field for field -- to the OLD ``register_swan_wave_layers`` path for the SAME
solved SWAN output, EXCEPT the internal ``layer_id`` stem, which the seam
standardizes on the physical quantity (``wave-height-*``) rather than the register
path's engine-prefixed stem (``swan-wave-height-*``). Every RENDER-affecting field
(name, style_preset, role, units, bbox, resolved rescale, stashed legend) is
identical; the layer_id is an internal idempotence key (grouping rides the
``name`` token), so the stem swap is an explained, non-rendering divergence.

The SWAN .mat reader is monkeypatched (no scipy mat file needed); everything
downstream (rasterisation + COG write + manifest + outputs.json) runs for real.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

rasterio = pytest.importorskip("rasterio")
import numpy as np  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from workers._swan_postprocess import postprocess as spp  # noqa: E402
from workers._raster_postprocess import outputs_manifest as om  # noqa: E402
from trid3nt_contracts.outputs_manifest import parse_outputs_manifest  # noqa: E402
from trid3nt_contracts.publish_manifest import parse_publish_manifest  # noqa: E402
from trid3nt_server.emission.outputs_seam import build_layers_from_outputs  # noqa: E402
from trid3nt_server.workflows.shared.register_published_manifest import (  # noqa: E402
    register_swan_wave_layers,
)

RID = "01SWANSEAM00000000000000000"
_BBOX = (-118.05, 33.60, -117.95, 33.70)
_BUILD_SPEC = {
    "bbox": list(_BBOX),
    "mode": "nonstationary",
    "sim_duration_s": 7200.0,
    "output_frames": 3,
}


def _frames(n: int) -> dict:
    """n Hs frames of increasing energy (peak = last), + matching Tp/Dir."""
    hs, tp, dir_ = [], [], []
    for k in range(n):
        a = np.zeros((20, 24), dtype="float64")
        a[8:12, 8:16] = 1.0 + k  # increasing wave height -> deterministic peak
        hs.append(a)
        tp.append(np.full_like(a, 8.0))
        dir_.append(np.full_like(a, 180.0))
    return {"hs": hs, "tp": tp, "dir": dir_}


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
    """RENDER-affecting fields only -- layer_id excluded (the seam standardizes the
    stem on the physical quantity; see module docstring)."""
    return {
        "name": layer.name, "layer_type": layer.layer_type,
        "style_preset": layer.style_preset, "role": layer.role,
        "units": layer.units,
        "bbox": tuple(round(v, 6) for v in layer.bbox) if layer.bbox else None,
        "rescale": resolved, "stashed_legend": _stashed_legend(layer.uri),
    }


def _make_run(scratch: Path, monkeypatch) -> spp.SwanPostprocessResult:
    (scratch / "swan_out.mat").write_bytes(b"stub")
    monkeypatch.setattr(spp, "_read_mat_fields", lambda _p: _frames(3))
    return spp.run_swan_postprocess(
        RID, scratch, _BUILD_SPEC, lambda rel: f"s3://trid3nt-runs/{RID}/{rel}"
    )


def test_swan_select_frame_indices_never_omits():
    # ADR 0281: the bespoke post-hoc subsample-to-cap thinning is deleted -- every
    # solver-written frame is kept (deck-side output_frames is the sole control).
    assert spp._select_frame_indices(0) == []
    assert spp._select_frame_indices(3) == [0, 1, 2]
    assert spp._select_frame_indices(500) == list(range(500))


def test_swan_producer_emits_outputs_entries(tmp_path, monkeypatch):
    res = _make_run(tmp_path, monkeypatch)
    assert res.status == "ok", res.error_message
    # publish_manifest keeps the peak alone; outputs.json = peak + every frame.
    assert len(res.manifest["layers"]) == 1
    assert len(res.outputs_entries) >= 3
    peak_e = res.outputs_entries[0]
    assert peak_e["quantity"] == "wave_height" and "t" not in peak_e
    frame_es = res.outputs_entries[1:]
    ts = [e["t"] for e in frame_es]
    assert all("t" in e for e in frame_es)
    assert ts == sorted(ts) and len(set(ts)) == len(ts)
    # 3 frames over 7200 s evenly spaced -> t = 0, 3600, 7200.
    assert ts == [0.0, 3600.0, 7200.0]


def test_swan_byte_equivalence_seam_vs_register(tmp_path, monkeypatch):
    """The seam's PEAK row matches the register path's identity fields, its RANGE
    spans the whole run, and every seam FRAME is painted on that one range.

    Two explained divergences. The layer_id stem swap (swan-wave-height ->
    wave-height). And the RANGE: a data-driven scale is scoped to the RUN, so the
    seam - which sees the peak and every frame - spans them all, while
    publish_manifest holds the peak alone and can only range over that one
    raster. The seam's range therefore CONTAINS the register path's."""
    res = _make_run(tmp_path, monkeypatch)
    assert res.status == "ok", res.error_message

    # OLD register path (the actual SWAN register: register_swan_wave_layers).
    pm = parse_publish_manifest(json.dumps(res.manifest))
    old_layers, _top, _dropped = register_swan_wave_layers(
        pm, run_id=RID, mode="nonstationary", bbox=None
    )
    old_stream = []
    for e, lyr in zip(pm.layers, old_layers):
        bs = {"is_categorical": e.band_stats.is_categorical,
              "is_rgba": e.band_stats.is_rgba, "p2": e.band_stats.p2,
              "p98": e.band_stats.p98}
        old_stream.append(_render_row(lyr, _resolved_style(e.style_preset, bs, e.cog_uri)))

    # NEW seam path.
    manifest = parse_outputs_manifest(
        om.append_entries(None, engine="swan", run_id=RID, new=res.outputs_entries)
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

    # The one EXPLAINED divergence: layer_id stem.
    assert all(l.layer_id.startswith("swan-wave-height-") for l in old_layers)
    assert all(l.layer_id.startswith("wave-height-") for l in new.layers)
    # NEW additionally carries per-frame t + group_id (additive replay metadata).
    temporal = [f for f in new.frames if f.t is not None]
    assert temporal and all(f.group_id == f"wave-height-{RID}" for f in temporal)
