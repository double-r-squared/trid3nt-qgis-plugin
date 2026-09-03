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
  render_selafin_animation.py --run-id <ULID> --slf res_agitation.slf \\
      --var "WAVE HEIGHT" --units m --quantity wave_height \\
      --stem artemis_harbor_agitation
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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

from trid3nt_server.workflows.telemac.result_reader import (  # noqa: E402
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
#: Vectors read as WHITE with a dark casing under them. A magnitude ramp runs
#: dark at one end and bright at the other, so a single-colour trace disappears
#: over half of any field it is drawn on.
_STREAM_COLOR = "#ffffff"
_STREAM_CASING = "#101318"
#: The casing drawn under a vector is wider than the vector itself by this
#: factor - visible as a dark outline rather than as its own shape.
_STREAM_CASING_RATIO = 2.2
#: A ``"quiver"`` still reads a COARSER cut of the same interpolation grid than
#: a moving trace does - this many times fewer points per axis - because a
#: frozen arrow per cell is meant to be sparse and calm, not a decimated trace.
_QUIVER_GRID_DECIMATE = 3


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
    #: How the values map onto the ramp - ``linear``, ``log`` or ``sqrt``. A
    #: field spanning orders of magnitude has no linear ramp that shows both
    #: ends, and the legend note carries this word so the reader is told.
    transform: str = "linear"

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


def resolve_animation_style(values, *, preset: str | None = None,
                            transform: str | None = None,
                            shared: tuple[float, float] | None = None) -> AnimationScale:
    """THE scale for an animation, resolved over EVERY frame at once.

    The scope of a data-policy rescale is the RUN, never the frame: resolving here,
    off the whole ``(time, node)`` array, is what makes one colour mean one value
    for the length of the GIF. Routing it through the style contract's resolver is
    what makes this GIF and the published raster of the same quantity agree on the
    ramp, the range and the sentence the legend says about them.

    ``shared`` is that agreement made literal: the range the PUBLISHED raster of
    this same quantity carries. A percentile read over the frames and a percentile
    read over a peak envelope are two reads of two different distributions, so
    without it the GIF and the panel beside it end up on two scales for one
    quantity - which is a reader's problem, not a renderer's detail.

    Falls back to a plain p2-p98 over the same whole array when the server package
    is not importable, so the script still runs standalone.
    """
    finite = _finite(values)
    if _STYLES is None:
        found = shared or _percentile_range(finite, _DEFAULT_CLIP)
        lo, hi = _widen(found or (0.0, 1.0))
        how = ("the published raster's own range" if shared else
               "scaled to this run (p2-p98)" if found else "empty field")
        return AnimationScale(lo, hi, "viridis", f"{how}: {lo:g} to {hi:g}", None,
                              transform or "linear")
    # The TRANSFORM rides in as a scale OVERRIDE, which is the contract's own
    # fourth entry point - not a second opinion invented here. ``merged`` keeps
    # the preset's policy, clip and fallback range, so the range is still read
    # p2-p98 over the whole run and only the ramp mapping moves; the resolver
    # then labels the override on the legend, which is the whole point.
    override = None
    if transform:
        from trid3nt_contracts.styles import ScaleSpec

        override = ScaleSpec(transform=transform)
    resolved = _STYLES.resolve_style(
        preset, read_range=lambda scale: _percentile_range(finite, scale.clip),
        override=override, shared=shared)
    lo, hi = _widen(resolved.range or (0.0, 1.0))
    return AnimationScale(lo, hi, _matplotlib_colormap(resolved.colormap),
                          resolved.legend_note(), resolved.preset,
                          resolved.scale.transform or "linear")


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
    place; the limits are left to the field being drawn. The third member is the
    basemap CREDIT the caption prints, and saying "no basemap" out loud is what
    keeps an offline render from reading as a failed tile fetch.
    """
    xw, yw = MR.ll_to_merc(np.array([bbox_ll[0], bbox_ll[2]]),
                           np.array([bbox_ll[1], bbox_ll[3]]))
    aspect = float(np.clip((yw[1] - yw[0]) / (xw[1] - xw[0]), 0.35, 1.8))
    fig, ax = plt.subplots(figsize=(10.0, 10.0 * aspect * 0.92), dpi=_DPI)
    ax.set_facecolor("#20242c")
    ax.set_xticks([])
    ax.set_yticks([])
    ax.set_title(title, fontsize=10)
    return fig, ax, "no basemap (offline render)"


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
    return fig, ax, MR.basemap_label(mosaic)


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


def _stream_field(tri: Triangulation, grid_n: int):
    """The regular grid a streamline trace needs, off a triangular mesh's extent.

    Streamlines cannot be traced on an unstructured mesh: matplotlib integrates
    on a rectilinear field. This is the DECLARED decimation - the components are
    interpolated onto ``grid_n`` points across the wider axis and traced there,
    which resolves the drainage network without paying for a trace through every
    element.
    """
    span_x = float(tri.x.max() - tri.x.min())
    span_y = float(tri.y.max() - tri.y.min())
    wider = max(span_x, span_y) or 1.0
    nx = max(int(grid_n * span_x / wider), 16)
    ny = max(int(grid_n * span_y / wider), 16)
    return np.meshgrid(np.linspace(tri.x.min(), tri.x.max(), nx),
                       np.linspace(tri.y.min(), tri.y.max(), ny))


def _interpolate(tri: Triangulation, node_values, gx, gy):
    from matplotlib.tri import LinearTriInterpolator

    return np.ma.filled(LinearTriInterpolator(tri, node_values)(gx, gy), 0.0)


def _speed_and_norm(u, v, scale: "AnimationScale"):
    """A grid's local speed, and that speed normalised into the COLOUR ramp's
    own ``(vmin, vmax)`` - so a vector's taper and the field's own colour agree
    about which point is fast, off one resolved range rather than two."""
    speed = np.hypot(u, v)
    span = max(scale.vmax - scale.vmin, 1e-9)
    return speed, np.clip((speed - scale.vmin) / span, 0.0, 1.0)


def _draw_streamlines(ax, gx, gy, u, v, *, density: float, arrow_size: float,
                      lw_bounds: tuple[float, float], scale: "AnimationScale"):
    """A traced flow field: ONE arrowhead per line (matplotlib's own streamplot
    default), the LINE WIDTH tapered by local magnitude between ``lw_bounds`` -
    the taper carries speed, the arrowhead is secondary and sized off
    ``arrow_size``, matplotlib's own ``arrowsize`` scale where 1.0 is that
    primitive's default."""
    speed, norm = _speed_and_norm(u, v, scale)
    if not np.any(speed > 0):
        return []  # a frame with no flow has no trace to draw
    lo, hi = lw_bounds
    lw_field = lo + (hi - lo) * norm
    artists = []
    for colour, lw, alpha, zorder in (
            (_STREAM_CASING, lw_field * _STREAM_CASING_RATIO, 0.5, 3.5),
            (_STREAM_COLOR, lw_field, 0.9, 3.6)):
        traced = ax.streamplot(gx, gy, u, v, density=density, color=colour,
                               linewidth=lw, arrowsize=arrow_size, zorder=zorder)
        traced.lines.set_alpha(alpha)
        artists.extend([traced.lines, traced.arrows])
    return artists


def _draw_quiver(ax, gx, gy, u, v, *, arrow_size: float, scale: "AnimationScale"):
    """One arrow per grid cell - the DISCRETE read a coarse or frozen grid
    carries more calmly than a traced line. ``arrow_size`` scales quiver's own
    head dimensions the same way it scales streamplot's ``arrowsize``: 1.0 is
    matplotlib's own default head.

    The arrow LENGTH is pinned explicitly rather than left to quiver's own
    autoscale: autoscale divides a target length by the grid's AVERAGE
    magnitude, and the interpolation grid pads past the mesh's own concave
    boundary with exact zeros, so a field with only a sparse interior of real
    flow collapses that average toward zero and streaks the few nonzero
    arrows far past the plot. Pinning it off the SAME ``(vmin, vmax)`` the
    colour ramp reads - the fastest value in the run spans one grid cell -
    keeps length and colour agreeing about which point is fast, regardless of
    how much of the grid outside the mesh is exact zero.
    """
    if not np.any(np.hypot(u, v) > 0):
        return []
    dx = float(abs(gx[0, 1] - gx[0, 0])) if gx.shape[1] > 1 else 1.0
    dy = float(abs(gy[1, 0] - gy[0, 0])) if gy.shape[0] > 1 else dx
    cell = min(dx, dy) or 1.0
    quiver_scale = max(scale.vmax, 1e-12) / (0.85 * cell)
    artists = []
    for colour, width, alpha, zorder in (
            (_STREAM_CASING, 0.0075, 0.5, 3.5),
            (_STREAM_COLOR, 0.0040, 0.9, 3.6)):
        q = ax.quiver(gx, gy, u, v, color=colour, width=width, alpha=alpha,
                     scale=quiver_scale, scale_units="xy", angles="xy",
                     headwidth=3.0 * arrow_size, headlength=5.0 * arrow_size,
                     headaxislength=4.5 * arrow_size, zorder=zorder)
        artists.append(q)
    return artists


def _draw_vectors(style: str | None, ax, gx, gy, u, v, *, density: float,
                  arrow_size: float, lw_bounds: tuple[float, float],
                  scale: "AnimationScale"):
    """The declared vector primitive, dispatched by name. Unknown or ``None``
    draws nothing - the vocabulary is closed, never a silent fallback."""
    if style == "streamlines":
        return _draw_streamlines(ax, gx, gy, u, v, density=density,
                                 arrow_size=arrow_size, lw_bounds=lw_bounds,
                                 scale=scale)
    if style == "quiver":
        return _draw_quiver(ax, gx, gy, u, v, arrow_size=arrow_size, scale=scale)
    return []


def render_frames(tri: Triangulation, values: np.ndarray, times, *, bbox_ll,
                  units: str, title: str, run_id: str, source_name: str,
                  variable: str, gif_path: Path, peak_path: Path,
                  preset: str | None = None, still: str = "peak",
                  plane_note: str = "", axes_factory=None,
                  transform: str | None = None, vector_uv=None,
                  vectors: str | None = None, vector_density: float = 1.4,
                  vector_grid_n: int = 200, arrow_size: float = 0.7,
                  vector_lw: tuple[float, float] = (0.35, 1.1),
                  still_vectors: str | None = None,
                  shared_range: tuple[float, float] | None = None) -> dict:
    """The plotting seam: a triangulation plus a ``(time, node)`` field -> GIF + still.

    THE COLOUR SCALE IS RESOLVED ONCE, HERE, BEFORE THE FIRST FRAME IS DRAWN, and
    the loop below only ever calls ``coll.set_array``. Nothing per-frame may touch
    ``vmin``, ``vmax``, the norm or the colorbar: a scale that moves with the frame
    makes one colour mean a different value each tick, so the reader watching the
    ramp is watching the renderer, not the water.

    ``axes_factory`` is ``(bbox_ll, title) -> (fig, ax, basemap_credit)``; it
    defaults to the ESRI basemap axes and takes :func:`plain_axes` where there is
    no tile access. The credit is what the caption prints, so a render always
    names the ground it was drawn on rather than the one it asked for.

    ``vectors`` is the DECLARED vocabulary the moving GIF draws in -
    ``"streamlines"`` (traced, magnitude-tapered width, one arrowhead per
    trace) or ``"quiver"`` (one arrow per grid cell) - and ``still_vectors``
    overrides it for the peak/final STILL alone, defaulting to ``vectors``
    when unset. ``arrow_size`` and ``vector_lw`` are declared, not derived:
    the caller states the arrow's prominence and the width taper's bounds
    rather than this function guessing them from the grid.
    """
    values = np.asarray(values, dtype="float64")
    scale = resolve_animation_style(values, preset=preset, transform=transform,
                                    shared=shared_range)
    # WHICH frame the still shows. "peak" is right for a field that BUILDS (a
    # rising tide, an arriving plume); "final" for one that DECAYS toward its
    # answer (a cooling column, a settling sea), where the peak frame is the
    # initial condition and shows the reader nothing the run did.
    # A masked frame can be ENTIRELY empty - a dry-start catchment holds no water
    # at t=0 - so the per-frame maximum is read with the all-NaN case named
    # rather than warned about, and a frame with nothing in it says "dry".
    def _frame_max(frame) -> float:
        finite = frame[np.isfinite(frame)]
        return float(np.max(finite)) if finite.size else float("nan")

    frame_max = [_frame_max(frame) for frame in values]
    peak_frame = (int(values.shape[0] - 1) if still == "final"
                  else int(np.nanargmax(frame_max)) if np.any(np.isfinite(frame_max))
                  else 0)

    # A LOG ramp needs a strictly positive floor, and a p2 clip over a field that
    # starts dry can sit at or below zero. The floor is the smallest POSITIVE
    # value the run actually produced rather than an invented epsilon, so the
    # bottom of the ramp is a number the solver wrote.
    norm = None
    if scale.transform == "log":
        from matplotlib.colors import LogNorm

        positive = values[np.isfinite(values) & (values > 0)]
        floor = (max(scale.vmin, float(positive.min())) if positive.size
                 else max(scale.vmin, scale.vmax / 1e4))
        norm = LogNorm(vmin=floor if floor > 0 else scale.vmax / 1e4,
                       vmax=max(scale.vmax, floor * 10.0))

    fig, ax, basemap_credit = (axes_factory or _axes_with_basemap)(bbox_ll, title)
    coll = ax.tripcolor(tri, values[0], shading="gouraud", cmap=scale.colormap,
                        alpha=0.85, zorder=2,
                        **({"norm": norm} if norm is not None
                           else {"vmin": scale.vmin, "vmax": scale.vmax}))
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
    # THE VECTOR FIELD over the magnitude ramp: direction (and, for
    # streamlines, speed via the width taper) in one frame. Redrawn per frame
    # because the flow field is what MOVES - the colour scale above is not,
    # and nothing here touches it. ANIM_STYLE and STILL_STYLE can differ: a
    # moving trace reads calmer as discrete arrows once it is frozen, so the
    # still gets its OWN, further-decimated grid rather than reusing the
    # trace's.
    have_vectors = vector_uv is not None
    anim_style = vectors if have_vectors and vectors in ("streamlines", "quiver") else None
    still_style = (still_vectors if still_vectors in ("streamlines", "quiver")
                   else anim_style) if have_vectors else None
    anim_grid = _stream_field(tri, vector_grid_n) if anim_style else None
    still_grid = (anim_grid if still_style == anim_style else
                  _stream_field(tri, max(vector_grid_n // _QUIVER_GRID_DECIMATE, 12))
                  if still_style else None)
    vector_state: dict[str, Any] = {"artists": []}

    def _draw_frame_vectors(style, grid, index: int) -> None:
        for artist in vector_state["artists"]:
            try:
                artist.remove()
            except (ValueError, NotImplementedError):
                pass
        gx, gy = grid
        u = _interpolate(tri, vector_uv[0][index], gx, gy)
        v = _interpolate(tri, vector_uv[1][index], gx, gy)
        vector_state["artists"] = _draw_vectors(
            style, ax, gx, gy, u, v, density=vector_density,
            arrow_size=arrow_size, lw_bounds=vector_lw, scale=scale)

    stamp = ax.text(0.012, 0.045, "", transform=ax.transAxes, fontsize=9,
                    color="white", zorder=5,
                    bbox=dict(facecolor="black", alpha=0.45, pad=3, edgecolor="none"))
    vector_note = ""
    if anim_style:
        gx, gy = anim_grid
        vector_note += (f"  |  {anim_style}: density {vector_density:g}, "
                        f"arrow {arrow_size:g} on a {gx.shape[1]}x{gx.shape[0]} "
                        "interpolated grid")
        if anim_style == "streamlines":
            vector_note += f", width tapered {vector_lw[0]:g}-{vector_lw[1]:g}"
    if still_style and still_style != anim_style:
        sgx, sgy = still_grid
        vector_note += (f"  |  still: {still_style} on a decimated "
                        f"{sgx.shape[1]}x{sgy.shape[0]} grid")
    ax.text(0.012, 0.955,
            f"run {run_id}  |  {source_name}, {values.shape[0]} frames"
            f"{plane_note}  |  wireframe = the meshed domain"
            + vector_note + f"  |  {basemap_credit}",
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
                if anim_style:
                    _draw_frame_vectors(anim_style, anim_grid, i)
                stamp.set_text(
                    f"t = {float(moment):8.0f} s      "
                    + (f"max {frame_max[i]:.3g} {units}"
                       if np.isfinite(frame_max[i]) else "dry (no wet nodes)"))
                writer.grab_frame()

    # The PEAK frame as its own still, off the SAME figure - same colours, same
    # extent, so the still and the animation cannot disagree. Its OWN vector
    # style/grid, which may differ from the moving GIF's.
    coll.set_array(values[peak_frame])
    if still_style:
        _draw_frame_vectors(still_style, still_grid, peak_frame)
    # A ONE-FRAME solve has no simulation clock, and the number in its time slot
    # is whatever the solver put there - for a steady elliptic wave solve it is
    # the wave PERIOD, which read as a timestamp says the run lasted eight
    # seconds. A steady field is stamped as one.
    measured = (f"max {frame_max[peak_frame]:.3g} {units}"
                if np.isfinite(frame_max[peak_frame]) else "dry (no wet nodes)")
    stamp.set_text(f"{still.upper()} FRAME  t = "
                   f"{float(times[peak_frame]):8.0f} s      {measured}" if animated
                   else f"STEADY STATE (no simulation clock)      {measured}")
    fig.savefig(peak_path, bbox_inches="tight")
    plt.close(fig)
    return {"frames": int(values.shape[0]), "animated": animated,
            "variable": variable,
            "plane": plane_note.strip(" |") or "2d", "peak_frame": peak_frame,
            "peak_time_s": float(times[peak_frame]),
            "peak_value": frame_max[peak_frame],
            "dry_frames": [i for i, v in enumerate(frame_max)
                           if not np.isfinite(v)],
            "vmin": (float(norm.vmin) if norm is not None else scale.vmin),
            "vmax": (float(norm.vmax) if norm is not None else scale.vmax),
            "style_preset": scale.preset, "legend_note": scale.note,
            "transform": scale.transform, "colormap": scale.colormap,
            "vectors": anim_style, "still_vectors": still_style,
            "vector_density": vector_density if anim_style else None,
            "arrow_size": arrow_size if (anim_style or still_style) else None,
            "vector_lw": (list(vector_lw) if anim_style == "streamlines"
                         else None),
            "vector_grid": ([int(anim_grid[0].shape[1]), int(anim_grid[0].shape[0])]
                            if anim_style else None),
            "still_vector_grid": ([int(still_grid[0].shape[1]), int(still_grid[0].shape[0])]
                                  if still_style else None)}


def render(slf_path: str, *, utm_epsg: int, origin_bbox, variable: str,
           units: str, title: str, run_id: str, gif_path: Path,
           peak_path: Path, nplan: int = 1, plane: str = "surface",
           still: str = "peak", mask_var: str | None = None,
           mask_min: float = 0.0, preset: str | None = None,
           source_name: str | None = None,
           initial_water_level: float | None = None,
           derived: tuple[str, ...] = (), transform: str | None = None,
           vectors: str | None = None, vector_density: float = 1.4,
           vector_grid_n: int = 200, arrow_size: float = 0.7,
           vector_lw: tuple[float, float] = (0.35, 1.1),
           still_vectors: str | None = None,
           shared_range: tuple[float, float] | None = None) -> dict:
    """The GIF over every frame, plus the PEAK frame as a still. One read, two products."""
    from pyproj import Transformer

    mesh = read_selafin(slf_path)
    if derived:
        # A field the solver did not write, built from the components it did.
        # TELEMAC stores VELOCITY U and VELOCITY V; the QUESTION is the speed,
        # and deriving it here beats asking a reader to imagine the magnitude of
        # two panels. The component names are DECLARED, so an engine that spells
        # them differently refuses in pick_variable rather than guessing.
        parts = [np.asarray(mesh["data"][pick_variable(mesh["varnames"], token)])
                 for token in derived]
        values = np.sqrt(sum(np.square(part) for part in parts))
        name = variable
        components = parts
    else:
        name = pick_variable(mesh["varnames"], variable)
        values = np.asarray(mesh["data"][name])
        components = []
    if values.ndim != 2 or values.shape[0] == 0:
        raise SystemExit(f"{name!r} carries no time steps in {slf_path}")
    mesh_x, mesh_y, triangles, values, plane_note = slice_plane(
        mesh, values, nplan=nplan, plane=plane)
    # The COMPONENTS ride the same slice as the magnitude, so a 3D plane pick
    # cannot leave the streamlines tracing a different plane than the field.
    components = [slice_plane(mesh, part, nplan=nplan, plane=plane)[3]
                  for part in components]
    # DRY NODES ARE NOT COLOURED. A coastal free surface on a dry node IS the bed
    # elevation, so an unmasked field is scaled by the LAND - six metres of hill
    # against a two-metre tide - and the tide reads as a flat wash that does not
    # move. Masking on the depth is what makes the wetted area grow on screen,
    # which is the thing the run is about.
    if mask_var:
        gate = np.asarray(mesh["data"][pick_variable(mesh["varnames"], mask_var)])
        _, _, _, gate, _ = slice_plane(mesh, gate, nplan=nplan, plane=plane)
        values = np.where(gate > float(mask_min), values, np.nan)

    # THE INUNDATION GATE, when the caller asks for it: keep only nodes that were
    # DRY at t=0. A depth field over a tidal bay is mostly the permanently
    # submerged floor, and painting that in an "inundation" ramp says the sea is
    # flooded - the scalar (flooded_land_km2) has always made this discrimination
    # and the picture did not. ``initial_water_level`` is the run's OWN
    # init_wl_m, the datum-corrected stage it cold-started from, so the mask is
    # the scalar's mask rather than one recomputed here.
    dry_land_note = ""
    if initial_water_level is not None:
        bed = np.asarray(mesh["data"][pick_variable(mesh["varnames"], "BOTTOM")])
        _, _, _, bed, _ = slice_plane(mesh, bed, nplan=nplan, plane=plane)
        initially_dry = bed[0] > float(initial_water_level)
        values = np.where(initially_dry[None, :], values, np.nan)
        dry_land_note = (f"  |  initially-dry land only (bed > "
                         f"{float(initial_water_level):.3f} m at t=0, "
                         f"{int(initially_dry.sum()):,} of {initially_dry.size:,} "
                         f"nodes)")

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
                           still=still, plane_note=plane_note + dry_land_note,
                           transform=transform,
                           vector_uv=(tuple(components[:2])
                                      if (vectors or still_vectors)
                                      and len(components) >= 2 else None),
                           vectors=vectors, vector_density=vector_density,
                           vector_grid_n=vector_grid_n, arrow_size=arrow_size,
                           vector_lw=vector_lw, still_vectors=still_vectors,
                           shared_range=shared_range)
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
               still: str = "peak", initial_water_level: float | None = None,
               name_infix: str = "", derived: tuple[str, ...] = (),
               transform: str | None = None, vectors: str | None = None,
               vector_density: float = 1.4, vector_grid_n: int = 200,
               arrow_size: float = 0.7,
               vector_lw: tuple[float, float] = (0.35, 1.1),
               still_vectors: str | None = None,
               shared_range: tuple[float, float] | None = None) -> dict:
    """One run's SELAFIN -> its GIF + still, straight off the object store.

    The importable seam under ``main``: the packet assembler renders through this
    rather than shelling out, so the delivered animation and a hand-rendered one
    are the same code. Returns the render report plus the two paths - ``animation``
    is ``None`` for a single-frame (steady) result, which has nothing to animate.

    ``shared_range`` is the range the PUBLISHED raster of this quantity carries.
    Passed, it IS the scale, so one quantity has one legend across everything a
    reader is handed; unset, the range is read off these frames alone.
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
    # ``name_infix`` separates a template's SEVERAL animations on disk. It is
    # empty for the templates that declare one, so their filenames - cited by
    # name in ADRs and evidence JSONs - do not move.
    gif = out_dir / f"{stem}_animation{name_infix}.gif"
    peak = out_dir / f"{stem}{name_infix}_{still}_frame.png"
    try:
        result = render(local, utm_epsg=int(epsg), origin_bbox=origin, variable=var,
                        units=units, title=title or f"{stem} - {var.strip()}",
                        run_id=run_id, gif_path=gif, peak_path=peak,
                        nplan=int(nplan or worker.get("nplan") or 1), plane=plane,
                        still=still, mask_var=mask_var, mask_min=mask_min,
                        preset=preset, source_name=slf,
                        initial_water_level=initial_water_level,
                        derived=derived, transform=transform, vectors=vectors,
                        vector_density=vector_density,
                        vector_grid_n=vector_grid_n, arrow_size=arrow_size,
                        vector_lw=vector_lw, still_vectors=still_vectors,
                        shared_range=shared_range)
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
