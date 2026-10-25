"""The GeoClaw worker: one staged run directory of .data files in, one solved run directory out.

The launcher bind-mounts the run directory at ``/data`` with ``manifest.json``
and every staged input beside it; this runs the prebuilt ``xgeoclaw`` once -
never compiling, never authoring - and writes ``geoclaw_metrics.json`` back.
No network, no object-store I/O: a refusal is read from GeoClaw's own sentences,
because its plain-Fortran ``stop`` exits with code 0.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Collection

LOG = logging.getLogger("trid3nt.worker.geoclaw")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)

#: Where the launcher bind-mounts the run directory (``docker run -w /data``).
DEFAULT_DATA_DIR = "/data"

#: The run summary the server's exit classifier reads out of the run directory.
METRICS_FILENAME = "geoclaw_metrics.json"

#: Bump on a manifest-contract change. The stamp is named in the strict-gate
#: refusal, so a stale image surfaces as a drifted version rather than as a knob
#: that silently did nothing.
_PARSER_VERSION = "geoclaw-case-1"

#: Wall-clock bound on ONE run. A wedged xgeoclaw holds the run directory
#: forever and the supervisor sees a container that never exits; the bound turns
#: that into a typed metrics write. Sized as a ceiling, not an estimate: measured
#: runs on this box land well under ten minutes, and an fgmax template's stop
#: time belongs to the author that will exist.
_SOLVE_TIMEOUT_ENV = "TRID3NT_GEOCLAW_SOLVE_TIMEOUT"
_SOLVE_TIMEOUT_DEFAULT_S = 86400.0

#: The keys a ``case`` section carries. tfinal is NOT one: it is the staged
#: claw.data's own number, read there, so the manifest cannot restate a time
#: that disagrees with the run.
_CASE_FIELDS = frozenset(("results",))

#: GeoClaw's mass line, printed once at t0 in E-format or fixed Fortran
#: notation - both parse to the same float.
_MASS_RE = re.compile(r"Total mass at initial time:\s*([-0-9.E+]+)")

#: GeoClaw's closing line, printed only when the integrate loop actually
#: completed to its stop time.
_DONE_RE = re.compile(r"Done integrating to time\s+([-0-9.E+]+)")

#: The q frames a run leaves in the directory; gauge tables and fort.chk are
#: other files and are not frames.
_FRAME_RE = re.compile(r"fort\.q\d{4}\Z")


class UnknownManifestFieldError(ValueError):
    """A manifest section carries a key this parser does not read."""

    error_code = "GEOCLAW_MANIFEST_UNKNOWN_FIELD"


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
    LOG.info("geoclaw metrics -> %s", path)
    return path


def _staged_tfinal(claw_data: Path) -> float:
    """The integrate-to time the STAGED claw.data declares - the single source
    the run is checked against. setrun writes ``<number> =: tfinal`` lines; an
    unreadable one refuses the case rather than defaulting a time."""
    if not claw_data.exists():
        raise CaseError(
            "GEOCLAW_CASE_CLAWDATA_MISSING",
            "claw.data is not in the run directory; the staged .data set is "
            "incomplete.")
    text = claw_data.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        if "=:" not in line:
            continue
        value, _, name = line.partition("=:")
        token = name.split("#", 1)[0].strip().split()
        if token and token[0] == "tfinal":
            try:
                return float(value.split()[0])
            except (ValueError, IndexError):
                break
    raise CaseError(
        "GEOCLAW_CASE_TFINAL_UNREAD",
        "claw.data carries no readable '<number> =: tfinal' line; the staged "
        "directory is not a run this worker can check.")


def _staged_restart(claw_data: Path) -> bool:
    """Whether the STAGED claw.data restarts from a checkpoint (``T =: restart``).
    A restart prints no initial-mass line, so the verdict reads it to know which
    run it is judging; an unreadable line is a fresh run."""
    for line in claw_data.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=:" not in line:
            continue
        value, _, name = line.partition("=:")
        token = name.split("#", 1)[0].strip().split()
        if token and token[0] == "restart":
            return value.strip().upper().startswith(("T", ".T"))
    return False


def _parse_stdout(stdout: str) -> dict[str, str | float | None]:
    """The three facts GeoClaw prints itself: its refusal (if any), the mass at
    t0 and the time it reports having integrated to - read off stdout because a
    plain-Fortran ``stop`` writes no listing file and still exits 0."""
    refusal: str | None = None
    mass: float | None = None
    done: float | None = None
    for line in stdout.splitlines():
        stripped = line.strip()
        if refusal is None and (stripped.startswith("ERROR:")
                                or stripped.startswith("STOP")):
            refusal = stripped
        if mass is None:
            hit = _MASS_RE.search(line)
            if hit:
                mass = float(hit.group(1))
        if done is None:
            hit = _DONE_RE.search(line)
            if hit:
                done = float(hit.group(1))
    return {"refusal": refusal, "initial_mass": mass, "final_time": done}


def _verdict(stdout: str, tfinal: float, missing: list[str],
             exit_code: int, restart: bool = False) -> dict[str, Any]:
    """What the run actually did, in one dict: success means GeoClaw's own
    refusal lines are absent, it reported the initial mass (a restart prints none), it reached the
    staged tfinal and every declared result exists - an exit code alone proves
    nothing, because GeoClaw's ``stop`` exits 0 too."""
    parsed = _parse_stdout(stdout)
    mass = parsed["initial_mass"]
    done = parsed["final_time"]
    metrics: dict[str, Any] = {"correct_end": False}
    if mass is not None:
        metrics["initial_mass"] = mass
    if done is not None:
        metrics["final_time"] = done
    refusal = parsed["refusal"]
    if refusal is not None:
        metrics["error_code"] = "GEOCLAW_ENGINE_REFUSED"
        metrics["error"] = refusal
    elif exit_code != 0:
        metrics["error_code"] = "GEOCLAW_SOLVE_FAILED"
        metrics["error"] = f"xgeoclaw exited with code {exit_code}"
    elif mass is None and not restart:
        metrics["error_code"] = "GEOCLAW_MASS_LINE_MISSING"
        metrics["error"] = ("xgeoclaw exited cleanly but stdout never printed "
                            "'Total mass at initial time:'")
    elif done is None:
        metrics["error_code"] = "GEOCLAW_DONE_LINE_MISSING"
        metrics["error"] = ("xgeoclaw exited cleanly but stdout never printed "
                            "'Done integrating to time'")
    elif not math.isclose(done, tfinal, rel_tol=1e-9, abs_tol=1e-9):
        metrics["error_code"] = "GEOCLAW_FINAL_TIME_MISMATCH"
        metrics["error"] = (f"xgeoclaw integrated to {done!r} but claw.data "
                            f"stages tfinal {tfinal!r}")
    elif missing:
        metrics["error_code"] = "GEOCLAW_RESULTS_MISSING"
        metrics["error"] = (f"xgeoclaw exited cleanly but did not write "
                            f"{missing}")
    else:
        metrics["correct_end"] = True
    return metrics


