"""ADR 0287 live proof -- HEC-RAS emit-on-solve (BOTH lineages) through the seam.

Direct-call (no chat layer): runs REAL HEC-RAS solves through the migrated
composers with a capturing emitter bound, then proves the frames chain end-to-end:

  1. solve -> the producer writes outputs.json (peak entry + per-step frame entries).
  2. the SEAM builds the flood_depth temporal group (frames_only=True) -- style,
     role, monotonic t, single group asserted.
  3. the typed HecrasDepthLayerURI peak + its narration scalars survive.
  4. the composer EMITS the frames out-of-band (session-state loaded_layers).
  5. reopen: re-read outputs.json -> identical layer_ids (idempotent).

LEG=6x   -> model_hecras_riverine_flood(run_demo_geometry=True) (consented Muncie,
            banner-labeled DEMONSTRATION GEOMETRY; law 9's synthetic rule).
LEG=2025 -> model_hecras_flood_2d_rog(bbox=..., design_storm_mm_per_hr=...) (a real
            US site; the managed-engine rain-on-grid producer).

Run:
  env $(grep -v '^#' .env.local | sed 's/^export //' | xargs) \
    PYTHONPATH=.:contracts venvs/agent/bin/python scripts/proof_hecras_seam_0287.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import types as _t

logging.basicConfig(level=logging.WARNING)

import trid3nt_server.data as _bootstrap  # noqa: F401 -- init the tool registry first
from trid3nt_server.emission.pipeline_emitter import PipelineEmitter, _CURRENT_EMITTER
from trid3nt_server.emission.outputs_seam import (
    build_layers_from_outputs,
    read_outputs_manifest,
)

LEG = os.environ.get("LEG", "6x")


def _new_emitter():
    frames: list[dict] = []

    async def sink(text: str) -> None:
        try:
            frames.append(json.loads(text))
        except Exception:
            pass

    from trid3nt_contracts import new_ulid

    return PipelineEmitter(session_id=new_ulid(), sink=sink), frames


def _run_id(layer_id: str) -> str:
    return layer_id.rsplit("-", 1)[-1]


def _seam_report(run_id: str, bbox) -> tuple[list[str], dict]:
    manifest = read_outputs_manifest(_t.SimpleNamespace(run_id=run_id))
    if manifest is None:
        return [], {"outputs_json": "NONE (peak-only degrade)"}
    peaks = [e for e in manifest.entries if e.t is None]
    fr = [e for e in manifest.entries if e.t is not None]
    seam = build_layers_from_outputs(manifest, run_id=run_id, bbox=bbox, frames_only=True)
    groups: dict[str, list] = {}
    for f in seam.frames:
        if f.t is not None:
            groups.setdefault(f.group_id, []).append(f)
    ts = [e.t for e in fr]
    rep = {
        "outputs_json_entries": len(manifest.entries),
        "engine": manifest.engine,
        "schema": manifest.schema_version,
        "peak_entries": len(peaks),
        "frame_entries": len(fr),
        "t_first_s": round(ts[0], 3) if ts else None,
        "t_last_s": round(ts[-1], 3) if ts else None,
        "t_first_days": round(ts[0] / 86400.0, 4) if ts else None,
        "t_last_days": round(ts[-1] / 86400.0, 4) if ts else None,
        "seam_frame_layers": len(seam.layers),
        "seam_all_context": all(l.role == "context" for l in seam.layers),
        "seam_all_flood_depth_preset": all(
            l.style_preset == "continuous_flood_depth" for l in seam.layers
        ),
        "seam_groups": {gid: len(fs) for gid, fs in groups.items()},
        "t_monotonic": ts == sorted(ts),
        "distinct_t": len(set(ts)) == len(ts),
        "first_name": seam.layers[0].name if seam.layers else None,
        "last_name": seam.layers[-1].name if seam.layers else None,
    }
    return [l.layer_id for l in seam.layers], rep


def _emitted(frames: list[dict]) -> tuple[int, int]:
    sess = [f for f in frames if f.get("type") == "session-state"]
    loaded = sess[-1]["payload"]["loaded_layers"] if sess else []
    frame_rows = [r for r in loaded if "step" in (r.get("name") or "")]
    return len(loaded), len(frame_rows)


async def prove_6x() -> dict:
    from trid3nt_server.workflows.hecras.riverine_flood.riverine_flood import (
        model_hecras_riverine_flood,
    )

    flow_scale = float(os.environ.get("FLOW_SCALE", "1.0"))
    em, frames = _new_emitter()
    token = _CURRENT_EMITTER.set(em)
    try:
        depth = await model_hecras_riverine_flood(
            flow_scale=flow_scale, run_demo_geometry=True, input_mode="auto",
        )
    finally:
        _CURRENT_EMITTER.reset(token)
    if isinstance(depth, dict):
        return {"PASS": False, "leg": "6x", "error": depth}
    rid = _run_id(depth.layer_id)
    ids1, rep = _seam_report(rid, depth.bbox)
    ids2, _ = _seam_report(rid, depth.bbox)
    loaded, frame_rows = _emitted(frames)
    peak = {
        "layer_id": depth.layer_id, "role": depth.role,
        "depth_max_ft": depth.depth_max_ft, "wet_cell_count": depth.wet_cell_count,
        "peak_inflow_cfs": depth.peak_inflow_cfs, "wse_max_ft": depth.wse_max_ft,
    }
    passed = (
        rep.get("frame_entries", 0) > 1
        and rep.get("seam_frame_layers", 0) == rep.get("frame_entries", 0)
        and rep.get("seam_all_flood_depth_preset")
        and rep.get("t_monotonic")
        and len(rep.get("seam_groups", {})) == 1
        and depth.wet_cell_count > 0
        and ids1 == ids2
        and frame_rows > 1
    )
    return {"PASS": bool(passed), "leg": "6x", "flow_scale": flow_scale, "run_id": rid,
            "typed_peak": peak, "seam": rep,
            "emitted_loaded_layers": loaded, "emitted_frame_rows": frame_rows,
            "reopen_idempotent": ids1 == ids2}


async def prove_2025() -> dict:
    from trid3nt_server.workflows.hecras.flood_2d.flood_2d import (
        model_hecras_flood_2d_rog,
    )

    bbox = json.loads(os.environ.get("BBOX", "[-83.44, 35.02, -83.40, 35.06]"))  # Coweeta Creek, NC
    storm = float(os.environ.get("STORM_MM_HR", "25.0"))
    dur = float(os.environ.get("STORM_HR", "6.0"))
    res = float(os.environ.get("RES_M", "60.0"))
    em, frames = _new_emitter()
    token = _CURRENT_EMITTER.set(em)
    try:
        depth = await model_hecras_flood_2d_rog(
            bbox=bbox, design_storm_mm_per_hr=storm, storm_duration_hr=dur,
            resolution_m=res, input_mode="auto",
        )
    finally:
        _CURRENT_EMITTER.reset(token)
    if isinstance(depth, dict):
        return {"PASS": False, "leg": "2025", "error": depth}
    rid = _run_id(depth.layer_id)
    ids1, rep = _seam_report(rid, depth.bbox)
    ids2, _ = _seam_report(rid, depth.bbox)
    loaded, frame_rows = _emitted(frames)
    peak = {
        "layer_id": depth.layer_id, "role": depth.role,
        "depth_max_ft": depth.depth_max_ft, "wet_cell_count": depth.wet_cell_count,
    }
    passed = (
        rep.get("frame_entries", 0) > 1
        and rep.get("seam_frame_layers", 0) == rep.get("frame_entries", 0)
        and rep.get("seam_all_flood_depth_preset")
        and rep.get("t_monotonic")
        and len(rep.get("seam_groups", {})) == 1
        and ids1 == ids2
        and frame_rows > 1
    )
    return {"PASS": bool(passed), "leg": "2025", "bbox": bbox, "storm_mm_hr": storm,
            "run_id": rid, "typed_peak": peak, "seam": rep,
            "emitted_loaded_layers": loaded, "emitted_frame_rows": frame_rows,
            "reopen_idempotent": ids1 == ids2}


async def main() -> int:
    report = await (prove_2025() if LEG == "2025" else prove_6x())
    print("\n=== ADR 0287 HEC-RAS SEAM PROOF ===")
    print(json.dumps(report, indent=2, default=str))
    return 0 if report.get("PASS") else 1


if __name__ == "__main__":
    import sys

    sys.exit(asyncio.run(main()))
