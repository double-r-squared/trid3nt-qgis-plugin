"""The MODFLOW worker: one manifest in, one solved run directory out.

The launcher bind-mounts the run directory at ``/data`` with ``manifest.json``
and every input staged beside it; this reads the manifest, runs ``mf6`` once -
optionally preceded by the engine's own ``--mode validate`` - and writes
``modflow_metrics.json`` back. No network, no object-store I/O, no authoring.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Collection

LOG = logging.getLogger("trid3nt.worker.modflow")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)

#: Where the launcher bind-mounts the run directory (``docker run -w /data``).
DEFAULT_DATA_DIR = "/data"

#: The run summary the server's exit classifier reads out of the run directory.
METRICS_FILENAME = "modflow_metrics.json"

#: The simulation listing mf6 itself writes; the listing tail rides into a
#: failed run's metrics, where the engine's own STOP says why.
SIM_LISTING_FILENAME = "mfsim.lst"

#: How much of the listing rides into the metrics when a run fails - enough for
#: the error block, not so much that the summary becomes the listing.
_LISTING_TAIL_CHARS = 4000

#: Bump on a manifest-contract change. The stamp is named in the strict-gate
#: refusal, so a stale image surfaces as a drifted version rather than as a knob
#: that silently did nothing.
_PARSER_VERSION = "modflow-case-1"

#: Wall-clock bound on ONE run. A wedged mf6 holds the run directory forever
#: and the supervisor sees a container that never exits; the bound turns that
#: into a typed metrics write. Sized as a ceiling, not an estimate: the first
#: template's DISV solves in well under a second, and TDIS-based sizing belongs
#: to the author that will exist.
_SOLVE_TIMEOUT_ENV = "TRID3NT_MODFLOW_SOLVE_TIMEOUT"
_SOLVE_TIMEOUT_DEFAULT_S = 86400.0

#: The modules a case may name; the simulation itself is whatever mfsim.nam
#: carries, and the word is provenance the metrics state.
_MODULES = frozenset(("gwf", "gwt", "gwe"))

#: The keys a ``case`` section carries.
_CASE_FIELDS = frozenset((
    "module", "mode", "results", "server_facts"))

#: mf6's own closing sentence, case-insensitive, in the simulation listing.
_NORMAL_TERMINATION = "normal termination"


class UnknownManifestFieldError(ValueError):
    """A manifest section carries a key this parser does not read."""

    error_code = "MODFLOW_MANIFEST_UNKNOWN_FIELD"


class CaseError(RuntimeError):
    """The case names a run this worker cannot start."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


def _strict_section(section: str, body: Any, valid: Collection[str]) -> dict[str, Any]:
    """The manifest's only gate: every key is known, or the run refuses. A
    dropped key silently no-ops the knob the caller meant to set, and reads
    afterwards as a solve that ignored its input."""
    clean: dict[str, Any] = {}
    unknown: list[str] = []
    for key, value in dict(body or {}).items():
        if key in valid:
            clean[key] = value
        else:
            unknown.append(key)
    if unknown:
        raise UnknownManifestFieldError(
            f"manifest.json[{section!r}] carries unknown field(s) "
            f"{sorted(unknown)} that parser {_PARSER_VERSION} does not read. "
            f"Either the caller has a typo or this worker image is stale. "
            f"Known fields: {sorted(valid)}.")
    return clean


def _write_metrics(data_dir: Path, payload: dict[str, Any]) -> Path:
    path = data_dir / METRICS_FILENAME
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    LOG.info("modflow metrics -> %s", path)
    return path


def _listing_tail(data_dir: Path) -> dict[str, str]:
    """The end of mf6's own simulation listing, which is where it says why."""
    try:
        text = (data_dir / SIM_LISTING_FILENAME).read_text(
            encoding="utf-8", errors="replace")
    except OSError:
        return {}
    return {"listing_tail": text[-_LISTING_TAIL_CHARS:]} if text else {}


def _reached_normal_termination(data_dir: Path) -> bool:
    """Did mf6's own listing close with its normal-termination sentence?"""
    try:
        text = (data_dir / SIM_LISTING_FILENAME).read_text(
            encoding="utf-8", errors="replace").lower()
    except OSError:
        return False
    return _NORMAL_TERMINATION in text


def _solve_timeout_s() -> float:
    """The wall-clock bound on one run, as the environment states it."""
    raw = (os.environ.get(_SOLVE_TIMEOUT_ENV) or "").strip()
    try:
        return float(raw) if raw else _SOLVE_TIMEOUT_DEFAULT_S
    except ValueError:
        LOG.warning("%s=%r is not a number; the %.0f s default stands",
                    _SOLVE_TIMEOUT_ENV, raw, _SOLVE_TIMEOUT_DEFAULT_S)
        return _SOLVE_TIMEOUT_DEFAULT_S


def _run_mf6(data_dir: Path, mode: str) -> int:
    """Run mf6 in the run directory; return its exit code.

    ``mf6`` takes no name-file argument: it reads ``mfsim.nam`` from its cwd,
    and ``--mode validate`` is the same invocation one flag longer. The child
    is bounded; an expiry KILLS it, because a wedged solve otherwise holds the
    container open with no report written."""
    argv = ["mf6"] + (["--mode", "validate"] if mode == "validate" else [])
    bound = _solve_timeout_s()
    child = subprocess.Popen(argv, cwd=str(data_dir), text=True, bufsize=1,
                             stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT)
    expired: list[bool] = []
    watchdog = threading.Timer(bound, lambda: (expired.append(True),
                                               child.kill()))
    watchdog.start()
    try:
        for line in child.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
        child.stdout.close()
        code = child.wait()
    finally:
        watchdog.cancel()
    if expired:
        raise CaseError(
            "MODFLOW_SOLVE_TIMEOUT",
            f"mf6 was killed after {bound:g} s without finishing; the bound is "
            f"{_SOLVE_TIMEOUT_ENV} and {SIM_LISTING_FILENAME} carries how far "
            "it got.")
    return code


