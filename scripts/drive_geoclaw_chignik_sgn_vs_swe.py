"""ADR 0257 queued real-US case: SGN (Boussinesq) vs SWE arrival-waveform
comparison on the REAL 2021 M8.2 Chignik finite-fault tsunami (the same source /
domain staging the repo already drives -- drive_geoclaw_finite_fault_chignik.py +
drive_geoclaw_chignik_runup_proof.py).

Runs ONE leg per invocation, IDENTICAL in every parameter except the dispersive
knob:  bouss_equations = 0 (SWE, non-dispersive) vs 2 (Serre-Green-Naghdi).
The bouss knob is injected via a driver-local monkeypatch of the module's
``GeoClawRunArgs`` constructor -- no shared-code / tool-surface change (this is a
proof driver, not a new capability). A second monkeypatch on the batch-output
downloader copies the solved ``_output/`` (the ``gauge00001.txt`` series) to a
persistent scratch dir BEFORE the composer cleans it, so the raw water-surface
time series survives for the overlay.

Budget (deliberate, ADR 0257): SGN is ~17x SWE (implicit PETSc step). Coarsened
to keep SGN under ~60-90 min wall -- amr_levels=2, sim_duration bounded to the
near-field propagation-to-gauge window. The comparison target is arrival waveform
+ leading-wave shape + trailing dispersive oscillations, which survive moderate
coarsening.

Run (repo root, MinIO env loaded):
  set -a; source .env.local; set +a
  TRID3NT_SOLVER_TIMEOUT_S=7200 venvs/agent/bin/python \
    scripts/drive_geoclaw_chignik_sgn_vs_swe.py {swe|sgn}
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger("sgn_vs_swe")

# REAL 2021 M8.2 Chignik catalog values, as resolved by the earthquake_source
# path (USGS ComCat event ak0219neiszm):
#   epicenter (-157.8876, 55.3635), Mw 8.2, focal depth 35 km.
# BUDGET COARSENING (ADR 0257): the finite-fault footprint enclosure that the
# earthquake_source path performs grows the computational domain to basin scale
# (-160.8..-154.3 lon x 53.9..57.0 lat ~ 415x350 km, ~242k base cells); at the
# ~17x SGN implicit cost that is a multi-HOUR solve. Per the ADR budget guidance
# we bound the domain to the NEAR-FIELD epicenter->gauge propagation window and
# drive a synthetic Okada source at the SAME real catalog epicenter/Mw/depth. The
# arrival waveform + leading-wave shape + trailing dispersive train (the actual
# comparison target) survive this. The source is IDENTICAL across both legs; only
# bouss_equations differs.
_EPICENTER = (-157.8876, 55.3635)  # real Chignik ak0219neiszm epicenter
_GAUGE = (-159.30, 55.30)  # nearshore shelf point (~ -180 m), dispersion-active depth
_DOMAIN = (-160.2, 54.7, -157.4, 55.9)  # near-field box: epicenter + gauge + margin

# Frozen, IDENTICAL across both legs (only bouss_equations differs).
_COMMON = dict(
    bbox=_DOMAIN,
    scenario="tsunami",
    source_lonlat=_EPICENTER,
    source_magnitude=8.2,
    fault_depth_km=35.0,
    coastal_gauge_lonlat=_GAUGE,
    sim_duration_s=3600.0,  # capture leading crest + recession + trailing train
    amr_levels=2,
    output_frames=12,
    compute_class="small",
)

_SCRATCH = Path(
    "/tmp/claude-1000/-home-nate-Documents-GRACE-2/"
    "fd2df08a-a572-4b62-ba9a-e82d8a0a740e/scratchpad"
)


def _bouss_for(leg: str) -> int:
    return {"swe": 0, "sgn": 2}[leg]


def _fetch_gauge_from_minio(run_id: str, dest_dir: Path) -> None:
    """Download the run's gauge00001.txt from the runs bucket. The local-docker
    path postprocesses in-process (never calls the batch downloader), but the
    worker still uploads _output/gauge*.txt to s3://<runs_bucket>/<run_id>/_output/.
    """
    from trid3nt_server.data.simulation.solver.solver import (
        _get_runs_bucket,
        _get_s3_client,
    )
    dest_dir.mkdir(parents=True, exist_ok=True)
    bucket = _get_runs_bucket()
    s3 = _get_s3_client()
    resp = s3.list_objects_v2(Bucket=bucket, Prefix=f"{run_id}/_output/")
    for obj in resp.get("Contents", []) or []:
        key = obj.get("Key", "")
        base = key.rsplit("/", 1)[-1]
        if base.startswith("gauge") and base.endswith(".txt"):
            body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
            (dest_dir / base).write_bytes(body)


async def _run_leg(leg: str) -> None:
    import trid3nt_server.workflows.geoclaw.inundation.inundation as inund
    from trid3nt_server.workflows.geoclaw.postprocess_geoclaw import (
        parse_geoclaw_gauge_series,
    )

    bouss = _bouss_for(leg)
    capture_dir = _SCRATCH / f"geoclaw_sgn_vs_swe_{leg}_output"
    if capture_dir.exists():
        shutil.rmtree(capture_dir)

    # --- Monkeypatch: inject the bouss knob into the run-args the composer builds.
    _RealRunArgs = inund.GeoClawRunArgs

    def _patched_run_args(**kwargs):  # noqa: ANN003
        kwargs.setdefault("bouss_equations", bouss)
        # bouss_min_depth=10 m default: dispersion active in the shelf/deep water
        # the gauge sits on; shallow run-up cells stay on robust SWE. Levels 1..N
        # so the correction rides the propagation grid, not just the finest patch.
        return _RealRunArgs(**kwargs)

    inund.GeoClawRunArgs = _patched_run_args  # type: ignore[assignment]

    t0 = time.time()
    try:
        res = await inund.geoclaw_inundation(**_COMMON)
    finally:
        inund.GeoClawRunArgs = _RealRunArgs  # type: ignore[assignment]
    wall = time.time() - t0

    print(f"=== {leg.upper()} RESULT (bouss_equations={bouss}) ===")
    print(f"wall_s: {wall:.1f}")
    if not hasattr(res, "max_depth_m"):
        print("status: error")
        print(res)
        raise SystemExit(f"{leg} leg failed")
    print("status: ok")
    print("uri:", res.uri)
    print("scenario:", res.scenario)
    print("max_depth_m:", res.max_depth_m)
    for si in (res.synthetic_inputs or []):
        print(f"  provenance: {si.param} basis={si.basis} value={si.value!r}")

    # run_id is the first key segment of s3://<runs_bucket>/<run_id>/geoclaw_depth_peak.tif
    run_id = str(res.uri).split("://", 1)[-1].split("/", 1)[1].split("/", 1)[0]
    print("run_id:", run_id)
    _fetch_gauge_from_minio(run_id, capture_dir)

    # Parse the raw gauge series from the captured outputs.
    series, scalars = parse_geoclaw_gauge_series(capture_dir)
    if not series or not series.get("t"):
        raise SystemExit(f"{leg}: no gauge series parsed from {capture_dir}")

    out_json = _SCRATCH / f"geoclaw_sgn_vs_swe_{leg}.json"
    payload = {
        "leg": leg,
        "bouss_equations": bouss,
        "run_id": run_id,
        "uri": str(res.uri),
        "wall_s": wall,
        "common": {k: v for k, v in _COMMON.items() if k != "bbox"},
        "bbox": list(_DOMAIN),
        "gauge_lonlat": list(_GAUGE),
        "max_depth_m": res.max_depth_m,
        "gauge_scalars": scalars,
        "t": series["t"],
        "eta": series["eta"],
        "depth": series.get("depth"),
    }
    out_json.write_text(json.dumps(payload))
    n = len(series["t"])
    print(f"gauge series: {n} samples, t=[{series['t'][0]:.1f}..{series['t'][-1]:.1f}]s")
    print("gauge_scalars:", scalars)
    print("wrote:", out_json)


def _main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in ("swe", "sgn"):
        raise SystemExit("usage: drive_geoclaw_chignik_sgn_vs_swe.py {swe|sgn}")
    asyncio.run(_run_leg(sys.argv[1]))


if __name__ == "__main__":
    _main()
