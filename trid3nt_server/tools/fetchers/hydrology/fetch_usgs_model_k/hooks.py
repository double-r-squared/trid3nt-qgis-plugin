"""usgs_model_k frames hooks: one plan per model layer over the covering grid.

The output is an ORDERED list of layer frames of the ONE archive whose grid
fully covers the AOI; the bytes hook reads that layer's K + mask members from
the constant archive, masks to active cells, warps the touched window to an
EPSG:4326 COG cropped to cells whose centers fall in the bbox. An AOI outside
both grids is a typed input refusal naming them; a layer with no active cells
degrades and is recorded, and only EVERY layer degrading raises the EMPTY."""

from __future__ import annotations

import io
import logging
import zipfile
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec
from trid3nt_server.render.cog import array_to_cog_bytes

from ..._router.errors import router_input_error, router_upstream_error
from ..._router.hooks import FrameDegraded, FramePlan, register_hook
from ..._router.transport import TransportError, get_bytes, get_client

logger = logging.getLogger(__name__)

__all__ = ["frames_plan", "frame_bytes"]

#: An AOI may poke this far outside a quad and still count as covered: the
#: rings are stated at 6 dp, so a corner's rounding sliver is not a partial
#: overlap worth refusing.
_COVER_EPS = 1e-4

#: The two published archives as FACTS off their ScienceBase items and each
#: model's own grid definition; no K value lives here. ``native`` is the grid's
#: full (west, south, east, north) in ``crs`` - the JBCC georeferencing's
#: corners, and the Des Moines DIS extent (its modelgeoref UL northing carries
#: an 80 m typo against LAYCBD/DEL*; the DIS text is the authority).
_ARCHIVES: dict[str, dict[str, Any]] = {
    "jbcc": {
        "endpoint": "jbcc_model",
        "label": "Cape Cod JBCC MODFLOW",
        "crs": "EPSG:26786",
        "native": (812245.0, 178498.0, 972245.0, 294498.0),
        "shape": (290, 400),
        "layers": 32,
        # K stored in ft/d over a US survey foot grid -> the row's m/day.
        "factor": 0.30480060960121924,
        "k_member": "model/externalfiles/Ks/mmr_pp{n}._kx",
        "mask_member": "model/externalfiles/Ibs/ib{n}",
    },
    "des_moines": {
        "endpoint": "des_moines_model",
        "label": "Des Moines MF6 MODFLOW",
        "crs": "EPSG:26915",
        "native": (429070.0, 4604260.0, 452050.0, 4629880.0),
        "shape": (427, 383),
        "layers": 6,
        "factor": 1.0,  # native m/day
        "k_member": "external_files/props/l{n}._kx",
        "mask_member": "external_files/id/l{n}_id.dat",
    },
}


def _ring_lonlat(arch: dict[str, Any]) -> list[tuple[float, float]]:
    """The grid's four corners as a lon/lat quad; the row's coverage rings are
    the 6 dp copies of exactly this."""
    from rasterio.warp import transform as warp_transform

    west, south, east, north = arch["native"]
    lons, lats = warp_transform(
        arch["crs"],
        "EPSG:4326",
        [west, east, east, west],
        [north, north, south, south],
    )
    return list(zip(lons, lats))


def _covered_fractions(
    bbox: tuple[float, ...],
) -> dict[str, float]:
    """Each grid's share of the AOI, by quad intersection; a covering grid
    reads ~1.0, a grid an ocean AOI never touches reads 0.0."""
    from shapely.geometry import Polygon, box

    aoi = box(*bbox)
    return {
        key: Polygon(_ring_lonlat(arch)).intersection(aoi).area / aoi.area
        for key, arch in _ARCHIVES.items()
    }