def _solve_case(data_dir: Path, body: Any, run_id: str | None) -> dict[str, Any]:
    """Run what the server authored, and check it produced what it promised:
    success is a clean exit AND mf6's own normal termination AND every declared
    result file on disk."""
    case = _strict_section("case", body, _CASE_FIELDS)
    module = str(case.get("module") or "")
    if module not in _MODULES:
        raise CaseError(
            "MODFLOW_CASE_MODULE_UNKNOWN",
            f"case.module {module!r} is not one of {sorted(_MODULES)}.")
    mode = str(case.get("mode") or "solve")
    if mode not in ("validate", "solve"):
        raise CaseError(
            "MODFLOW_CASE_MODE_UNKNOWN",
            f"case.mode {mode!r} is not 'validate' or 'solve'.")
    if not (data_dir / "mfsim.nam").exists():
        raise CaseError(
            "MODFLOW_CASE_SIMULATION_MISSING",
            "mfsim.nam is not in the run directory; the simulation the run is "
            "supposed to solve was never staged.")
    results = [str(r) for r in (case.get("results") or [])]

    LOG.info("modflow case module=%s mode=%s results=%s", module, mode, results)
    code = _run_mf6(data_dir, mode)
    terminated = _reached_normal_termination(data_dir)
    missing = [r for r in results if not (data_dir / r).exists()]
    # server_facts is copied into the metrics VERBATIM - a fact re-derived in
    # the container is a second answer that can disagree with the first.
    server_facts = dict(case.get("server_facts") or {})
    metrics: dict[str, Any] = {
        **server_facts,
        "module": module,
        "mode": mode,
        "normal_termination": terminated,
        "correct_end": code == 0 and terminated and not missing,
    }
    if code != 0:
        metrics["error_code"] = "MODFLOW_SOLVE_FAILED"
        metrics["error"] = f"mf6 exited with code {code}"
    elif not terminated:
        metrics["error_code"] = "MODFLOW_NO_NORMAL_TERMINATION"
        metrics["error"] = ("mf6 exited cleanly but its listing never printed "
                            f"{_NORMAL_TERMINATION!r}")
    elif missing:
        metrics["error_code"] = "MODFLOW_RESULTS_MISSING"
        metrics["error"] = (f"mf6 exited cleanly but did not write {missing}")
    return metrics


#: Manifest section -> what runs it. The section a manifest carries IS the
#: routing decision; a manifest that names none of them is a refusal.
_DISPATCH = {"case": _solve_case}


def _build_argv_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="trid3nt-modflow-entrypoint",
        description="The TRID3NT MODFLOW worker (manifest in, run dir out).")
    p.add_argument(
        "--manifest",
        default=os.environ.get("TRID3NT_MANIFEST_PATH", "").strip(),
        help="Path to the worker manifest (default <data-dir>/manifest.json).")
    p.add_argument(
        "--data-dir",
        default=os.environ.get("TRID3NT_MODFLOW_DATA_DIR",
                               DEFAULT_DATA_DIR).strip(),
        help="Working/output dir (the bind-mounted rundir; default /data).")
    p.add_argument(
        "--run-id", default=os.environ.get("TRID3NT_RUN_ID", "").strip(),
        help="Run identifier (echoed into modflow_metrics.json).")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_argv_parser().parse_args(argv)
    data_dir = Path(args.data_dir or DEFAULT_DATA_DIR)
    data_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = (Path(args.manifest) if args.manifest
                     else data_dir / "manifest.json")
    LOG.info("trid3nt-modflow worker starting data_dir=%s manifest=%s",
             data_dir, manifest_path)

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("manifest must be a JSON object")
        section = next((s for s in _DISPATCH if manifest.get(s) is not None),
                       None)
        if section is None:
            raise ValueError("manifest names no runnable section (one of "
                             f"{sorted(_DISPATCH)})")
    except Exception as exc:  # noqa: BLE001 -- a bad manifest is a typed metrics error
        LOG.exception("modflow manifest read failed")
        _write_metrics(data_dir, {
            "status": "error", "correct_end": False,
            "error_code": "MODFLOW_MANIFEST_INVALID",
            "error": f"manifest read failed: {type(exc).__name__}: {exc}"})
        return 2

    run_id = args.run_id or manifest.get("run_id") or None
    started = time.time()
    try:
        metrics = _DISPATCH[section](data_dir, manifest[section], run_id)
    except Exception as exc:  # noqa: BLE001 -- any refusal is a typed metrics error
        error_code = getattr(exc, "error_code", None)
        LOG.exception("modflow %s dispatch failed", section)
        _write_metrics(data_dir, {
            "status": "error", "correct_end": False, "run_id": run_id,
            "wall_s": round(time.time() - started, 2),
            **({"error_code": error_code} if error_code else {}),
            "error": f"{type(exc).__name__}: {exc}",
            **_listing_tail(data_dir)})
        return 5 if error_code else 1

    ok = bool(metrics.get("correct_end"))
    payload = {**metrics,
               "status": "ok" if ok else "error",
               "correct_end": ok,
               "run_id": run_id,
               "wall_s": round(time.time() - started, 2)}
    if not ok:
        payload.update(_listing_tail(data_dir))
    _write_metrics(data_dir, payload)
    LOG.info("trid3nt-modflow worker done section=%s status=%s wall_s=%s",
             section, payload["status"], payload["wall_s"])
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
