"""The TELEMAC worker: one manifest in, one solved run directory out.

The launcher bind-mounts the run directory at ``/data`` with ``manifest.json``
and every staged input beside it. This entrypoint reads the manifest, runs ONE
dispatch, and writes ``telemac_metrics.json`` back into the same directory. The
container does no network and no object-store I/O: everything it reads arrives
staged, and the supervisor uploads everything it wrote.

A manifest names exactly one runnable section:

  * ``case`` - the authored run. The deck is a record and the server wrote it, so
    what reaches the worker is which engine, which steering file, and which
    result files must exist for the run to have happened.
  * ``agitation`` / ``stratified`` - the two builders that still author their own
    domain in-container. They run behind this dispatch unchanged and are never
    extended; each is superseded by a ``case`` as it is rebuilt server-side.

A ``case`` solves in a CHILD process. A Fortran STOP kills the process it runs
in, and the metrics file is the only channel the server has for reading what went
wrong, so the write has to outlive the solve.

Success is a clean child exit AND every declared result file on disk: a solver
that returns zero without writing its result has not solved anything.
"""

from __future__ import annotations

import argparse
import dataclasses
import importlib
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Collection

LOG = logging.getLogger("trid3nt.worker.telemac")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)

#: Where the launcher bind-mounts the run directory (``docker run -w /data``).
DEFAULT_DATA_DIR = "/data"

#: The run summary the server's exit classifier reads out of the run directory.
METRICS_FILENAME = "telemac_metrics.json"

#: The solver listing, teed off the child's stdout. Diagnostics parse it.
LISTING_FILENAME = "full_listing.log"

#: How much of the listing rides into the metrics when a run fails - enough for
#: the Fortran error block, not so much that the summary becomes the listing.
_LISTING_TAIL_CHARS = 4000

#: Bump on a manifest-contract change. The stamp is named in the strict-gate
#: refusal, so a stale image surfaces as a drifted version rather than as a knob
#: that silently did nothing.
_PARSER_VERSION = "telemac-unified-1"

#: The telapy class per engine a case may name, imported in the CHILD only:
#: telapy exists inside this image and nowhere else.
_MODULES: dict[str, tuple[str, str]] = {
    "telemac2d": ("telapy.api.t2d", "Telemac2d"),
    "telemac3d": ("telapy.api.t3d", "Telemac3d"),
    "tomawac": ("telapy.api.wac", "Tomawac"),
    "artemis": ("telapy.api.art", "Artemis"),
}

#: The keys a ``case`` section carries.
_CASE_FIELDS = frozenset((
    "module", "steering", "user_fortran", "results", "family", "echo"))


class UnknownManifestFieldError(ValueError):
    """A manifest section carries a key this parser does not read."""

    error_code = "TELEMAC_MANIFEST_UNKNOWN_FIELD"