def _frame_count(data_dir: Path) -> int:
    """How many q frames the run left in the directory - GeoClaw's own proof of
    progress, counted from fort.qNNNN names rather than read from a header."""
    return sum(1 for entry in data_dir.iterdir() if _FRAME_RE.fullmatch(entry.name))


def _solve_timeout_s() -> float:
    """The wall-clock bound on one run, as the environment states it."""
    raw = (os.environ.get(_SOLVE_TIMEOUT_ENV) or "").strip()
    try:
        return float(raw) if raw else _SOLVE_TIMEOUT_DEFAULT_S
    except ValueError:
        LOG.warning("%s=%r is not a number; the %.0f s default stands",
                    _SOLVE_TIMEOUT_ENV, raw, _SOLVE_TIMEOUT_DEFAULT_S)
        return _SOLVE_TIMEOUT_DEFAULT_S


def _run_xgeoclaw(data_dir: Path) -> tuple[int, str]:
    """Run the prebuilt xgeoclaw in the run directory; return its exit code and
    everything it said (stdout merged with stderr, so a runtime refusal on
    either stream is read). The child is bounded; an expiry KILLS it, because a
    wedged solve otherwise holds the container open with no report written."""
    bound = _solve_timeout_s()
    child = subprocess.Popen(["xgeoclaw"], cwd=str(data_dir), text=True,
                             bufsize=1, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT)
    lines: list[str] = []
    expired: list[bool] = []
    watchdog = threading.Timer(bound, lambda: (expired.append(True),
                                               child.kill()))
    watchdog.start()
    try:
        for line in child.stdout:
            lines.append(line)
            sys.stdout.write(line)
            sys.stdout.flush()
        child.stdout.close()
        code = child.wait()
    finally:
        watchdog.cancel()
    if expired:
        raise CaseError(
            "GEOCLAW_SOLVE_TIMEOUT",
            f"xgeoclaw was killed after {bound:g} s without finishing; the "
            f"bound is {_SOLVE_TIMEOUT_ENV}.")
    return code, "".join(lines)


