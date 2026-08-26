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

ONE SCALE FOR THE WHOLE GIF. The colour range is resolved once, over every frame
at once, through the style contract (``--quantity`` names the published quantity,
so this animation and the published raster of that quantity get the same ramp,
the same range and the same legend sentence). A scale that moved with the frame
would make the same colour mean a different value each tick.

LOCAL COORDINATES. The open-water builds lay their mesh with node 0 at the AOI's
SW corner, so a SELAFIN's metres are usually LOCAL. ``--origin-bbox`` (or, by
default, the ``bbox`` the worker recorded in ``telemac_metrics.json``) is what
puts the frames back on the map; without it they land at the UTM false origin.

Env (MinIO): set -a; source .env.local; set +a
Usage:
  render_selafin_animation.py --run-id <ULID> --slf res_coastal.slf \\
      --var "WATER DEPTH" --units m --quantity flood_depth \\
      --stem coastal_tidal_surge
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.animation import PillowWriter  # noqa: E402
from matplotlib.tri import Triangulation  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "contracts"))
sys.path.insert(0, str(REPO / "scripts" / "sandbox" / "oceanmesh"))

import merc_render as MR  # noqa: E402

from trid3nt_server.workflows.telemac.postprocess_telemac import (  # noqa: E402
    read_selafin,
)

try:  # a standalone script must still run where the server package is not importable
    from trid3nt_server.emission import styles as _STYLES  # noqa: E402
except Exception:  # noqa: BLE001 - absence degrades to the local percentile scale
    _STYLES = None

#: Frames per second. Slow enough that a reader can follow a 10-40 frame solve.
_FPS = 4
#: Fraction of the mesh bbox padded, so nothing sits flush to the frame.
_PAD_FRAC = 0.06
#: Render density: fine enough that the elements survive, coarse enough that a
#: 40-frame GIF stays a few megabytes.
_DPI = 130
#: The clip the style contract's own band reader uses when a preset declares none.
_DEFAULT_CLIP = (2.0, 98.0)


@dataclass(frozen=True)
class AnimationScale:
    """ONE colour scale for a whole animation: the range, the ramp, the caption.

    Carrying the caption alongside the numbers is what stops the picture and its
    legend disagreeing: both are read off this single value, resolved once.
    """

    vmin: float
    vmax: float
    colormap: str
    note: str
    preset: str | None = None

    @property
    def range(self) -> tuple[float, float]:
        return (self.vmin, self.vmax)


def _finite(values) -> np.ndarray:
    arr = np.asarray(values, dtype="float64")
    return arr[np.isfinite(arr)]


def _percentile_range(finite: np.ndarray, clip) -> tuple[float, float] | None:
    """The clipped range over EVERY frame handed in, or ``None`` when there is none."""
    if finite.size == 0:
        return None
    lo_pct, hi_pct = clip or _DEFAULT_CLIP
    return (float(np.percentile(finite, lo_pct)), float(np.percentile(finite, hi_pct)))


def _widen(rng: tuple[float, float]) -> tuple[float, float]:
    """A zero-width range is not a scale - matplotlib collapses it, so pad it."""
    lo, hi = float(rng[0]), float(rng[1])
    if hi > lo:
        return (lo, hi)
    pad = max(abs(lo) * 0.01, 1e-6)
    return (lo - pad, hi + pad)


def _matplotlib_colormap(name: str | None) -> str:
    """The contract's colormap under matplotlib's spelling; ``viridis`` when unknown.

    The contract names ramps the way the tile renderer spells them (lowercase,
    ``ylgnbu``); matplotlib spells the same ramp ``YlGnBu``. Matching case-blind is
    what keeps ONE declared colormap on both the published raster and this GIF.
    """
    table = {key.lower(): key for key in matplotlib.colormaps}
    return table.get((name or "").strip().lower(), "viridis")


