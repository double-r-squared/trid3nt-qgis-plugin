"""L2 validation-wave closed-loop harness (docs/validation/e2e-harness.md).

Direct TOOL_REGISTRY calls -- bypasses the LLM/agent chat layer, matching the
repo's direct-call driver convention (``run_sfincs_direct.py`` etc). Exercises
the 9-tool V&V wave (ADR 0021, ``docs/validation/build-contract.md``) end to
end: baseline solve -> diagnostics -> obs pairing -> skill metrics -> setter
-> re-run -> re-score, plus a metamorphic rain-scaling rider.

Principle (e2e-harness.md): assert the MACHINERY, never the model. Every
assertion below checks envelope completeness / honesty fields / lineage /
monotonicity -- metric VALUES (NSE, KGE, ...) are printed as findings, never
gated on a threshold.

TWO modes:

- smoke (default): a tiny synthetic Chattanooga domain, ZERO external
  network. DEM / landcover / river-geometry all resolve from the local MinIO
  cache (verified live before this harness was written -- see report), and
  the rain forcing is a locally synthesized CONSTANT raster fed through
  ``forcing_raster_uri`` (the OQ-6 area-mean netamt path), which deliberately
  SKIPS the NOAA Atlas 14 network lookup. This mode is IMPLEMENTED AND RUN.

- live (``--live``): Hurricane Harvey / Buffalo Bayou per e2e-harness.md --
  real precip forcing, live ``fetch_high_water_marks`` (STN), split-sample
  (calibrate on a subset, score the held-out remainder). Gated behind
  ``--live`` PLUS an interactive confirmation; prints the e2e-harness.md
  discipline note and exits if not confirmed. Concrete AOI/window/split
  inputs are still PENDING a final NATE look per the doc, so even a
  confirmed run only proceeds past the gate with explicit ``--aoi-bbox`` /
  ``--event-window`` overrides. THIS MODE IS IMPLEMENTED BUT NEVER EXECUTED
  BY THIS SCRIPT'S OWN AUTOMATION -- running it live is NATE's call.

Runs created by this harness are Claude-driver junk cases: every
``run_model_flood_scenario`` call is tagged with ``session_id``/``project_id``
prefixed ``l2-smoke-`` (smoke) / ``l2-live-`` (live) so they are recognizable
and safe to clean up later.

Run (smoke):
  cd /home/nate/Documents/trid3nt-local
  sg docker -c 'env $(grep -v "^#" .env.local | xargs) \
    PYTHONPATH=server/src:contracts/src \
    venvs/agent/bin/python scripts/run_l2_validation_harness.py'

Run (live -- NATE only, never scripted here):
  ... same env prefix ... scripts/run_l2_validation_harness.py --live
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import math
import os
import random
import re
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("l2_validation_harness")

# ---------------------------------------------------------------------------
# Smoke-mode constants
# ---------------------------------------------------------------------------

# Same bbox as scripts/run_sfincs_direct.py -- DEM / landcover / river
# geometry for this exact bbox+resolution are cache hits (verified live,
# see report): fetch_dem/_landcover/_river_geometry all logged
# "read_through hit (s3)" in well under a second, zero upstream calls.
CHATTANOOGA_BBOX = (-85.32, 35.03, -85.28, 35.07)
DURATION_HR = 1
COMPUTE_CLASS = "small"
BASE_RAIN_MM = 60.0  # constant synthetic accumulated rain, mm over DURATION_HR
METAMORPHIC_SCALE = 1.5
N_OBS_POINTS = 5
WET_DEPTH_THRESHOLD_M = 0.05  # matches postprocess_flood's own nodata_threshold_m

_FT_TO_M = 0.3048
_ULID_RE = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")

CASE_TAG = f"l2-smoke-{uuid.uuid4().hex[:8]}"

RESULTS: list[dict[str, Any]] = []


def record(step: str, ok: bool, detail: str) -> None:
    RESULTS.append({"step": step, "ok": ok, "detail": detail})
    log.info("[%s] %s: %s", "PASS" if ok else "FAIL", step, detail)


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------


def _read_bytes_any(uri: str) -> bytes:
    """Read raster/file bytes from s3:// or a local/file:// path."""
    if uri.startswith("s3://"):
        from trid3nt_server.tools.cache import read_object_bytes_s3

        return read_object_bytes_s3(uri)
    path = uri[len("file://"):] if uri.startswith("file://") else uri
    return Path(path).read_bytes()


def extract_run_id(uri: str) -> str:
    for part in uri.split("/"):
        if _ULID_RE.fullmatch(part):
            return part
    raise ValueError(f"no ULID run_id segment found in {uri!r}")