class CaseError(RuntimeError):
    """The case names a run this worker cannot start."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


def _strict_section(section: str, body: Any, valid: Collection[str], *,
                    drop: tuple[str, ...] = ()) -> dict[str, Any]:
    """The manifest's only gate: every key is known, or the run refuses.

    A dropped key silently no-ops the knob the caller meant to set, which reads
    afterwards as a solve that ignored its input rather than as the typo or the
    stale image it was.
    """
    clean: dict[str, Any] = {}
    unknown: list[str] = []
    for key, value in dict(body or {}).items():
        if key in drop:
            continue
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


def _config_fields(cls: Any) -> frozenset[str]:
    return frozenset(f.name for f in dataclasses.fields(cls))


def _write_metrics(data_dir: Path, payload: dict[str, Any]) -> Path:
    path = data_dir / METRICS_FILENAME
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    LOG.info("telemac metrics -> %s", path)
    return path


def _listing_tail(data_dir: Path) -> dict[str, str]:
    """The end of the listing, which is where a Fortran STOP says why."""
    try:
        text = (data_dir / LISTING_FILENAME).read_text(
            encoding="utf-8", errors="replace")
    except OSError:
        return {}
    return {"listing_tail": text[-_LISTING_TAIL_CHARS:]} if text else {}


def _solve_in_process(module: str, steering: str,
                      user_fortran: str | None) -> int:
    """Drive ONE telapy study to the end of its time loop, in THIS process.

    The listing is Fortran unit 6, so it arrives on this process's stdout and the
    parent tees it to the listing file.
    """
    path, name = _MODULES[module]
    study = getattr(importlib.import_module(path), name)(
        steering, user_fortran=user_fortran)
    study.set_case()
    study.init_state_default()
    study.run_all_time_steps()
    study.finalize()
    return 0


def _run_child(data_dir: Path, module: str, steering: str,
               user_fortran: str | None) -> int:
    """Solve in a child process, teeing its listing; return the child's code.

    The tee is two writes rather than a redirect because both readers are real:
    the listing file is the run's evidence, and the container's own stdout is
    where a watching human sees the time loop advance.
    """
    argv = [sys.executable, os.path.abspath(__file__),
            "--solve", module, "--steering", steering]
    if user_fortran:
        argv += ["--user-fortran", str(user_fortran)]
    with (data_dir / LISTING_FILENAME).open("w", encoding="utf-8") as listing:
        child = subprocess.Popen(argv, cwd=str(data_dir), text=True, bufsize=1,
                                 stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT)
        for line in child.stdout:
            listing.write(line)
            sys.stdout.write(line)
            sys.stdout.flush()
        child.stdout.close()
        return child.wait()


def _solve_case(data_dir: Path, body: Any, run_id: str | None) -> dict[str, Any]:
    """Run the deck the server authored, and check it produced what it promised.

    ``echo`` is copied into the metrics VERBATIM: the utm zone, the extent, the
    node and element counts are facts the server measured, and a fact re-derived
    in the container is a second answer that can disagree with the first.
    """
    case = _strict_section("case", body, _CASE_FIELDS)
    module = str(case.get("module") or "")
    if module not in _MODULES:
        raise CaseError(
            "TELEMAC_CASE_MODULE_UNKNOWN",
            f"case.module {module!r} is not one of {sorted(_MODULES)}.")
    steering = str(case.get("steering") or "")
    if not (data_dir / steering).exists():
        raise CaseError(
            "TELEMAC_CASE_STEERING_MISSING",
            f"case.steering {steering!r} is not in the run directory; the deck "
            "the run is supposed to be was never staged.")
    results = [str(r) for r in (case.get("results") or [])]
    LOG.info("telemac case module=%s steering=%s results=%s",
             module, steering, results)

    code = _run_child(data_dir, module, steering, case.get("user_fortran"))
    missing = [r for r in results if not (data_dir / r).exists()]
    metrics: dict[str, Any] = {
        **dict(case.get("echo") or {}),
        "module": module,
        "family": str(case.get("family") or module),
        "correct_end": code == 0 and not missing,
    }
    if code != 0:
        metrics["error_code"] = "TELEMAC_SOLVE_FAILED"
        metrics["error"] = f"{module} exited with code {code}"
    elif missing:
        metrics["error_code"] = "TELEMAC_RESULTS_MISSING"
        metrics["error"] = (f"{module} exited cleanly but did not write "
                            f"{missing}")
    return metrics


def _solve_agitation(data_dir: Path, body: Any,
                     run_id: str | None) -> dict[str, Any]:
    """The ARTEMIS harbour-agitation builder, behind the one dispatch."""
    import artemis_build as A  # noqa: WPS433 -- worker payload

    clean = _strict_section("agitation", body, _config_fields(A.ArtemisConfig),
                            drop=("workdir", "mode"))
    for key in ("bbox", "breakwater"):
        if clean.get(key) is not None:
            clean[key] = tuple(float(v) for v in clean[key])
    clean["workdir"] = str(data_dir)
    return {"module": "artemis", "family": "agitation",
            **A.solve(A.ArtemisConfig(**clean), str(data_dir), run_id=run_id)}


def _solve_stratified(data_dir: Path, body: Any,
                      run_id: str | None) -> dict[str, Any]:
    """The TELEMAC-3D stratified builder, behind the one dispatch."""
    import telemac3d_build as T  # noqa: WPS433 -- worker payload

    clean = _strict_section("stratified", body,
                            _config_fields(T.Telemac3dConfig),
                            drop=("workdir", "mode"))
    if clean.get("bbox") is not None:
        clean["bbox"] = tuple(float(v) for v in clean["bbox"])
    clean["workdir"] = str(data_dir)
    return {"module": "telemac3d", "family": "stratified",
            **T.solve(T.Telemac3dConfig(**clean), str(data_dir),
                      run_id=run_id)}


#: Manifest section -> what runs it. The section a manifest carries IS the
#: routing decision; a manifest that names none of them is a refusal.
_DISPATCH = {
    "case": _solve_case,
    "agitation": _solve_agitation,
    "stratified": _solve_stratified,
}


def _build_argv_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="trid3nt-telemac-entrypoint",
        description="The TRID3NT TELEMAC worker (manifest in, run dir out).")
    p.add_argument(
        "--manifest",
        default=os.environ.get("TRID3NT_MANIFEST_PATH", "").strip(),
        help="Path to the worker manifest (default <data-dir>/manifest.json).")
    p.add_argument(
        "--data-dir",
        default=os.environ.get("TRID3NT_TELEMAC_DATA_DIR",
                               DEFAULT_DATA_DIR).strip(),
        help="Working/output dir (the bind-mounted rundir; default /data).")
    p.add_argument(
        "--run-id", default=os.environ.get("TRID3NT_RUN_ID", "").strip(),
        help="Run identifier (echoed into telemac_metrics.json).")
    p.add_argument(
        "--solve", choices=sorted(_MODULES),
        help="CHILD mode: drive this telapy module and exit.")
    p.add_argument("--steering", help="CHILD mode: the steering file to run.")
    p.add_argument("--user-fortran",
                   help="CHILD mode: the user Fortran to compile in.")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_argv_parser().parse_args(argv)
    if args.solve:
        return _solve_in_process(args.solve, str(args.steering or ""),
                                 args.user_fortran)

    data_dir = Path(args.data_dir or DEFAULT_DATA_DIR)
    data_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = (Path(args.manifest) if args.manifest
                     else data_dir / "manifest.json")
    LOG.info("trid3nt-telemac worker starting data_dir=%s manifest=%s",
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
        LOG.exception("telemac manifest read failed")
        _write_metrics(data_dir, {
            "status": "error", "correct_end": False,
            "error_code": "TELEMAC_MANIFEST_INVALID",
            "error": f"manifest read failed: {type(exc).__name__}: {exc}"})
        return 2

    run_id = args.run_id or manifest.get("run_id") or None
    started = time.time()
    try:
        metrics = _DISPATCH[section](data_dir, manifest[section], run_id)
    except Exception as exc:  # noqa: BLE001 -- any refusal is a typed metrics error
        error_code = getattr(exc, "error_code", None)
        LOG.exception("telemac %s dispatch failed", section)
        _write_metrics(data_dir, {
            "status": "error", "correct_end": False, "run_id": run_id,
            "family": section, "wall_s": round(time.time() - started, 2),
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
    LOG.info("trid3nt-telemac worker done family=%s status=%s wall_s=%s",
             payload.get("family"), payload["status"], payload["wall_s"])
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
