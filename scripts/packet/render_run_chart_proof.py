#!/usr/bin/env python
"""Diagnostic CHART proof: a run's own persisted spec, through the dock's renderer.

Reads the chart spec off the run's own prefix and draws it through the plugin
dock's chart renderer at the dock's own geometry, so the proof is the dock's
picture rather than a matplotlib lookalike of it.
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

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "packet"))

import doc_size as DOC  # noqa: E402


def _dock_renderer():
    """The PLUGIN's own chart module, imported as the package member it is."""
    # By PATH would be simpler but wrong: ``charts.py`` relatively imports its
    # sibling ``install_dependencies``, so loading the file in isolation fails.
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
    ``RunResult.charts`` is a MAP of DECLARED name to payload; a payload that is
    itself a chart is yielded unnamed, so both document shapes read here."""
    if any(key in document for key in ("vega_lite_spec", "spec", "layer", "mark")):
        yield None, document
        return
    for name, payload in document.items():
        if wanted and name != wanted:
            continue
        if isinstance(payload, dict):
            yield name, payload


def render_charts(*, run_id: str, stem: str, out_dir: str | os.PathLike[str],
                  bucket: str | None = None, chart: str | None = None,
                  caption: str = "", doc: bool = False) -> list[dict]:
    """Every chart the run persisted, drawn through the DOCK's renderer."""
    ns = argparse.Namespace(
        run_id=run_id, stem=stem, out_dir=str(out_dir),
        bucket=bucket or os.environ.get("TRID3NT_RUNS_BUCKET", "trid3nt-runs"),
        chart=chart, caption=caption)
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
        figure.savefig(out, dpi=DOC.CHART_DPI if doc else 200,
                       bbox_inches="tight")
        written.append({"chart": str(out), "name": name,
                        "title": envelope.get("title") or charts.spec_title(spec),
                        "render_summary": summary, "bytes": out.stat().st_size})
    if not written:
        raise SystemExit(f"run {ns.run_id} persisted no chart matching "
                         f"{ns.chart!r} (has {sorted(persisted)})")
    return written


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
    ap.add_argument("--doc", action="store_true", default=False,
                    help="the DOC size a template page embeds")
    ns = ap.parse_args(argv)

    written = render_charts(run_id=ns.run_id, stem=ns.stem, out_dir=ns.out_dir,
                            bucket=ns.bucket, chart=ns.chart, caption=ns.caption,
                            doc=ns.doc)
    print(json.dumps({"run_id": ns.run_id, "charts": written}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
