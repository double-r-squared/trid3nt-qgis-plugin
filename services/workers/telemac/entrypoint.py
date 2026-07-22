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
        "release_lon": -122.934, "release_lat": 46.106,  # BK-6 source picker
        "seed_from_release": true,     # call-provided release seeds the reach
        "seed_release_lon": -122.934, "seed_release_lat": 46.106,
        # ^ BK-3b decouple: ORIGINAL call coords the reach seed follows when a
        #   gate click overwrote release_lon/release_lat (source moves only)
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
    "drogues.txt",         # oil class: raw particle track (TecPlot ASCII)
    "particles.json",      # oil class: parsed slick snapshots (EPSG:4326)
    "oil_spill.txt",       # oil class: the steering file used (evidence)
    "slick.geojson",       # oil class: renderable slick snapshots
    "t2d_river.waqtel",    # decay class: the WAQTEL steering file (forcing evidence)
    "gaia_river.slf",      # sediment class: GAIA result (CUMUL BED EVOL deposition)
    "gaia_river.cas",      # sediment class: the GAIA steering file (evidence)
]

#: Metrics filename the ``LocalSolverSpec.classify_exit`` reads from the rundir.
METRICS_FILENAME = "telemac_metrics.json"


def _parse_gaia_mass_balance(listing_text: str) -> dict[str, Any]:
    """Parse GAIA's FINAL MASS-BALANCE OF SEDIMENTS block from the solver listing.

    Returns the authoritative deposited / eroded / net-bed / lost masses (kg) GAIA
    itself reports (the honesty-floor evidence: the run narrates from THESE typed
    numbers, never invents them). In-image (v9) listing shape::

        FINAL MASS-BALANCE OF SEDIMENTS:
        GAIA MASS-BALANCE OF SEDIMENTS PER CLASS:
         SEDIMENT CLASS NUMBER          =        1
         CUMULATED BED EVOLUTIONS       =    0.481E-06  ( KG )
         CUMULATED EROSION              =     394.4448  ( KG )
         CUMULATED DEPOSITION           =     394.4448  ( KG )
         CUMULATED LOST MASS            =    0.511E-12  ( KG )

    Best-effort: any missing field is simply omitted (fail-open)."""
    import re

    out: dict[str, Any] = {}
    m0 = re.search(r"FINAL MASS-BALANCE OF SEDIMENTS", listing_text or "")
    block = (listing_text or "")[m0.end():] if m0 else ""
    if not block:
        return out
    # isolate to the end-of-run marker so we do not read past the final block.
    m1 = re.search(r"END OF TIME LOOP|CORRECT END OF RUN", block)
    if m1:
        block = block[:m1.start()]

    def _num(label: str) -> float | None:
        mm = re.search(
            re.escape(label) + r"\s*=\s*([-\d.Ee+]+)", block)
        try:
            return float(mm.group(1)) if mm else None
        except (TypeError, ValueError):
            return None

    dep = _num("CUMULATED DEPOSITION")
    ero = _num("CUMULATED EROSION")
    net = _num("CUMULATED BED EVOLUTIONS")
    lost = _num("CUMULATED LOST MASS")
    if dep is not None:
        out["sediment_deposited_mass_kg"] = round(dep, 4)
    if ero is not None:
        out["sediment_eroded_mass_kg"] = round(ero, 4)
    if net is not None:
        out["sediment_net_bed_mass_kg"] = round(net, 6)
    if lost is not None:
        out["sediment_mass_lost_kg"] = round(lost, 8)
    return out


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
            # pad must cover FAR channels behind mid-river islands (NATE
            # 2026-07-18: Fisher/Cottonwood back-channels were unmeshed - the
            # 0.01deg pad + corridor clipped them off laterally)
            pad = 0.03
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
                    # Discharge must scale to the MEASURED river (live
                    # 2026-07-18: the Snake-tuned 250 m3/s default left the
                    # 1.3km-wide Columbia nearly stagnant/shallow - wrong for
                    # dye velocity AND fatal for oil slicks). When the caller
                    # kept the default, estimate q = width * depth * 0.7 m/s.
                    # Data-driven NWM streamflow is the follow-up (#223).
                    if float(cfg.inflow_q_m3s) == 250.0:
                        w_mean = float(2 * halfw.mean())
                        q_est = round(w_mean * float(cfg.init_depth_m) * 0.7, 0)
                        if q_est > 250.0:
                            LOG.info(
                                "inflow scaled to measured river: 250 -> %.0f "
                                "m3/s (width %.0f m x depth %.1f m x 0.7 m/s)",
                                q_est, w_mean, cfg.init_depth_m)
                            cfg.inflow_q_m3s = q_est
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
        # Wireframe budget: past the edge cap SUBSAMPLE interior edges rather
        # than dropping to boundary-only (NATE 2026-07-18: the hollow preview
        # read as "less detailed / wrong mesh"). Boundary rings (banks +
        # island walls, one closed linestring per ring) are ALWAYS complete;
        # interior edges take whatever budget remains at a uniform stride.
        EDGE_BUDGET = 30000
        walks = [np.asarray(w, dtype=np.int64)
                 for w in (mesh.get("boundary_rings") or [mesh["ring"]])]
        bnd_pairs = set()
        ring_coords = []
        for w in walks:
            w_closed = np.append(w, w[:1])
            ring_coords.append([[float(lon[i]), float(lat[i])] for i in w_closed])
            for a, b in zip(w_closed[:-1], w_closed[1:]):
                bnd_pairs.add((min(int(a), int(b)), max(int(a), int(b))))
        interior = [(a, b) for a, b in e if (int(a), int(b)) not in bnd_pairs]
        room = max(EDGE_BUDGET - len(bnd_pairs), 0)
        wireframe_capped = bool(len(interior) > room)
        if wireframe_capped and room > 0:
            stride = int(np.ceil(len(interior) / room))
            interior = interior[::stride]
        elif room == 0:
            interior = []
        coords = ring_coords + [
            [[float(lon[a]), float(lat[a])], [float(lon[b]), float(lat[b])]]
            for a, b in interior
        ]
        # inflow/outflow caps as separate features (role property) so clients
        # can color the open boundaries like the proof renders
        cls = mesh["cls"]; ring_all = mesh["ring"]
        cap_feats = []
        for role in ("inflow", "outflow"):
            pts = [[float(lon[int(n)]), float(lat[int(n)])]
                   for k, n in enumerate(ring_all) if cls[k] == role]
            if pts:
                cap_feats.append({
                    "type": "Feature",
                    "geometry": {"type": "MultiPoint", "coordinates": pts},
                    "properties": {"kind": "telemac-mesh-preview", "role": role},
                })
        preview = {
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "geometry": {"type": "MultiLineString", "coordinates": coords},
                "properties": {
                    "kind": "telemac-mesh-preview",
                    "role": "mesh",
                    "npoin": int(mesh["npoin"]),
                    "nelem": int(len(ik)),
                    "mesh_size_m": float(cfg.mesh_size_m),
                    "n_islands": mesh.get("n_islands"),
                    "water_coverage_frac": mesh.get("water_coverage_frac"),
                    "wireframe_capped": wireframe_capped,
                },
            }, *cap_feats],
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
            "domain_mode": mesh.get("domain_mode"),
            "n_islands": mesh.get("n_islands"),
            "water_coverage_frac": mesh.get("water_coverage_frac"),
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
    mesh["bed_z"] = Z  # oil release snaps to the local thalweg (deepest node)
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

    # M3 oil class: parse the drogues particle track into particles.json
    # (EPSG:4326 snapshots) + summary metrics. Fail-open - a parse problem
    # never voids a CORRECT END solve.
    oil_stats: dict[str, Any] = {}
    drogues_path = data_dir / "drogues.txt"
    if str(getattr(cfg, "substance_class", "tracer")).lower() == "oil" \
            and drogues_path.exists():
        try:
            from pyproj import Transformer as _T  # noqa: WPS433

            zones = B.parse_drogues(str(drogues_path))
            tr_back = _T.from_crs(pmeta.get("utm_epsg") or 32610, 4326,
                                  always_xy=True)
            snaps = []
            for t_s, pts in zones:
                if not pts:
                    snaps.append({"t_s": t_s, "lonlat": []})
                    continue
                xs, ys = zip(*pts)
                lo, la = tr_back.transform(xs, ys)
                snaps.append({"t_s": t_s,
                              "lonlat": [[round(a, 6), round(b, 6)]
                                         for a, b in zip(lo, la)]})
            (data_dir / "particles.json").write_text(
                json.dumps({"snapshots": snaps}), encoding="utf-8")
            # slick.geojson: renderable snapshot points (t_s property) the
            # composer publishes as a vector layer, mesh-preview-style
            keep = [snaps[0], snaps[len(snaps) // 2], snaps[-1]] \
                if len(snaps) >= 3 else snaps
            feats = [{
                "type": "Feature",
                "geometry": {"type": "MultiPoint", "coordinates": sn["lonlat"]},
                "properties": {"kind": "oil-slick", "t_s": sn["t_s"],
                               "n": len(sn["lonlat"])},
            } for sn in keep if sn["lonlat"]]
            (data_dir / "slick.geojson").write_text(
                json.dumps({"type": "FeatureCollection", "features": feats}),
                encoding="utf-8")
            if zones and zones[0][1] and zones[-1][1]:
                import numpy as _np2  # noqa: WPS433
                c0 = _np2.mean(zones[0][1], axis=0)
                c1 = _np2.mean(zones[-1][1], axis=0)
                # Honest exit accounting (drogue root-cause 2026-07-18):
                # TELEMAC deletes a float from the drogues file when its
                # trajectory crosses a LIQUID boundary (streamline.f SCHAR11,
                # IFABOR=0) - i.e. it EXITED the domain through the outlet.
                # released - final is therefore "exited" (beached mass shows
                # separately in the listing's oil balance), never a tracker
                # bug; report it so a low survivor count reads honestly.
                released = len(zones[0][1])
                remaining = len(zones[-1][1])
                oil_stats = {
                    "oil_particles": remaining,
                    "oil_particles_released": released,
                    "oil_particles_exited_domain": max(0, released - remaining),
                    "oil_snapshots": len(zones),
                    "oil_drift_m": round(float(_np2.hypot(*(c1 - c0))), 1),
                }
            LOG.info("oil particles parsed: %s", oil_stats)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("drogues parse failed (%s) - slick layer skipped", exc)

    # GAIA v1 sediment class: read the GAIA result + listing for the deposition
    # summary (mirrors the oil_stats block). CUMUL BED EVOL (var 'B/E', metres) in
    # gaia_river.slf is the deposition map; the solver listing's FINAL
    # MASS-BALANCE OF SEDIMENTS carries the authoritative deposited / eroded / net
    # / lost masses in kg (GAIA's own closure - the honesty-floor evidence). All
    # best-effort: a parse problem never voids a CORRECT END solve.
    sediment_stats: dict[str, Any] = {}
    gaia_slf = data_dir / B.GAIA_RESULT_FILENAME
    if str(getattr(cfg, "substance_class", "tracer")).lower() == "sediment" \
            and gaia_slf.exists():
        try:
            sediment_stats.update(_parse_gaia_mass_balance(out))
            # injected sediment mass (kg): source discharge x source conc x pulse
            # window. conc = the dye pulse concentration reused as source conc,
            # kg/m3 = mg/L / 1000 (the GAIA source-keyword unit).
            q = float(getattr(cfg, "source_q_m3s", 8.0))
            conc_kgm3 = max(float(getattr(cfg, "dye_conc_mgl", 100.0)) / 1000.0, 0.0)
            pulse_s = float(getattr(cfg, "pulse_window_s", 300.0))
            injected_kg = round(q * conc_kgm3 * pulse_s, 3)
            sediment_stats["sediment_injected_kg"] = injected_kg
            # max deposition (mm) + deposit centroid distance from the release,
            # read from the final CUMUL BED EVOL frame (metres -> mm x1000).
            from data_manip.extraction.telemac_file import TelemacFile  # noqa: WPS433
            gf = TelemacFile(str(gaia_slf))
            evol = [v for v in gf.varnames
                    if "EVOL" in v.upper() or v.strip().upper().startswith("E")]
            if evol:
                ev = np.asarray(gf.get_data_value(evol[0], len(gf.times) - 1))
                gx = np.asarray(gf.meshx)
                gy = np.asarray(gf.meshy)
                dep = ev.copy()
                dep[dep < 0] = 0.0                     # deposition only (mm map)
                max_dep_mm = round(float(dep.max()) * 1000.0, 4)
                sediment_stats["sediment_max_deposition_mm"] = max_dep_mm
                # deposit centroid distance from the source point (metres): the
                # spill point is re-derived from the SAME seam author_deck used.
                try:
                    sx, sy, _snode = B.spill_point(mesh, cfg)
                except Exception:  # noqa: BLE001 - centroid dist is best-effort
                    sx = sy = float("nan")
                mmask = dep > (0.05 * dep.max() if dep.max() > 0 else 1e9)
                if mmask.any() and dep[mmask].sum() > 0 and sx == sx and sy == sy:
                    cx = float((gx[mmask] * dep[mmask]).sum() / dep[mmask].sum())
                    cy = float((gy[mmask] * dep[mmask]).sum() / dep[mmask].sum())
                    sediment_stats["sediment_deposit_centroid_dist_m"] = round(
                        float(np.hypot(cx - sx, cy - sy)), 1)
            gf.close()
            # deposit fraction: net bed mass (kg) / injected (kg), clamped [0,1].
            net = sediment_stats.get("sediment_net_bed_mass_kg")
            if net is not None and injected_kg > 0:
                sediment_stats["sediment_deposit_fraction"] = round(
                    min(max(float(net) / injected_kg, 0.0), 1.0), 4)
            LOG.info("gaia sediment parsed: %s", sediment_stats)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("gaia sediment parse failed (%s) - deposition metrics "
                        "skipped", exc)

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
        **oil_stats,
        "substance_class": str(getattr(cfg, "substance_class", "tracer")),
        # WAQTEL v1a decay honesty: record the degradation law + coefficient the
        # deck was authored with (only meaningful for the decay class; harmless
        # defaults otherwise) so the run summary carries the decay parameters.
        **({"decay_law": int(getattr(cfg, "decay_law", 1)),
            "decay_coef": float(getattr(cfg, "decay_coef", 2.0))}
           if str(getattr(cfg, "substance_class", "tracer")).lower() == "decay"
           else {}),
        # GAIA v1 sediment: the deposition summary from gaia_river.slf + the GAIA
        # listing mass balance (deposited/eroded/net/lost kg, max_deposition_mm,
        # deposit_fraction, injected/centroid) - the Invariant-1 typed numbers the
        # agent narrates. Harmless empty dict for every non-sediment run.
        **sediment_stats,
        **({"grain_size_um": float(getattr(cfg, "grain_size_um", 200.0)),
            "sediment_type": str(getattr(cfg, "sediment_type", "sand")),
            "sediment_density": float(getattr(cfg, "sediment_density", 2650.0))}
           if str(getattr(cfg, "substance_class", "tracer")).lower() == "sediment"
           else {}),
        "domain_mode": mesh.get("domain_mode"),
        "n_islands": mesh.get("n_islands"),
        "water_coverage_frac": mesh.get("water_coverage_frac"),
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
        default=os.environ.get("TRID3NT_MANIFEST_PATH", "").strip(),
        help="Path to the worker manifest (default /data/manifest.json).",
    )
    p.add_argument(
        "--data-dir",
        default=os.environ.get("TRID3NT_TELEMAC_DATA_DIR", DEFAULT_DATA_DIR).strip(),
        help="Working/output dir (the bind-mounted rundir; default /data).",
    )
    p.add_argument(
        "--run-id",
        default=os.environ.get("TRID3NT_RUN_ID", "").strip(),
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
