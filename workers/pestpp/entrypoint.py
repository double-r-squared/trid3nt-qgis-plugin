"""The PEST++ worker: one manifest in, one finished study directory out.

The launcher bind-mounts the run directory at ``/data`` with ``manifest.json``,
the control file, its template and instruction files and the model files beside
it; this copies that directory once per agent into ``/work``, runs one PEST++
manager in ``/data`` and the agents on localhost, and writes
``pestpp_metrics.json`` back. No network, no object-store I/O, no authoring.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Collection

LOG = logging.getLogger("trid3nt.worker.pestpp")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)

#: Where the launcher bind-mounts the run directory (``docker run -w /data``).
DEFAULT_DATA_DIR = "/data"

#: The container's own disk the agent copies live on; nothing here is uploaded.
DEFAULT_WORK_DIR = "/work"

#: The run summary the server's exit classifier reads out of the run directory.
METRICS_FILENAME = "pestpp_metrics.json"

#: The control file every staged study is named by. PEST++ names everything it
#: writes after it, so its record is ``pestpp.rec``.
STEERING_FILENAME = "pestpp.pst"
RECORD_FILENAME = "pestpp.rec"

#: How much of the record rides into the metrics when a run fails.
_RECORD_TAIL_CHARS = 4000

#: The manager's port on the container's loopback; nothing outside reaches it.
_PORT = 4004

#: How long agents get to leave once asked, after their manager has, before they are killed.
_AGENT_EXIT_S = 5.0

#: PEST++'s own closing sentence in the record, ``pestpp-<tool> analysis complete...``.
_COMPLETE = "analysis complete"

#: Bump on a manifest-contract change; named in the strict-gate refusal.
_PARSER_VERSION = "pestpp-case-1"

#: The PEST++ tools the release ships, each the executable ``pestpp-<tool>``.
_TOOLS = frozenset(("glm", "ies", "sen", "swp", "opt", "mou", "da", "sqp"))

#: The keys a ``case`` section carries.
_CASE_FIELDS = frozenset(("module", "steering", "results", "server_facts", "cores"))

#: Files of the run directory an agent copy never carries: the launcher's own
#: logs (still being written) and the run's records.
_NOT_COPIED = ("*.stdout", "*.stderr", "manifest.json", "code_provenance.json",
               METRICS_FILENAME)


class UnknownManifestFieldError(ValueError):
    """A manifest section carries a key this parser does not read."""

    error_code = "PESTPP_MANIFEST_UNKNOWN_FIELD"


class CaseError(RuntimeError):
    """The case names a run this worker cannot start."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


def _strict_section(section: str, body: Any, valid: Collection[str]) -> dict[str, Any]:
    """The manifest's only gate: every key is known, or the run refuses. A
    dropped key silently no-ops the knob the caller meant to set."""
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
    LOG.info("pestpp metrics -> %s", path)
    return path


def _record_tail(data_dir: Path) -> dict[str, str]:
    """The end of the manager's own record, which is where PEST++ says why."""
    try:
        text = (data_dir / RECORD_FILENAME).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    return {"listing_tail": text[-_RECORD_TAIL_CHARS:]} if text else {}


def _completed(data_dir: Path) -> bool:
    """Did the manager's record close with PEST++'s own completion sentence?"""
    try:
        text = (data_dir / RECORD_FILENAME).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return _COMPLETE in text.lower()


def _stream(child: subprocess.Popen) -> int:
    for line in child.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
    child.stdout.close()
    return child.wait()


def _agent_homes(data_dir: Path, work_dir: Path, count: int) -> list[Path]:
    """One copy of the staged directory per agent, taken before the manager writes
    anything into it."""
    homes = []
    for index in range(count):
        home = work_dir / f"agent_{index}"
        shutil.rmtree(home, ignore_errors=True)
        shutil.copytree(data_dir, home, ignore=shutil.ignore_patterns(*_NOT_COPIED))
        homes.append(home)
    return homes


def _start_agents(homes: list[Path], exe: str) -> list[subprocess.Popen]:
    """Each agent started in its own copy against the manager's port; an agent's
    own output stays in its copy."""
    agents: list[subprocess.Popen] = []
    for home in homes:
        out = (home / "agent.stdout").open("w", encoding="utf-8")
        agents.append(subprocess.Popen(
            [exe, STEERING_FILENAME, "/h", f"localhost:{_PORT}"], cwd=str(home),
            stdout=out, stderr=subprocess.STDOUT, text=True))
        out.close()
    return agents


def _stop_agents(agents: list[subprocess.Popen]) -> None:
    """An agent outlives its manager by seconds of reconnect attempts; the study is over, so each is asked to leave."""
    for agent in agents:
        if agent.poll() is None:
            agent.terminate()
    deadline = time.time() + _AGENT_EXIT_S
    for agent in agents:
        try:
            agent.wait(timeout=max(0.0, deadline - time.time()))
        except subprocess.TimeoutExpired:
            agent.kill()
            agent.wait()


