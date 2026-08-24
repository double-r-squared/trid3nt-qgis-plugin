#!/usr/bin/env python
"""Diagnostic contact sheet: EVERY layer a run put on the canvas, in emission order.

The plume/peak renderers show the ANSWER. This one shows the CANVAS STORY - what
QGIS receives, panel by panel, in the order the emit-on-fetch and emit-on-solve
seams delivered it: river geometry, terrain, mesh preview, the drawn marker, then
the result rasters, and finally the whole stack composited as one view.

The order is not reconstructed here. ``loaded_layers`` is an append-ordered list
(the emitter appends before it emits, and a re-publish replaces in place), and
each row carries its ``z_index``, so the evidence a drive script writes IS the
emission record.

Styling comes from the PRODUCT, never from a palette invented here: a raster with
a data-driven ``legend`` renders through that key, otherwise through
``publish_layer._resolve_qgis_style_params`` for its declared ``style_preset``.
A preset that resolves to EMPTY style params is the terrain / RGBA passthrough -
QGIS auto-scales it, and so does the panel, captioned as such. Vector presets are
QGIS-side symbology with no server-side colour to read, so vectors get honest
neutral geometry (lines, points, outlines) with the declared preset named on the
panel.

Env (MinIO): set -a; source .env.local; set +a
Usage:
  render_all_layers_proof.py --evidence docs/proof/templates/<drive>_evidence.json
  render_all_layers_proof.py --case-id <ULID> --out sheet.png
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "sandbox" / "oceanmesh"))

import merc_render as MR  # noqa: E402

#: Panels per row on the sheet. Three keeps a 6-8 layer run to two or three rows
#: at a size where the reach is still readable.
_COLS = 3
_DPI = 130
#: Fraction of the canvas bbox padded on every panel, so nothing sits flush to
#: the frame.
_PAD_FRAC = 0.06
#: Decimated read cap for a raster panel. A contact-sheet panel is ~1200 px wide;
#: reading a full-resolution DEM to shrink it is only slower.
_MAX_READ_PX = 1024
#: A trailing frame index in a layer name ("... step 7", "... t=3600 s"). Frames
#: collapse to ONE panel: the animation is the GIF renderer's job, and 25 near
#: identical panels bury the layers that differ.
_FRAME_SUFFIX_RE = re.compile(
    r"\s*(?:[-(]\s*)?(?:step|frame|t)\s*[=# ]\s*[\d.]+\s*\w*\s*\)?$",
    re.IGNORECASE,
)
#: Above this vertex count a vector layer is a WIREFRAME (a mesh preview ships
#: every element edge as one MultiLineString), and a flowline's line weight turns
#: it into a solid blob. Drawn thin instead, so the panel reads as the domain.
_WIREFRAME_VERTICES = 5000
#: Per-layer colours for the VECTOR geometry on the composite panel, so six
#: stacked layers are still separable. A sheet-side choice, stated on the panel -
#: the product declares no vector colour to read.
_VECTOR_CYCLE = ("#ff2fa0", "#00e5ff", "#ffe600", "#7cff4f", "#ff8a3d", "#c08cff")
#: A composite sheet's panels are too small for NATE to spot-check by eye - each
#: one is also saved full-size on its own. Width/dpi chosen so a panel this size
#: reads clearly at normal zoom, distinct from the grid's cramped ~800px cells.
_SINGLE_PANEL_W = 10.5
_SINGLE_DPI = 150
#: Strips the composite sheet's own suffix off its filename to get the shared
#: prefix every per-panel file is named from.
_CANVAS_SUFFIX_RE = re.compile(r"_canvas_layers$")


class RenderProofError(RuntimeError):
    """There is no sheet to draw, and the message says why."""


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #
def _layers_from_evidence(path: Path) -> tuple[list[dict], str]:
    blob = json.loads(path.read_text(encoding="utf-8"))
    evidence = blob.get("evidence") if isinstance(blob.get("evidence"), dict) else blob
    layers = evidence.get("layers") or []
    if not isinstance(layers, list):
        raise RenderProofError(f"{path}: 'layers' is not a list")
    title = str(evidence.get("tool") or path.stem)
    return [dict(x) for x in layers], title


def _layers_from_case(case_id: str) -> tuple[list[dict], str]:
    """The persisted Case's own ordered ``loaded_layer_summaries``."""
    import asyncio

    from trid3nt_server.persistence.persistence import make_persistence_for_backend

    state = asyncio.run(make_persistence_for_backend().get_session_state(case_id))
    rows = []
    for row in state.loaded_layers:
        rows.append(row if isinstance(row, dict) else row.model_dump())
    return rows, str(getattr(state.case, "title", case_id))


