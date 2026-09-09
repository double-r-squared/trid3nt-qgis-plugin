#!/usr/bin/env python
"""Live driver: the surface the LLM and the human actually use.

Five arms over ONE reach, driven through the daemon exactly as the plugin drives
it. The first builds the world and solves it plainly; the four after it reuse
that world through the step ledger - same invocation, same params - so what each
one exercises is the RAW KEYWORD FLOOR on the wire and nothing else.

  baseline        the reach as the template writes it, and the source it settles
  two_releases    the same reach with the releases composite stated as a longer
                  list: two point sources, at two mesh nodes the baseline's own
                  result names
  raw_keyword     LAW OF BOTTOM FRICTION stated raw, beating what the reach
                  derived, and shown in the deck as a fill
  unknown_keyword a misspelling, refused naming the nearest keyword
  bad_choice      a value outside the dictionary's choices, refused naming them

Env (MinIO): set -a; source .env.local; set +a
Usage: drive_keyword_floor.py --out-dir D [--only NAME ...] [--timeout 2400]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from trid3nt_server.testing import GateAnswers, LiveRun, run_live  # noqa: E402

#: The reach the module-surface canaries are proved on: a real NHDPlus reach with
#: NHDArea polygon coverage, coarse enough that the world is built once.
REACH = {
    "location": "Eel River near Scotia, California",
    "reach_length_km": 1.0,
    "sim_duration_s": 600.0,
    "spill_duration_s": 120.0,
    "source_q_m3s": 8.0,
    "mesh_resolution_m": 14.0,
    "discharge_m3s": 2.2,
    "dye_concentration_mgl": 100.0,
    "input_mode": "auto",
}
TOOL = "telemac_river_dye"
_DECK = "t2d_river.cas"
_RESULT = "r2d_river.slf"
#: What the second source is offset by, as a fraction of the reach's own length
#: along its principal axis. The template settles the first at 0.25 of the reach.
_SECOND_SOURCE_AT = 0.55


def _s3():
    from trid3nt_server.workflows.solver.solver import _get_s3_client

    return _get_s3_client()


def _bucket_of(report: dict) -> str:
    """The prefix the run wrote to, off a product uri the harness resolved."""
    for uri in report.get("product_uris", {}).values():
        if str(uri).startswith("s3://"):
            return str(uri)[len("s3://"):].split("/", 1)[0]
    raise SystemExit(f"run {report.get('run_id')} names no product prefix")


def _fetch(bucket: str, key: str) -> bytes:
    return _s3().get_object(Bucket=bucket, Key=key)["Body"].read()


def _deck_sources(deck: str) -> tuple[list[float], list[float]]:
    """The source coordinates the baseline's own deck states."""
    def _row(keyword: str) -> list[float]:
        for line in deck.splitlines():
            head, _, tail = line.partition("=")
            if head.strip().upper() == keyword:
                return [float(v) for v in tail.replace(";", " ").split()]
        raise SystemExit(f"the deck states no {keyword}")

    return _row("ABSCISSAE OF SOURCES"), _row("ORDINATES OF SOURCES")


def _second_point(bucket: str, run_id: str, first: tuple[float, float]
                  ) -> tuple[float, float]:
    """A wet mesh node farther down the reach - the second source's own place.

    Read off the baseline run's OWN result: the reach's principal axis is the
    long axis of its node cloud, and the second source sits at the deepest node
    near a station farther along it, so the point is inside the water the run
    actually solved rather than a coordinate this script invented.
    """
    import numpy as np

    from trid3nt_server.workflows.telemac.products.result_reader import read_selafin

    with tempfile.TemporaryDirectory(prefix="floor-") as scratch:
        path = os.path.join(scratch, _RESULT)
        with open(path, "wb") as fh:
            fh.write(_fetch(bucket, f"{run_id}/{_RESULT}"))
        result = read_selafin(path)
    x, y = np.asarray(result["x"], float), np.asarray(result["y"], float)
    depth = np.asarray(result["data"]["WATER DEPTH"][-1], float)
    cloud = np.column_stack([x - x.mean(), y - y.mean()])
    axis = np.linalg.svd(cloud, full_matrices=False)[2][0]
    along = cloud @ axis
    # Point the axis DOWNSTREAM: the first source sits a quarter of the way down,
    # so the far end from it is where the second one goes.
    first_along = (np.asarray(first, float) - [x.mean(), y.mean()]) @ axis
    span = along.max() - along.min()
    target = along.min() + _SECOND_SOURCE_AT * span
    if abs(target - first_along) < 0.15 * span:
        target = along.max() - _SECOND_SOURCE_AT * span
    near = np.abs(along - target) < 0.05 * span
    if not near.any():
        raise SystemExit("no mesh node near the second source station")
    wet = np.where(near, depth, -1.0)
    node = int(np.argmax(wet))
    return float(x[node]), float(y[node])