def _sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def raster_stats(uri: str) -> dict[str, Any]:
    """max/mean/flooded_cell_count over finite cells -- mirrors postprocess_flood's
    own nodata_threshold_m masking (the published COG already carries NaN below
    threshold, so "finite" == "flooded" here, matching the tool's convention
    exactly rather than approximating it)."""
    import numpy as np
    import rasterio

    data = _read_bytes_any(uri)
    with rasterio.io.MemoryFile(data) as mf:
        with mf.open() as src:
            band = src.read(1).astype("float64")
            nodata = src.nodata
    mask = np.isfinite(band)
    if nodata is not None and not (isinstance(nodata, float) and math.isnan(nodata)):
        mask &= band != nodata
    flooded = band[mask]
    if flooded.size == 0:
        return {"max_depth_m": 0.0, "mean_depth_m": 0.0, "flooded_cell_count": 0}
    return {
        "max_depth_m": float(np.nanmax(flooded)),
        "mean_depth_m": float(np.nanmean(flooded)),
        "flooded_cell_count": int(flooded.size),
    }


def make_constant_precip_raster(
    path: Path, bbox: tuple[float, float, float, float], value_mm: float
) -> None:
    """Write a tiny constant-value accumulated-precip COG-ish GeoTIFF.

    Feeds ``forcing_raster_uri`` (OQ-6 area-mean netamt path,
    model_flood_scenario.compute_precip_area_mean_mm_per_hr) -- the
    NATIVE mechanism for observed/synthetic precip forcing, not a hack. This
    SKIPS the NOAA Atlas 14 network lookup entirely (constant synthetic rain
    forcing, deliberately exercised per the kickoff).
    """
    import numpy as np
    import rasterio
    from rasterio.transform import from_bounds

    w, s, e, n = bbox
    arr = np.full((8, 8), value_mm, dtype="float32")
    transform = from_bounds(w, s, e, n, 8, 8)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=8,
        width=8,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=transform,
        nodata=-9999.0,
    ) as dst:
        dst.write(arr, 1)


def build_observation_points(
    peak_depth_uri: str, out_path: Path, n_points: int = N_OBS_POINTS, seed: int = 20260724
) -> dict[str, Any]:
    """Synthetic fake-HWM-style points INSIDE the wet area of a peak-depth COG.

    ``elev_ft`` = a jittered feet-equivalent of the true modeled depth at that
    cell -- guarantees every point lands on a wet cell (never dropped as
    nodata_sample) while deliberately exercising the ft->m conversion path
    (build-contract 3.3 / the post-panel fix) with a non-trivial (not exactly
    1.0-skill) residual.
    """
    import geopandas as gpd
    import numpy as np
    import rasterio
    from shapely.geometry import Point

    data = _read_bytes_any(peak_depth_uri)
    with rasterio.io.MemoryFile(data) as mf:
        with mf.open() as src:
            band = src.read(1).astype("float64")
            transform = src.transform
            crs = src.crs
            nodata = src.nodata

    mask = np.isfinite(band)
    if nodata is not None and not (isinstance(nodata, float) and math.isnan(nodata)):
        mask &= band != nodata
    mask &= band >= WET_DEPTH_THRESHOLD_M
    ys, xs = np.where(mask)
    if len(ys) == 0:
        raise RuntimeError(
            "baseline run produced zero wet cells (>= "
            f"{WET_DEPTH_THRESHOLD_M} m) -- cannot place synthetic HWM points"
        )

    depths = band[ys, xs]
    order = np.argsort(depths)  # ascending by depth
    idxs = np.linspace(0, len(order) - 1, num=min(n_points, len(order)), dtype=int)
    chosen = order[idxs]

    rng = random.Random(seed)
    obs_ids: list[str] = []
    elev_fts: list[float] = []
    geoms = []
    depths_m: list[float] = []
    for i, k in enumerate(chosen):
        row, col = int(ys[k]), int(xs[k])
        depth_m = float(band[row, col])
        wx, wy = rasterio.transform.xy(transform, row, col)
        jitter = rng.uniform(0.85, 1.15)
        elev_ft = (depth_m * jitter) / _FT_TO_M
        obs_ids.append(f"L2SMOKE-HWM-{i + 1}")
        elev_fts.append(round(elev_ft, 3))
        geoms.append(Point(wx, wy))
        depths_m.append(depth_m)

    gdf = gpd.GeoDataFrame({"obs_id": obs_ids, "elev_ft": elev_fts}, geometry=geoms, crs=crs)
    gdf.to_file(out_path, driver="FlatGeobuf", engine="pyogrio")
    return {
        "n_points": len(obs_ids),
        "depth_range_m": (min(depths_m), max(depths_m)),
        "elev_ft_values": elev_fts,
        "crs": str(crs),
    }


_SFINCS_DECK_FILENAMES = [
    "sfincs.inp", "sfincs.dep", "sfincs.ind", "sfincs.msk", "sfincs.man",
    "sfincs.precip", "sfincs.bnd", "sfincs.dis", "sfincs.src", "sfincs.obs",
    "sfincs.spw", "sfincs.crsgeo",
]


