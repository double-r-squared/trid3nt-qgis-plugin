"""Seed the run journal from what SURVIVED: run prefixes + proof evidence JSONs.

The journal starts the day it is built, so every run before it is missing. Two
sources still hold real records: the object store's run prefixes (``metrics.json``
beside ``completion.json``) and the canary evidence files under
``docs/proof/templates``. Runs whose case data was swept are honestly gone and are
not invented here.

Backfilled lines carry ``"backfilled": true`` and the source they came from, so
nothing downstream mistakes a reconstructed record for one the publish stage
wrote. Idempotent: a run_id already on file is skipped.

Run:
  cd /home/nate/Documents/trid3nt-local
  env $(grep -v "^#" .env.local | xargs) PYTHONPATH=.:contracts \\
    venvs/agent/bin/python scripts/backfill_run_journal.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trid3nt_server.workflows.lib import journal  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("backfill_run_journal")

_EVIDENCE_DIR = Path(__file__).resolve().parents[1] / "docs" / "proof" / "templates"
_NON_RUN_PREFIXES = ("case-manifests/", "case-views/")


def _records_from_evidence() -> list[dict]:
    """One record per canary evidence file that names a run."""
    out: list[dict] = []
    for path in sorted(_EVIDENCE_DIR.rglob("*evidence*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.warning("unreadable evidence file %s", path)
            continue
        if not isinstance(doc, dict) or not doc.get("run_id"):
            continue
        out.append({
            "run_id": doc.get("run_id"),
            "recorded_at": datetime.fromtimestamp(
                path.stat().st_mtime, tz=timezone.utc).isoformat(),
            "template": doc.get("tool"),
            "engine": None,
            # Evidence files are written by the canary harness, so the origin is
            # KNOWN here rather than guessed - which is the whole reason to
            # separate them from user runs downstream.
            "origin": "canary",
            "sheet": [],
            "args": doc.get("args") or {},
            "answer": doc.get("metrics") or {},
            "provenance": [],
            "mesh": {},
            "compute_class": None,
            "wall_seconds": None,
            "executed": [],
            "replayed": [],
            "notes": [],
            "backfilled": True,
            "backfill_source": str(path.relative_to(_EVIDENCE_DIR.parents[2])),
        })
    return out


def _records_from_run_prefixes() -> list[dict]:
    """One record per surviving run prefix that still has a ``metrics.json``."""
    from trid3nt_server.workflows.solver.solver import _get_runs_bucket, _get_s3_client

    s3 = _get_s3_client()
    bucket = _get_runs_bucket()
    prefixes: set[str] = set()
    try:
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
            for obj in page.get("Contents", []) or []:
                key = obj["Key"]
                if key.startswith(_NON_RUN_PREFIXES) or "/" not in key:
                    continue
                prefixes.add(key.split("/", 1)[0])
    except Exception:  # noqa: BLE001 - an unreachable store backfills nothing
        log.warning("the runs bucket %s is unreachable; skipping prefix backfill",
                    bucket, exc_info=True)
        return []

    out: list[dict] = []
    for run_id in sorted(prefixes):
        metrics = _read_json(s3, bucket, f"{run_id}/metrics.json")
        completion = _read_json(s3, bucket, f"{run_id}/completion.json")
        if metrics is None and completion is None:
            continue
        completion = completion or {}
        out.append({
            "run_id": run_id,
            "recorded_at": completion.get("finished_at")
            or datetime.now(timezone.utc).isoformat(),
            "template": None,
            "engine": completion.get("solver"),
            "origin": "unknown",
            "sheet": [],
            "answer": metrics or {},
            "provenance": [],
            "mesh": {},
            "compute_class": None,
            "wall_seconds": None,
            "executed": [],
            "replayed": [],
            "notes": [],
            "backfilled": True,
            "backfill_source": f"s3://{bucket}/{run_id}/",
        })
    return out


def _read_json(s3, bucket: str, key: str) -> dict | None:
    try:
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    except Exception:  # noqa: BLE001 - absent is the common answer, not an error
        return None
    try:
        doc = json.loads(body)
    except json.JSONDecodeError:
        return None
    return doc if isinstance(doc, dict) else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    known = {rec.get("run_id") for rec in journal.read_records()}
    candidates = _records_from_evidence() + _records_from_run_prefixes()
    fresh = [rec for rec in candidates if rec["run_id"] and rec["run_id"] not in known]
    # Evidence files carry the template NAME; a bare run prefix does not. When both
    # sources describe one run the evidence line is the better record, and it is
    # first in the list, so first-wins keeps it.
    seen: set[str] = set()
    deduped = [rec for rec in fresh
               if not (rec["run_id"] in seen or seen.add(rec["run_id"]))]

    log.info("journal has %d records; %d candidates, %d new",
             len(known), len(candidates), len(deduped))
    if args.dry_run:
        for rec in deduped:
            log.info("would append %s (%s) from %s", rec["run_id"],
                     rec.get("template") or rec.get("engine") or "?",
                     rec["backfill_source"])
        return 0
    for rec in deduped:
        journal.append_record(rec)
    log.info("appended %d records to %s", len(deduped), journal.journal_path())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