@register_hook("usgs_model_k.frames_plan")
def frames_plan(spec: SourceSpec, params: dict[str, Any]) -> list[FramePlan]:
    """One FramePlan per layer of the single grid that fully covers the AOI;
    an AOI outside both grids is a typed refusal naming them and their share."""
    sc, sfx = spec.error_code_prefix, spec.input_error_suffix
    q_bbox = tuple(round(float(v), 6) for v in params["bbox"])
    fracs = _covered_fractions(q_bbox)
    covering = next(
        (key for key, frac in fracs.items() if frac >= 1.0 - _COVER_EPS), None
    )
    if covering is None:
        named = "; ".join(
            f"{_ARCHIVES[key]['label']} covers {frac:.0%} of the AOI"
            for key, frac in fracs.items()
        )
        raise router_input_error(
            sc,
            f"bbox {list(q_bbox)} is outside the published USGS MODFLOW model "
            f"grids this row serves ({named}); the row reaches Cape Cod (JBCC) "
            "and Des Moines only",
            sfx,
        )
    arch = _ARCHIVES[covering]
    plans = [
        FramePlan(
            cache_params={
                "bbox": list(q_bbox),
                "archive": covering,
                "layer": n,
                "tool": "fetch_usgs_model_k",
            },
            name=f"{arch['label']} K layer {n}",
            layer_id=(
                f"model-k-{covering}-l{n}-{q_bbox[0]:.3f}-{q_bbox[1]:.3f}"
            ),
            bbox=q_bbox,  # type: ignore[arg-type]
            fetch_context={"archive": covering, "layer": n},
        )
        for n in range(1, int(arch["layers"]) + 1)
    ]
    logger.info(
        "usgs_model_k: bbox %s -> %s frames=%d (covered=%.4f)",
        list(q_bbox),
        arch["label"],
        len(plans),
        fracs[covering],
    )
    return plans


def _archive_bytes(spec: SourceSpec, key: str) -> bytes:
    """The archive through an INNER constant-key read-through: every frame of
    a call shares ONE live download, keyed by the archive alone."""
    from ....cache import read_through
    from ..._router.router import synthesize_metadata
    from ..._router.spec import record_shape

    arch = _ARCHIVES[key]
    endpoint = spec.endpoints[arch["endpoint"]]

    def _fetch() -> bytes:
        try:
            body, _ct, _url = get_bytes(
                get_client(),
                endpoint.url,
                headers={"User-Agent": spec.auth.user_agent},
            )
        except TransportError as exc:
            raise router_upstream_error(
                spec.error_code_prefix,
                f"{type(exc).__name__}: {exc}",
                exc.retryable,
            ) from exc
        return body

    result = read_through(
        metadata=synthesize_metadata(spec),
        params={"file": arch["endpoint"]},
        ext="zip",
        fetch_fn=_fetch,
        record_shape=record_shape(spec),
    )
    assert result.data is not None, "the archive cache entry holds bytes"
    return result.data


def _read_grid(
    zf: zipfile.ZipFile, member: str, arch: dict[str, Any]
) -> Any:
    """One K or mask member as the model's own (nrow, ncol) float grid; a
    value count off the stated grid raises ValueError (corrupt member)."""
    import numpy as np

    arr = np.array(zf.read(member).decode().split(), dtype=np.float64)
    nrow, ncol = arch["shape"]
    if arr.size != nrow * ncol:
        raise ValueError(f"{member} has {arr.size} values, expected {nrow * ncol}")
    return arr.reshape(nrow, ncol)


