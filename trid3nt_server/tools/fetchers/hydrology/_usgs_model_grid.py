"""Published USGS MODFLOW model grids shared by the rows that read them.

The two ScienceBase archives (Cape Cod JBCC, Des Moines MF6) as facts: grid
constants, member paths, the covering-grid choice and its typed refusal, the one
archive download, and the masked window warped to an EPSG:4326 COG. No value
lives here; the row packages say which member they read and what it means."""

from __future__ import annotations

import io
import zipfile
from typing import Any, Callable

from trid3nt_contracts.source_spec import SourceSpec
from trid3nt_server.render.cog import array_to_cog_bytes

from .._router.errors import router_input_error, router_upstream_error
from .._router.hooks import FrameDegraded
from .._router.transport import TransportError, get_client

__all__ = [
    "ARCHIVES",
    "ring_lonlat",
    "covering_archive",
    "archive_bytes",
    "read_grid",
    "masked_window_cog",
]

#: An AOI may poke this far outside a quad and still count as covered: the
#: rings are stated at 6 dp, so a corner's rounding sliver is not a partial
#: overlap worth refusing.
_COVER_EPS = 1e-4

#: The two published archives as FACTS off their ScienceBase items and each
#: model's own grid definition; no K value lives here. ``native`` is the grid's
#: full (west, south, east, north) in ``crs`` - the JBCC georeferencing's
#: corners, and the Des Moines DIS extent (its modelgeoref UL northing carries
#: an 80 m typo against LAYCBD/DEL*; the DIS text is the authority). ``factor``
#: takes the model's length unit to metres. ``datum`` is the vertical datum the
#: archive's layer tops and bottoms count from: the JBCC data release metadata
#: (sir2018-5139.xml, altsys) states NGVD 29 in feet, the Des Moines readme
#: states the surfs files as "meters above North American Vertical Datum of
#: 1988". ``top_member`` is layer 1's top; ``bot_member`` is layer n's bottom.
ARCHIVES: dict[str, dict[str, Any]] = {
    "jbcc": {
        "endpoint": "jbcc_model",
        "label": "Cape Cod JBCC MODFLOW",
        "crs": "EPSG:26786",
        "native": (812245.0, 178498.0, 972245.0, 294498.0),
        "shape": (290, 400),
        "layers": 32,
        # K stored in ft/d over a US survey foot grid -> the row's m/day.
        "factor": 0.30480060960121924,
        "datum": "NGVD29",
        "k_member": "model/externalfiles/Ks/mmr_pp{n}._kx",
        "mask_member": "model/externalfiles/Ibs/ib{n}",
        "top_member": "model/externalfiles/Bots/model_top_c2_3",
        "bot_member": "model/externalfiles/Bots/bot{n}",
    },
    "des_moines": {
        "endpoint": "des_moines_model",
        "label": "Des Moines MF6 MODFLOW",
        "crs": "EPSG:26915",
        "native": (429070.0, 4604260.0, 452050.0, 4629880.0),
        "shape": (427, 383),
        "layers": 6,
        "factor": 1.0,  # native m/day
        "datum": "NAVD88",
        "k_member": "external_files/props/l{n}._kx",
        "mask_member": "external_files/id/l{n}_id.dat",
        "top_member": "external_files/surfs/l1_top.dat",
        "bot_member": "external_files/surfs/l{n}_bot.dat",
    },
}


def ring_lonlat(arch: dict[str, Any]) -> list[tuple[float, float]]:
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
        key: Polygon(ring_lonlat(arch)).intersection(aoi).area / aoi.area
        for key, arch in ARCHIVES.items()
    }