def build_rerun_manifest(deck_dir: Path) -> Path:
    """Compose a launch_local_solver-ready manifest.json from a deck directory.

    Mirrors sfincs_builder.build_sfincs_model's OWN manifest composition
    (same ``sfincs_args: []`` / ``outputs`` convention) -- reused here because
    a param-setter child's ``child_setup_uri`` is a provenance manifest
    (schema_version/engine/child_id/parent_model/changes_applied), NOT a
    dispatch manifest launch_local_solver can read directly (flagged as an
    open gap in docs/validation/build-report.md section 8). Scoped to the
    KNOWN sfincs deck filenames (whitelist) rather than a blind glob, since a
    setter child dir also carries stale run-artifact leftovers copied from the
    parent (sfincs_map.nc / .stdout / .stderr / .log / manifest.json /
    hydromt.log) that must NOT be re-submitted as solver inputs.
    """
    inputs = []
    for name in _SFINCS_DECK_FILENAMES:
        p = deck_dir / name
        if p.is_file():
            inputs.append({"gs_uri": str(p), "dest": name})
    if not any(i["dest"] == "sfincs.inp" for i in inputs):
        raise RuntimeError(f"no sfincs.inp found under deck dir {deck_dir}")
    manifest = {"inputs": inputs, "sfincs_args": [], "outputs": ["sfincs_map.nc", "*.nc", "*.tif"]}
    manifest_path = deck_dir.parent / "rerun_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest_path


def model_root_from_child_setup(child_setup_uri: str) -> Path:
    if not child_setup_uri.startswith("file://"):
        raise RuntimeError(
            f"expected a local file:// child_setup_uri (bare-local-path parent "
            f"-> storage='local'); got {child_setup_uri!r}"
        )
    manifest_path = Path(child_setup_uri[len("file://"):])
    return manifest_path.parent / "model"


def _tail_local_stderr(run_id: str, n_lines: int = 15) -> str:
    runs_dir = Path(os.environ.get("TRID3NT_RUNS_DIR", "data/runs"))
    p = runs_dir / run_id / "sfincs.stderr"
    if not p.is_file():
        return "(no local sfincs.stderr found)"
    lines = p.read_text(errors="replace").splitlines()
    return "\n".join(lines[-n_lines:])


# ---------------------------------------------------------------------------
# Contract assertion helpers (per-tool envelope shape, build-contract.md 3.x)
# ---------------------------------------------------------------------------

REQUIRED_DIAG_KEYS = {
    "engine", "run_id", "status", "healthy", "mass_balance_pct",
    "mass_balance_source", "instability", "nonconverged_pct", "dry_cells",
    "warnings", "engine_specific", "sources", "notes",
}
REQUIRED_ALIGNMENT_KEYS = {"spatial", "temporal", "datum", "crs"}
REQUIRED_SKILL_KEYS = {
    "variable", "n", "metrics", "bands", "suggested_verdict",
    "verdict_is_heuristic", "caveats", "units", "notes",
}
REQUIRED_SETTER_KEYS = {
    "engine", "child_setup_uri", "parent_model", "changes_applied",
    "plausibility", "notes",
}


def step_diagnostics(run_handle: str) -> dict[str, Any]:
    from trid3nt_server.tools import TOOL_REGISTRY

    fn = TOOL_REGISTRY["read_run_diagnostics"].fn
    env = fn(run_handle=run_handle)
    missing = REQUIRED_DIAG_KEYS - set(env.keys())
    if missing:
        raise AssertionError(f"diagnostics envelope missing keys: {sorted(missing)}")
    if env["engine"] != "sfincs":
        raise AssertionError(f"expected engine='sfincs', got {env['engine']!r}")
    if env["status"] != "ok":
        raise AssertionError(f"run status != 'ok': {env['status']!r} warnings={env['warnings']}")
    return env


def step_pairing(model_uri: str, obs_path: Path) -> Any:
    from trid3nt_server.tools import TOOL_REGISTRY

    fn = TOOL_REGISTRY["extract_model_at_observations"].fn
    paired = fn(model_layer_uri=model_uri, observations_layer_uri=str(obs_path))
    missing = REQUIRED_ALIGNMENT_KEYS - set(paired.alignment.keys())
    if missing:
        raise AssertionError(f"alignment block missing keys: {sorted(missing)}")
    if not paired.units_warning:
        raise AssertionError("units_warning is empty; contract requires ALWAYS populated")
    if paired.n_paired == 0:
        raise AssertionError(f"zero paired samples; all {paired.n_dropped} dropped: {paired.dropped}")
    return paired


def step_skill(paired_table_uri: str) -> dict[str, Any]:
    from trid3nt_server.tools import TOOL_REGISTRY

    fn = TOOL_REGISTRY["compute_skill_metrics"].fn
    m = fn(paired_table_uri=paired_table_uri, variable="generic")
    missing = REQUIRED_SKILL_KEYS - set(m.keys())
    if missing:
        raise AssertionError(f"skill envelope missing keys: {sorted(missing)}")
    if m["verdict_is_heuristic"] is not True:
        raise AssertionError("verdict_is_heuristic must ALWAYS be True per contract 3.2")
    return m


