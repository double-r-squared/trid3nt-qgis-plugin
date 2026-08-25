#!/usr/bin/env python
"""Diagnostic CHART proof: a run's own persisted spec, through the dock's renderer.

The chart SPEC is the product (the plugin's chart dock is the one renderer, and
server-side figure generation is retired), so a proof that redrew the numbers with
matplotlib here would be showing a SECOND chart that merely resembles the one the
user sees. This reads ``<run>/chart_spec.json`` off the run's own prefix and draws
it through ``plugin/ui/charts.render_spec`` at the dock's own 6.0 x 2.2 in
geometry - the same interpreter, the same size, the same result.

Generic by construction: every template on the workflow skeleton persists its
chart spec under its run prefix, so this needs no per-template knowledge at all.

Env (MinIO): set -a; source .env.local; set +a
Usage: render_run_chart_proof.py --run-id <ULID> --stem telemac_do_sag
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

from matplotlib.figure import Figure  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def _dock_renderer():
    """The PLUGIN's own chart module, imported as the package member it is.

    By PATH would be simpler but wrong: ``charts.py`` does a relative import of
    its sibling ``install_dependencies``, so loading the file in isolation fails.
    Importing it as ``plugin.ui.charts`` with the repo root on the path is what
    gives it the package it was written inside - and is what keeps this proof the
    DOCK's renderer rather than a copy of it.
    """
    import importlib

    module = importlib.import_module("plugin.ui.charts")
    if module.Figure is None:            # the dock degrades without matplotlib
        module.Figure = Figure
        module._MATPLOTLIB_ERROR = None
    return module


def _read_spec(bucket: str, run_id: str) -> dict:
    import boto3

    s3 = boto3.client("s3", endpoint_url=os.environ["AWS_ENDPOINT_URL"],
                      region_name=os.environ.get("AWS_REGION", "us-east-1"))
    blob = s3.get_object(Bucket=bucket, Key=f"{run_id}/chart_spec.json")["Body"].read()
    return json.loads(blob)


def _each_chart(document: dict, wanted: str | None):
    """The chart payloads in a persisted document, as ``(name, payload)`` pairs.

    A run persists ``RunResult.charts``, which is a MAP of the DECLARED chart name
    to its payload - a template may declare more than one. A payload that is
    itself a chart (the single-chart shape a hand-written composer wrote) is
    yielded unnamed, so both documents read here.
    """
    if any(key in document for key in ("vega_lite_spec", "spec", "layer", "mark")):
        yield None, document
        return
    for name, payload in document.items():
        if wanted and name != wanted:
            continue
        if isinstance(payload, dict):
            yield name, payload


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--stem", required=True, help="output basename (the workflow file)")
    ap.add_argument("--bucket", default=os.environ.get("TRID3NT_RUNS_BUCKET",
                                                       "trid3nt-runs"))
    ap.add_argument("--out-dir", default=str(REPO / "docs" / "proof" / "templates"))
    ap.add_argument("--chart", default=None,
                    help="render only this DECLARED chart name (default: all)")
    ap.add_argument("--caption", default="")
    ns = ap.parse_args(argv)

    persisted = _read_spec(ns.bucket, ns.run_id)
    charts = _dock_renderer()
    out_dir = Path(ns.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    written = []
    for name, payload in _each_chart(persisted, ns.chart):
        # The persisted payload is the emission ENVELOPE (chart_id, title, caption
        # and the spec); ``parse_chart_payload`` validates that envelope and hands
        # it back whole, while ``render_spec`` draws the VEGA-LITE SPEC inside it.
        # Passing the envelope renders an empty figure with a "skipped" summary -
        # the dock unwraps it, and so does this.
        envelope = charts.parse_chart_payload(payload)
        if envelope is None:
            continue
        spec = envelope["vega_lite_spec"]
        figure = Figure(figsize=(6.0, 2.2), dpi=100)
        summary = charts.render_spec(figure, spec)
        caption = (ns.caption or f"{ns.stem} - run {ns.run_id} - "
                   f"{envelope.get('caption') or 'the chart the run persisted'}")
        figure.suptitle(envelope.get("title") or charts.spec_title(spec),
                        fontsize=9, y=1.04)
        figure.text(0.01, 0.005, caption[:200], fontsize=6.0, color="#888888")
        out = out_dir / (f"{ns.stem}_chart.png" if name is None
                         else f"{ns.stem}_chart_{name}.png")
        figure.savefig(out, dpi=200, bbox_inches="tight")
        written.append({"chart": str(out), "name": name,
                        "title": envelope.get("title") or charts.spec_title(spec),
                        "render_summary": summary, "bytes": out.stat().st_size})
    if not written:
        raise SystemExit(f"run {ns.run_id} persisted no chart matching "
                         f"{ns.chart!r} (has {sorted(persisted)})")
    print(json.dumps({"run_id": ns.run_id, "charts": written}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
