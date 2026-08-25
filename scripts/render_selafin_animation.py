#!/usr/bin/env python
"""Diagnostic ANIMATION for any TELEMAC-family run: the frames, as a GIF + the peak.

The contact sheet (``render_all_layers_proof.py``) shows the canvas STORY, one
still panel per layer. A time-stepped solve has a story the stills cannot tell -
the tide rising, the column stratifying, the plume arriving - and this renders it
straight off the run's OWN time-stepped SELAFIN. Never a re-solve, never a
re-derivation.

Generic on purpose. ``proof_river_dye_frames.py`` is the river-dye version of
this, with the dye variable, its filenames and its mg/L labels welded in; every
other engine in the family then had no animation at all. Here the run says what
to read - which SELAFIN, which variable, which units - so one tool covers the
family and a new template gets its GIF by naming its file.

LOCAL COORDINATES. The open-water builds lay their mesh with node 0 at the AOI's
SW corner, so a SELAFIN's metres are usually LOCAL. ``--origin-bbox`` (or, by
default, the ``bbox`` the worker recorded in ``telemac_metrics.json``) is what
puts the frames back on the map; without it they land at the UTM false origin.

Env (MinIO): set -a; source .env.local; set +a
Usage:
  render_selafin_animation.py --run-id <ULID> --slf res_coastal.slf \\
      --var "WATER DEPTH" --units m --stem coastal_tidal_surge
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.animation import PillowWriter  # noqa: E402
from matplotlib.tri import Triangulation  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "sandbox" / "oceanmesh"))

import merc_render as MR  # noqa: E402

from trid3nt_server.workflows.telemac.postprocess_telemac import (  # noqa: E402
    read_selafin,
)

#: Frames per second. Slow enough that a reader can follow a 10-40 frame solve.
_FPS = 4
#: Fraction of the mesh bbox padded, so nothing sits flush to the frame.
_PAD_FRAC = 0.06
#: Render density: fine enough that the elements survive, coarse enough that a
#: 40-frame GIF stays a few megabytes.
_DPI = 130


def _s3():
    import boto3

    return boto3.client("s3", endpoint_url=os.environ["AWS_ENDPOINT_URL"],
                        region_name=os.environ.get("AWS_REGION", "us-east-1"))


def _download(bucket: str, key: str, suffix: str) -> str:
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    _s3().download_file(bucket, key, path)
    return path


def _read_json(bucket: str, key: str) -> dict:
    try:
        return json.loads(_s3().get_object(Bucket=bucket, Key=key)["Body"].read())
    except Exception:  # noqa: BLE001 - absence is a fact the caller handles
        return {}


def pick_variable(varnames: list[str], token: str) -> str:
    """The SELAFIN variable whose padded 32-char name contains ``token``."""
    wanted = token.strip().upper()
    for name in varnames:
        if wanted in name.upper():
            return name
    raise SystemExit(f"no variable matching {token!r} among {varnames}")


def local_origin(bbox, utm_epsg: int) -> tuple[float, float]:
    """The UTM corner a LOCAL-coordinate mesh was built from; ``(0, 0)`` with none."""
    if not (bbox and len(tuple(bbox)) == 4):
        return (0.0, 0.0)
    from pyproj import Transformer

    fwd = Transformer.from_crs(4326, int(utm_epsg), always_xy=True)
    x0, y0 = fwd.transform(float(bbox[0]), float(bbox[1]))
    x1, y1 = fwd.transform(float(bbox[2]), float(bbox[3]))
    return (min(x0, x1), min(y0, y1))


def _axes_with_basemap(bbox_ll, title: str):
    zoom = MR.pick_zoom(bbox_ll, max_tiles=6)
    mosaic, extent = MR.fetch_basemap(bbox_ll, zoom)
    xw, yw = MR.ll_to_merc(np.array([bbox_ll[0], bbox_ll[2]]),
                           np.array([bbox_ll[1], bbox_ll[3]]))
    aspect = float(np.clip((yw[1] - yw[0]) / (xw[1] - xw[0]), 0.35, 1.8))
    fig, ax = plt.subplots(figsize=(10.0, 10.0 * aspect * 0.92), dpi=_DPI)
    ax.imshow(np.asarray(mosaic), extent=extent, origin="upper", zorder=0)
    padx, pady = (xw[1] - xw[0]) * _PAD_FRAC, (yw[1] - yw[0]) * _PAD_FRAC
    ax.set_xlim(xw[0] - padx, xw[1] + padx)
    ax.set_ylim(yw[0] - pady, yw[1] + pady)
    ax.set_xticks([])
    ax.set_yticks([])
    ax.set_title(title, fontsize=10)
    return fig, ax


def slice_plane(mesh: dict, values: np.ndarray, *, nplan: int, plane: str):
    """One horizontal plane out of a 3D PRISM SELAFIN, as a 2D mesh + its values.

    A TELEMAC-3D result is prisms (six nodes an element) stacked ``nplan`` deep,
    and node ``k`` of plane ``p`` sits at index ``p * n2d + k``. Rendering it as if
    it were 2D would draw the BOTTOM plane's triangles under whatever slice of the
    value array happened to line up - a picture of the wrong water. So the plane is
    named, sliced, and captioned.
    """
    ikle = np.asarray(mesh["ikle"])
    if ikle.shape[1] < 6 or nplan <= 1:
        return mesh["x"], mesh["y"], ikle[:, :3], values, ""
    n2d = int(mesh["npoin"]) // int(nplan)
    n2d_elements = int(mesh["nelem"]) // (int(nplan) - 1)
    index = (nplan - 1) if plane == "surface" else 0
    start = index * n2d
    return (np.asarray(mesh["x"])[:n2d], np.asarray(mesh["y"])[:n2d],
            ikle[:n2d_elements, :3], values[:, start:start + n2d],
            f"  |  {plane} plane of {nplan}")


def render(slf_path: str, *, utm_epsg: int, origin_bbox, variable: str,
           units: str, title: str, run_id: str, gif_path: Path,
           peak_path: Path, nplan: int = 1, plane: str = "surface",
           still: str = "peak") -> dict:
    """The GIF over every frame, plus the PEAK frame as a still. One read, two products."""
    from pyproj import Transformer

    mesh = read_selafin(slf_path)
    name = pick_variable(mesh["varnames"], variable)
    values = np.asarray(mesh["data"][name])
    if values.ndim != 2 or values.shape[0] == 0:
        raise SystemExit(f"{name!r} carries no time steps in {slf_path}")
    mesh_x, mesh_y, triangles, values, plane_note = slice_plane(
        mesh, values, nplan=nplan, plane=plane)

    x_org, y_org = local_origin(origin_bbox, utm_epsg)
    back = Transformer.from_crs(int(utm_epsg), 4326, always_xy=True)
    lon, lat = back.transform(np.asarray(mesh_x) + x_org, np.asarray(mesh_y) + y_org)
    lon, lat = np.asarray(lon), np.asarray(lat)
    mx, my = MR.ll_to_merc(lon, lat)
    tri = Triangulation(mx, my, triangles)

    finite = values[np.isfinite(values)]
    vmin = float(np.nanpercentile(finite, 2)) if finite.size else 0.0
    vmax = float(np.nanpercentile(finite, 98)) if finite.size else 1.0
    if vmax <= vmin:
        vmax = vmin + 1e-6
    # WHICH frame the still shows. "peak" is right for a field that BUILDS (a
    # rising tide, an arriving plume); "final" for one that DECAYS toward its
    # answer (a cooling column, a settling sea), where the peak frame is the
    # initial condition and shows the reader nothing the run did.
    peak_frame = (int(values.shape[0] - 1) if still == "final"
                  else int(np.nanargmax([np.nanmax(frame) for frame in values])))
    bbox_ll = (float(lon.min()), float(lat.min()), float(lon.max()), float(lat.max()))

    fig, ax = _axes_with_basemap(bbox_ll, title)
    coll = ax.tripcolor(tri, values[0], shading="gouraud", cmap="viridis",
                        vmin=vmin, vmax=vmax, alpha=0.85, zorder=2)
    # The MESH is the modeled domain, drawn OVER the field: a wireframe hidden
    # under an opaque field tells the reader nothing about what was solved.
    ax.triplot(tri, color="white", linewidth=0.1, alpha=0.25, zorder=3)
    cbar = fig.colorbar(coll, ax=ax, fraction=0.025, pad=0.01)
    cbar.set_label(f"{name.strip()} ({units})", fontsize=8)
    cbar.ax.tick_params(labelsize=7)
    stamp = ax.text(0.012, 0.045, "", transform=ax.transAxes, fontsize=9,
                    color="white", zorder=5,
                    bbox=dict(facecolor="black", alpha=0.45, pad=3, edgecolor="none"))
    ax.text(0.012, 0.955,
            f"run {run_id}  |  {Path(slf_path).name}, {values.shape[0]} frames"
            f"{plane_note}  |  wireframe = the meshed domain  |  ESRI World Imagery",
            transform=ax.transAxes, fontsize=6.5, color="white", va="top", zorder=5,
            bbox=dict(facecolor="black", alpha=0.4, pad=2, edgecolor="none"))

    writer = PillowWriter(fps=_FPS)
    with writer.saving(fig, str(gif_path), dpi=_DPI):
        for i, moment in enumerate(mesh["times"]):
            coll.set_array(values[i])
            stamp.set_text(f"t = {float(moment):8.0f} s      "
                           f"max {float(np.nanmax(values[i])):.3g} {units}")
            writer.grab_frame()

    # The PEAK frame as its own still, off the SAME figure - same colours, same
    # extent, so the still and the animation cannot disagree.
    coll.set_array(values[peak_frame])
    stamp.set_text(f"{still.upper()} FRAME  t = "
                   f"{float(mesh['times'][peak_frame]):8.0f} s      "
                   f"max {float(np.nanmax(values[peak_frame])):.3g} {units}")
    fig.savefig(peak_path, bbox_inches="tight")
    plt.close(fig)
    return {"frames": int(values.shape[0]), "variable": name.strip(),
            "plane": plane_note.strip(" |") or "2d", "peak_frame": peak_frame,
            "peak_time_s": float(mesh["times"][peak_frame]),
            "peak_value": float(np.nanmax(values[peak_frame])),
            "vmin": vmin, "vmax": vmax}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--slf", required=True, help="the run's time-stepped SELAFIN")
    ap.add_argument("--var", required=True, help="a token in the variable's name")
    ap.add_argument("--units", default="")
    ap.add_argument("--stem", required=True, help="output basename (the workflow file)")
    ap.add_argument("--title", default=None)
    ap.add_argument("--bucket", default=os.environ.get("TRID3NT_RUNS_BUCKET",
                                                       "trid3nt-runs"))
    ap.add_argument("--out-dir", default=str(REPO / "docs" / "proof" / "templates"))
    ap.add_argument("--origin-bbox", default=None,
                    help="4326 min_lon,min_lat,max_lon,max_lat the LOCAL mesh was "
                         "built from; default reads telemac_metrics.json's bbox")
    ap.add_argument("--utm-epsg", type=int, default=None)
    ap.add_argument("--plane", choices=("surface", "bottom"), default="surface",
                    help="which horizontal plane of a 3D PRISM result to render")
    ap.add_argument("--nplan", type=int, default=None,
                    help="sigma planes in a 3D result; default reads telemac_metrics")
    ap.add_argument("--still", choices=("peak", "final"), default="peak",
                    help="which frame the still shows - peak for a field that "
                         "builds, final for one that decays toward its answer")
    ns = ap.parse_args(argv)

    out_dir = Path(ns.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    worker = _read_json(ns.bucket, f"{ns.run_id}/telemac_metrics.json")
    utm_epsg = ns.utm_epsg or worker.get("utm_epsg")
    if utm_epsg is None:
        raise SystemExit(f"run {ns.run_id} records no utm_epsg; pass --utm-epsg")
    origin = ([float(v) for v in ns.origin_bbox.split(",")] if ns.origin_bbox
              else worker.get("bbox"))

    slf = _download(ns.bucket, f"{ns.run_id}/{ns.slf}", ".slf")
    gif = out_dir / f"{ns.stem}_animation.gif"
    peak = out_dir / f"{ns.stem}_{ns.still}_frame.png"
    try:
        result = render(slf, utm_epsg=int(utm_epsg), origin_bbox=origin,
                        variable=ns.var, units=ns.units,
                        title=ns.title or f"{ns.stem} - {ns.var.strip()}",
                        run_id=ns.run_id, gif_path=gif, peak_path=peak,
                        nplan=int(ns.nplan or worker.get("nplan") or 1),
                        plane=ns.plane, still=ns.still)
    finally:
        Path(slf).unlink(missing_ok=True)
    print(json.dumps({**result, "run_id": ns.run_id, "animation": str(gif),
                      "peak": str(peak), "origin_bbox": origin,
                      "animation_bytes": gif.stat().st_size,
                      "peak_bytes": peak.stat().st_size}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