def resolve_animation_style(values, *, preset: str | None = None) -> AnimationScale:
    """THE scale for an animation, resolved over EVERY frame at once.

    The scope of a data-policy rescale is the RUN, never the frame: resolving here,
    off the whole ``(time, node)`` array, is what makes one colour mean one value
    for the length of the GIF. Routing it through the style contract's resolver is
    what makes this GIF and the published raster of the same quantity agree on the
    ramp, the range and the sentence the legend says about them.

    Falls back to a plain p2-p98 over the same whole array when the server package
    is not importable, so the script still runs standalone.
    """
    finite = _finite(values)
    if _STYLES is None:
        found = _percentile_range(finite, _DEFAULT_CLIP)
        lo, hi = _widen(found or (0.0, 1.0))
        how = "scaled to this run (p2-p98)" if found else "empty field"
        return AnimationScale(lo, hi, "viridis", f"{how}: {lo:g} to {hi:g}", None)
    resolved = _STYLES.resolve_style(
        preset, read_range=lambda scale: _percentile_range(finite, scale.clip))
    lo, hi = _widen(resolved.range or (0.0, 1.0))
    return AnimationScale(lo, hi, _matplotlib_colormap(resolved.colormap),
                          resolved.legend_note(), resolved.preset)


def animation_scale(values, *, preset: str | None = None) -> tuple[float, float]:
    """``(vmin, vmax)`` for a whole animation - the pure scale decision, alone."""
    return resolve_animation_style(values, preset=preset).range


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


#: Below this easting a mesh cannot be in real UTM metres: the zone's western
#: edge sits at 166 km, so anything near zero is local coordinates.
_LOCAL_EASTING_M = 1000.0


def local_origin(bbox, utm_epsg: int) -> tuple[float, float]:
    """The UTM corner a LOCAL-coordinate mesh was built from; ``(0, 0)`` with none."""
    if not (bbox and len(tuple(bbox)) == 4):
        return (0.0, 0.0)
    from pyproj import Transformer

    fwd = Transformer.from_crs(4326, int(utm_epsg), always_xy=True)
    x0, y0 = fwd.transform(float(bbox[0]), float(bbox[1]))
    x1, y1 = fwd.transform(float(bbox[2]), float(bbox[3]))
    return (min(x0, x1), min(y0, y1))