def _collapse_frames(layers: list[dict]) -> list[dict]:
    """Collapse an animation frame series to its FIRST frame + a count."""
    seen: dict[tuple, int] = {}
    out: list[dict] = []
    for layer in layers:
        stem = _FRAME_SUFFIX_RE.sub("", str(layer.get("name") or "")).strip()
        key = (stem, layer.get("role"), layer.get("style_preset"),
               layer.get("layer_type"))
        if stem != str(layer.get("name") or "").strip() and key in seen:
            out[seen[key]]["_frames"] = out[seen[key]].get("_frames", 1) + 1
            continue
        seen[key] = len(out)
        out.append(dict(layer))
    return out


# --------------------------------------------------------------------------- #
# Object store
# --------------------------------------------------------------------------- #
def _s3():
    import boto3

    return boto3.client("s3", endpoint_url=os.environ["AWS_ENDPOINT_URL"],
                        region_name=os.environ.get("AWS_REGION", "us-east-1"))


def _download(uri: str) -> str:
    bucket, _, key = uri[len("s3://"):].partition("/")
    fd, path = tempfile.mkstemp(suffix=Path(key).suffix or ".bin")
    os.close(fd)
    _s3().download_file(bucket, key, path)
    return path


# --------------------------------------------------------------------------- #
# Product styling
# --------------------------------------------------------------------------- #
def _raster_style(layer: dict) -> tuple[float | None, float | None, str | None, str]:
    """(vmin, vmax, colormap, basis) as the PRODUCT resolves them for this layer."""
    legend = layer.get("legend")
    if isinstance(legend, dict) and legend.get("kind") == "continuous":
        return (legend.get("vmin"), legend.get("vmax"),
                legend.get("colormap") or "viridis", "LayerURI.legend")
    from trid3nt_server.data.publish_layer.publish_layer import (
        _parse_style_params,
        _resolve_qgis_style_params,
    )

    params = _resolve_qgis_style_params(layer.get("style_preset") or "",
                                        str(layer.get("uri") or ""))
    if not params:
        return (None, None, None, "passthrough (QGIS auto-scale)")
    vmin, vmax, cmap = _parse_style_params(params)
    return vmin, vmax, cmap, "publish_layer style params"


def _preset_label(preset: str | None) -> str:
    from trid3nt_server.data.publish_layer.publish_layer import _label_from_style_preset

    return _label_from_style_preset(preset) or (preset or "(none)")