def covering_archive(spec: SourceSpec, bbox: tuple[float, ...]) -> str:
    """The key of the single grid that fully covers the AOI; an AOI outside
    both grids is a typed refusal naming them and their share."""
    sc, sfx = spec.error_code_prefix, spec.input_error_suffix
    fracs = _covered_fractions(bbox)
    covering = next(
        (key for key, frac in fracs.items() if frac >= 1.0 - _COVER_EPS), None
    )
    if covering is None:
        named = "; ".join(
            f"{ARCHIVES[key]['label']} covers {frac:.0%} of the AOI"
            for key, frac in fracs.items()
        )
        raise router_input_error(
            sc,
            f"bbox {list(bbox)} is outside the published USGS MODFLOW model "
            f"grids this row serves ({named}); the row reaches Cape Cod (JBCC) "
            "and Des Moines only",
            sfx,
        )
    return covering


def archive_bytes(
    spec: SourceSpec, key: str, get_bytes: Callable[..., Any]
) -> bytes:
    """The archive through an INNER constant-key read-through: every frame of
    a call shares ONE live download, keyed by the archive alone. The cache
    identity and the endpoints are ``fetch_usgs_model_k``'s, so every row
    reading an archive shares its one cached download; a failure raises under
    the calling row's ``spec`` error prefix."""
    from ...cache import read_through
    from .._router.registration import get_spec
    from .._router.router import synthesize_metadata
    from .._router.spec import record_shape

    owner = get_spec("fetch_usgs_model_k")
    arch = ARCHIVES[key]
    endpoint = owner.endpoints[arch["endpoint"]]

    def _fetch() -> bytes:
        try:
            body, _ct, _url = get_bytes(
                get_client(),
                endpoint.url,
                headers={"User-Agent": owner.auth.user_agent},
            )
        except TransportError as exc:
            raise router_upstream_error(
                spec.error_code_prefix,
                f"{type(exc).__name__}: {exc}",
                exc.retryable,
            ) from exc
        return body

    result = read_through(
        metadata=synthesize_metadata(owner),
        params={"file": arch["endpoint"]},
        ext="zip",
        fetch_fn=_fetch,
        record_shape=record_shape(owner),
    )
    assert result.data is not None, "the archive cache entry holds bytes"
    return result.data


def read_grid(
    zf: zipfile.ZipFile, member: str, arch: dict[str, Any]
) -> Any:
    """One K, surface or mask member as the model's own (nrow, ncol) float
    grid; a value count off the stated grid raises ValueError (corrupt member)."""
    import numpy as np

    arr = np.array(zf.read(member).decode().split(), dtype=np.float64)
    nrow, ncol = arch["shape"]
    if arr.size != nrow * ncol:
        raise ValueError(f"{member} has {arr.size} values, expected {nrow * ncol}")
    return arr.reshape(nrow, ncol)


def masked_window_cog(
    arch: dict[str, Any],
    values: Any,
    mask: Any,
    bbox: tuple[float, ...],
    what: str,
    shift: Callable[[Any, Any], Any] | None = None,
) -> bytes:
    """One model-grid array as an EPSG:4326 COG over the AOI: the touched
    window, active cells only, warped nearest, cropped to cells whose centres
    fall in the bbox. ``shift(xs, ys)`` is added to the warped cells (lon/lat
    centres in, one value per cell out). A window with no active cell raises
    ``FrameDegraded`` naming ``what``."""
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

    label = arch["label"]
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
        raise FrameDegraded(f"{label} {what}: AOI misses the grid")
    # Both masks agree on 1 = active: JBCC ib is {0,1}; DM idomain is {-1,0,1}
    # and its -1 cells are not part of the flow array.
    arr = np.where(mask[r0:r1, c0:c1] == 1, values[r0:r1, c0:c1], np.nan)
    if not np.isfinite(arr).any():
        raise FrameDegraded(f"{label} {what} has no active cells over this AOI")
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
        raise FrameDegraded(f"{label} {what} has no active cells over this AOI")
    if shift is not None:
        out = (out + shift(xs[keep_c][None, :], ys[keep_r][:, None])).astype(
            "float32"
        )
    out_t = dst_t * rasterio.Affine.translation(
        int(np.argmax(keep_c)), int(np.argmax(keep_r))
    )
    return array_to_cog_bytes(out, out_t, "EPSG:4326")