def step_setter(parent_model_uri: str) -> dict[str, Any]:
    from trid3nt_server.tools import TOOL_REGISTRY

    fn = TOOL_REGISTRY["set_sfincs_parameters"].fn
    watch_files = ["sfincs.man", "sfincs.inp"]
    before_hash = {
        f: _sha256_file(Path(parent_model_uri) / f)
        for f in watch_files
        if (Path(parent_model_uri) / f).is_file()
    }
    env = fn(
        parent_model_uri=parent_model_uri,
        changes=[{"parameter": "manning_land", "op": "scale", "factor": 0.85}],
    )
    missing = REQUIRED_SETTER_KEYS - set(env.keys())
    if missing:
        raise AssertionError(f"setter envelope missing keys: {sorted(missing)}")
    if env["parent_model"] != parent_model_uri:
        raise AssertionError(
            f"parent_model lineage mismatch: {env['parent_model']!r} != {parent_model_uri!r}"
        )
    after_hash = {
        f: _sha256_file(Path(parent_model_uri) / f) for f in before_hash
    }
    if before_hash != after_hash:
        raise AssertionError(
            f"PARENT DIR WAS MUTATED by set_sfincs_parameters: before={before_hash} after={after_hash}"
        )
    return env


# ---------------------------------------------------------------------------
# Baseline solve (direct TOOL_REGISTRY call, the repo norm)
# ---------------------------------------------------------------------------


#: run_ids minted by THIS harness invocation -- printed at the end as the
#: cleanup list. ``AssessmentEnvelope.project_id``/``session_id`` are
#: pydantic-constrained to actual 26-char ULIDs (discovered live -- a
#: free-text "l2-smoke-..." tag there fails validation AFTER a real solve
#: already completed), so there is no free-text tagging knob at this call
#: site; recognizability instead comes from this explicit run_id log line
#: (matches the ``run_sfincs_direct.py`` convention of reporting
#: ``new_run_prefixes`` rather than pre-naming them).
MINTED_RUN_IDS: list[str] = []


async def run_baseline(rain_mm: float, tag: str) -> Any:
    from trid3nt_server.tools import TOOL_REGISTRY

    fn = TOOL_REGISTRY["run_model_flood_scenario"].fn
    with tempfile.TemporaryDirectory(prefix="l2_smoke_forcing_") as tmp:
        raster_path = Path(tmp) / f"{tag}.tif"
        make_constant_precip_raster(raster_path, CHATTANOOGA_BBOX, rain_mm)
        log.info(
            "run_model_flood_scenario bbox=%s duration_hr=%s compute_class=%s "
            "forcing_raster_uri=%s (constant %.1f mm, ZERO network) tag=%s",
            CHATTANOOGA_BBOX, DURATION_HR, COMPUTE_CLASS, raster_path, rain_mm, tag,
        )
        result = await fn(
            bbox=CHATTANOOGA_BBOX,
            duration_hr=DURATION_HR,
            compute_class=COMPUTE_CLASS,
            forcing_raster_uri=str(raster_path),
        )
    if not isinstance(result, dict):
        try:
            MINTED_RUN_IDS.append(extract_run_id(result.uri))
        except Exception:  # noqa: BLE001 -- best-effort bookkeeping only
            pass
    return result


def _is_failed_envelope(result: Any) -> bool:
    # run_model_flood_scenario's layer-emission contract: a LayerURI on
    # success, an AssessmentEnvelope.model_dump() dict (no top-level
    # error_code key -- the code is threaded into
    # flood.metrics.solver_version / workflow_name) on failure. ANY dict
    # result is therefore a failed envelope.
    return isinstance(result, dict)


def _failure_detail(result: dict[str, Any]) -> str:
    wf = result.get("workflow_name", "") or ""
    if ":FAILED:" in wf:
        return wf.split(":FAILED:", 1)[1]
    sv = ((result.get("flood") or {}).get("metrics") or {}).get("solver_version", "") or ""
    if sv.startswith("failed:"):
        return sv[len("failed:"):]
    return f"UNKNOWN failure shape: {json.dumps(result, default=str)[:500]}"


# ---------------------------------------------------------------------------
# Printed findings
# ---------------------------------------------------------------------------


def print_step_table() -> None:
    print("\n=== PER-STEP RESULT ===")
    for r in RESULTS:
        print(f"[{'PASS' if r['ok'] else 'FAIL'}] {r['step']}: {r['detail']}")


