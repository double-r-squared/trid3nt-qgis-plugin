"""TELEMAC-2D river-dye local worker entrypoint (PHASE 2).

The TELEMAC analogue of ``services/workers/geoclaw/entrypoint.py``, adapted to
the local-docker VOLUME-MOUNT envelope (the SFINCS/MODFLOW-canonical local seam)
rather than GeoClaw's Batch self-S3-I/O:

  * The agent-side launcher (``tools.solver.launch_local_solver``) stages the
    worker-contract manifest into ``<rundir>/manifest.json`` and bind-mounts the
    rundir at ``/data`` (``docker run ... -v <rundir>:/data -w /data``). So this
    entrypoint reads ``/data/manifest.json``, runs the pipeline IN ``/data``, and
    writes every output (mesh + result ``.slf`` + ``.cas`` + ``.cli`` + listing +
    ``telemac_metrics.json``) into ``/data``.
  * The agent-side supervisor (``tools.solver._supervise_local_run``) then uploads
    the mounted outputs to ``s3://<runs-bucket>/<run_id>/`` and writes the run's
    ``completion.json`` (EXACT worker-entrypoint schema). The TELEMAC
    ``LocalSolverSpec.classify_exit`` reads ``telemac_metrics.json`` from the
    rundir and folds the dye metrics into that completion (the MODFLOW
    ``mfsim.lst`` convergence-guard analogue). So the container itself does NO S3
    I/O and needs NO boto3 -- keeping the (already heavy) conda/TELEMAC image lean.

The worker payload is the PROVEN P1 pipeline module ``telemac_river_dye_build``
(a REAL Snake River dye run authored + solved it; copied verbatim). This
entrypoint is the deterministic driver: manifest -> ReachConfig -> pipeline ->
outputs + metrics, mirroring the standalone ``run_p1.py`` flow.

Manifest schema (the ``telemac_river_dye`` worker contract):

    {
      "reach": {                       # ReachConfig field overrides (all optional)
        "name": "snake_river_twin_falls",
        "seed_lon": -114.307, "seed_lat": 42.579,
        "nav_direction": "DM", "distance_km": 6.0,
        "channel_width_m": 60.0, "mesh_size_m": 14.0,
        "inflow_q_m3s": 250.0, "init_depth_m": 2.5,
        "dye_conc_mgl": 100.0, "duration_s": 3600.0, "time_step_s": 1.0,
        "graphic_period": 200
      },
      "run_id": "<ulid>",              # optional; echoed into metrics
      "outputs": ["r2d_river.slf", ...]   # advisory; the supervisor globs these
    }

Exit code: 0 on a clean TELEMAC "CORRECT END OF RUN"; non-zero otherwise. The
supervisor turns exit!=0 into completion.status="error" (byte-identical to the
SFINCS/MODFLOW local path).

ASCII only. No agent code imported; this runs only inside the worker image.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

LOG = logging.getLogger("trid3nt.worker.telemac")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)

#: Where the agent-side launcher bind-mounts the rundir (``-w /data``). The
#: manifest lands at ``/data/manifest.json`` and every output is written here.
DEFAULT_DATA_DIR = "/data"

#: The output filenames the pipeline writes into the data dir (the supervisor
#: uploads whatever the manifest ``outputs`` globs match; this is the canonical
#: set + the default when the manifest omits ``outputs``).
DEFAULT_OUTPUTS = [
    "r2d_river.slf",       # the RESULT mesh (dye tracer per frame) -- the artifact
    "river.slf",           # the SELAFIN geometry (mesh + bed)
    "river.cli",           # boundary conditions
    "t2d_river.cas",       # the authored steering deck
    "full_listing.log",    # the solver listing (evidence)
    "telemac_metrics.json",  # the run summary (classify_exit reads this)
]

#: Metrics filename the ``LocalSolverSpec.classify_exit`` reads from the rundir.
METRICS_FILENAME = "telemac_metrics.json"


def _reach_config(data_dir: Path, reach_overrides: dict[str, Any]) -> Any:
    """Build a ``ReachConfig`` from manifest overrides, pinned to ``data_dir``.

    Only known ReachConfig fields are accepted (unknown keys are dropped with a
    warning) so a stray manifest key never crashes the worker. ``workdir`` is
    forced to the mounted data dir so every artifact lands where the supervisor
    uploads from.
    """
    from telemac_river_dye_build import ReachConfig  # noqa: WPS433 -- worker payload

    import dataclasses

    valid = {f.name for f in dataclasses.fields(ReachConfig)}
    clean: dict[str, Any] = {}
    for key, value in (reach_overrides or {}).items():
        if key == "workdir":
            continue  # always pinned to the mounted data dir
        if key in valid:
            clean[key] = value
        else:
            LOG.warning("telemac manifest: ignoring unknown reach key %r", key)
    clean["workdir"] = str(data_dir)
    cfg = ReachConfig(**clean)
    return cfg


def _write_metrics(data_dir: Path, payload: dict[str, Any]) -> Path:
    path = data_dir / METRICS_FILENAME
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    LOG.info("telemac metrics -> %s", path)
    return path


def run_pipeline(
    data_dir: Path,
    reach_overrides: dict[str, Any],
    run_id: str | None,
    mesh_only: bool = False,
) -> dict[str, Any]:
    """Run the proven P1 pipeline in ``data_dir``; return a metrics dict.

    Mirrors ``run_p1.py`` end-to-end: fetch the real river centerline (NLDI
    NHDPlus) -> project/resample/smooth -> Gmsh channel mesh -> Copernicus DEM
    bed -> write SELAFIN + CLI -> probe solve (guess liquid-boundary order) ->
    parse the listing to map liquid boundaries -> final solve -> tracer sanity
    from the result. The heavy imports (numpy, gmsh, rasterio, TELEMAC) all live
    INSIDE the payload functions, so this driver stays import-light.
    """
    import numpy as np  # noqa: WPS433
    import telemac_river_dye_build as B  # noqa: WPS433 -- worker payload

    t0 = time.time()
    cfg = _reach_config(data_dir, reach_overrides)
    LOG.info("telemac reach: name=%s seed=(%.4f,%.4f) nav=%s dist=%.1fkm width=%.1fm",
             cfg.name, cfg.seed_lon, cfg.seed_lat, cfg.nav_direction,
             cfg.distance_km, cfg.channel_width_m)

    # 1. real river centerline
    ll, fmeta = B.fetch_river_centerline(cfg)
    LOG.info("centerline fetched: %s", fmeta)

    # 2. project / resample / smooth
    cl, pmeta = B.process_centerline(ll, cfg)
    tr = pmeta.pop("lonlat_transformer")
    LOG.info("centerline processed: %s", pmeta)

    # 2b. BK-7: real river banks from NHDArea polygons (honest fallback to the
    # constant-width ribbon when the fetch/sampling cannot see water).
    bank_source = "constant"
    bank_stats = {}
    if str(getattr(cfg, "bank_source", "auto")).lower() != "constant":
        _bank_t0 = time.time()
        try:
            lon0, lat0 = ll[:, 0].min(), ll[:, 1].min()
            lon1, lat1 = ll[:, 0].max(), ll[:, 1].max()
            pad = 0.01
            polys = B.fetch_bank_polygons(
                (lon0 - pad, lat0 - pad, lon1 + pad, lat1 + pad))
            if polys:
                polys_utm = []
                for ext, holes in polys:
                    ex, ey = tr.transform(ext[:, 0], ext[:, 1])
                    hs = []
                    for h in holes:
                        hx, hy = tr.transform(h[:, 0], h[:, 1])
                        hs.append(np.column_stack([hx, hy]))
                    polys_utm.append((np.column_stack([ex, ey]), hs))
                LOG.info("bank polygons fetched in %.1fs; sampling...",
                         time.time() - _bank_t0)
                res = B.estimate_bank_offsets(cl, polys_utm)
                LOG.info("bank sampling done at %.1fs", time.time() - _bank_t0)
                if res is not None:
                    # recentered mid-water axis + symmetric half-widths
                    cl, halfw, frac = res
                    cfg.bank_offsets = (halfw, halfw)
                    # M3: the mesh builder carves ribbon-minus-water as island
                    # holes (walls), so slicks/dye route around real islands.
                    cfg.water_polys_utm = polys_utm
                    bank_source = "nhdarea"
                    bank_stats = {
                        "bank_valid_frac": frac,
                        "bank_width_min_m": round(float(2 * halfw.min()), 1),
                        "bank_width_mean_m": round(float(2 * halfw.mean()), 1),
                        "bank_width_max_m": round(float(2 * halfw.max()), 1),
                    }
                    LOG.info("real banks: nhdarea frac=%.2f width min/mean/max="
                             "%.0f/%.0f/%.0f m", frac,
                             bank_stats["bank_width_min_m"],
                             bank_stats["bank_width_mean_m"],
                             bank_stats["bank_width_max_m"])
                else:
                    LOG.warning("bank sampling saw too little water; "
                                "constant-width fallback")
            else:
                LOG.warning("no NHDArea polygons; constant-width fallback")
        except Exception:  # noqa: BLE001 -- banks are an enhancement, never fatal
            LOG.exception("bank estimation failed; constant-width fallback")

    # 3. Gmsh mesh (tagged boundary)
    mesh = B.build_channel_mesh(cl, cfg)
    LOG.info("mesh: npoin=%d nelem=%d nptfr=%d in=%d out=%d banks_ok=%s smooth_tries=%d",
             mesh["npoin"], len(mesh["ikle"]), mesh["nptfr"], mesh["n_in"],
             mesh["n_out"], mesh["banks_ok"], mesh["smooth_tries"])

    # MESH_ONLY (BK-3b approve-mesh gate): stop after the mesh is built. The DEM
    # bed is SKIPPED entirely - bed elevation only sets node Z (BOTTOM), never
    # connectivity, so npoin/nelem/edge stats shown at the gate are EXACT for the
    # eventual solve mesh - and skipping it sidesteps the untimed DEM fetch
    # (OPEN-25) plus ~30 s wall. Writes river.slf (Z=0) + river.cli +
    # mesh_preview.geojson (triangle edges, EPSG:4326) + gate-stat metrics.
    if mesh_only:
        import numpy as _np  # noqa: WPS433 -- local alias for clarity

        slf = str(data_dir / "river.slf")
        cli = str(data_dir / "river.cli")
        B.write_slf(mesh, _np.zeros(int(mesh["npoin"])), slf)
        B.write_cli(mesh, cli)

        X, Y, ik = mesh["X"], mesh["Y"], np.asarray(mesh["ikle"])  # 0-based ikle
        # unique undirected edges -> lengths (vectorized)
        e = np.vstack([ik[:, [0, 1]], ik[:, [1, 2]], ik[:, [2, 0]]])
        e = np.unique(np.sort(e, axis=1), axis=0)
        seg = np.hypot(X[e[:, 0]] - X[e[:, 1]], Y[e[:, 0]] - Y[e[:, 1]])

        # triangle-edge wireframe in EPSG:4326 (one MultiLineString feature).
        # Cap the wireframe at 30k edges (a max-budget mesh would be ~5 MB of
        # GeoJSON); past the cap emit the boundary ring only - honest note in
        # metrics either way.
        from pyproj import Transformer as _T  # noqa: WPS433
        tr_back = _T.from_crs(tr.target_crs, 4326, always_xy=True)
        lon, lat = tr_back.transform(X, Y)
        bbox4326 = [float(lon.min()), float(lat.min()),
                    float(lon.max()), float(lat.max())]
        wireframe_capped = bool(e.shape[0] > 30000)
        if wireframe_capped:
            # one closed linestring PER boundary ring (outer + island holes);
            # drawing the concatenated multi-ring walk as a single polyline
            # painted bogus chords between rings (live 2026-07-18)
            walks = mesh.get("boundary_rings") or [mesh["ring"]]
            coords = []
            for w in walks:
                w = np.asarray(w, dtype=np.int64)
                w_closed = np.append(w, w[:1])
                coords.append([[float(lon[i]), float(lat[i])] for i in w_closed])
        else:
            coords = [
                [[float(lon[a]), float(lat[a])], [float(lon[b]), float(lat[b])]]
                for a, b in e
            ]
        preview = {
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "geometry": {"type": "MultiLineString", "coordinates": coords},
                "properties": {
                    "kind": "telemac-mesh-preview",
                    "npoin": int(mesh["npoin"]),
                    "nelem": int(len(ik)),
                    "mesh_size_m": float(cfg.mesh_size_m),
                    "wireframe_capped": wireframe_capped,
                },
            }],
        }
        (data_dir / "mesh_preview.geojson").write_text(
            json.dumps(preview), encoding="utf-8")

        metrics = {
            # correct_end=True: the mesh phase ENDED CORRECTLY (there is no
            # solve to reach CORRECT END OF RUN); classify_exit treats a clean
            # exit + correct_end as ok, and mesh_only labels the run honestly.
            "status": "ok",
            "correct_end": True,
            "mesh_only": True,
            "run_id": run_id,
            "geometry_slf": "river.slf",
            "cli": "river.cli",
            "preview_geojson": "mesh_preview.geojson",
            "reach_name": cfg.name,
            "seed_comid": fmeta.get("seed_comid"),
            "n_flowlines": fmeta.get("n_flowlines"),
            "utm_epsg": pmeta.get("utm_epsg"),
            "centerline_length_m": pmeta.get("centerline_length_m"),
            "npoin": int(mesh["npoin"]),
            "nelem": int(len(ik)),
            "nptfr": int(mesh["nptfr"]),
            "n_inflow_nodes": int(mesh["n_in"]),
            "n_outflow_nodes": int(mesh["n_out"]),
            "mesh_size_m": float(cfg.mesh_size_m),
            "time_step_s": float(cfg.time_step_s),
            "edge_min_m": round(float(seg.min()), 2),
            "edge_mean_m": round(float(seg.mean()), 2),
            "edge_max_m": round(float(seg.max()), 2),
            "bbox4326": bbox4326,
            "bed_assigned": False,
            "bank_source": bank_source,
            **bank_stats,
            "wireframe_capped": wireframe_capped,
            "wall_s": round(time.time() - t0, 1),
        }
        LOG.info("mesh_only complete: npoin=%d nelem=%d edge_mean=%.1fm wall=%.1fs",
                 metrics["npoin"], metrics["nelem"], metrics["edge_mean_m"],
                 metrics["wall_s"])
        return metrics

    # 4. Copernicus DEM bed + gentle downstream slope
    Z, bed = B.fetch_dem_bed(mesh, cfg, tr)
    LOG.info("dem bed: %s", bed)

    # BK-6: project the user-picked release point (lonlat) into the mesh UTM
    # so spill_point can honor it (validated there within 2 channel widths).
    if getattr(cfg, "release_lon", None) is not None \
            and getattr(cfg, "release_lat", None) is not None:
        rx, ry = tr.transform(float(cfg.release_lon), float(cfg.release_lat))
        cfg.release_utm = (float(rx), float(ry))
        LOG.info("release point provided: (%.5f, %.5f) -> UTM (%.1f, %.1f)",
                 cfg.release_lon, cfg.release_lat, rx, ry)

    slf = str(data_dir / "river.slf")
    cli = str(data_dir / "river.cli")
    res = str(data_dir / "r2d_river.slf")
    cas = str(data_dir / "t2d_river.cas")

    # 5. write SELAFIN geometry + CLI
    B.write_slf(mesh, Z, slf)
    B.write_cli(mesh, cli)

    # 6. probe solve -> parse listing -> map liquid boundaries (gotcha 4)
    from pyproj import Transformer  # noqa: WPS433
    tr_back = Transformer.from_crs(tr.target_crs, 4326, always_xy=True)
    guess = ["outflow", "inflow"]
    B.author_deck(cfg, mesh, slf, cli, res, cas, guess, bed)
    ok, out = B.run_solver(cas, res, str(data_dir), timeout=1800)
    lb = B.map_liquid_boundaries(out, mesh, tr_back)
    LOG.info("probe solve CORRECT_END=%s parsed lb_order=%s", ok, lb)

    # 7. final solve with the mapped liquid-boundary order (if it differs)
    if lb and lb != guess:
        B.author_deck(cfg, mesh, slf, cli, res, cas, lb, bed)
        ok, out = B.run_solver(cas, res, str(data_dir), timeout=1800)
        LOG.info("final solve CORRECT_END=%s", ok)

    # persist the full solver listing as evidence
    (data_dir / "full_listing.log").write_text(out, encoding="utf-8")

    wall_s = round(time.time() - t0, 1)

    metrics: dict[str, Any] = {
        "status": "ok" if ok else "error",
        "correct_end": bool(ok),
        "run_id": run_id,
        "result_slf": "r2d_river.slf",
        "geometry_slf": "river.slf",
        "cli": "river.cli",
        "cas": "t2d_river.cas",
        "reach_name": cfg.name,
        "seed_comid": fmeta.get("seed_comid"),
        "n_flowlines": fmeta.get("n_flowlines"),
        "utm_epsg": pmeta.get("utm_epsg"),
        "centerline_length_m": pmeta.get("centerline_length_m"),
        "npoin": int(mesh["npoin"]),
        "nelem": int(len(mesh["ikle"])),
        "nptfr": int(mesh["nptfr"]),
        "n_inflow_nodes": int(mesh["n_in"]),
        "n_outflow_nodes": int(mesh["n_out"]),
        "lb_order": lb or guess,
        "bank_source": bank_source,
        **bank_stats,
        "release_point_used": bool(mesh.get("release_point_used")),
        "release_point_rejected_dist_m": mesh.get("release_point_rejected_dist_m"),
        "enforced_slope": bed.get("enforced_slope"),
        "bed_drop_m": bed.get("bed_drop_m"),
        "reach_len_m": bed.get("reach_len_m"),
        "wall_s": wall_s,
    }

    if not ok:
        metrics["error"] = "TELEMAC did not reach CORRECT END OF RUN"
        metrics["listing_tail"] = "\n".join(out.splitlines()[-40:])
        return metrics

    # 8. tracer sanity from the result mesh (dye advance down the reach)
    try:
        from data_manip.extraction.telemac_file import TelemacFile  # noqa: WPS433
        tf = TelemacFile(res)
        tvar = [v for v in tf.varnames
                if "DYE" in v.upper() or v.strip().upper().startswith("T")]
        vn = tvar[0]
        times = np.asarray(tf.times)
        x = np.asarray(tf.meshx)
        cmax_final = 0.0
        dye_nodes_final = 0
        front_x_final = float("nan")
        cmax_overall = 0.0
        peak_time_s = 0.0
        active_frames = 0        # frames with the dye pulse present in-reach
        for i in range(len(times)):
            c = np.asarray(tf.get_data_value(vn, i))
            fmax = float(c.max())
            if fmax > cmax_overall:
                cmax_overall = fmax
                peak_time_s = float(times[i])
            if fmax > 1.0:
                active_frames += 1
            if i == len(times) - 1:
                m = c > 1.0
                cmax_final = fmax
                dye_nodes_final = int(m.sum())
                front_x_final = float(x[m].max()) if m.any() else float("nan")
        tf.close()
        metrics.update({
            "n_frames": int(len(times)),
            "dye_var": vn.strip(),
            "dye_cmax_overall": round(cmax_overall, 3),
            "dye_cmax_final": round(cmax_final, 3),
            "dye_nodes_final": dye_nodes_final,
            # FINITE PULSE: the plume travels down and passes, so the FINAL frame
            # is often clear (front None) -- the overall/peak/active-frame fields
            # honestly carry "how strong, when, how long present".
            "dye_peak_time_s": round(peak_time_s, 1),
            "dye_active_frames": int(active_frames),
            "dye_front_x_final_m": (
                round(front_x_final, 1)
                if front_x_final == front_x_final else None
            ),
        })
    except Exception as exc:  # noqa: BLE001 -- sanity read is best-effort; solve is OK
        LOG.warning("telemac tracer-sanity read failed (non-fatal): %s", exc)
        metrics["tracer_sanity_error"] = f"{type(exc).__name__}: {exc}"

    return metrics


def _build_argv_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="trid3nt-telemac-entrypoint",
        description="TRID3NT TELEMAC-2D river-dye local worker (P2).",
    )
    p.add_argument(
        "--manifest",
        default=os.environ.get("GRACE2_MANIFEST_PATH", "").strip(),
        help="Path to the worker manifest (default /data/manifest.json).",
    )
    p.add_argument(
        "--data-dir",
        default=os.environ.get("GRACE2_TELEMAC_DATA_DIR", DEFAULT_DATA_DIR).strip(),
        help="Working/output dir (the bind-mounted rundir; default /data).",
    )
    p.add_argument(
        "--run-id",
        default=os.environ.get("GRACE2_RUN_ID", "").strip(),
        help="Run identifier (echoed into telemac_metrics.json).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_argv_parser().parse_args(argv)
    data_dir = Path(args.data_dir or DEFAULT_DATA_DIR)
    manifest_path = Path(args.manifest) if args.manifest else (data_dir / "manifest.json")

    LOG.info("trid3nt-telemac worker starting data_dir=%s manifest=%s run_id=%s",
             data_dir, manifest_path, args.run_id or "(none)")

    data_dir.mkdir(parents=True, exist_ok=True)

    reach_overrides: dict[str, Any] = {}
    run_id = args.run_id or None
    mesh_only = False
    try:
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                raise ValueError("manifest must be a JSON object")
            reach_overrides = manifest.get("reach") or {}
            run_id = run_id or manifest.get("run_id")
            mesh_only = bool(manifest.get("mesh_only"))
        else:
            LOG.warning("no manifest at %s; running with default ReachConfig",
                        manifest_path)
    except Exception as exc:  # noqa: BLE001 -- surface a bad manifest as a typed metrics error
        LOG.exception("telemac manifest read failed")
        _write_metrics(data_dir, {
            "status": "error",
            "correct_end": False,
            "error": f"manifest read failed: {type(exc).__name__}: {exc}",
        })
        return 2

    try:
        metrics = run_pipeline(data_dir, reach_overrides, run_id, mesh_only=mesh_only)
    except Exception as exc:  # noqa: BLE001 -- any pipeline failure is a typed metrics error
        LOG.exception("telemac pipeline failed")
        _write_metrics(data_dir, {
            "status": "error",
            "correct_end": False,
            "error": f"{type(exc).__name__}: {exc}",
        })
        return 1

    _write_metrics(data_dir, metrics)
    ok = bool(metrics.get("correct_end"))
    LOG.info("trid3nt-telemac worker done status=%s correct_end=%s wall_s=%s",
             metrics.get("status"), ok, metrics.get("wall_s"))
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