# --------------------------------------------------------------------------- #
# Layer payloads - read once, drawn twice (own panel + the composite)
# --------------------------------------------------------------------------- #
def _load_raster(uri: str) -> dict:
    import rasterio
    from rasterio.warp import Resampling, calculate_default_transform, reproject

    path = _download(uri) if uri.startswith("s3://") else uri
    try:
        with rasterio.open(path) as src:
            scale = max(1, int(max(src.width, src.height) / _MAX_READ_PX))
            out_h, out_w = max(1, src.height // scale), max(1, src.width // scale)
            bands = min(src.count, 3)
            src_transform = src.transform * src.transform.scale(
                src.width / out_w, src.height / out_h)
            data = src.read(list(range(1, bands + 1)),
                            out_shape=(bands, out_h, out_w),
                            resampling=Resampling.average, masked=False)
            dst_transform, w, h = calculate_default_transform(
                src.crs, "EPSG:3857", out_w, out_h, *src.bounds)
            arr = np.full((bands, h, w), np.nan, dtype="float32")
            for i in range(bands):
                reproject(source=data[i].astype("float32"), destination=arr[i],
                          src_transform=src_transform, src_crs=src.crs,
                          dst_transform=dst_transform, dst_crs="EPSG:3857",
                          resampling=Resampling.bilinear,
                          src_nodata=src.nodata, dst_nodata=np.nan)
            left, top = dst_transform * (0, 0)
            right, bottom = dst_transform * (w, h)
            bounds_ll = rasterio.warp.transform_bounds(src.crs, "EPSG:4326",
                                                       *src.bounds)
            colormap = None
            try:
                colormap = src.colormap(1)
            except (ValueError, IndexError):
                pass
    finally:
        if path != uri:
            Path(path).unlink(missing_ok=True)
    return {"array": arr, "extent": (left, right, bottom, top),
            "bbox_ll": bounds_ll, "bands": bands, "colormap": colormap}


def _load_vector(layer: dict) -> dict:
    import geopandas as gpd

    inline = layer.get("inline_geojson")
    uri = str(layer.get("uri") or "")
    if isinstance(inline, dict) and inline.get("features"):
        gdf = gpd.GeoDataFrame.from_features(inline["features"], crs="EPSG:4326")
    elif uri.startswith("s3://"):
        path = _download(uri)
        try:
            gdf = gpd.read_file(path)
        finally:
            Path(path).unlink(missing_ok=True)
    else:
        gdf = gpd.read_file(uri)
    if gdf.crs is None:
        gdf = gdf.set_crs("EPSG:4326")
    import shapely

    bbox_ll = tuple(gdf.to_crs("EPSG:4326").total_bounds)
    vertices = int(len(shapely.get_coordinates(gdf.geometry.values)))
    return {"gdf": gdf.to_crs("EPSG:3857"), "bbox_ll": bbox_ll,
            "count": int(len(gdf)), "vertices": vertices,
            "wireframe": vertices > _WIREFRAME_VERTICES}


def _load_mesh(layer: dict) -> dict:
    """A solver mesh (SELAFIN) as its real element connectivity."""
    from matplotlib.tri import Triangulation
    from pyproj import Transformer

    from trid3nt_server.workflows.telemac.postprocess_telemac import read_selafin

    uri = str(layer.get("uri") or "")
    path = _download(uri) if uri.startswith("s3://") else uri
    try:
        mesh = read_selafin(path)
    finally:
        if path != uri:
            Path(path).unlink(missing_ok=True)
    authid = str(layer.get("crs_authid") or "EPSG:4326")
    lon, lat = Transformer.from_crs(authid, "EPSG:4326", always_xy=True).transform(
        mesh["x"], mesh["y"])
    mx, my = MR.ll_to_merc(np.asarray(lon), np.asarray(lat))
    return {"tri": Triangulation(mx, my, mesh["ikle"][:, :3]),
            "bbox_ll": (float(np.min(lon)), float(np.min(lat)),
                        float(np.max(lon)), float(np.max(lat))),
            "nodes": int(len(mesh["x"])),
            "elements": int(len(mesh["ikle"])),
            "frames": int(len(mesh.get("times", []))),
            "varnames": list(mesh.get("varnames", []))}


def load_layer(layer: dict) -> dict:
    kind = str(layer.get("layer_type") or "")
    if kind == "raster":
        payload = _load_raster(str(layer["uri"]))
        vmin, vmax, cmap, basis = _raster_style(layer)
        payload.update(vmin=vmin, vmax=vmax, cmap=cmap, style_basis=basis)
        return payload
    if kind == "vector":
        return _load_vector(layer)
    if kind == "mesh":
        return _load_mesh(layer)
    raise ValueError(f"unrenderable layer_type {kind!r}")


# --------------------------------------------------------------------------- #
# Drawing
# --------------------------------------------------------------------------- #
def _draw_raster(ax, payload: dict, *, alpha: float):
    arr = payload["array"]
    if payload["bands"] >= 3:
        rgb = np.moveaxis(arr[:3], 0, -1)
        finite = rgb[np.isfinite(rgb)]
        hi = float(np.nanmax(finite)) if finite.size else 1.0
        rgb = np.clip(np.nan_to_num(rgb) / (hi or 1.0), 0.0, 1.0)
        return ax.imshow(rgb, extent=payload["extent"], origin="upper",
                         alpha=alpha, zorder=2)
    band = np.ma.masked_invalid(arr[0])
    vmin, vmax, cmap = payload["vmin"], payload["vmax"], payload["cmap"]
    if cmap is None:  # passthrough: QGIS auto-scales, so the panel does too
        finite = band.compressed()
        if finite.size:
            vmin, vmax = (float(np.percentile(finite, 2)),
                          float(np.percentile(finite, 98)))
        cmap = "gray"
    return ax.imshow(band, extent=payload["extent"], origin="upper", cmap=cmap,
                     vmin=vmin, vmax=vmax, alpha=alpha, zorder=2)


def _draw_vector(ax, payload: dict, *, color: str, zorder: int):
    gdf = payload["gdf"]
    geom_types = set(gdf.geom_type)
    if geom_types & {"Point", "MultiPoint"}:
        pts = gdf[gdf.geom_type.isin(["Point", "MultiPoint"])].explode(
            index_parts=False)
        ax.scatter(pts.geometry.x, pts.geometry.y,
                   s=90, marker="o", facecolor=color, edgecolor="white",
                   linewidths=1.2, zorder=zorder + 2)
    lines = gdf[gdf.geom_type.isin(["LineString", "MultiLineString"])]
    if len(lines):
        lines.plot(ax=ax, color=color, zorder=zorder,
                   linewidth=0.12 if payload["wireframe"] else 1.4,
                   alpha=0.7 if payload["wireframe"] else 1.0)
    polys = gdf[gdf.geom_type.isin(["Polygon", "MultiPolygon"])]
    if len(polys):
        # Mesh-preview and bank polygons are thousands of tiny cells: an outline
        # at this scale reads as the domain, a fill reads as a solid blob.
        polys.plot(ax=ax, facecolor="none", edgecolor=color, linewidth=0.25,
                   zorder=zorder)


def _draw_mesh(ax, payload: dict, *, color: str, zorder: int):
    ax.triplot(payload["tri"], color=color, linewidth=0.12, alpha=0.55,
               zorder=zorder)


def _draw(ax, layer: dict, payload: dict, *, color: str, zorder: int,
          alpha: float):
    kind = layer.get("layer_type")
    if kind == "raster":
        return _draw_raster(ax, payload, alpha=alpha)
    if kind == "vector":
        _draw_vector(ax, payload, color=color, zorder=zorder)
        return None
    _draw_mesh(ax, payload, color=color, zorder=zorder)
    return None


def _union_bbox(bboxes: list[tuple]) -> tuple[float, float, float, float]:
    xs0, ys0, xs1, ys1 = zip(*bboxes)
    return (min(xs0), min(ys0), max(xs1), max(ys1))


def _frame_axes(ax, mosaic, extent, bbox_ll):
    ax.imshow(np.asarray(mosaic), extent=extent, origin="upper", zorder=0)
    xw, yw = MR.ll_to_merc(np.array([bbox_ll[0], bbox_ll[2]]),
                           np.array([bbox_ll[1], bbox_ll[3]]))
    padx, pady = (xw[1] - xw[0]) * _PAD_FRAC, (yw[1] - yw[0]) * _PAD_FRAC
    ax.set_xlim(xw[0] - padx, xw[1] + padx)
    ax.set_ylim(yw[0] - pady, yw[1] + pady)
    ax.set_xticks([])
    ax.set_yticks([])


#: Caption wrap width, in characters. Panels are ~6 in wide at 7 pt, so a longer
#: line spills into the neighbouring panel's title.
_WRAP = 78


def _panel_caption(index: int, layer: dict, payload: dict) -> str:
    kind = str(layer.get("layer_type"))
    bits = [f"{index}. {layer.get('name')}",
            f"role={layer.get('role')}  kind={kind}  "
            f"preset={layer.get('style_preset') or '(none)'} "
            f"[{_preset_label(layer.get('style_preset'))}]"]
    if kind == "raster":
        rng = ("auto-scaled" if payload.get("cmap") is None
               else f"{payload['vmin']:.4g} to {payload['vmax']:.4g}"
               if payload.get("vmin") is not None else "unrescaled")
        bits.append(f"{payload['bands']}-band, {rng}, via {payload['style_basis']}")
    elif kind == "vector":
        bits.append(f"{payload['count']} feature(s), {payload['vertices']:,} "
                    f"vertices" + (" - drawn as a wireframe"
                                   if payload["wireframe"] else ""))
    else:
        bits.append(f"{payload['nodes']:,} nodes / {payload['elements']:,} elements"
                    + (f", {payload['frames']} time steps" if payload["frames"] else "")
                    + (f", vars {', '.join(v.strip() for v in payload['varnames'])}"
                       if payload["varnames"] else ""))
    if layer.get("_frames"):
        bits.append(f"FRAME SERIES: {layer['_frames']} frames on the canvas, "
                    f"first shown (the GIF renderer covers the animation)")
    return _wrap(bits)


def _wrap(lines: list[str]) -> str:
    import textwrap

    out: list[str] = []
    for line in lines:
        out.extend(textwrap.wrap(line, width=_WRAP) or [""])
    return "\n".join(out)


def _panel_slug(name: str) -> str:
    """Filesystem-safe slug for a layer name, used in the per-panel filename."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(name or "layer").lower()).strip("-")
    return slug or "layer"


def _panel_base(out_path: Path) -> str:
    """Shared filename prefix the per-panel files are named from."""
    return _CANVAS_SUFFIX_RE.sub("", out_path.stem)


def _single_panel_h(bbox_ll: tuple) -> float:
    xw, yw = MR.ll_to_merc(np.array([bbox_ll[0], bbox_ll[2]]),
                           np.array([bbox_ll[1], bbox_ll[3]]))
    return (float(np.clip((yw[1] - yw[0]) / (xw[1] - xw[0]), 0.45, 1.6))
            * _SINGLE_PANEL_W + 1.6)


def _save_full_panel(out_path: Path, *, mosaic, extent, bbox_ll, panel_h: float,
                     title: str, caption: str, draw_fn, colorbar: bool) -> Path:
    """One panel, full-size, its own file - same framing/basemap/caption as its
    grid cell, just legible at NATE's screen size instead of squeezed 3-up."""
    fig, ax = plt.subplots(figsize=(_SINGLE_PANEL_W, panel_h), dpi=_SINGLE_DPI)
    _frame_axes(ax, mosaic, extent, bbox_ll)
    artist = draw_fn(ax)
    if artist is not None and colorbar:
        cbar = fig.colorbar(artist, ax=ax, fraction=0.030, pad=0.01)
        cbar.ax.tick_params(labelsize=8)
    ax.set_title(caption, fontsize=10, loc="left", linespacing=1.4)
    fig.suptitle(title, fontsize=9, y=0.998)
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    return out_path


def _save_layer_panels(renderable: list[tuple[dict, dict]], out_path: Path, *,
                       mosaic, extent, bbox_ll, title: str) -> list[dict]:
    """Every renderable layer PLUS the stacked composite view, each its own
    full-size PNG, in emission order: ``<base>_panel_01_<layer-slug>.png``."""
    base = _panel_base(out_path)
    panel_h = _single_panel_h(bbox_ll)
    saved: list[dict] = []
    for i, (layer, payload) in enumerate(renderable):
        color = _VECTOR_CYCLE[i % len(_VECTOR_CYCLE)]
        path = out_path.parent / (
            f"{base}_panel_{i + 1:02d}_{_panel_slug(layer.get('name'))}.png")
        _save_full_panel(
            path, mosaic=mosaic, extent=extent, bbox_ll=bbox_ll, panel_h=panel_h,
            title=title, caption=_panel_caption(i + 1, layer, payload),
            draw_fn=lambda ax, layer=layer, payload=payload, color=color:
                _draw(ax, layer, payload, color=color, zorder=3, alpha=0.9),
            colorbar=payload.get("bands", 1) == 1)
        saved.append({"path": str(path), "layer": layer.get("name")})

    composite_index = len(renderable) + 1
    composite_path = out_path.parent / f"{base}_panel_{composite_index:02d}_canvas-view.png"
    composite_caption = _wrap([
        f"{composite_index}. CANVAS VIEW - all {len(renderable)} layers "
        f"stacked in emission order",
        "(vector colours cycle per layer so the stack stays separable; rasters "
        "keep their product styling)"])

    def _draw_composite(ax):
        for i, (layer, payload) in enumerate(renderable):
            _draw(ax, layer, payload, color=_VECTOR_CYCLE[i % len(_VECTOR_CYCLE)],
                 zorder=3 + i, alpha=0.75)
        return None

    _save_full_panel(
        composite_path, mosaic=mosaic, extent=extent, bbox_ll=bbox_ll,
        panel_h=panel_h, title=title, caption=composite_caption,
        draw_fn=_draw_composite, colorbar=False)
    saved.append({"path": str(composite_path), "layer": "CANVAS VIEW"})
    return saved


def render_sheet(layers: list[dict], out_path: Path, *, title: str,
                 max_tiles: int, composite_only: bool = False) -> dict:
    renderable, skipped = [], []
    for layer in layers:
        try:
            renderable.append((layer, load_layer(layer)))
        except Exception as exc:  # noqa: BLE001 - a layer that will not load IS a finding
            skipped.append({"name": layer.get("name"), "uri": layer.get("uri"),
                            "error": f"{type(exc).__name__}: {exc}"})
    if not renderable:
        raise RenderProofError(
            f"no renderable layer among {len(layers)}: {skipped}")

    bbox_ll = _union_bbox([p["bbox_ll"] for _, p in renderable])
    zoom = MR.pick_zoom(bbox_ll, max_tiles=max_tiles)
    mosaic, extent = MR.fetch_basemap(bbox_ll, zoom)

    panels = len(renderable) + 1
    rows = (panels + _COLS - 1) // _COLS
    # Panel height follows the CANVAS aspect, so a long thin reach does not sit
    # in a landscape box surrounded by whitespace.
    xw, yw = MR.ll_to_merc(np.array([bbox_ll[0], bbox_ll[2]]),
                           np.array([bbox_ll[1], bbox_ll[3]]))
    panel_w = 6.2
    panel_h = float(np.clip((yw[1] - yw[0]) / (xw[1] - xw[0]), 0.45, 1.6)) * panel_w + 1.1
    fig, axes = plt.subplots(rows, _COLS, figsize=(panel_w * _COLS, panel_h * rows),
                             dpi=_DPI)
    axes = np.atleast_1d(axes).ravel()

    for i, (layer, payload) in enumerate(renderable):
        ax = axes[i]
        _frame_axes(ax, mosaic, extent, bbox_ll)
        artist = _draw(ax, layer, payload,
                       color=_VECTOR_CYCLE[i % len(_VECTOR_CYCLE)],
                       zorder=3, alpha=0.9)
        if artist is not None and payload.get("bands", 1) == 1:
            cbar = fig.colorbar(artist, ax=ax, fraction=0.030, pad=0.01)
            cbar.ax.tick_params(labelsize=6)
        ax.set_title(_panel_caption(i + 1, layer, payload), fontsize=7,
                     loc="left", linespacing=1.5)

    composite = axes[len(renderable)]
    _frame_axes(composite, mosaic, extent, bbox_ll)
    for i, (layer, payload) in enumerate(renderable):
        _draw(composite, layer, payload,
              color=_VECTOR_CYCLE[i % len(_VECTOR_CYCLE)],
              zorder=3 + i, alpha=0.75)
    composite.set_title(_wrap([
        f"{len(renderable) + 1}. CANVAS VIEW - all {len(renderable)} layers "
        f"stacked in emission order",
        "(vector colours cycle per layer so the stack stays separable; rasters "
        "keep their product styling)"]),
        fontsize=7, loc="left", linespacing=1.5)

    for ax in axes[panels:]:
        ax.axis("off")

    emitted = sum(int(l.get("_frames") or 1) for l in layers)
    footer = [f"{title}  |  {emitted} emitted layer rows, {len(renderable)} "
              f"rendered  |  emission order = loaded_layers order (z_index "
              f"{[l.get('z_index') for l, _ in renderable]})  |  "
              f"ESRI World Imagery, EPSG:3857"]
    footer += [f"NOT RENDERED: {s['name']} -> {s['error']}" for s in skipped]
    fig.suptitle("\n".join(footer), fontsize=8, y=0.998)
    fig.tight_layout(rect=(0, 0, 1, 0.985))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)

    panel_pngs = [] if composite_only else _save_layer_panels(
        renderable, out_path, mosaic=mosaic, extent=extent, bbox_ll=bbox_ll,
        title=title)

    return {"sheet": str(out_path), "bytes": out_path.stat().st_size,
            "panels": panels, "layers": [l.get("name") for l, _ in renderable],
            "not_rendered": skipped, "panel_pngs": panel_pngs}