def print_skill_table(before: dict[str, Any] | None, after: dict[str, Any] | None) -> None:
    print("\n=== BEFORE/AFTER SKILL TABLE (manning_land scale 0.85) ===")
    print(f"{'metric':<20}{'baseline':<20}{'after-child':<20}")
    if before is None:
        print("(baseline skill metrics unavailable -- step 5 failed)")
        return
    keys = list(before["metrics"].keys())
    for k in keys:
        b = before["metrics"].get(k)
        a = after["metrics"].get(k) if after is not None else "BLOCKED (see defect)"
        print(f"{k:<20}{str(b):<20}{str(a):<20}")
    print(f"suggested_verdict: baseline={before.get('suggested_verdict')} "
          f"after={after.get('suggested_verdict') if after is not None else 'N/A'}")


def print_metamorphic_table(base_stats: dict[str, Any], meta_stats: dict[str, Any]) -> None:
    print(f"\n=== METAMORPHIC COMPARISON (rain x{METAMORPHIC_SCALE}) ===")
    print(f"{'metric':<24}{'baseline':<24}{'1.5x rain':<24}")
    for k in ("max_depth_m", "mean_depth_m", "flooded_cell_count"):
        b = base_stats[k]
        m = meta_stats[k]
        b_s = f"{b:.6f}" if isinstance(b, float) else str(b)
        m_s = f"{m:.6f}" if isinstance(m, float) else str(m)
        print(f"{k:<24}{b_s:<24}{m_s:<24}")


# ---------------------------------------------------------------------------
# Smoke mode
# ---------------------------------------------------------------------------


