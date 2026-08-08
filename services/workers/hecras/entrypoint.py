"""HEC-RAS 6.x Linux worker entrypoint (mesh wave M3 / hecras_geometry gate).

Runs the headless geometry-preprocess -> unsteady-solve pipeline HEC's own
Linux computation engines expose, over a bind-mounted rundir. The agent-side
launcher mounts a rundir at ``/data`` (``docker run ... -v <rundir>:/data``)
carrying the HEC-RAS deck files + ``manifest.json``; this shim:

  1. optionally runs ``RasGeomPreprocess <plan_hdf> <geom_suffix>`` -- rebuilds
     the hydraulic property tables (cell volume-elevation, face area-elevation,
     1D cross-section conveyance) on the plan HDF from the geometry;
  2. runs ``RasUnsteady <plan_hdf> <geom_suffix>`` -- the unsteady solve, which
     appends a ``Results`` group to the plan HDF;
  3. extracts the volume-accounting summary + max water-surface from the plan
     HDF via h5py and writes ``hecras_metrics.json`` back into the rundir.

The launcher uploads ``/data`` and writes completion.json, so this image does
NO object-store I/O (mirror of the telemac local worker). Honest failure: a
nonzero engine exit, a missing "Finished" sentinel, or an absent Results group
raises -- never a silent success.

This is the M3 mesh-wave gate (prove the geometry pipeline on Muncie), NOT the
HEC-RAS engine landing: there is no registered tool / template / contract
archetype here yet. The ras-commander ``Hdf*`` readers ride in the image for the
engine-landing wave; the metric extraction below stays pure-h5py so the gate
does not depend on the heavy geo closure.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import h5py
import numpy as np

try:  # flat-import (worker-dir) AND package-import (image PYTHONPATH) both work.
    from deck_edit import DeckEditError, scale_flow_hydrograph, set_breach_enabled
except ImportError:  # pragma: no cover - image runs from the worker dir
    from services.workers.hecras.deck_edit import (  # type: ignore[no-redef]
        DeckEditError,
        scale_flow_hydrograph,
        set_breach_enabled,
    )

#: Baked shipped-geometry decks (engine-landing wave). ``archetype`` in the
#: manifest names one; the entrypoint copies its ``wrk_source`` into the rundir
#: when the deck is not already staged there. The geometry is FROZEN (ADR 0100);
#: only the unsteady flow forcing is reparameterized.
_HERE = Path(__file__).resolve().parent
_BAKED_DECKS: dict[str, dict[str, str]] = {
    "muncie_riverine_flood": {
        "wrk_source": str(_HERE / "fixtures" / "muncie_smoke" / "wrk_source"),
        "plan_hdf": "Muncie.p04.tmp.hdf",
        "geom_suffix": "x04",
        "boundary_file": "Muncie.b04",
    },
    # The SAME Muncie White River deck: its 2D Interior Area is a leveed protected
    # floodplain and the ``.bNN`` carries a Breach Data block with 2 lateral-
    # structure breaches. The levee-breach archetype toggles those breaches (the
    # protected side floods when the levee fails, stays dry when it holds).
    "muncie_levee_breach": {
        "wrk_source": str(_HERE / "fixtures" / "muncie_smoke" / "wrk_source"),
        "plan_hdf": "Muncie.p04.tmp.hdf",
        "geom_suffix": "x04",
        "boundary_file": "Muncie.b04",
    },
}

# HEC's run scripts set LD_LIBRARY_PATH to libs : libs/mkl : libs/rhel_8 and put
# the engines on PATH; the Dockerfile bakes both env vars, so a bare engine name
# resolves. TRID3NT_HECRAS_BIN_DIR is the fallback if PATH was not inherited.
BIN_DIR = os.environ.get("TRID3NT_HECRAS_BIN_DIR", "/opt/hecras/bin")
DATA_DIR = os.environ.get("TRID3NT_HECRAS_DATA_DIR", "/data")

# HDF sentinel for a "no data" cell/face (HEC-RAS writes a large positive fill).
_HDF_FILL = 1e30


class HecrasError(RuntimeError):
    """A HEC-RAS engine leg failed or produced no usable result."""


#: PARSER VERSION -- bump on a manifest.json shape change. Named in the
#: strict-field error (ADR 0158).
_PARSER_VERSION = "hecras-manifest-1"

#: Every top-level manifest.json key ``run()`` reads (both manifest shapes:
#: the M3 gate -- plan_hdf/geom_suffix/run_geompre -- and the engine-landing
#: archetype path -- archetype/breach_enabled/flow_scale/target_peak_cfs).
#: An unknown key would otherwise silently keep the deck's baked default
#: (e.g. a typo'd flow knob solving the UNSCALED baseline, never erroring) --
#: the ADR 0148 lesson.
#:
#: The GENERIC run_solver-seam envelope (``run_id`` / ``inputs`` / ``outputs`` /
#: ``hecras_args``) rides the SAME manifest.json: the seam reads ``inputs``/
#: ``outputs`` to stage the deck + collect results (solver.py) while the worker
#: reads only the solve fields. They are ACCEPTED-and-ignored here so the M3-gate
#: no-archetype path (a fresh composed deck staged as ``inputs``, ADR 0140/0188)
#: is not rejected as "unknown fields" -- they are the seam's contract, not typos.
_KNOWN_MANIFEST_FIELDS = frozenset(
    {
        "archetype",
        "plan_hdf",
        "geom_suffix",
        "run_geompre",
        "breach_enabled",
        "flow_scale",
        "target_peak_cfs",
        # generic run_solver-seam envelope (consumed by the seam, not the worker):
        "run_id",
        "inputs",
        "outputs",
        "hecras_args",
    }
)


def _reject_unknown_manifest_fields(manifest: dict) -> None:
    """Raise loudly if ``manifest`` carries a top-level key ``run()`` never
    reads (ADR 0158 -- the ADR 0148 lesson: a stale image silently dropped
    unknown build_spec fields and two registered knob templates ran as
    no-ops)."""
    unknown = sorted(set(manifest) - _KNOWN_MANIFEST_FIELDS)
    if unknown:
        raise HecrasError(
            f"manifest.json carries unknown field(s) {unknown} that parser "
            f"{_PARSER_VERSION} does not read -- this SILENTLY no-ops the "
            f"intended knob(s) rather than applying them. Either the caller "
            f"has a typo, or the worker image is stale (rebuild it -- ADR "
            f"0148). Known fields: {sorted(_KNOWN_MANIFEST_FIELDS)}."
        )


def _engine(name: str) -> str:
    """Absolute path to a bundled engine, preferring PATH then the bin dir."""
    direct = Path(BIN_DIR) / name
    if direct.is_file():
        return str(direct)
    return name  # rely on PATH (Dockerfile bakes /opt/hecras/bin)


def _run_engine(name: str, plan_hdf: str, geom_suffix: str, cwd: Path) -> None:
    """Run one engine leg, streaming output, and assert a clean finish.

    The engines return 0 and print a ``Finished ...`` line on success. We assert
    BOTH the exit code AND the sentinel so a mid-run abort (which can still exit
    0 after printing an error) is caught honestly.
    """
    cmd = [_engine(name), plan_hdf, geom_suffix]
    print(f"[hecras] running: {' '.join(cmd)}  (cwd={cwd})", flush=True)
    proc = subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True, timeout=3600
    )
    tail = "\n".join(proc.stdout.splitlines()[-12:])
    print(f"[hecras] {name} exit={proc.returncode}\n{tail}", flush=True)
    if proc.returncode != 0:
        raise HecrasError(
            f"{name} exited {proc.returncode}\nstderr:\n{proc.stderr[-2000:]}"
        )
    if "Finished" not in proc.stdout:
        raise HecrasError(
            f"{name} exited 0 but printed no 'Finished' sentinel -- treating as "
            f"a failed run.\nstdout tail:\n{tail}"
        )


def _finite(arr: np.ndarray) -> np.ndarray:
    """Mask HEC-RAS fill values to NaN so summaries ignore dry/no-data cells."""
    a = np.asarray(arr, dtype=np.float64)
    return np.where(np.abs(a) > _HDF_FILL, np.nan, a)


def _extract_metrics(plan_hdf: Path) -> dict:
    """Pull volume accounting + max WSE from the results-bearing plan HDF.

    Raises if no ``Results`` group is present (an unsteady solve that wrote no
    results is a failure, not an empty success).
    """
    with h5py.File(plan_hdf, "r") as f:
        if "Results" not in f:
            raise HecrasError(
                f"{plan_hdf.name} has no /Results group after the unsteady run"
            )
        va = f["Results/Unsteady/Summary/Volume Accounting"]
        metrics: dict = {
            "volume_accounting": {
                k: (
                    va.attrs[k].decode()
                    if isinstance(va.attrs[k], bytes)
                    else float(va.attrs[k])
                    if np.isscalar(va.attrs[k]) and not isinstance(va.attrs[k], bytes)
                    else va.attrs[k].tolist()
                )
                for k in va.attrs
            }
        }
        # 2D flow-area max water surface (headline for the coastal/riverine 2D
        # result) -- present only when the deck has a 2D flow area.
        base = "Results/Unsteady/Output/Output Blocks/Base Output/Summary Output"
        two_d = f.get(f"{base}/2D Flow Areas")
        if two_d is not None:
            for area_name in two_d:
                mw = two_d[f"{area_name}/Maximum Water Surface"]
                a = _finite(mw[()])
                metrics.setdefault("max_water_surface_2d", {})[area_name] = {
                    "cells": int(a.shape[-1]),
                    "min_ft": float(np.nanmin(a)),
                    "max_ft": float(np.nanmax(a)),
                }
        xs = f.get(f"{base}/Cross Sections/Maximum Water Surface")
        if xs is not None:
            # Shape is (n_profiles, n_xs); profile row 0 is the max WSE profile
            # (later rows carry other summary quantities in HEC's flow units, so a
            # global max would misreport a discharge as a water surface).
            a = _finite(xs[()])
            wse = a[0] if a.ndim == 2 else a
            metrics["max_water_surface_1d"] = {
                "cross_sections": int(a.shape[-1]),
                "min_ft": float(np.nanmin(wse)),
                "max_ft": float(np.nanmax(wse)),
            }
    return metrics


def _stage_baked_deck(archetype: str, data_dir: Path) -> dict[str, str]:
    """Copy a baked shipped-geometry deck into the rundir; return its deck spec.

    Engine-landing wave: the Muncie deck is baked in the image, so an
    engine-landing manifest carries only the ``archetype`` + flow knobs (no 4 MB
    HDF staged as an input). The geometry/terrain/mesh are FROZEN -- we copy them
    verbatim and only the ``.bNN`` flow forcing is edited downstream.
    """
    spec = _BAKED_DECKS.get(archetype)
    if spec is None:
        raise HecrasError(
            f"unknown archetype {archetype!r}; baked decks: {sorted(_BAKED_DECKS)}"
        )
    src = Path(spec["wrk_source"])
    if not src.is_dir():
        raise HecrasError(f"baked deck source not found: {src}")
    for fn in sorted(os.listdir(src)):
        s = src / fn
        if s.is_file():
            shutil.copy2(s, data_dir / fn)
    print(f"[hecras] staged baked deck {archetype!r} from {src}", flush=True)
    return spec


def _apply_breach(data_dir: Path, boundary_file: str, manifest: dict) -> dict:
    """Toggle the ``.bNN`` lateral-structure breaches per the manifest.

    Absent ``breach_enabled`` the shipped breaches are left as-is (ON) -- so the
    riverine-flood archetype is unaffected. When present, ``set_breach_enabled``
    rewrites the ``Breach Data`` block deterministically (disabling zeroes the
    count AND drops the record lines -- the in-container-verified valid edit).
    Returns the breach provenance folded into the metrics.
    """
    if "breach_enabled" not in manifest:
        return {}
    breach_enabled = bool(manifest.get("breach_enabled"))
    bpath = data_dir / boundary_file
    if not bpath.is_file():
        raise HecrasError(f"boundary file {boundary_file} not found in {data_dir}")
    try:
        new_text, n_active = set_breach_enabled(bpath.read_text(), breach_enabled)
    except DeckEditError as exc:
        raise HecrasError(f"breach toggle deck edit failed: {exc}") from exc
    bpath.write_text(new_text)
    print(
        f"[hecras] breach_enabled={breach_enabled} -> {n_active} lateral-structure "
        f"breach(es) active (boundary {boundary_file})",
        flush=True,
    )
    return {"breach_enabled": breach_enabled, "breach_count_active": int(n_active)}


def _apply_flow_scale(
    data_dir: Path, boundary_file: str, manifest: dict
) -> dict:
    """Reparameterize the unsteady inflow hydrograph in the ``.bNN`` boundary file.

    The plain multiplier (``flow_scale``) is the user/default path; an optional
    ``target_peak_cfs`` derives the multiplier from the baseline peak (the seam-1
    fetcher path -- pin the forcing to a real gauge/NWM peak). A no-op edit
    (scale 1.0) still rewrites the block byte-equivalently. Returns the forcing
    provenance folded into the metrics.
    """
    bpath = data_dir / boundary_file
    if not bpath.is_file():
        raise HecrasError(f"boundary file {boundary_file} not found in {data_dir}")

    flow_scale = float(manifest.get("flow_scale", 1.0) or 1.0)
    target_peak = manifest.get("target_peak_cfs")

    text = bpath.read_text()
    # First pass with scale 1.0 recovers the true baseline peak so target_peak can
    # derive the multiplier deterministically.
    try:
        _, base_peak, _ = scale_flow_hydrograph(text, 1.0)
    except DeckEditError as exc:
        raise HecrasError(f"could not parse the inflow hydrograph: {exc}") from exc

    if target_peak is not None and base_peak > 0:
        flow_scale = float(target_peak) / base_peak
        print(
            f"[hecras] target_peak_cfs={target_peak} / baseline {base_peak:.0f} "
            f"-> flow_scale={flow_scale:.4f}",
            flush=True,
        )
    # Clamp to the modelable band (mirrors the contract; a frozen demo geometry is
    # only faithful within it).
    flow_scale = min(max(flow_scale, 0.25), 4.0)

    try:
        new_text, base_peak, scaled_peak = scale_flow_hydrograph(text, flow_scale)
    except DeckEditError as exc:
        raise HecrasError(f"flow-scale deck edit failed: {exc}") from exc
    bpath.write_text(new_text)
    print(
        f"[hecras] flow_scale={flow_scale:.4f} peak {base_peak:.0f} -> "
        f"{scaled_peak:.0f} cfs (boundary {boundary_file})",
        flush=True,
    )
    return {
        "flow_scale": round(flow_scale, 6),
        "baseline_peak_cfs": round(base_peak, 3),
        "peak_inflow_cfs": round(scaled_peak, 3),
    }


def run(data_dir: Path) -> dict:
    """Execute the manifest's HEC-RAS legs and return the metrics dict.

    Two manifest shapes are supported:

    - **M3 gate** (deck already staged into the rundir): ``plan_hdf`` +
      ``geom_suffix`` name files present in ``data_dir`` (the Muncie smoke driver).
    - **Engine landing** (``archetype`` names a baked deck): the entrypoint copies
      the baked shipped-geometry deck into ``data_dir`` and applies the unsteady
      flow-forcing reparameterization (``flow_scale`` / ``target_peak_cfs``) before
      solving. The geometry is FROZEN (ADR 0100).
    """
    manifest_path = data_dir / "manifest.json"
    if not manifest_path.is_file():
        raise HecrasError(f"no manifest.json in {data_dir}")
    manifest = json.loads(manifest_path.read_text())
    if not isinstance(manifest, dict):
        raise HecrasError(f"manifest.json must be a JSON object, got {type(manifest)}")
    _reject_unknown_manifest_fields(manifest)

    archetype = manifest.get("archetype")
    forcing: dict = {}
    if archetype:
        spec = _stage_baked_deck(str(archetype), data_dir)
        plan_hdf = manifest.get("plan_hdf") or spec["plan_hdf"]
        geom_suffix = manifest.get("geom_suffix") or spec["geom_suffix"]
        # Breach toggle THEN flow scale -- disjoint .bNN blocks, so they compose.
        forcing = _apply_breach(data_dir, spec["boundary_file"], manifest)
        forcing.update(_apply_flow_scale(data_dir, spec["boundary_file"], manifest))
    else:
        plan_hdf = manifest["plan_hdf"]  # e.g. "Muncie.p04.tmp.hdf"
        geom_suffix = manifest["geom_suffix"]  # e.g. "x04"

    run_geompre = bool(manifest.get("run_geompre", True))

    if not (data_dir / plan_hdf).is_file():
        raise HecrasError(f"plan HDF {plan_hdf} not found in {data_dir}")

    if run_geompre:
        _run_engine("RasGeomPreprocess", plan_hdf, geom_suffix, data_dir)
    _run_engine("RasUnsteady", plan_hdf, geom_suffix, data_dir)

    metrics = _extract_metrics(data_dir / plan_hdf)
    metrics["plan_hdf"] = plan_hdf
    metrics["ran_geompre"] = run_geompre
    metrics.update(forcing)
    if archetype:
        metrics["archetype"] = str(archetype)
    # Physics-level truth for the classify_exit hook (mirrors telemac's
    # correct_end): both honesty gates (Finished sentinel + a Results group)
    # passed if we reached here without raising.
    metrics["correct_end"] = True
    (data_dir / "hecras_metrics.json").write_text(json.dumps(metrics, indent=2))
    print(f"[hecras] wrote hecras_metrics.json", flush=True)
    return metrics


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    data_dir = Path(argv[0]) if argv else Path(DATA_DIR)
    try:
        metrics = run(data_dir)
    except Exception as exc:  # honest surface: non-zero exit + the reason
        print(f"[hecras] FAILED: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1
    err_pct = metrics["volume_accounting"].get("Error Percent")
    print(f"[hecras] DONE -- volume accounting error {err_pct}%", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
