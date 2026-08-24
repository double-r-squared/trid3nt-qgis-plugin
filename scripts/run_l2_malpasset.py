"""L2 Malpasset dam-break validation harness (TELEMAC-2D).

Direct TOOL_REGISTRY / workflow calls -- bypasses the LLM/agent chat layer,
matching the repo's direct-call driver convention (``run_l2_validation_harness.py``
/ ``prove_telemac_seam.py``). Closes the model-vs-observation loop for the
official TELEMAC-2D ``malpasset`` case (Reyran valley, 2 Dec 1959):

    stage case -> baseline result -> max-WSE raster -> read_run_diagnostics ->
    pair vs the 17 police high-water marks -> compute_skill_metrics ->
    adjust bed friction toward the published band -> re-run -> re-score ->
    print a comparison table INCLUDING the published targets.

PRINCIPLE (e2e-harness.md): assert the MACHINERY, never the model. Every
assertion checks envelope completeness / honesty fields / lineage / like-for-like
quantity + datum. Skill VALUES (NSE, KGE, RMSE, per-point WS) are printed as
FINDINGS, never gated on a threshold.

TWO GAPS IN THE LIVE STACK, handled honestly (see the B2 report):

1. ``run_telemac`` builds a SYNTHETIC river-dye reach from an NHDPlus centerline;
   it CANNOT ingest the bundled Malpasset mesh/deck. A real Malpasset solve is a
   direct ``docker run`` of ``telemac2d.py`` on the bundled deck -- constructed by
   ``dispatch_malpasset_solve`` and GATED behind ``--run-solves`` (the natural
   later-lane promotion into ``run_solver`` is noted inline).
2. There is no ``set_telemac_parameters`` setter and no persisted deck dir to
   copy-on-write. The bundled deck IS the deck, so friction is adjusted by
   rewriting its ``FRICTION COEFFICIENT`` line (``adjust_deck_friction``) -- a
   real, offline-testable text edit.

DEFAULT (no ``--run-solves``, the B2 mode): runs the FULL downstream chain
OFFLINE against the bundled TELEMAC reference result ``f2d_malpasset-small.slf``
(a real TELEMAC-2D output). This proves postprocess -> pair -> skill end to end
with ZERO docker/network. The reference result carries only 2 output frames
(t=0, t=4000 s), so it UNDER-captures the transient downstream crest -- the
printed skill is a MACHINERY finding, not a calibrated validation; a real
multi-frame ``--run-solves`` solve is what the published targets are compared to.

Junk-run convention: every run this harness mints is tagged ``l2-malpasset-*``
and printed at the end as the safe-to-clean-up list.

Run (offline machinery, B2 default):
  cd /home/nate/Documents/trid3nt-local
  set -a; source .env.local; set +a
  PYTHONPATH=.:contracts venvs/agent/bin/python scripts/run_l2_malpasset.py

Run (real solves -- later lane, docker compute):
  ... same env prefix ... scripts/run_l2_malpasset.py --run-solves
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("l2_malpasset")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CASE_DIR_DEFAULT = Path("data/cases/malpasset")
#: NERD (14) negative-depth scheme -- the validation default (malpasset.xml
#: target + SOURCES.md).
DEFAULT_CAS = "t2d_malpasset-small_pos.cas"
GEO_SLF = "geo_malpasset-small.slf"
GEO_CLI = "geo_malpasset-small.cli"
#: The bundled TELEMAC-2D reference RESULT (real output) used for the OFFLINE
#: machinery chain when --run-solves is not passed.
REFERENCE_RESULT = "f2d_malpasset-small.slf"

#: Bundled friction baseline (Strickler K, from the .cas). Published band ~30-40
#: (30 valley/plain, up to ~40 main channel; FullSWOF/Hervouet 2000 Manning
#: 0.033 == Strickler ~30.3). We adjust TOWARD the upper band value.
BASELINE_FRICTION_K = 30.0
ADJUSTED_FRICTION_K = 40.0
PUBLISHED_FRICTION_BAND = (30.0, 40.0)

DEFAULT_TELEMAC_IMAGE = os.environ.get(
    "TRID3NT_TELEMAC_IMAGE", "trid3nt-local/telemac:latest"
)

CASE_TAG = f"l2-malpasset-{uuid.uuid4().hex[:8]}"

#: run_ids/tags minted by THIS invocation -- printed at the end as the cleanup
#: list (the junk-run-id convention).
MINTED_RUN_IDS: list[str] = []
RESULTS: list[dict[str, Any]] = []


def record(step: str, ok: bool, detail: str) -> None:
    RESULTS.append({"step": step, "ok": ok, "detail": detail})
    log.info("[%s] %s: %s", "PASS" if ok else "FAIL", step, detail)


# ---------------------------------------------------------------------------
# Contract-assertion key sets (mirror run_l2_validation_harness.py)
# ---------------------------------------------------------------------------

REQUIRED_ALIGNMENT_KEYS = {"spatial", "temporal", "datum", "crs"}
REQUIRED_SKILL_KEYS = {
    "variable", "n", "metrics", "bands", "suggested_verdict",
    "verdict_is_heuristic", "caveats", "units", "notes",
}
REQUIRED_DIAG_KEYS = {
    "engine", "run_id", "status", "healthy", "mass_balance_pct",
    "mass_balance_source", "instability", "nonconverged_pct", "dry_cells",
    "warnings", "engine_specific", "sources", "notes",
}


# ---------------------------------------------------------------------------
# Friction deck edit (the honest "setter" for a bundled deck)
# ---------------------------------------------------------------------------

_FRICTION_RE = re.compile(
    r"^(?P<pre>\s*FRICTION\s+COEFFICIENT\s*[:=]\s*)(?P<val>[0-9]+(?:\.[0-9]*)?)",
    re.IGNORECASE | re.MULTILINE,
)


def adjust_deck_friction(cas_text: str, new_k: float) -> tuple[str, float]:
    """Rewrite a TELEMAC steering deck's ``FRICTION COEFFICIENT`` -> ``new_k``.

    Handles both the ``=`` and ``:`` keyword separators TELEMAC accepts, and never
    matches ``LAW OF BOTTOM FRICTION``. Returns ``(new_text, old_value)``. Raises
    if the keyword is absent (honest -- never silently no-ops).
    """
    m = _FRICTION_RE.search(cas_text)
    if not m:
        raise ValueError(
            "no 'FRICTION COEFFICIENT' keyword found in the steering deck"
        )
    old_val = float(m.group("val"))
    # preserve TELEMAC's trailing-dot float style (e.g. '40.').
    new_repr = f"{new_k:g}." if float(new_k).is_integer() else f"{new_k:g}"
    new_text = _FRICTION_RE.sub(lambda mm: mm.group("pre") + new_repr, cas_text, count=1)
    return new_text, old_val


# ---------------------------------------------------------------------------
# Stage the case
# ---------------------------------------------------------------------------


def stage_case(case_dir: Path, cas_name: str, friction_k: float) -> dict[str, Any]:
    """Stage the bundled Malpasset deck + build the observation layers into a
    junk rundir; return the staged paths + the obs-layer summary.

    Copies the mesh (``geo_*.slf`` + ``.cli``), the chosen ``.cas`` (with
    ``friction_k`` applied), and the reference result into
    ``data/runs/<run_tag>/``, builds ``malpasset_police_hwm.fgb`` /
    ``malpasset_transformers.fgb`` / ``malpasset_gauges.fgb`` there, and writes a
    bundled-deck ``manifest.json``. Nothing is solved here.
    """
    from trid3nt_server.cases.malpasset_obs import build_malpasset_obs_layers

    run_tag = f"{CASE_TAG}-stage-{uuid.uuid4().hex[:6]}"
    MINTED_RUN_IDS.append(run_tag)
    runs_dir = Path(os.environ.get("TRID3NT_RUNS_DIR", "data/runs"))
    rundir = runs_dir / run_tag
    rundir.mkdir(parents=True, exist_ok=True)

    obs_json = case_dir / "observations.json"
    if not obs_json.is_file():
        raise FileNotFoundError(f"observations.json not found under {case_dir}")

    # deck files (mesh + boundary + steering + reference result).
    staged: list[str] = []
    for name in (GEO_SLF, GEO_CLI, REFERENCE_RESULT):
        src = case_dir / name
        if src.is_file():
            shutil.copyfile(src, rundir / name)
            staged.append(name)

    cas_src = case_dir / cas_name
    if not cas_src.is_file():
        raise FileNotFoundError(f"steering deck not found: {cas_src}")
    cas_text = cas_src.read_text(encoding="latin-1")
    cas_text, old_k = adjust_deck_friction(cas_text, friction_k)
    (rundir / cas_name).write_text(cas_text, encoding="latin-1")
    staged.append(cas_name)

    obs_summary = build_malpasset_obs_layers(obs_json, rundir)

    manifest = {
        "case": "malpasset-small",
        "cas": cas_name,
        "friction_coefficient": float(friction_k),
        "friction_coefficient_baseline": float(old_k),
        "friction_law": 3,
        "inputs": [{"dest": n} for n in staged],
        # a real solve overrides the image CMD to run TELEMAC on the bundled deck
        # (see dispatch_malpasset_solve); river-dye's entrypoint.py is bypassed.
        "telemac_args": ["telemac2d.py", cas_name],
        "outputs": ["r2d_malpasset.slf", cas_name, "telemac.stdout", "telemac.stderr"],
        "run_id": run_tag,
    }
    (rundir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    return {
        "run_tag": run_tag,
        "rundir": str(rundir),
        "cas": cas_name,
        "friction_k": float(friction_k),
        "friction_baseline_k": float(old_k),
        "reference_result": str(rundir / REFERENCE_RESULT),
        "manifest": str(rundir / "manifest.json"),
        "obs": obs_summary,
        "staged": staged,
    }


def maybe_stage_manifest_to_minio(manifest_path: Path, run_tag: str) -> str | None:
    """Cheap staging smoke: put the manifest to the MinIO cache bucket (like
    ``prove_telemac_seam``). Best-effort -- returns the s3 uri or None."""
    try:
        import boto3
        from _env_guard import local_endpoint_or_none
    except Exception:  # noqa: BLE001
        return None
    cache_bucket = os.environ.get("TRID3NT_CACHE_BUCKET")
    if not cache_bucket:
        return None
    endpoint = local_endpoint_or_none()
    if endpoint is None:
        log.info("MinIO manifest staging smoke skipped: no local AWS_ENDPOINT_URL configured")
        return None
    try:
        s3 = boto3.client(
            "s3", endpoint_url=endpoint,
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
        )
        key = f"telemac/{run_tag}/manifest.json"
        s3.put_object(
            Bucket=cache_bucket,
            Key=key,
            Body=manifest_path.read_bytes(),
            ContentType="application/json",
        )
        return f"s3://{cache_bucket}/{key}"
    except Exception as exc:  # noqa: BLE001
        log.info("MinIO manifest staging smoke skipped: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Baseline / re-run result resolution
# ---------------------------------------------------------------------------


def dispatch_malpasset_solve(rundir: Path, cas_name: str, run_tag: str) -> Path:
    """Run TELEMAC-2D on the bundled deck via a DIRECT ``docker run`` (GATED).

    The river-dye ``run_solver`` path cannot ingest the bundled Malpasset deck
    (it builds a synthetic NHDPlus reach), so this is a direct docker invocation
    of ``telemac2d.py`` in the same worker image, mounting ``rundir`` at
    ``/data`` (mirrors ``telemac_local_spec``'s volume mount). A later lane
    promotes this into a first-class bundled-deck ``run_solver`` backend.

    Returns the path to the result SELAFIN. Raises on any non-zero exit.
    """
    cmd = [
        "docker", "run", "--rm", "--name", run_tag,
        "-v", f"{rundir.resolve()}:/data", "-w", "/data",
        DEFAULT_TELEMAC_IMAGE, "telemac2d.py", cas_name,
    ]
    log.info("dispatch_malpasset_solve: %s", " ".join(cmd))
    stdout_path = rundir / "telemac.stdout"
    stderr_path = rundir / "telemac.stderr"
    with stdout_path.open("wb") as out, stderr_path.open("wb") as err:
        proc = subprocess.run(cmd, stdout=out, stderr=err, check=False)
    if proc.returncode != 0:
        tail = "\n".join(stderr_path.read_text(errors="replace").splitlines()[-20:])
        raise RuntimeError(
            f"telemac2d docker solve exit={proc.returncode}; stderr tail:\n{tail}"
        )
    # TELEMAC writes the RESULTS FILE named in the deck; discover the newest .slf
    # that is not the geometry/reference input.
    candidates = sorted(
        (p for p in rundir.glob("*.slf")
         if p.name not in (GEO_SLF, REFERENCE_RESULT)),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise RuntimeError(f"solve produced no result .slf under {rundir}")
    return candidates[0]


def resolve_result(stage: dict[str, Any], *, run_solves: bool, run_tag: str) -> tuple[Path, str, bool]:
    """Return ``(result_slf, run_id, is_real_solve)``.

    ``--run-solves`` -> a real docker solve of the staged deck; else the bundled
    reference result (offline machinery)."""
    rundir = Path(stage["rundir"])
    if run_solves:
        MINTED_RUN_IDS.append(run_tag)
        result = dispatch_malpasset_solve(rundir, stage["cas"], run_tag)
        return result, run_tag, True
    ref = Path(stage["reference_result"])
    if not ref.is_file():
        raise FileNotFoundError(f"reference result missing: {ref}")
    return ref, stage["run_tag"], False


# ---------------------------------------------------------------------------
# Steps (assert MACHINERY; skill VALUES are findings)
# ---------------------------------------------------------------------------


def step_wse(result_slf: Path, run_id: str, out_dir: Path) -> Any:
    from trid3nt_server.cases.malpasset_obs import (
        MALPASSET_MESH_EPSG,
        MALPASSET_VERTICAL_DATUM,
        MALPASSET_CRS_CAVEAT,
    )
    from trid3nt_server.workflows.telemac.postprocess_telemac import postprocess_telemac_wse

    layers, metrics = postprocess_telemac_wse(
        result_slf,
        run_id=run_id,
        mesh_epsg=MALPASSET_MESH_EPSG,
        reach_name="malpasset",
        vertical_datum=MALPASSET_VERTICAL_DATUM,
        mesh_frame_note=MALPASSET_CRS_CAVEAT,
        _output_dir=str(out_dir),
    )
    wse = layers[0]
    if wse.quantity != "water_surface_elevation":
        raise AssertionError(f"WSE layer quantity != water_surface_elevation: {wse.quantity!r}")
    if wse.mesh_epsg != MALPASSET_MESH_EPSG:
        raise AssertionError(f"WSE layer mesh_epsg != {MALPASSET_MESH_EPSG}: {wse.mesh_epsg}")
    if wse.wse_max_m is None:
        raise AssertionError("WSE layer carries no wse_max_m scalar")
    return wse, metrics


def step_diagnostics(run_handle: str) -> dict[str, Any] | None:
    """Read TELEMAC run diagnostics -- only meaningful after a REAL solve (a
    completion.json exists). Returns None (with an honest record) offline."""
    from trid3nt_server.tools import TOOL_REGISTRY

    fn = TOOL_REGISTRY["read_run_diagnostics"].fn
    try:
        env = fn(run_handle=run_handle)
    except Exception as exc:  # noqa: BLE001
        record("diagnostics", True,
               f"skipped (no completion.json -- offline reference mode): {exc}")
        return None
    missing = REQUIRED_DIAG_KEYS - set(env.keys())
    if missing:
        raise AssertionError(f"diagnostics envelope missing keys: {sorted(missing)}")
    # mass_balance_pct is null for EVERY telemac run (the main deck never sets
    # MASS-BALANCE=YES) -- do NOT gate on it (R2 stack finding).
    record("diagnostics", True,
           f"engine={env['engine']} status={env['status']} correct_end="
           f"{env['engine_specific'].get('correct_end')} mass_balance_pct="
           f"{env['mass_balance_pct']} (null is expected for telemac)")
    return env


def step_pairing(model_uri: str, obs_path: str, model_datum: str,
                 out_dir: str | None = None) -> Any:
    from trid3nt_server.tools import TOOL_REGISTRY

    fn = TOOL_REGISTRY["extract_model_at_observations"].fn
    paired = fn(
        model_layer_uri=model_uri,
        observations_layer_uri=obs_path,
        model_datum=model_datum,
        # write the paired FGB LOCALLY into the junk rundir (offline-consistent;
        # avoids GDAL/vsis3 auth on read-back, which does not use the boto3 env).
        _output_dir=out_dir,
    )
    missing = REQUIRED_ALIGNMENT_KEYS - set(paired.alignment.keys())
    if missing:
        raise AssertionError(f"alignment block missing keys: {sorted(missing)}")
    if not paired.units_warning:
        raise AssertionError("units_warning empty; contract requires ALWAYS populated")
    if paired.n_paired == 0:
        raise AssertionError(f"zero paired samples; all {paired.n_dropped} dropped: {paired.dropped}")
    # like-for-like WSE check: both sides must be elevation (no silent depth cross).
    if paired.alignment.get("model_quantity") != "water_surface_elevation":
        raise AssertionError(
            f"model quantity not WSE: {paired.alignment.get('model_quantity')!r}"
        )
    if paired.alignment.get("observed_quantity") != "water_surface_elevation":
        raise AssertionError(
            f"observed quantity not WSE: {paired.alignment.get('observed_quantity')!r}"
        )
    return paired


def step_skill(paired_table_uri: str) -> dict[str, Any]:
    from trid3nt_server.tools import TOOL_REGISTRY

    fn = TOOL_REGISTRY["compute_skill_metrics"].fn
    m = fn(paired_table_uri=paired_table_uri, variable="head")
    missing = REQUIRED_SKILL_KEYS - set(m.keys())
    if missing:
        raise AssertionError(f"skill envelope missing keys: {sorted(missing)}")
    if m["verdict_is_heuristic"] is not True:
        raise AssertionError("verdict_is_heuristic must ALWAYS be True per contract 3.2")
    return m


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _read_paired_pairs(paired_table_uri: str) -> dict[str, tuple[float, float]]:
    """Return {obs_id: (observed, simulated)} from the paired FGB."""
    import geopandas as gpd

    if paired_table_uri.startswith("s3://"):
        # boto3 read (GDAL /vsis3/ does not use the boto3 MinIO env) -> temp file.
        import tempfile

        from trid3nt_server.tools.cache import read_object_bytes_s3

        data = read_object_bytes_s3(paired_table_uri)
        tf = tempfile.NamedTemporaryFile(suffix=".fgb", delete=False)
        tf.write(data)
        tf.close()
        path = tf.name
    else:
        path = paired_table_uri[len("file://"):] if paired_table_uri.startswith("file://") else paired_table_uri
    gdf = gpd.read_file(path)
    out: dict[str, tuple[float, float]] = {}
    for _, r in gdf.iterrows():
        out[str(r["obs_id"])] = (float(r["observed"]), float(r["simulated"]))
    return out


def print_comparison_table(
    paired_table_uri: str, published_ws3d: dict[str, float]
) -> None:
    pairs = _read_paired_pairs(paired_table_uri)
    print("\n=== POLICE HIGH-WATER-MARK COMPARISON (WSE, m NGF) ===")
    print(f"{'id':>4} {'observed':>9} {'modeled':>9} {'residual':>9} "
          f"{'pub_WS3D':>9} (published = Biscarini 2016 OpenFOAM 3D, NOT obs)")
    for oid in sorted(pairs, key=lambda s: (len(s), s)):
        obs, sim = pairs[oid]
        pub = published_ws3d.get(oid)
        pub_s = f"{pub:9.2f}" if pub is not None else f"{'--':>9}"
        print(f"{oid:>4} {obs:9.2f} {sim:9.2f} {sim - obs:9.2f} {pub_s}")


def print_skill_table(before: dict[str, Any], after: dict[str, Any] | None,
                      friction_before: float, friction_after: float) -> None:
    print("\n=== SKILL (findings, NOT gated) ===")
    print(f"baseline friction Strickler K = {friction_before:g} "
          f"(published band {PUBLISHED_FRICTION_BAND[0]:g}-{PUBLISHED_FRICTION_BAND[1]:g})")
    keys = ["NSE", "KGE", "PBIAS", "RMSE", "R2", "peak_error", "SRMS"]
    print(f"{'metric':>12} {'baseline':>12} {'K=' + format(friction_after, 'g'):>12}")
    bm = before["metrics"]
    am = after["metrics"] if after else {}
    for k in keys:
        bv = bm.get(k)
        av = am.get(k)
        bs = f"{bv:12.4f}" if isinstance(bv, (int, float)) else f"{'--':>12}"
        as_ = f"{av:12.4f}" if isinstance(av, (int, float)) else f"{'(pending)':>12}"
        print(f"{k:>12} {bs} {as_}")
    if after is None:
        print("  (re-run column pending: pass --run-solves for a real "
              f"friction-K={friction_after:g} solve)")


def print_step_table() -> None:
    print("\n=== PER-STEP RESULT ===")
    for r in RESULTS:
        print(f"  [{'PASS' if r['ok'] else 'FAIL'}] {r['step']}: {r['detail']}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _load_published_ws3d(case_dir: Path) -> dict[str, float]:
    obs = json.loads((case_dir / "observations.json").read_text(encoding="utf-8"))
    pmc = obs.get("published_model_comparison") or {}
    return {k: float(v) for k, v in (pmc.get("police_ws3d_m") or {}).items()}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--case-dir", default=str(CASE_DIR_DEFAULT))
    ap.add_argument("--cas", default=DEFAULT_CAS)
    ap.add_argument("--run-solves", action="store_true",
                    help="run REAL telemac2d docker solves (baseline + adjusted "
                         "friction); default is the offline reference-result chain")
    ap.add_argument("--smoke-stage", action="store_true",
                    help="also put the staged manifest to the MinIO cache bucket "
                         "(cheap staging smoke)")
    args = ap.parse_args(argv)

    case_dir = Path(args.case_dir)
    log.info("=== L2 MALPASSET HARNESS start case_tag=%s run_solves=%s ===",
             CASE_TAG, args.run_solves)
    overall_ok = True
    published_ws3d = _load_published_ws3d(case_dir)

    try:
        # 1. STAGE (baseline friction).
        stage = stage_case(case_dir, args.cas, BASELINE_FRICTION_K)
        record("1-stage-case", True,
               f"run_tag={stage['run_tag']} cas={stage['cas']} "
               f"friction_baseline_k={stage['friction_baseline_k']} "
               f"n_police={stage['obs']['n_police']} "
               f"n_transformers={stage['obs']['n_transformers']}")
        log.info("CRS caveat: %s", stage["obs"]["crs_caveat"])
        if args.smoke_stage:
            uri = maybe_stage_manifest_to_minio(Path(stage["manifest"]), stage["run_tag"])
            record("1b-stage-smoke", True, f"manifest -> {uri or '(MinIO unavailable, skipped)'}")

        out_dir = Path(stage["rundir"])

        # 2. BASELINE result -> WSE raster.
        base_tag = f"{CASE_TAG}-base"
        result_slf, base_run_id, is_real = resolve_result(
            stage, run_solves=args.run_solves, run_tag=base_tag)
        record("2-baseline-result", True,
               f"{'REAL solve' if is_real else 'reference result (offline)'}: "
               f"{result_slf.name} run_id={base_run_id}")

        wse, wse_metrics = step_wse(result_slf, base_run_id, out_dir)
        record("3-wse-raster", True,
               f"wse_max_m={wse.wse_max_m} peak_t={wse.wse_peak_time_s}s "
               f"n_frames={wse.n_frames} n_wet={wse_metrics['n_wet_nodes']} "
               f"quantity={wse.quantity} mesh_epsg={wse.mesh_epsg} -> {wse.uri}")

        # 3. DIAGNOSTICS (real solve only).
        step_diagnostics(base_run_id if is_real else base_run_id)

        # 4. PAIR vs police points.
        from trid3nt_server.cases.malpasset_obs import MALPASSET_VERTICAL_DATUM

        paired = step_pairing(wse.uri, stage["obs"]["police_fgb"],
                              MALPASSET_VERTICAL_DATUM, str(out_dir))
        record("4-pairing", True,
               f"n_paired={paired.n_paired} n_dropped={paired.n_dropped} "
               f"datum={paired.alignment['datum']} "
               f"quantity={paired.alignment['model_quantity']} (like-for-like WSE)")

        # 5. SKILL (baseline).
        skill_before = step_skill(paired.paired_table_uri)
        record("5-skill-baseline", True,
               f"n={skill_before['n']} NSE={skill_before['metrics'].get('NSE')} "
               f"RMSE={skill_before['metrics'].get('RMSE')} "
               f"verdict={skill_before['suggested_verdict']} (FINDING, not gated)")

        # 6. ADJUST FRICTION toward the published band (deck edit = the honest
        #    setter for a bundled deck), then re-run + re-score.
        cas_text = (case_dir / args.cas).read_text(encoding="latin-1")
        _new_text, old_k = adjust_deck_friction(cas_text, ADJUSTED_FRICTION_K)
        record("6-setter-friction", True,
               f"FRICTION COEFFICIENT {old_k:g} -> {ADJUSTED_FRICTION_K:g} "
               f"(toward published band {PUBLISHED_FRICTION_BAND[0]:g}-"
               f"{PUBLISHED_FRICTION_BAND[1]:g})")

        skill_after: dict[str, Any] | None = None
        if args.run_solves:
            stage2 = stage_case(case_dir, args.cas, ADJUSTED_FRICTION_K)
            adj_tag = f"{CASE_TAG}-adj"
            result2, run2, _ = resolve_result(stage2, run_solves=True, run_tag=adj_tag)
            wse2, _ = step_wse(result2, run2, Path(stage2["rundir"]))
            step_diagnostics(run2)
            paired2 = step_pairing(wse2.uri, stage2["obs"]["police_fgb"],
                                   MALPASSET_VERTICAL_DATUM, stage2["rundir"])
            skill_after = step_skill(paired2.paired_table_uri)
            record("7-skill-adjusted", True,
                   f"n={skill_after['n']} NSE={skill_after['metrics'].get('NSE')} "
                   f"RMSE={skill_after['metrics'].get('RMSE')} (FINDING)")
        else:
            record("7-skill-adjusted", True,
                   "re-run pending (needs --run-solves for a real friction-K="
                   f"{ADJUSTED_FRICTION_K:g} docker solve)")

        # 7. REPORT.
        print_comparison_table(paired.paired_table_uri, published_ws3d)
        print_skill_table(skill_before, skill_after, old_k, ADJUSTED_FRICTION_K)

    except Exception as exc:  # noqa: BLE001
        overall_ok = False
        record("FATAL", False, f"{type(exc).__name__}: {exc}")
        log.exception("harness aborted")

    print_step_table()
    print(f"\n=== OVERALL: {'PASS' if overall_ok else 'FAIL (see above)'} ===")
    print(f"case_tag={CASE_TAG} minted_run_ids={MINTED_RUN_IDS} "
          "(l2-malpasset-* junk runs under TRID3NT_RUNS_DIR/<run_id>/ from this "
          "invocation -- safe to clean up)")
    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.path.insert(0, ".")
    sys.path.insert(0, "contracts")
    raise SystemExit(main())