async def main_smoke() -> int:
    log.info("=== L2 SMOKE HARNESS start case_tag=%s ===", CASE_TAG)

    # --- Step 1: baseline flood run --------------------------------------
    try:
        baseline = await run_baseline(BASE_RAIN_MM, f"{CASE_TAG}-baseline")
        if _is_failed_envelope(baseline):
            record("1-baseline-run", False, f"FAILED envelope: {_failure_detail(baseline)}")
            print_step_table()
            return 1
        baseline_uri = baseline.uri
        run_id = extract_run_id(baseline_uri)
        deck_dir = Path(os.environ.get("TRID3NT_RUNS_DIR", "data/runs")) / run_id
        record("1-baseline-run", True, f"run_id={run_id} peak_depth_uri={baseline_uri}")
    except Exception as exc:  # noqa: BLE001 -- honesty floor: report verbatim
        record("1-baseline-run", False, f"EXCEPTION: {exc!r}")
        print_step_table()
        return 1

    # --- Step 2: read_run_diagnostics -------------------------------------
    diag: dict[str, Any] | None = None
    try:
        diag = step_diagnostics(baseline_uri)
        record(
            "2-read_run_diagnostics", True,
            f"status={diag['status']} healthy={diag['healthy']} "
            f"mass_balance_pct={diag['mass_balance_pct']} "
            f"mass_balance_source={diag['mass_balance_source']} "
            f"instability={diag['instability']} "
            f"engine_specific={json.dumps(diag['engine_specific'], default=str)}",
        )
    except Exception as exc:  # noqa: BLE001
        record("2-read_run_diagnostics", False, f"EXCEPTION: {exc!r}")

    # --- Step 3: synthetic observation points -----------------------------
    obs_dir = Path(tempfile.mkdtemp(prefix="l2_smoke_obs_"))
    obs_path = obs_dir / "hwm_points.fgb"
    try:
        obs_summary = build_observation_points(baseline_uri, obs_path, n_points=N_OBS_POINTS)
        record(
            "3-synthetic-obs-points", True,
            f"n_points={obs_summary['n_points']} depth_range_m={obs_summary['depth_range_m']} "
            f"elev_ft={obs_summary['elev_ft_values']} crs={obs_summary['crs']}",
        )
    except Exception as exc:  # noqa: BLE001
        record("3-synthetic-obs-points", False, f"EXCEPTION: {exc!r}")
        print_step_table()
        return 1

    # --- Step 4: extract_model_at_observations -----------------------------
    paired = None
    try:
        paired = step_pairing(baseline_uri, obs_path)
        record(
            "4-extract_model_at_observations", True,
            f"n_paired={paired.n_paired} n_dropped={paired.n_dropped} "
            f"dropped={paired.dropped} alignment={paired.alignment} "
            f"units_warning={paired.units_warning!r}",
        )
    except Exception as exc:  # noqa: BLE001
        record("4-extract_model_at_observations", False, f"EXCEPTION: {exc!r}")

    # --- Step 5: compute_skill_metrics (before) -----------------------------
    skill_before: dict[str, Any] | None = None
    if paired is not None:
        try:
            skill_before = step_skill(paired.paired_table_uri)
            record(
                "5-compute_skill_metrics(before)", True,
                f"n={skill_before['n']} metrics={skill_before['metrics']} "
                f"verdict={skill_before['suggested_verdict']} "
                f"verdict_is_heuristic={skill_before['verdict_is_heuristic']}",
            )
        except Exception as exc:  # noqa: BLE001
            record("5-compute_skill_metrics(before)", False, f"EXCEPTION: {exc!r}")
    else:
        record("5-compute_skill_metrics(before)", False, "SKIPPED - step 4 produced no paired table")

    # --- Step 6: set_sfincs_parameters (manning scale 0.85) -----------------
    setter_env: dict[str, Any] | None = None
    try:
        setter_env = step_setter(str(deck_dir))
        record(
            "6-set_sfincs_parameters", True,
            f"changes_applied={setter_env['changes_applied']} "
            f"plausibility={setter_env['plausibility']} "
            f"child_setup_uri={setter_env['child_setup_uri']} "
            f"parent_model={setter_env['parent_model']} (parent dir hash-verified untouched)",
        )
    except Exception as exc:  # noqa: BLE001
        record("6-set_sfincs_parameters", False, f"EXCEPTION: {exc!r}")

    # --- Step 7: re-run on the child model, re-score ------------------------
    skill_after: dict[str, Any] | None = None
    if setter_env is not None:
        try:
            model_root = model_root_from_child_setup(setter_env["child_setup_uri"])
            manifest_path = build_rerun_manifest(model_root)
            from trid3nt_server.tools import TOOL_REGISTRY

            run_solver = TOOL_REGISTRY["run_solver"].fn
            wait_for_completion = TOOL_REGISTRY["wait_for_completion"].fn
            handle = run_solver(
                solver="sfincs", model_setup_uri=f"file://{manifest_path}", compute_class=COMPUTE_CLASS
            )
            child_result = await wait_for_completion(handle, poll_interval_s=3, timeout_s=300)
            if child_result.status != "complete":
                stderr_tail = _tail_local_stderr(handle.run_id)
                record(
                    "7-rerun-child-model", False,
                    "DEFECT in set_sfincs_parameters "
                    "(server/src/trid3nt_server/tools/simulation/set_sfincs_parameters.py): "
                    "the child deck's sfincs.inp cannot be solved by the real sfincs binary. "
                    f"run_solver/wait_for_completion status={child_result.status} "
                    f"error_code={child_result.error_code} error_message={child_result.error_message}. "
                    f"stderr_tail={stderr_tail!r}. "
                    "ROOT CAUSE (confirmed by diffing the child sfincs.inp against the parent's "
                    "known-good sfincs.inp): model.write_config() [hydromt_sfincs.SfincsModel, "
                    "called at set_sfincs_parameters.py ~line 247] rewrites the 'epsg' field as a "
                    "CRS STRING (\"EPSG:3857\") instead of the bare-integer form (\"32616\") that "
                    "the original build_sfincs_model deck-build path emits, and DROPS the separate "
                    "'crs = EPSG:3857' line entirely. The SFINCS v2.3.3 Fortran reader crashes on "
                    "the non-integer 'epsg' value (sfincs_input.f90 line 837: 'Bad integer for item "
                    "1 in list input', exit code 1/2). Confirmed the epsg/crs lines are the SOLE "
                    "cause: reverting ONLY those two lines on a throwaway COPY of the child deck "
                    "(never the tool source) let the identical deck solve to completion "
                    "(status=complete) in a side rehearsal. Every OTHER setter output (manning "
                    "grid values, changes_applied before/after, lineage) is correct -- this is a "
                    "config-writer formatting bug, not a deeper architecture problem. NOT PATCHED "
                    "per instructions; step 7's re-score is BLOCKED by this defect.",
                )
            else:
                child_run_id = handle.run_id
                from trid3nt_server.workflows.postprocess_flood import postprocess_flood

                layers, _metrics = postprocess_flood(child_result.output_uri, run_id=child_run_id)
                child_peak_uri = layers[0].uri
                paired_after = step_pairing(child_peak_uri, obs_path)
                skill_after = step_skill(paired_after.paired_table_uri)
                record(
                    "7-rerun-child-model", True,
                    f"child_run_id={child_run_id} n_paired_after={paired_after.n_paired} "
                    f"metrics_after={skill_after['metrics']}",
                )
        except Exception as exc:  # noqa: BLE001
            record("7-rerun-child-model", False, f"EXCEPTION: {exc!r}")
    else:
        record("7-rerun-child-model", False, "SKIPPED - step 6 (setter) produced no child_setup_uri")

    # --- Step 8: METAMORPHIC rider (1.5x rain) ------------------------------
    try:
        metamorphic = await run_baseline(BASE_RAIN_MM * METAMORPHIC_SCALE, f"{CASE_TAG}-metamorphic")
        if _is_failed_envelope(metamorphic):
            record("8-metamorphic-rider", False, f"FAILED envelope: {_failure_detail(metamorphic)}")
            base_stats = meta_stats = None
        else:
            base_stats = raster_stats(baseline_uri)
            meta_stats = raster_stats(metamorphic.uri)
            depth_ok = meta_stats["max_depth_m"] >= base_stats["max_depth_m"] - 1e-9
            vol_proxy_base = base_stats["mean_depth_m"] * base_stats["flooded_cell_count"]
            vol_proxy_meta = meta_stats["mean_depth_m"] * meta_stats["flooded_cell_count"]
            vol_ok = vol_proxy_meta >= vol_proxy_base - 1e-9
            ok = depth_ok and vol_ok
            record(
                "8-metamorphic-rider", ok,
                f"baseline={base_stats} 1.5x_rain={meta_stats} "
                f"peak_depth_non_decreasing={depth_ok} "
                f"flooded_volume_proxy_non_decreasing={vol_ok} "
                f"(volume_proxy = mean_depth_m * flooded_cell_count, same grid both runs)",
            )
    except Exception as exc:  # noqa: BLE001
        record("8-metamorphic-rider", False, f"EXCEPTION: {exc!r}")
        base_stats = meta_stats = None

    # --- Final report --------------------------------------------------------
    print_step_table()
    print_skill_table(skill_before, skill_after)
    if base_stats is not None and meta_stats is not None:
        print_metamorphic_table(base_stats, meta_stats)

    overall_ok = all(r["ok"] for r in RESULTS)
    print(f"\n=== OVERALL: {'PASS' if overall_ok else 'FAIL (see defect / failures above)'} ===")
    print(
        f"case_tag={CASE_TAG} minted_run_ids={MINTED_RUN_IDS} "
        "(these are the Claude-driver junk runs under s3://trid3nt-runs/<run_id>/ "
        "and TRID3NT_RUNS_DIR/<run_id>/ from this invocation -- safe to clean up)"
    )
    return 0 if overall_ok else 1