def _drive(name: str, args: dict, timeout: float, out_dir: str,
           expect_refusal: bool = False) -> dict:
    ev = run_live(LiveRun(
        tool=TOOL, args=args, case_title=f"keyword floor: {name}",
        answers=GateAnswers(confirm="proceed"), timeout_s=timeout,
        cleanup_case=True))
    metrics = ev.metrics or {}
    report = {
        "arm": name, "tool": TOOL, "args": args,
        "tool_status": ev.tool_status, "step_state": ev.step_state,
        "step_error": ev.step_error, "turn_complete": ev.turn_complete,
        "preflight": ev.preflight_note, "run_id": ev.run_id,
        "layers": [layer.get("name") for layer in ev.layers],
        "product_uris": ev.product_uris, "product_errors": ev.product_errors,
        "charts_emitted": ev.charts,
        "answer": {field: metrics.get(field) for field in
                   ("dye_cmax_mgl", "dye_peak_time_s", "plume_reach_m",
                    "active_frames", "mesh_size_m")},
        "detail": ev.detail,
    }
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, f"{name}_evidence.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"report": report, "evidence": ev.as_dict()}, fh, indent=2,
                  default=str)
    print(json.dumps(report, indent=2, default=str))
    if expect_refusal:
        if not (ev.is_error or ev.step_state == "failed"):
            raise SystemExit(f"{name}: the run did NOT refuse")
        return report
    ev.require_ok()
    ev.require_run_products()
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", dest="out_dir", required=True)
    ap.add_argument("--timeout", type=float, default=2400.0)
    ap.add_argument("--only", nargs="*", default=None)
    ns = ap.parse_args()
    wanted = ns.only or ["baseline", "two_releases", "raw_keyword",
                         "unknown_keyword", "bad_choice"]

    state_path = os.path.join(ns.out_dir, "baseline_state.json")
    state: dict = {}
    if os.path.exists(state_path):
        with open(state_path, encoding="utf-8") as fh:
            state = json.load(fh)

    if "baseline" in wanted:
        print("\n=== baseline ===", flush=True)
        report = _drive("baseline", dict(REACH), ns.timeout, ns.out_dir)
        bucket = _bucket_of(report)
        deck = _fetch(bucket, f"{report['run_id']}/{_DECK}")
        with open(os.path.join(ns.out_dir, "baseline_deck.cas"), "wb") as fh:
            fh.write(deck)
        abscissae, ordinates = _deck_sources(deck.decode("utf-8", "replace"))
        first = (abscissae[0], ordinates[0])
        state = {"run_id": report["run_id"], "bucket": bucket,
                 "first": list(first),
                 "second": list(_second_point(bucket, report["run_id"], first))}
        with open(state_path, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
        print(f"sources: first={state['first']} second={state['second']}")

    # Every arm after the baseline REUSES its world: same invocation, same
    # params, so the ledger replays the domain, the mesh and the settle and the
    # fill is the only thing that runs differently.
    release = {"q": REACH["source_q_m3s"],
               "tracers": [REACH["dye_concentration_mgl"]],
               "window_s": REACH["spill_duration_s"],
               "until_s": REACH["sim_duration_s"]}
    arms = {
        "two_releases": (
            {**REACH, "restart_clean": False,
             "keywords": {"releases": [{**release, "at": state.get("first")},
                                       {**release, "at": state.get("second")}]}},
            False),
        "raw_keyword": (
            {**REACH, "restart_clean": False,
             "keywords": {"LAW OF BOTTOM FRICTION": 4}}, False),
        "unknown_keyword": (
            {**REACH, "restart_clean": False,
             "keywords": {"LAW OF BOTOM FRICTION": 4}}, True),
        "bad_choice": (
            {**REACH, "restart_clean": False,
             "keywords": {"LAW OF BOTTOM FRICTION": 9}}, True),
    }
    failed = []
    for name in wanted:
        if name == "baseline":
            continue
        args, refusal = arms[name]
        print(f"\n=== {name} ===", flush=True)
        try:
            _drive(name, args, ns.timeout, ns.out_dir, expect_refusal=refusal)
        except Exception as exc:  # noqa: BLE001 - the driver reports every arm
            print(f"ARM FAILED {name}: {exc}", flush=True)
            failed.append(name)
    if failed:
        print(f"\nFAILED: {failed}")
        return 1
    print("\nevery arm answered as declared")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