def render_from_evidence(evidence_path: str | os.PathLike[str], *,
                         out_path: str | os.PathLike[str] | None = None,
                         max_tiles: int = 6, composite_only: bool = False) -> dict:
    """Sheet from a drive script's evidence JSON. The drives' ``--render-proof``."""
    src = Path(evidence_path).resolve()
    layers, title = _layers_from_evidence(src)
    out = Path(out_path) if out_path else src.with_name(
        re.sub(r"_evidence$", "", src.stem) + "_canvas_layers.png")
    return render_sheet(_collapse_frames(layers), out, title=title,
                        max_tiles=max_tiles, composite_only=composite_only)


def render_proof(evidence_path: str | os.PathLike[str], *,
                 composite_only: bool = False) -> dict:
    """``--render-proof`` for a drive script: the sheet, or why there is none.

    Never raises. A refused run publishes no layers and a drive that asserts on
    the refusal still has to reach its assertions, so the absent sheet is
    REPORTED rather than thrown.
    """
    try:
        return render_from_evidence(evidence_path, composite_only=composite_only)
    except Exception as exc:  # noqa: BLE001 - the reason IS the report
        return {"sheet": None, "error": f"{type(exc).__name__}: {exc}"}


def add_render_proof_flag(ap: argparse.ArgumentParser) -> None:
    """``--render-proof`` (default on) / ``--no-render-proof`` on a drive script."""
    ap.add_argument("--render-proof", dest="render_proof", action="store_true",
                    default=True, help="contact sheet of every emitted layer "
                                       "(default on)")
    ap.add_argument("--no-render-proof", dest="render_proof",
                    action="store_false")


def main() -> int:
    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--evidence", help="a drive script's evidence JSON")
    src.add_argument("--case-id", help="a persisted Case, read for its loaded layers")
    ap.add_argument("--out", default=None)
    ap.add_argument("--title", default=None)
    ap.add_argument("--max-tiles", type=int, default=6)
    ap.add_argument("--composite-only", action="store_true", default=False,
                    help="skip the per-layer full-size PNGs, sheet only "
                         "(old behavior)")
    ns = ap.parse_args()

    try:
        if ns.evidence:
            result = render_from_evidence(ns.evidence, out_path=ns.out,
                                          max_tiles=ns.max_tiles,
                                          composite_only=ns.composite_only)
        else:
            layers, title = _layers_from_case(ns.case_id)
            out = Path(ns.out or (REPO / "docs" / "proof" / "templates"
                                  / f"case_{ns.case_id}_canvas_layers.png"))
            result = render_sheet(_collapse_frames(layers), out,
                                  title=ns.title or title, max_tiles=ns.max_tiles,
                                  composite_only=ns.composite_only)
    except RenderProofError as exc:
        print(f"no sheet: {exc}")
        return 1
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