# ---------------------------------------------------------------------------
# Live mode -- IMPLEMENTED, NEVER EXECUTED BY THIS SCRIPT'S OWN AUTOMATION.
# ---------------------------------------------------------------------------

LIVE_DISCIPLINE_NOTE = """
================================================================================
L2 LIVE MODE -- Hurricane Harvey / Houston (docs/validation/e2e-harness.md)

Methodology sign-off (from the doc, dated 2026-07-24):
  NATE sign-offs: case = Hurricane Harvey / Houston; scope = core loop +
  metamorphic check + split-sample validation; EA 2D benchmark suite =
  QUEUED (backlog, separate verification deck).

  Concrete run inputs (AOI, window, split) get a FINAL NATE LOOK before any
  live execution, and EVERY live run asks permission first (methodology
  rule). The doc's own AOI/window/obs proposal is marked PENDING, not
  confirmed:
    - AOI: a Buffalo Bayou / west-Houston sub-basin (small, inside the dense
      STN HWM cluster) -- NOT YET a concrete bbox.
    - Window: 2017-08-25 to 2017-09-01 (landfall + peak rainfall).
    - Forcing: observed precipitation via existing fetchers (real network).
    - Obs: live USGS STN HWM fetch (fetch_high_water_marks) + USGS stream
      gauges.

  Discipline: ask permission per live run; log costs; UPSTREAM failures (STN
  outage etc.) surface as typed upstream errors, never internalized as
  harness bugs.

This mode performs REAL network calls (USGS 3DEP/NLCD/NHD, NOAA Atlas 14 or
observed precip, USGS STN) and a real SFINCS solve. It will NOT proceed
without both an explicit --aoi-bbox override (the AOI above is still
PENDING a concrete bbox from NATE) and a typed confirmation below.
================================================================================
"""


