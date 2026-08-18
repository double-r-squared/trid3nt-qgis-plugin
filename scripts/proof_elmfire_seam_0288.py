"""ADR 0288 live proof -- ELMFIRE emit-on-solve (derived-from-ToA frames) via seam.

Direct-call (no chat layer): runs a REAL ELMFIRE solve through a migrated composer
with a capturing emitter bound, then proves the frames chain end-to-end:

  1. solve -> postprocess writes outputs.json (peak entry + per-hour burned-extent
     frame entries), each a lossless per-hour threshold of the single solved ToA.
  2. the SEAM builds the fire_arrival temporal group (frames_only=True) -- style,
     role, monotonic t, single group asserted.
  3. the typed peak (FireSpreadLayerURI / ElmfireSensitivityLayerURI) + its scalars
     survive.
  4. the composer EMITS the frames out-of-band (session-state loaded_layers).
  5. reopen: re-read outputs.json -> identical layer_ids (idempotent).

LEG=fire_spread -> model_elmfire_fire_spread over a real Sierra fire-country AOI
                   (real LANDFIRE fuels + 3DEP topo; scenario-labeled fire weather).
LEG=spotting    -> model_elmfire_river_barrier_crossing (real US river reach found by
                   the existing river-barrier sub-reach search; OFF vs ON, honest
                   verdict + the ON-case burned-extent animation).

Run:
  env $(grep -v '^#' .env.local | sed 's/^export //' | xargs) \
    PYTHONPATH=.:contracts venvs/agent/bin/python scripts/proof_elmfire_seam_0288.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import types as _t

logging.basicConfig(level=logging.WARNING)

import trid3nt_server.data as _bootstrap  # noqa: F401 -- init the tool registry first
from trid3nt_contracts.elmfire_contracts import ElmfireRunArgs
from trid3nt_server.emission.pipeline_emitter import PipelineEmitter, _CURRENT_EMITTER
from trid3nt_server.emission.outputs_seam import (
    build_layers_from_outputs,
    read_outputs_manifest,
)

LEG = os.environ.get("LEG", "fire_spread")
_FIRE_PRESET = "continuous_fire_arrival_hr"


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
        "t_first_hr": round(ts[0] / 3600.0, 4) if ts else None,
        "t_last_hr": round(ts[-1] / 3600.0, 4) if ts else None,
        "seam_frame_layers": len(seam.layers),
        "seam_all_context": all(l.role == "context" for l in seam.layers),
        "seam_all_fire_arrival_preset": all(
            l.style_preset == _FIRE_PRESET for l in seam.layers
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


def _passed(rep: dict, ids1: list[str], ids2: list[str], frame_rows: int) -> bool:
    return bool(
        rep.get("frame_entries", 0) > 1
        and rep.get("seam_frame_layers", 0) == rep.get("frame_entries", 0)
        and rep.get("seam_all_fire_arrival_preset")
        and rep.get("seam_all_context")
        and rep.get("t_monotonic")
        and rep.get("distinct_t")
        and len(rep.get("seam_groups", {})) == 1
        and ids1 == ids2
        and frame_rows > 1
    )


async def prove_fire_spread() -> dict:
    from trid3nt_server.workflows.elmfire.fire_spread.fire_spread import (
        model_elmfire_fire_spread,
    )

    # A real Sierra-foothill fire-country AOI (CONUS, EPSG:5070); real LANDFIRE
    # fuels + 3DEP topo are FETCHED; the wind/moisture ride as scenario-labeled
    # fire weather (law 9, P8). ~5 km domain, 6-h dry wind-driven run -> hourly
    # burned-extent frames.
    bbox = json.loads(os.environ.get("BBOX", "[-120.88, 38.98, -120.82, 39.03]"))
    ign = json.loads(os.environ.get("IGN", "[-120.86, 39.00]"))
    run_args = ElmfireRunArgs(
        bbox=bbox,
        ignition_lonlat=ign,
        wind_speed_mph=float(os.environ.get("WIND_MPH", "25.0")),
        wind_dir_deg=float(os.environ.get("WIND_DIR", "270.0")),
        fuel_moisture="dry",
        duration_hours=float(os.environ.get("DUR_HR", "6.0")),
    )
    em, frames = _new_emitter()
    token = _CURRENT_EMITTER.set(em)
    try:
        primary = await model_elmfire_fire_spread(run_args)
    finally:
        _CURRENT_EMITTER.reset(token)
    if isinstance(primary, dict):
        return {"PASS": False, "leg": "fire_spread", "error": primary}
    rid = _run_id(primary.layer_id)
    ids1, rep = _seam_report(rid, primary.bbox)
    ids2, _ = _seam_report(rid, primary.bbox)
    loaded, frame_rows = _emitted(frames)
    peak = {
        "layer_id": primary.layer_id, "role": primary.role,
        "burned_area_km2": primary.burned_area_km2,
        "fire_arrival_max_hr": primary.fire_arrival_max_hr,
        "max_flame_length_m": primary.max_flame_length_m,
        "max_spread_rate_m_min": primary.max_spread_rate_m_min,
    }
    return {"PASS": _passed(rep, ids1, ids2, frame_rows), "leg": "fire_spread",
            "bbox": bbox, "ignition": ign, "run_id": rid, "typed_peak": peak,
            "seam": rep, "emitted_loaded_layers": loaded,
            "emitted_frame_rows": frame_rows, "reopen_idempotent": ids1 == ids2}


async def prove_spotting() -> dict:
    # Reuse the existing river-barrier proof's real sub-reach search (finds a real
    # US river reach that splits the domain into two land components), then run the
    # migrated real-mode composer with the ON-case animation. Rank EVERY passing
    # window across every region + take the first that survives an independent
    # re-warp of its bbox (the existing proof's meander-robust selection); on the
    # composer rejecting a reach at solve time (ELMFIRE_RIVER_NOT_SEPARATING), fall
    # through to the next surviving candidate.
    import scripts.proof_elmfire_river_barrier as rb
    from trid3nt_server.workflows.elmfire.spotting.spotting import (
        check_river_separates_domain,
        model_elmfire_river_barrier_crossing,
    )

    ranked: list[tuple[str, dict]] = []
    for name, region_bbox in rb.SEARCH_REGIONS.items():
        try:
            hits = rb._find_straight_subreach(region_bbox)
        except Exception as exc:  # noqa: BLE001
            print(f"  region {name}: search failed ({exc})")
            continue
        for h in hits:
            ranked.append((name, h))
    ranked.sort(
        key=lambda ns: (ns[1]["median_river_width_m"], ns[1]["cross_wind_coverage"]),
        reverse=True,
    )
    if not ranked:
        return {"PASS": False, "leg": "spotting", "error": "no viable river reach"}

    # Candidates surviving an independent re-warp two-component check (same grid
    # math the composer warps with), best first.
    candidates: list[tuple[str, list[float], tuple[float, float]]] = []
    for name, window in ranked:
        cand_bbox = window["_sub_bbox"]
        try:
            cand_arr, cand_grid = rb._warp_fbfm(cand_bbox)
        except Exception:  # noqa: BLE001
            continue
        if not check_river_separates_domain(cand_arr)["two_component"]:
            continue
        candidates.append((name, cand_bbox, rb._auto_ignition(cand_arr, cand_grid)))
        if len(candidates) >= 4:
            break
    if not candidates:
        return {"PASS": False, "leg": "spotting", "error": "no reach survived re-warp"}

    primary = None
    name = bbox = ign = None
    attempts: list[str] = []
    for name, bbox, ign in candidates:
        print(f"  TRYING region={name} bbox={bbox} ignition={ign}")
        run_args = ElmfireRunArgs(
            bbox=bbox, ignition_lonlat=list(ign),
            wind_speed_mph=float(os.environ.get("WIND_MPH", "35.0")),
            wind_dir_deg=float(os.environ.get("WIND_DIR", "270.0")),
            fuel_moisture="dry", duration_hours=float(os.environ.get("DUR_HR", "6.0")),
        )
        em, frames = _new_emitter()
        token = _CURRENT_EMITTER.set(em)
        try:
            result = await model_elmfire_river_barrier_crossing(
                run_args,
                mean_spotting_distance_m=60.0, nembers=30, pign_pct=100.0,
                critical_spotting_intensity_kwm=0.0,
            )
        except Exception as exc:  # noqa: BLE001 -- a rejected reach -> next candidate
            attempts.append(f"{name}: {type(exc).__name__}: {exc}")
            print(f"  reach {name} rejected: {exc}")
            _CURRENT_EMITTER.reset(token)
            continue
        _CURRENT_EMITTER.reset(token)
        if isinstance(result, dict):
            attempts.append(f"{name}: {result}")
            continue
        primary = result
        break
    if primary is None:
        return {"PASS": False, "leg": "spotting", "error": "all reaches rejected",
                "attempts": attempts}
    rid = _run_id(primary.layer_id)
    ids1, rep = _seam_report(rid, primary.bbox)
    ids2, _ = _seam_report(rid, primary.bbox)
    loaded, frame_rows = _emitted(frames)
    _sm = primary.summary
    _verdict = (
        "leaked" if _sm.get("off_side_leaks", 0.0) > 0.5
        else ("jumped" if _sm.get("break_jumped", 0.0) > 0.5 else "held")
    )
    peak = {
        "layer_id": primary.layer_id, "role": primary.role,
        "burned_area_km2": primary.burned_area_km2,
        "fire_arrival_max_hr": primary.fire_arrival_max_hr,
        "verdict": _verdict,
        "far_off_km2": _sm.get("far_side_area_spotting_off_km2"),
        "far_on_km2": _sm.get("far_side_area_spotting_on_km2"),
    }
    return {"PASS": _passed(rep, ids1, ids2, frame_rows), "leg": "spotting",
            "region": name, "bbox": bbox, "ignition": list(ign), "run_id": rid,
            "typed_peak": peak, "seam": rep, "emitted_loaded_layers": loaded,
            "emitted_frame_rows": frame_rows, "reopen_idempotent": ids1 == ids2}


async def main() -> int:
    report = await (prove_spotting() if LEG == "spotting" else prove_fire_spread())
    print("\n=== ADR 0288 ELMFIRE SEAM PROOF ===")
    print(json.dumps(report, indent=2, default=str))
    return 0 if report.get("PASS") else 1


if __name__ == "__main__":
    import sys

    sys.exit(asyncio.run(main()))