@register_hook("usgs_model_k.frame_bytes")
def frame_bytes(
    spec: SourceSpec, params: dict[str, Any], frame: FramePlan
) -> bytes:
    """Build ONE layer's EPSG:4326 COG; an unreadable member or a layer with
    no active cells raises ``FrameDegraded`` for the executor to record, while
    the archive download itself stays a typed upstream error."""
    ctx = frame.fetch_context
    key = str(ctx["archive"])
    arch = _ARCHIVES[key]
    layer = int(ctx["layer"])
    bbox = tuple(float(v) for v in frame.cache_params["bbox"])
    blob = _archive_bytes(spec, key)
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            k = _read_grid(zf, arch["k_member"].format(n=layer), arch)
            mask = _read_grid(zf, arch["mask_member"].format(n=layer), arch)
    except (zipfile.BadZipFile, KeyError, ValueError, OSError) as exc:
        raise FrameDegraded(
            f"{arch['label']} layer {layer}: unreadable archive member ({exc})"
        ) from exc

    import numpy as np
    import rasterio
    from rasterio.transform import from_origin
    from rasterio.warp import (
        Resampling,
        calculate_default_transform,
        reproject,
        transform_bounds,
    )
    from rasterio.windows import from_bounds

    nrow, ncol = arch["shape"]
    west, south, east, north = arch["native"]
    xres = (east - west) / ncol
    yres = (north - south) / nrow
    grid = from_origin(west, north, xres, yres)
    # The touched window is floored to whole cells and clipped to the grid, so
    # an AOI inside the quad can never reach past the model's own array.
    nb = transform_bounds("EPSG:4326", arch["crs"], *bbox)
    win = from_bounds(*nb, transform=grid)
    r0 = max(int(np.floor(win.row_off)), 0)
    c0 = max(int(np.floor(win.col_off)), 0)
    r1 = min(int(np.ceil(win.row_off + win.height)), nrow)
    c1 = min(int(np.ceil(win.col_off + win.width)), ncol)
    if r1 <= r0 or c1 <= c0:
        raise FrameDegraded(f"{arch['label']} layer {layer}: AOI misses the grid")
    # Both masks agree on 1 = active: JBCC ib is {0,1}; DM idomain is {-1,0,1}
    # and its -1 cells are not part of the flow array.
    arr = (
        np.where(mask[r0:r1, c0:c1] == 1, k[r0:r1, c0:c1], np.nan)
        * float(arch["factor"])
    )
    if not np.isfinite(arr).any():
        raise FrameDegraded(
            f"{arch['label']} layer {layer} has no active cells over this AOI"
        )
    win_t = from_origin(west + c0 * xres, north - r0 * yres, xres, yres)
    sh, sw = arr.shape
    dst_t, dw, dh = calculate_default_transform(
        arch["crs"],
        "EPSG:4326",
        sw,
        sh,
        win_t.c,
        win_t.f - sh * yres,
        win_t.c + sw * xres,
        win_t.f,
    )
    dst = np.full((dh, dw), np.nan, dtype="float32")
    reproject(
        arr.astype("float32"),
        dst,
        src_transform=win_t,
        src_crs=arch["crs"],
        dst_transform=dst_t,
        dst_crs="EPSG:4326",
        resampling=Resampling.nearest,
        src_nodata=np.nan,
        dst_nodata=np.nan,
    )
    # The warp grid spans the whole touched window; the COG carries only cells
    # whose CENTERS fall inside the bbox, so a sliver AOI keeps whole cells and
    # the frame's own cells ARE the row's stated coverage of the ask.
    xs = dst_t.c + (np.arange(dw) + 0.5) * dst_t.a
    ys = dst_t.f + (np.arange(dh) + 0.5) * dst_t.e
    keep_c = (xs >= bbox[0]) & (xs <= bbox[2])
    keep_r = (ys >= bbox[1]) & (ys <= bbox[3])
    out = dst[np.ix_(keep_r, keep_c)]
    if out.size == 0 or not np.isfinite(out).any():
        raise FrameDegraded(
            f"{arch['label']} layer {layer} has no active cells over this AOI"
        )
    out_t = dst_t * rasterio.Affine.translation(
        int(np.argmax(keep_c)), int(np.argmax(keep_r))
    )
    logger.info(
        "usgs_model_k: %s layer %d -> %dx%d cells, %d active",
        arch["label"],
        layer,
        int(out.shape[1]),
        int(out.shape[0]),
        int(np.isfinite(out).sum()),
    )
    return array_to_cog_bytes(out, out_t, "EPSG:4326")