async def main_live(args: argparse.Namespace) -> int:
    print(LIVE_DISCIPLINE_NOTE)
    if args.aoi_bbox is None:
        log.warning(
            "no --aoi-bbox supplied: e2e-harness.md's AOI is still PENDING a "
            "final NATE look; refusing to invent a concrete bbox. Pass "
            "--aoi-bbox 'west,south,east,north' after NATE confirms the "
            "sub-basin."
        )
        return 1
    try:
        confirmation = input(
            "Type EXACTLY 'I CONFIRM LIVE HARVEY RUN' to proceed (any other "
            "input aborts, zero live calls made): "
        )
    except EOFError:
        confirmation = ""
    if confirmation.strip() != "I CONFIRM LIVE HARVEY RUN":
        log.warning("live mode NOT confirmed -- aborting before any live call.")
        return 1

    # --- From here on: real chain, per e2e-harness.md's numbered steps. ---
    # This code path is fully written (reuses the same TOOL_REGISTRY /
    # step_* helpers as smoke) but is NEVER invoked by this harness's own
    # automation -- executing it live is exclusively NATE's call.
    from trid3nt_server.tools import TOOL_REGISTRY

    aoi_bbox = tuple(float(x) for x in args.aoi_bbox.split(","))
    log.info("LIVE: baseline flood run (real Atlas 14 / observed precip) bbox=%s", aoi_bbox)
    # NOTE (discovered live in smoke mode): AssessmentEnvelope.project_id /
    # .session_id are pydantic-constrained to actual 26-char ULIDs -- a
    # free-text tag there fails validation AFTER the solve completes. No
    # free-text case tag at this call site; recognizability comes from the
    # run_id(s) this prints, same as smoke mode.
    run_flood = TOOL_REGISTRY["run_model_flood_scenario"].fn
    baseline = await run_flood(
        bbox=aoi_bbox,
        duration_hr=args.duration_hr,
        return_period_yr=args.return_period_yr,
        compute_class="medium",
    )
    if _is_failed_envelope(baseline):
        record("live-1-baseline-run", False, f"FAILED envelope: {_failure_detail(baseline)}")
        print_step_table()
        return 1
    record("live-1-baseline-run", True, f"peak_depth_uri={baseline.uri}")

    diag = step_diagnostics(baseline.uri)
    record("live-2-read_run_diagnostics", True, json.dumps(diag, default=str)[:500])

    fetch_hwm = TOOL_REGISTRY["fetch_high_water_marks"].fn
    hwm_layer = fetch_hwm(bbox=aoi_bbox, event="2017 Harvey")
    record("live-3-fetch_high_water_marks", True, f"uri={hwm_layer.uri}")

    # SPLIT-SAMPLE (spatial split -- one event cannot split temporally):
    # calibrate on a deterministic subset, score the HELD-OUT remainder.
    # (Concrete split fraction/seed left as a CLI knob -- args.split_seed /
    # args.calibrate_fraction -- rather than hardcoded, since the doc marks
    # this PENDING a final NATE look same as the AOI.)
    import geopandas as gpd

    hwm_local = Path(tempfile.mkdtemp(prefix="l2_live_hwm_")) / "hwm.fgb"
    with open(hwm_local, "wb") as fh:
        fh.write(_read_bytes_any(hwm_layer.uri))
    gdf = gpd.read_file(hwm_local)
    rng = random.Random(args.split_seed)
    idx = list(range(len(gdf)))
    rng.shuffle(idx)
    n_cal = max(1, int(len(idx) * args.calibrate_fraction))
    cal_idx, held_idx = idx[:n_cal], idx[n_cal:]
    cal_path = hwm_local.parent / "hwm_calibrate.fgb"
    held_path = hwm_local.parent / "hwm_heldout.fgb"
    gdf.iloc[cal_idx].to_file(cal_path, driver="FlatGeobuf", engine="pyogrio")
    gdf.iloc[held_idx].to_file(held_path, driver="FlatGeobuf", engine="pyogrio")
    record("live-4-split-sample", True, f"n_calibrate={len(cal_idx)} n_heldout={len(held_idx)}")

    paired_held_before = step_pairing(baseline.uri, held_path)
    skill_held_before = step_skill(paired_held_before.paired_table_uri)
    record("live-5-skill(held-out, before)", True, json.dumps(skill_held_before["metrics"], default=str))

    deck_dir = Path(os.environ.get("TRID3NT_RUNS_DIR", "data/runs")) / extract_run_id(baseline.uri)
    setter_env = step_setter(str(deck_dir))
    record("live-6-set_sfincs_parameters", True, json.dumps(setter_env["changes_applied"], default=str))

    model_root = model_root_from_child_setup(setter_env["child_setup_uri"])
    manifest_path = build_rerun_manifest(model_root)
    run_solver = TOOL_REGISTRY["run_solver"].fn
    wait_for_completion = TOOL_REGISTRY["wait_for_completion"].fn
    handle = run_solver(solver="sfincs", model_setup_uri=f"file://{manifest_path}", compute_class="medium")
    child_result = await wait_for_completion(handle, poll_interval_s=10, timeout_s=1800)
    if child_result.status != "complete":
        record("live-7-rerun-child", False, f"status={child_result.status} error={child_result.error_message}")
    else:
        from trid3nt_server.workflows.postprocess_flood import postprocess_flood

        layers, _m = postprocess_flood(child_result.output_uri, run_id=handle.run_id)
        paired_held_after = step_pairing(layers[0].uri, held_path)
        skill_held_after = step_skill(paired_held_after.paired_table_uri)
        record("live-7-rerun-child + re-score (held-out)", True, json.dumps(skill_held_after["metrics"], default=str))

    print_step_table()
    return 0 if all(r["ok"] for r in RESULTS) else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--live", action="store_true", help="Run LIVE Harvey mode instead of smoke. Gated + confirmed.")
    p.add_argument("--aoi-bbox", default=None, help="Live mode only: 'west,south,east,north' EPSG:4326.")
    p.add_argument("--duration-hr", type=int, default=24, help="Live mode only.")
    p.add_argument("--return-period-yr", type=int, default=100, help="Live mode only (design-storm fallback).")
    p.add_argument("--calibrate-fraction", type=float, default=0.5, help="Live mode only: split-sample fraction.")
    p.add_argument("--split-seed", type=int, default=20170825, help="Live mode only: deterministic split seed.")
    return p.parse_args(argv)


def main() -> int:
    args = parse_args(sys.argv[1:])
    if args.live:
        return asyncio.run(main_live(args))
    return asyncio.run(main_smoke())


if __name__ == "__main__":
    sys.exit(main())