def _global_palette(frames):
    """ONE 256-colour palette derived from EVERY frame at once.

    Derived off downscaled copies: the palette a median cut picks is the same, and
    a 40-frame full-resolution montage is hundreds of megabytes for no gain.
    """
    from PIL import Image

    w, h = frames[0].size
    tw, th = max(w // 4, 1), max(h // 4, 1)
    strip = Image.new("RGB", (tw, th * len(frames)))
    for i, frame in enumerate(frames):
        strip.paste(frame.convert("RGB").resize((tw, th), Image.Resampling.NEAREST),
                    (0, i * th))
    return strip.quantize(colors=256, method=Image.Quantize.MEDIANCUT)


class StablePaletteWriter(PillowWriter):
    """A GIF whose PALETTE is fixed once, over all frames, before anything is encoded.

    Pillow's default is an adaptive palette PER FRAME, so unchanged pixels - the
    colorbar above all - come out as slightly different colours in every frame.
    That is the same dishonesty a per-frame vmin/vmax would be, moved out of the
    scale and into the encoder: the legend appears to shift while the numbers
    behind it did not. One palette, chosen over every frame at once, removes it,
    and identical pixels stay identical bytes.
    """

    def finish(self) -> None:
        from PIL import Image

        master = _global_palette(self._frames)
        frames = [f.convert("RGB").quantize(palette=master, dither=Image.Dither.NONE)
                  for f in self._frames]
        frames[0].save(self.outfile, save_all=True, append_images=frames[1:],
                       duration=int(1000 / self.fps), loop=0)


def plain_axes(bbox_ll, title: str):
    """Axes with NO basemap - the offline seam, for a caller with no tile access.

    Same figure geometry as the basemap axes so the colorbar lands in the same
    place; the limits are left to the field being drawn.
    """
    xw, yw = MR.ll_to_merc(np.array([bbox_ll[0], bbox_ll[2]]),
                           np.array([bbox_ll[1], bbox_ll[3]]))
    aspect = float(np.clip((yw[1] - yw[0]) / (xw[1] - xw[0]), 0.35, 1.8))
    fig, ax = plt.subplots(figsize=(10.0, 10.0 * aspect * 0.92), dpi=_DPI)
    ax.set_facecolor("#20242c")
    ax.set_xticks([])
    ax.set_yticks([])
    ax.set_title(title, fontsize=10)
    return fig, ax


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


def render_frames(tri: Triangulation, values: np.ndarray, times, *, bbox_ll,
                  units: str, title: str, run_id: str, source_name: str,
                  variable: str, gif_path: Path, peak_path: Path,
                  preset: str | None = None, still: str = "peak",
                  plane_note: str = "", axes_factory=None) -> dict:
    """The plotting seam: a triangulation plus a ``(time, node)`` field -> GIF + still.

    THE COLOUR SCALE IS RESOLVED ONCE, HERE, BEFORE THE FIRST FRAME IS DRAWN, and
    the loop below only ever calls ``coll.set_array``. Nothing per-frame may touch
    ``vmin``, ``vmax``, the norm or the colorbar: a scale that moves with the frame
    makes one colour mean a different value each tick, so the reader watching the
    ramp is watching the renderer, not the water.

    ``axes_factory`` is ``(bbox_ll, title) -> (fig, ax)``; it defaults to the ESRI
    basemap axes and takes :func:`plain_axes` where there is no tile access.
    """
    values = np.asarray(values, dtype="float64")
    scale = resolve_animation_style(values, preset=preset)
    # WHICH frame the still shows. "peak" is right for a field that BUILDS (a
    # rising tide, an arriving plume); "final" for one that DECAYS toward its
    # answer (a cooling column, a settling sea), where the peak frame is the
    # initial condition and shows the reader nothing the run did.
    peak_frame = (int(values.shape[0] - 1) if still == "final"
                  else int(np.nanargmax([np.nanmax(frame) for frame in values])))

    fig, ax = (axes_factory or _axes_with_basemap)(bbox_ll, title)
    coll = ax.tripcolor(tri, values[0], shading="gouraud", cmap=scale.colormap,
                        vmin=scale.vmin, vmax=scale.vmax, alpha=0.85, zorder=2)
    # The MESH is the modeled domain, drawn OVER the field: a wireframe hidden
    # under an opaque field tells the reader nothing about what was solved. Its
    # weight is ADAPTIVE, because one fixed line width cannot read across the
    # domain sizes this family covers - 900 elements over a lake and 50,000 over a
    # reach are two different pictures, and a width tuned for the dense one
    # disappears on the sparse one (which is exactly what a reader reports as "I
    # do not see a mesh"). The element count is the scale that matters, not the
    # extent in metres.
    n_elements = max(int(tri.triangles.shape[0]), 1)
    ax.triplot(tri, color="white",
               linewidth=float(np.clip(60.0 / n_elements ** 0.5, 0.12, 0.6)),
               alpha=float(np.clip(0.25 + 1200.0 / n_elements, 0.30, 0.75)),
               zorder=3)
    # ONE colorbar, off that one scale, captioned with the POLICY that produced
    # it: a reader cannot tell a fixed domain scale from a range read off this
    # run's own values by looking at the colours, so the legend says which.
    cbar = fig.colorbar(coll, ax=ax, fraction=0.025, pad=0.01)
    cbar.set_label(f"{variable} ({units})\n{scale.note}", fontsize=8)
    cbar.ax.tick_params(labelsize=7)
    stamp = ax.text(0.012, 0.045, "", transform=ax.transAxes, fontsize=9,
                    color="white", zorder=5,
                    bbox=dict(facecolor="black", alpha=0.45, pad=3, edgecolor="none"))
    ax.text(0.012, 0.955,
            f"run {run_id}  |  {source_name}, {values.shape[0]} frames"
            f"{plane_note}  |  wireframe = the meshed domain  |  ESRI World Imagery",
            transform=ax.transAxes, fontsize=6.5, color="white", va="top", zorder=5,
            bbox=dict(facecolor="black", alpha=0.4, pad=2, edgecolor="none"))

    # A STEADY solve has one frame, and a one-frame GIF is a still pretending to
    # be an animation. It gets the still and an honest "no animation" instead.
    animated = values.shape[0] > 1
    if animated:
        writer = StablePaletteWriter(fps=_FPS)
        with writer.saving(fig, str(gif_path), dpi=_DPI):
            for i, moment in enumerate(times):
                coll.set_array(values[i])
                stamp.set_text(f"t = {float(moment):8.0f} s      "
                               f"max {float(np.nanmax(values[i])):.3g} {units}")
                writer.grab_frame()

    # The PEAK frame as its own still, off the SAME figure - same colours, same
    # extent, so the still and the animation cannot disagree.
    coll.set_array(values[peak_frame])
    stamp.set_text(f"{still.upper()} FRAME  t = "
                   f"{float(times[peak_frame]):8.0f} s      "
                   f"max {float(np.nanmax(values[peak_frame])):.3g} {units}")
    fig.savefig(peak_path, bbox_inches="tight")
    plt.close(fig)
    return {"frames": int(values.shape[0]), "animated": animated,
            "variable": variable,
            "plane": plane_note.strip(" |") or "2d", "peak_frame": peak_frame,
            "peak_time_s": float(times[peak_frame]),
            "peak_value": float(np.nanmax(values[peak_frame])),
            "vmin": scale.vmin, "vmax": scale.vmax,
            "style_preset": scale.preset, "legend_note": scale.note,
            "colormap": scale.colormap}


def render(slf_path: str, *, utm_epsg: int, origin_bbox, variable: str,
           units: str, title: str, run_id: str, gif_path: Path,
           peak_path: Path, nplan: int = 1, plane: str = "surface",
           still: str = "peak", mask_var: str | None = None,
           mask_min: float = 0.0, preset: str | None = None,
           source_name: str | None = None) -> dict:
    """The GIF over every frame, plus the PEAK frame as a still. One read, two products."""
    from pyproj import Transformer

    mesh = read_selafin(slf_path)
    name = pick_variable(mesh["varnames"], variable)
    values = np.asarray(mesh["data"][name])
    if values.ndim != 2 or values.shape[0] == 0:
        raise SystemExit(f"{name!r} carries no time steps in {slf_path}")
    mesh_x, mesh_y, triangles, values, plane_note = slice_plane(
        mesh, values, nplan=nplan, plane=plane)
    # DRY NODES ARE NOT COLOURED. A coastal free surface on a dry node IS the bed
    # elevation, so an unmasked field is scaled by the LAND - six metres of hill
    # against a two-metre tide - and the tide reads as a flat wash that does not
    # move. Masking on the depth is what makes the wetted area grow on screen,
    # which is the thing the run is about.
    if mask_var:
        gate = np.asarray(mesh["data"][pick_variable(mesh["varnames"], mask_var)])
        _, _, _, gate, _ = slice_plane(mesh, gate, nplan=nplan, plane=plane)
        values = np.where(gate > float(mask_min), values, np.nan)

    x_org, y_org = local_origin(origin_bbox, utm_epsg)
    # WHETHER an origin belongs is a fact about the FILE, not about the caller. A
    # UTM easting is never below 160 km, so a mesh whose minimum sits near zero was
    # written in LOCAL metres and needs its corner back; one that already carries
    # real eastings is ABSOLUTE and adding a corner would shift it off the map by
    # exactly that corner. Both mistakes land the frames at the false origin, and
    # both used to be silent.
    is_local = float(np.nanmin(mesh_x)) < _LOCAL_EASTING_M
    if x_org and not is_local:
        x_org = y_org = 0.0
    elif not x_org and is_local:
        print(f"WARNING: {Path(slf_path).name} looks LOCAL (min easting "
              f"{float(np.nanmin(mesh_x)):.1f} m) and no origin bbox was given - "
              "the frames will land at the UTM false origin. Pass --origin-bbox.",
              file=sys.stderr)
    back = Transformer.from_crs(int(utm_epsg), 4326, always_xy=True)
    lon, lat = back.transform(np.asarray(mesh_x) + x_org, np.asarray(mesh_y) + y_org)
    lon, lat = np.asarray(lon), np.asarray(lat)
    mx, my = MR.ll_to_merc(lon, lat)
    tri = Triangulation(mx, my, triangles)
    bbox_ll = (float(lon.min()), float(lat.min()), float(lon.max()), float(lat.max()))

    result = render_frames(tri, values, mesh["times"], bbox_ll=bbox_ll, units=units,
                           title=title, run_id=run_id,
                           source_name=source_name or Path(slf_path).name,
                           variable=name.strip(),
                           gif_path=gif_path, peak_path=peak_path, preset=preset,
                           still=still, plane_note=plane_note)
    # WHERE the frames actually landed. A LOCAL mesh rendered with no origin lands
    # at the UTM false origin, thousands of km from the water, and every other
    # number in this report stays perfectly healthy while it does - so the extent
    # is REPORTED and a caller can check it against the run's own AOI.
    return {**result, "bbox_ll": [float(v) for v in bbox_ll],
            "local_origin_m": [float(x_org), float(y_org)]}


def _epsg_from_outputs(bucket: str, run_id: str) -> int | None:
    """The mesh CRS off the run's OWN outputs manifest; ``None`` when it has none.

    A SELAFIN carries no CRS, and not every leg's worker echoes one: the
    rain-on-grid mesh is projected AGENT-side, so its ``telemac_metrics.json``
    records no zone at all. What every leg does write is ``outputs.json``, where
    the mesh entry is stamped with the ``crs_authid`` it was published under -
    the same fact, recorded by the party that knew it.
    """
    for entry in (_read_json(bucket, f"{run_id}/outputs.json") or {}).get("entries", []):
        authid = str(entry.get("crs_authid") or "")
        if authid.upper().startswith("EPSG:"):
            return int(authid.split(":", 1)[1])
    return None


def render_run(*, run_id: str, slf: str, var: str, stem: str, out_dir,
               units: str = "", quantity: str | None = None,
               title: str | None = None, bucket: str | None = None,
               origin_bbox=None, utm_epsg: int | None = None,
               plane: str = "surface", nplan: int | None = None,
               mask_var: str | None = None, mask_min: float = 0.0,
               still: str = "peak") -> dict:
    """One run's SELAFIN -> its GIF + still, straight off the object store.

    The importable seam under ``main``: the packet assembler renders through this
    rather than shelling out, so the delivered animation and a hand-rendered one
    are the same code. Returns the render report plus the two paths - ``animation``
    is ``None`` for a single-frame (steady) result, which has nothing to animate.
    """
    bucket = bucket or os.environ.get("TRID3NT_RUNS_BUCKET", "trid3nt-runs")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    worker = _read_json(bucket, f"{run_id}/telemac_metrics.json")
    epsg = utm_epsg or worker.get("utm_epsg") or _epsg_from_outputs(bucket, run_id)
    if epsg is None:
        raise SystemExit(f"run {run_id} records no utm_epsg; pass utm_epsg")
    origin = origin_bbox if origin_bbox is not None else worker.get("bbox")

    preset = None
    if quantity and _STYLES is not None:
        preset, _fallback = _STYLES.resolve_style_preset(quantity)

    local = _download(bucket, f"{run_id}/{slf}", ".slf")
    gif = out_dir / f"{stem}_animation.gif"
    peak = out_dir / f"{stem}_{still}_frame.png"
    try:
        result = render(local, utm_epsg=int(epsg), origin_bbox=origin, variable=var,
                        units=units, title=title or f"{stem} - {var.strip()}",
                        run_id=run_id, gif_path=gif, peak_path=peak,
                        nplan=int(nplan or worker.get("nplan") or 1), plane=plane,
                        still=still, mask_var=mask_var, mask_min=mask_min,
                        preset=preset, source_name=slf)
    finally:
        Path(local).unlink(missing_ok=True)
    return {**result, "run_id": run_id, "origin_bbox": origin,
            "animation": str(gif) if result["animated"] else None,
            "peak": str(peak),
            "animation_bytes": gif.stat().st_size if result["animated"] else 0,
            "peak_bytes": peak.stat().st_size}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--slf", required=True, help="the run's time-stepped SELAFIN")
    ap.add_argument("--var", required=True, help="a token in the variable's name")
    ap.add_argument("--units", default="")
    ap.add_argument("--quantity", default=None,
                    help="the PUBLISHED quantity this field is (e.g. flood_depth); "
                         "the style contract turns it into the same colormap and "
                         "range the published raster of that quantity is painted on")
    ap.add_argument("--stem", required=True, help="output basename (the workflow file)")
    ap.add_argument("--title", default=None)
    ap.add_argument("--bucket", default=os.environ.get("TRID3NT_RUNS_BUCKET",
                                                       "trid3nt-runs"))
    # The proof layout is docs/proof/templates/<template>/<variant>/ and the
    # writer asks proof_paths for it rather than joining its own path, so a
    # render and the canary evidence it was made from land together.
    ap.add_argument("--template", default=None,
                    help="proof folder template name; default is --stem's own "
                         "template (a _refined stem files under refined/)")
    ap.add_argument("--variant", default=None,
                    choices=("coarse", "refined", "postmigration", "addendum"),
                    help="proof folder variant; default is read off --stem")
    ap.add_argument("--out-dir", default=None,
                    help="explicit output directory; overrides the proof layout")
    ap.add_argument("--origin-bbox", default=None,
                    help="4326 min_lon,min_lat,max_lon,max_lat the LOCAL mesh was "
                         "built from; default reads telemac_metrics.json's bbox")
    ap.add_argument("--utm-epsg", type=int, default=None)
    ap.add_argument("--plane", choices=("surface", "bottom"), default="surface",
                    help="which horizontal plane of a 3D PRISM result to render")
    ap.add_argument("--nplan", type=int, default=None,
                    help="sigma planes in a 3D result; default reads telemac_metrics")
    ap.add_argument("--mask-var", default=None,
                    help="a second variable that gates which nodes are coloured "
                         "(e.g. WATER DEPTH, so dry land is not painted)")
    ap.add_argument("--mask-min", type=float, default=0.0)
    ap.add_argument("--still", choices=("peak", "final"), default="peak",
                    help="which frame the still shows - peak for a field that "
                         "builds, final for one that decays toward its answer")
    ns = ap.parse_args(argv)

    if ns.out_dir:
        out_dir = Path(ns.out_dir)
    else:
        from trid3nt_server.testing.proof_paths import proof_dir, split_variant

        template, variant = split_variant(ns.stem)
        out_dir = Path(proof_dir(ns.template or template, ns.variant or variant))

    result = render_run(
        run_id=ns.run_id, slf=ns.slf, var=ns.var, stem=ns.stem, out_dir=out_dir,
        units=ns.units, quantity=ns.quantity, title=ns.title, bucket=ns.bucket,
        origin_bbox=([float(v) for v in ns.origin_bbox.split(",")]
                     if ns.origin_bbox else None),
        utm_epsg=ns.utm_epsg, plane=ns.plane, nplan=ns.nplan,
        mask_var=ns.mask_var, mask_min=ns.mask_min, still=ns.still)
    print(json.dumps({**result, "animation": result["animation"] or
                      "NONE - a single-frame (steady) result has nothing to animate"},
                     indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
