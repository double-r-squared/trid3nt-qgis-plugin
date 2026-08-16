"""SCHISM worker entrypoint -- volume-mount envelope (TELEMAC/mesh-worker canon).

The caller bind-mounts a rundir at /data holding a ready SCHISM case (hgrid.gr3,
vgrid.in, param.nml, bctides.in, sflux/, ...) plus a manifest.json selecting the
executable variant and the MPI rank layout. This runs the solver under mpirun,
gates on SCHISM's own "Run completed successfully" sentinel (SCHISM exits 0 even
on a solve abort -- the HEC-RAS lesson: never trust the exit code), and writes
schism_metrics.json back into /data with the output inventory. NO object-store
I/O here (a supervisor uploads /data); netCDF -> COG postprocess is the landing.

manifest.json::

    {
      "variant": "hydro" | "full" | "wwm" | "icm" | "sed",
                                          # baked executable (default hydro):
                                          #   hydro = pschism_TVD-VL (bare core)
                                          #   full  = the full-monty WWM_COSINE_...
                                          #   wwm   = pschism_WWM_GOTM_TVD-VL
                                          #           (targeted WWM+GOTM coupled waves)
                                          #   icm   = pschism_ICM_TVD-VL
                                          #           (targeted USE_ICM water quality)
                                          #   sed   = pschism_SED_TVD-VL
                                          #           (targeted USE_SED sediment transport)
      "ncompute": 3,                     # compute ranks (default 3)
      "nscribe": 2,                      # scribe I/O ranks (default 2; >= # out vars)
      "timeout_s": 3600,
      "run_id": "<ulid>"
    }
"""

from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
import time
from pathlib import Path

BIN_DIR = Path(os.environ.get("SCHISM_BIN_DIR", "/opt/schism/bin"))
DATA = Path(os.environ.get("SCHISM_DATA_DIR", "/data"))

#: PARSER VERSION -- bump on a manifest.json shape change. Named in the
#: strict-field error (ADR 0158). v2 (ADR 0189): accept-and-ignore the generic
#: run_solver-seam envelope fields (inputs/outputs/schism_args) the local-docker
#: launcher writes verbatim into rundir/manifest.json.
_PARSER_VERSION = "schism-manifest-2"

#: Every top-level manifest.json key this entrypoint reads (see the module
#: docstring schema). An unknown key would otherwise silently keep its default
#: (e.g. a typo'd rank/variant knob solving with the WRONG config, never
#: erroring) -- the ADR 0148 lesson.
_KNOWN_MANIFEST_FIELDS = frozenset(
    {"variant", "ncompute", "nscribe", "timeout_s", "run_id"}
)

#: Generic run_solver-seam envelope fields (ADR 0189). The local-docker launcher
#: reads ``inputs`` to stage the deck + ``outputs`` to collect results, then writes
#: the WHOLE manifest verbatim into rundir/manifest.json -- so the entrypoint SEES
#: these keys but does not act on them. Accept-and-ignore (they are the seam's
#: contract, not a typo) rather than reject as unknown. Mirrors the HEC-RAS
#: ``hecras-manifest`` allowlist fix (ADR 0188).
_SEAM_ENVELOPE_FIELDS = frozenset({"inputs", "outputs", "schism_args"})


class SchismManifestUnknownFieldsError(ValueError):
    """manifest.json carries a top-level key this entrypoint does not read."""


def _reject_unknown_manifest_fields(manifest: dict) -> None:
    unknown = sorted(set(manifest) - _KNOWN_MANIFEST_FIELDS - _SEAM_ENVELOPE_FIELDS)
    if unknown:
        raise SchismManifestUnknownFieldsError(
            f"manifest.json carries unknown field(s) {unknown} that parser "
            f"{_PARSER_VERSION} does not read -- this SILENTLY keeps the "
            f"default for the intended knob rather than applying it. Either "
            f"the caller has a typo, or the worker image is stale (rebuild it "
            f"-- ADR 0148). Known fields: {sorted(_KNOWN_MANIFEST_FIELDS)}."
        )


def _resolve_exe(variant: str) -> Path:
    # NOTE (ADR 0126): the "full" glob pschism_WWM_* sorts the COSINE full-monty
    # binary FIRST, so the targeted WWM+GOTM coupled-wave binary needs its OWN,
    # more-specific glob -- never the shared pschism_WWM_* prefix.
    if variant == "wwm":
        cands = sorted(BIN_DIR.glob("pschism_WWM_GOTM_*"))
    elif variant == "icm":
        cands = sorted(BIN_DIR.glob("pschism_ICM_*"))
    elif variant == "sed":
        cands = sorted(BIN_DIR.glob("pschism_SED_*"))
    elif variant == "full":
        cands = sorted(BIN_DIR.glob("pschism_WWM_COSINE_*")) or sorted(
            BIN_DIR.glob("pschism_WWM_*")
        )
    else:
        cands = sorted(BIN_DIR.glob("pschism_TVD-*"))
    if not cands:
        raise FileNotFoundError(f"no SCHISM executable for variant={variant!r} in {BIN_DIR}")
    return cands[0]


def _write(name: str, payload: dict) -> None:
    (DATA / name).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def main() -> int:
    manifest_path = DATA / "manifest.json"
    manifest = {}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                raise ValueError(f"manifest.json must be a JSON object, got {type(manifest)}")
            _reject_unknown_manifest_fields(manifest)
        except Exception as exc:  # noqa: BLE001
            _write("schism_metrics.json", {"status": "error",
                   "error_code": "SCHISM_MANIFEST_INVALID", "error": f"{type(exc).__name__}: {exc}"})
            return 2

    variant = str(manifest.get("variant", "hydro"))
    ncompute = int(manifest.get("ncompute", 3))
    nscribe = int(manifest.get("nscribe", 2))
    timeout_s = float(manifest.get("timeout_s", 3600))
    outdir = DATA / "outputs"
    outdir.mkdir(exist_ok=True)

    try:
        exe = _resolve_exe(variant)
    except FileNotFoundError as exc:
        _write("schism_metrics.json", {"status": "error",
               "error_code": "SCHISM_EXE_MISSING", "error": str(exc)})
        return 3

    cmd = ["mpirun", "--allow-run-as-root", "-np", str(ncompute + nscribe),
           str(exe), str(nscribe)]
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, cwd=DATA, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _write("schism_metrics.json", {"status": "error",
               "error_code": "SCHISM_TIMEOUT", "error": f"exceeded {timeout_s:.0f}s"})
        return 4
    wall = round(time.time() - t0, 1)

    mirror = outdir / "mirror.out"
    completed = mirror.exists() and "Run completed successfully" in mirror.read_text()
    outputs = sorted(os.path.basename(p) for p in glob.glob(str(outdir / "*.nc")))
    metrics = {
        "status": "ok" if completed else "error",
        "variant": variant,
        "executable": exe.name,
        "ncompute": ncompute,
        "nscribe": nscribe,
        "wall_s": wall,
        "run_id": manifest.get("run_id"),
        "netcdf_outputs": outputs,
        "n_netcdf_outputs": len(outputs),
    }
    if not completed:
        metrics["error_code"] = "SCHISM_RUN_INCOMPLETE"
        metrics["stderr_tail"] = "\n".join(proc.stderr.splitlines()[-12:])
    _write("schism_metrics.json", metrics)
    return 0 if completed else 5


if __name__ == "__main__":
    sys.exit(main())
