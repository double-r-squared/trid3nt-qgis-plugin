"""The DECLARED canary runs: one named Tier-A invocation per template.

A canary is the path-A test of the three-path model - every unfilled param
supplied on the call, so the gates are SATISFIED rather than skipped - sized so
the solve proves the plumbing and the physics answer in minutes rather than the
half hour a showcase takes. It is a DECLARATION: the tool, the args, the answers
its cards get. Nothing here implements a protocol; :mod:`live_run` does that.

Why this is product code and not a script per template: a canary is what a
migration's REPEATABILITY rests on. The same declaration runs before a change and
after it, and "same question, same answer" is only evidence when both runs came
from one frozen declaration rather than from two hand-typed command lines. One
home, one registry, one runner:

    venvs/agent/bin/python -m trid3nt_server.testing.canaries <name>

writes the run's evidence JSON, which the ``scripts/`` diagnostic lane renders
into the proof sheet (``scripts/render_all_layers_proof.py --evidence ...``).
Proof RENDERING stays out of the product tree by ruling; the declaration does
not.

DEMO VALUES LIVE IN THE DECLARATION. A canary's location, window and station are
here, in a labeled declaration, never as a constant inside workflow code.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from .live_run import GateAnswers, LiveRun, RunEvidence, run_live

__all__ = ["CANARIES", "evidence_path", "main", "run"]

#: Where a canary's evidence lands. Named after the TOOL, beside the proof
#: renders the diagnostic lane writes from it.
_EVIDENCE_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "docs", "proof", "templates")


# --------------------------------------------------------------------------- #
# TELEMAC family
# --------------------------------------------------------------------------- #

#: Apalachicola Bay, FL - the CO-OPS 8728690 gauge and the Hurricane Michael
#: window (2018-10-09..11). A HISTORICAL window on purpose: a canary that read
#: "the last few days" would report a quiet week as a physics regression.
_COASTAL_BBOX = [-85.02, 29.69, -84.90, 29.80]

CANARIES: dict[str, LiveRun] = {
    # The Michael surge coast at a coarse grid over a 6-hour window: wide enough
    # that the rising boundary actually floods low land (a canary whose answer is
    # zero cannot detect a regression in the thing it measures), small enough to
    # solve in seconds.
    "coastal_tidal_surge": LiveRun(
        tool="coastal_tidal_surge",
        args={
            "bbox": _COASTAL_BBOX,
            "series_type": "observed",
            "station": "8728690",
            "start_date": "2018-10-09",
            "end_date": "2018-10-11",
            "datum_offset_m": 0.0,
            "ocean_edge": "auto",
            "target_resolution_m": 250.0,
            "duration_hours": 6.0,
            "time_step_s": 20.0,
            "bathy_source": "noaa_demall",
            "compute_class": "medium",
            # user_gated, not auto: the resolved window / station / datum go past
            # a review card and the harness answers it. The datum offset is a
            # physics-consequential row, so a run that never showed it to anybody
            # refuses under law 9 - which is the floor working, not a canary to
            # route around.
            "input_mode": "user_gated",
        },
        case_title="canary: coastal tidal surge (Apalachicola Bay, coarse)",
        answers=GateAnswers(confirm="proceed"),
        cleanup_case=True,
    ),
}


def evidence_path(name: str) -> str:
    return os.path.join(_EVIDENCE_DIR, f"{name}_canary_evidence.json")


def run(name: str, *, timeout_s: float | None = None) -> RunEvidence:
    """Drive one declared canary over the live socket."""
    declared = CANARIES.get(name)
    if declared is None:
        raise KeyError(f"no canary named {name!r} (declared: {sorted(CANARIES)})")
    if timeout_s is not None:
        declared = LiveRun(**{**declared.__dict__, "timeout_s": timeout_s})
    return run_live(declared)


def _answer(ev: RunEvidence) -> dict[str, Any]:
    """The run's own PHYSICAL ANSWER, read off the artifacts it persisted.

    Never recomputed: the metrics document under the run prefix is the product,
    and a parity comparison that rebuilt the number would be comparing two
    implementations rather than two runs.
    """
    return {
        "tool": ev.tool,
        "run_id": ev.run_id,
        "step_state": ev.step_state,
        "step_error": ev.step_error,
        "tool_status": ev.tool_status,
        "turn_complete": ev.turn_complete,
        "layers": [layer.get("name") for layer in ev.layers],
        "metrics": ev.metrics,
        "chart_titles": [payload.get("title") for payload in ev.chart_payloads],
        "product_errors": ev.product_errors,
        "detail": ev.detail,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("name", choices=sorted(CANARIES))
    ap.add_argument("--timeout", type=float, default=1800.0)
    ap.add_argument("--out", default=None,
                    help="evidence JSON path (default: the canonical one)")
    ns = ap.parse_args(argv)

    ev = run(ns.name, timeout_s=ns.timeout)
    out = ns.out or evidence_path(ns.name)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(ev.as_dict(), fh, indent=2, default=str)
    print(json.dumps({**_answer(ev), "evidence": out}, indent=2, default=str))
    try:
        ev.require_ok()
    except Exception as exc:  # noqa: BLE001 - the reason IS the report
        print(f"CANARY FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