def _solve_case(data_dir: Path, body: Any, run_id: str | None) -> dict[str, Any]:
    """Run the staged .data set once and check it against its own record:
    success is refusal-free stdout whose mass line and completed time match the
    staged tfinal, with every declared result on disk."""
    case = _strict_section("case", body, _CASE_FIELDS)
    results = [str(r) for r in (case.get("results") or [])]
    tfinal = _staged_tfinal(data_dir / "claw.data")
    LOG.info("geoclaw case tfinal=%s results=%s", tfinal, results)
    code, stdout = _run_xgeoclaw(data_dir)
    missing = [r for r in results if not (data_dir / r).exists()]
    metrics = _verdict(stdout, tfinal, missing, code,
                       _staged_restart(data_dir / "claw.data"))
    # The raw engine code rides verbatim: the verdict never rests on it alone
    # (a plain-Fortran stop exits 0), so a reader can see both facts together.
    metrics["engine_exit_code"] = code
    metrics["tfinal"] = tfinal
    metrics["frames"] = _frame_count(data_dir)
    return metrics


#: Manifest section -> what runs it. The section a manifest carries IS the
#: routing decision; a manifest that names none of them is a refusal.
_DISPATCH = {"case": _solve_case}


def _build_argv_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="trid3nt-geoclaw-entrypoint",
        description="The TRID3NT GeoClaw worker (.data run dir in, solved run dir out).")
    p.add_argument(
        "--manifest",
        default=os.environ.get("TRID3NT_MANIFEST_PATH", "").strip(),
        help="Path to the worker manifest (default <data-dir>/manifest.json).")
    p.add_argument(
        "--data-dir",
        default=os.environ.get("TRID3NT_GEOCLAW_DATA_DIR",
                               DEFAULT_DATA_DIR).strip(),
        help="Working/output dir (the bind-mounted rundir; default /data).")
    p.add_argument(
        "--run-id", default=os.environ.get("TRID3NT_RUN_ID", "").strip(),
        help="Run identifier (echoed into geoclaw_metrics.json).")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_argv_parser().parse_args(argv)
    data_dir = Path(args.data_dir or DEFAULT_DATA_DIR)
    data_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = (Path(args.manifest) if args.manifest
                     else data_dir / "manifest.json")
    LOG.info("trid3nt-geoclaw worker starting data_dir=%s manifest=%s",
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
        LOG.exception("geoclaw manifest read failed")
        _write_metrics(data_dir, {
            "status": "error", "correct_end": False,
            "error_code": "GEOCLAW_MANIFEST_INVALID",
            "error": f"manifest read failed: {type(exc).__name__}: {exc}"})
        return 2

    run_id = args.run_id or manifest.get("run_id") or None
    started = time.time()
    try:
        metrics = _DISPATCH[section](data_dir, manifest[section], run_id)
    except Exception as exc:  # noqa: BLE001 -- any refusal is a typed metrics error
        error_code = getattr(exc, "error_code", None)
        LOG.exception("geoclaw %s dispatch failed", section)
        _write_metrics(data_dir, {
            "status": "error", "correct_end": False, "run_id": run_id,
            "wall_s": round(time.time() - started, 2),
            **({"error_code": error_code} if error_code else {}),
            "error": f"{type(exc).__name__}: {exc}"})
        return 5 if error_code else 1

    ok = bool(metrics.get("correct_end"))
    payload = {**metrics,
               "status": "ok" if ok else "error",
               "correct_end": ok,
               "run_id": run_id,
               "wall_s": round(time.time() - started, 2)}
    _write_metrics(data_dir, payload)
    LOG.info("trid3nt-geoclaw worker done section=%s status=%s wall_s=%s",
             section, payload["status"], payload["wall_s"])
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
