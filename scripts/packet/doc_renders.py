#!/usr/bin/env python
"""The DOC RENDERS of one template's proving run: its figures and its run record.

Pins the run a template page documents, writes ``run.json`` beside the figures,
and renders composite, animations, stills and charts at the doc size, each
stamped with the run id and the commit. REFUSES a run whose products are gone.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "contracts"))
sys.path.insert(0, str(REPO / "scripts" / "packet"))

import assemble_proof_packet as PACKET  # noqa: E402
import doc_size as DOC  # noqa: E402

from trid3nt_server.testing.proof_animations import animations_for  # noqa: E402

__all__ = ["DOC_ROOT", "main", "proving_run", "render_template", "templates"]

#: Where a template's page and its figures live. The page is
#: ``<name>.md`` and its figures are the folder ``<name>/`` beside it.
DOC_ROOT = REPO / "docs" / "templates"
#: The run record a page is generated from: the declaration as invoked, the
#: sheet the run resolved, its provenance, its answer and its published layers.
RUN_RECORD = "run.json"
#: Where the local daemon writes its run journal. Untracked, so it is an input to
#: the RENDER lane only - a page is generated from the committed record instead.
_JOURNAL = REPO / "data" / "persistence" / "run_journal.jsonl"


class DocRenderError(RuntimeError):
    """The doc figures cannot be rendered, and the message says why."""


def templates() -> dict[str, Any]:
    """Every registered engine template, by tool name -> its ``Workflow``."""
    import trid3nt_server.tools  # noqa: F401 - the import IS the registration
    from trid3nt_server.tools import TOOL_REGISTRY

    found = {}
    for name, entry in TOOL_REGISTRY.items():
        workflow = getattr(entry.fn, "workflow", None)
        if workflow is not None and getattr(workflow.plan_decl, "steering", None):
            found[name] = workflow
    return dict(sorted(found.items()))


def _journal_rows(template: str) -> list[dict]:
    """This template's run-journal rows, oldest first."""
    if not _JOURNAL.is_file():
        raise DocRenderError(
            f"no run journal at {_JOURNAL}; the doc renders are driven from the "
            "runs this machine actually made")
    rows = []
    for line in _JOURNAL.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("template") == template:
            rows.append(row)
    return sorted(rows, key=lambda r: str(r.get("recorded_at") or ""))


def _case_id_for(run_id: str) -> str | None:
    """The case that published this run, found in the local persistence store.
    The store is the render lane's own input; a page is generated from the
    committed record instead."""
    # A soft-deleted case still holds its layers, and every canary deletes its
    # case on the way out - so the case is looked up by the run it names rather
    # than through the live-case listing, which excludes exactly these.
    root = Path(os.environ.get("TRID3NT_DEV_PERSISTENCE_DIR")
                or REPO / "data" / "persistence")
    for path in sorted(root.glob("*/*.json")):
        try:
            documents = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(documents, dict):
            continue
        for case_id, document in documents.items():
            if not isinstance(document, dict) or "layer_handles" not in document:
                continue
            if run_id in json.dumps(document.get("layer_handles") or {}):
                return str(document.get("case_id") or case_id)
    return None


def _case_layers(run_id: str) -> list[dict]:
    """The layers the run PUBLISHED, off the persisted case that holds them.
    Empty when the case is gone, which is what makes a run unrenderable."""
    import asyncio

    from trid3nt_server.persistence import make_persistence_for_backend

    case_id = _case_id_for(run_id)
    if case_id is None:
        return []
    state = asyncio.run(make_persistence_for_backend().get_session_state(case_id))
    return [row if isinstance(row, dict) else row.model_dump()
            for row in state.loaded_layers]


def proving_run(template: str, *, run_id: str | None = None) -> dict:
    """The run a page documents: the newest one that still has its products.
    A named ``run_id`` is taken as given; without one the journal is walked back."""
    rows = _journal_rows(template)
    if run_id:
        rows = [row for row in rows if row.get("run_id") == run_id]
        if not rows:
            raise DocRenderError(f"run {run_id} is not a {template} run in the journal")
    tried = []
    for row in reversed(rows):
        layers = _case_layers(str(row["run_id"]))
        if layers:
            return {**row, "layers": layers}
        tried.append(row["run_id"])
    raise DocRenderError(
        f"no {template} run still has its published layers (tried {tried[:6]}); "
        "re-run its canary through the drive lane and render from that")