def _solve_case(data_dir: Path, work_dir: Path, body: Any) -> dict[str, Any]:
    """Run the staged study, and check it produced what it promised: success is
    a clean manager exit AND PEST++'s own completion sentence in its record AND
    every declared result on disk."""
    case = _strict_section("case", body, _CASE_FIELDS)
    module = str(case.get("module") or "")
    if module not in _TOOLS:
        raise CaseError(
            "PESTPP_CASE_TOOL_UNKNOWN",
            f"case.module {module!r} is not one of {sorted(_TOOLS)}.")
    steering = str(case.get("steering") or STEERING_FILENAME)
    if steering != STEERING_FILENAME:
        raise CaseError(
            "PESTPP_CASE_STEERING_UNREAD",
            f"case.steering {steering!r} is not {STEERING_FILENAME!r}, the control "
            "file every staged study is named by.")
    if not (data_dir / STEERING_FILENAME).exists():
        raise CaseError(
            "PESTPP_CASE_CONTROL_MISSING",
            f"{STEERING_FILENAME} is not in the run directory; the study the run "
            "is supposed to carry out was never staged.")
    agents_n = max(1, int(case.get("cores") or 1))
    results = [str(r) for r in (case.get("results") or [])]
    exe = f"pestpp-{module}"

    LOG.info("pestpp case tool=%s agents=%d results=%s", module, agents_n, results)
    homes = _agent_homes(data_dir, work_dir, agents_n)
    manager = subprocess.Popen(
        [exe, STEERING_FILENAME, "/h", f":{_PORT}"], cwd=str(data_dir),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    agents = _start_agents(homes, exe)
    try:
        code = _stream(manager)
    finally:
        _stop_agents(agents)
    missing = [r for r in results if not (data_dir / r).exists()]
    completed = _completed(data_dir)
    # server_facts is copied into the metrics VERBATIM - a fact re-derived in
    # the container is a second answer that can disagree with the first.
    metrics: dict[str, Any] = {
        **dict(case.get("server_facts") or {}),
        "module": module,
        "agents": agents_n,
        "completed": completed,
        "correct_end": code == 0 and completed and not missing,
    }
    if code != 0:
        metrics["error_code"] = "PESTPP_FAILED"
        metrics["error"] = f"{exe} exited with code {code}"
    elif not completed:
        metrics["error_code"] = "PESTPP_INCOMPLETE"
        metrics["error"] = (f"{exe} exited cleanly but its {RECORD_FILENAME} never "
                            f"printed {_COMPLETE!r}")
    elif missing:
        metrics["error_code"] = "PESTPP_RESULTS_MISSING"
        metrics["error"] = f"{exe} exited cleanly but did not write {missing}"
    return metrics


#: Manifest section -> what runs it; a manifest that names none is a refusal.
_DISPATCH = {"case": _solve_case}


def _build_argv_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="trid3nt-pestpp-entrypoint",
        description="The TRID3NT PEST++ worker (manifest in, run dir out).")
    p.add_argument(
        "--manifest",
        default=os.environ.get("TRID3NT_MANIFEST_PATH", "").strip(),
        help="Path to the worker manifest (default <data-dir>/manifest.json).")
    p.add_argument(
        "--data-dir",
        default=os.environ.get("TRID3NT_PESTPP_DATA_DIR", DEFAULT_DATA_DIR).strip(),
        help="Working/output dir (the bind-mounted rundir; default /data).")
    p.add_argument(
        "--work-dir", default=DEFAULT_WORK_DIR,
        help="Where the agent copies live (default /work).")
    p.add_argument(
        "--run-id", default=os.environ.get("TRID3NT_RUN_ID", "").strip(),
        help="Run identifier (echoed into pestpp_metrics.json).")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_argv_parser().parse_args(argv)
    data_dir = Path(args.data_dir or DEFAULT_DATA_DIR)
    work_dir = Path(args.work_dir or DEFAULT_WORK_DIR)
    work_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = (Path(args.manifest) if args.manifest
                     else data_dir / "manifest.json")
    LOG.info("trid3nt-pestpp worker starting data_dir=%s manifest=%s",
             data_dir, manifest_path)

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("manifest must be a JSON object")
        section = next((s for s in _DISPATCH if manifest.get(s) is not None), None)
        if section is None:
            raise ValueError("manifest names no runnable section (one of "
                             f"{sorted(_DISPATCH)})")
    except Exception as exc:  # noqa: BLE001 -- a bad manifest is a typed metrics error
        LOG.exception("pestpp manifest read failed")
        _write_metrics(data_dir, {
            "status": "error", "correct_end": False,
            "error_code": "PESTPP_MANIFEST_INVALID",
            "error": f"manifest read failed: {type(exc).__name__}: {exc}"})
        return 2

    run_id = args.run_id or manifest.get("run_id") or None
    started = time.time()
    try:
        metrics = _DISPATCH[section](data_dir, work_dir, manifest[section])
    except Exception as exc:  # noqa: BLE001 -- any refusal is a typed metrics error
        error_code = getattr(exc, "error_code", None)
        LOG.exception("pestpp %s dispatch failed", section)
        _write_metrics(data_dir, {
            "status": "error", "correct_end": False, "run_id": run_id,
            "wall_s": round(time.time() - started, 2),
            **({"error_code": error_code} if error_code else {}),
            "error": f"{type(exc).__name__}: {exc}",
            **_record_tail(data_dir)})
        return 5 if error_code else 1

    ok = bool(metrics.get("correct_end"))
    payload = {**metrics,
               "status": "ok" if ok else "error",
               "correct_end": ok,
               "run_id": run_id,
               "wall_s": round(time.time() - started, 2)}
    if not ok:
        payload.update(_record_tail(data_dir))
    _write_metrics(data_dir, payload)
    LOG.info("trid3nt-pestpp worker done section=%s status=%s wall_s=%s",
             section, payload["status"], payload["wall_s"])
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