def _record(template: str, run: dict, commit: str) -> dict:
    """The committed run record: what the page is generated from."""
    # The INVOCATION, not the sheet: a row the caller left unset resolved to a
    # declared default or to nothing, and reprinting it as an argument would
    # document a call nobody made.
    args = {row["name"]: row["value"] for row in run.get("sheet") or []
            if row.get("basis") == "user" and row.get("value") is not None}
    return {
        "tool": template,
        "run_id": run["run_id"],
        "commit": commit,
        "recorded_at": run.get("recorded_at"),
        "engine": run.get("engine"),
        "wall_seconds": run.get("wall_seconds"),
        "args": args,
        "sheet": run.get("sheet") or [],
        "provenance": run.get("provenance") or [],
        "answer": run.get("answer") or {},
        "mesh": run.get("mesh") or {},
        "layers": run.get("layers") or [],
    }


def render_template(template: str, *, run_id: str | None = None,
                    bucket: str | None = None) -> dict:
    """Pin one template's proving run and render its doc figures. Returns a report."""
    run = proving_run(template, run_id=run_id)
    commit = DOC.head_commit(REPO)
    out = DOC_ROOT / template
    out.mkdir(parents=True, exist_ok=True)
    record = _record(template, run, commit)
    record_path = out / RUN_RECORD
    record_path.write_text(json.dumps(record, indent=2, default=str) + "\n",
                           encoding="utf-8")

    bucket = bucket or os.environ.get("TRID3NT_RUNS_BUCKET", "trid3nt-runs")
    s3 = PACKET._s3()
    completion = PACKET._read_json(s3, bucket, f"{record['run_id']}/completion.json")
    result_slf = str(completion.get("result_slf") or "")
    if not result_slf:
        raise DocRenderError(
            f"run {record['run_id']} publishes no completion.json/result_slf; its "
            "products are gone and there is nothing to render")
    frames = int(PACKET.measure_frames(s3, bucket, record["run_id"],
                                       result_slf).get("frames", -1))

    report = PACKET._render(
        out, template, record_path, record, template=template, variant="coarse",
        run_id=record["run_id"], bucket=bucket, declared=animations_for(template),
        frames=frames, s3=s3, doc=True)

    figures = []
    for path in sorted(PACKET._rendered_paths(report)):
        if path.suffix.lower() == ".png":
            PACKET.stamp_png(path, run_id=record["run_id"], template=template,
                             variant="coarse",
                             caption=f"{template} - run {record['run_id']}")
        DOC.stamp_commit(path, run_id=record["run_id"], commit=commit)
        figures.append({"path": str(path.relative_to(REPO)),
                        "bytes": path.stat().st_size})
    errors = [value for key, value in report.items() if key.endswith("_error")]
    errors += list(report.get("animation_errors") or [])
    return {"template": template, "run_id": record["run_id"], "commit": commit,
            "frames": frames, "figures": figures, "errors": errors}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--template", default=None,
                    help="one registered template (default: every one)")
    ap.add_argument("--run", default=None, help="pin this run instead of the newest")
    ap.add_argument("--bucket", default=None)
    ns = ap.parse_args(argv)

    wanted = [ns.template] if ns.template else list(templates())
    if ns.template and ns.template not in templates():
        raise SystemExit(f"{ns.template!r} is not a registered template; the "
                         f"registered ones are {sorted(templates())}")
    reports, failed = [], []
    for name in wanted:
        try:
            report = render_template(name, run_id=ns.run, bucket=ns.bucket)
        except (DocRenderError, Exception) as exc:  # noqa: BLE001 - the reason IS the report
            failed.append({"template": name, "error": f"{type(exc).__name__}: {exc}"})
            continue
        reports.append(report)
        failed += [{"template": name, "error": err} for err in report["errors"]]
    print(json.dumps({"rendered": reports, "failed": failed}, indent=2, default=str))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
