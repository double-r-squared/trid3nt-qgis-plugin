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

from .._fetch_common import FetchError
from .._router.errors import router_empty_error, router_input_error, router_upstream_error
from .._router.hooks import FrameDegraded
from .._router.transport import TransportError, get_client

__all__ = [
    "ARCHIVES",
    "ring_lonlat",
    "covering_archive",
    "archive_bytes",
    "read_grid",
    "read_head",
    "last_active_bottom",
    "head_column",
    "TARGET",
    "datum_offset_m",
    "shift_to_navd88",
    "masked_window",
    "masked_window_cog",
]

#: The frame the rows state their elevations on; a model counted from another
#: frame is converted onto it, and one already on it is read as stated.
TARGET = "NAVD88"

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
#: ``head_member`` is the model's starting-head array in ``head_format`` (text:
#: the model's text array; mf6_binary: one MF6 binary record per layer) times ``head_factor``, the multiplier the model's own initial-condition
#: input states for it (JBCC MODEL.BA6 1.0, Des Moines dmww_tr6.ic 1.001).
#: JBCC states one head array for every layer; Des Moines states one record per
#: layer, and a cell's head is the record of its topmost ACTIVE layer.
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
        "head_member": "model/externalfiles/Init_Heads/start_heads",
        "head_format": "text",
        "head_factor": 1.0,
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
        "head_member": "external_files/ic/dmww_strt.hds",
        "head_format": "mf6_binary",
        "head_factor": 1.001,
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
    both grids is a typed refusal naming them and their share, and an AOI a
    grid holds only in part is the row's EMPTY, so the next source takes it."""
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
        if any(frac > _COVER_EPS for frac in fracs.values()):
            raise router_empty_error(
                sc,
                f"bbox {list(bbox)} is held by no single published USGS MODFLOW "
                f"model grid ({named}); a run takes a model only where its grid "
                "holds the whole domain",
                spec.empty_error_suffix,
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


def read_head(zf: zipfile.ZipFile, arch: dict[str, Any]) -> Any:
    """The model's starting head as an (nrow, ncol) grid in the model's own
    length unit and datum, times the multiplier its initial-condition input
    states: per cell the head of the topmost ACTIVE layer (the one array on
    JBCC, that layer's record on Des Moines). A cell no layer holds active, or
    whose topmost active layer has no head (1e30), reads NaN; a record count or
    size off the stated grid raises ValueError."""
    import numpy as np

    member, (nrow, ncol) = arch["head_member"], arch["shape"]
    layers = int(arch["layers"])
    if arch["head_format"] == "text":
        table = read_grid(zf, member, arch)
        table[np.abs(table) > 1e20] = np.nan
        records = None
    else:
        raw = zf.read(member)
        header = np.dtype([("kstp", "<i4"), ("kper", "<i4"), ("pertim", "<f8"),
                           ("totim", "<f8"), ("text", "S16"), ("ncol", "<i4"),
                           ("nrow", "<i4"), ("ilay", "<i4")])
        size = header.itemsize + 8 * nrow * ncol
        if len(raw) < size * layers:
            raise ValueError(f"{member} holds {len(raw)} bytes, {layers} records need {size * layers}")
        records = []
        for layer in range(1, layers + 1):
            first = np.frombuffer(raw, header, 1, (layer - 1) * size)[0]
            if (int(first["ncol"]), int(first["nrow"]), int(first["ilay"])) != (ncol, nrow, layer):
                raise ValueError(f"{member} record {layer} is a {int(first['nrow'])} x {int(first['ncol'])} "
                                 f"record for layer {int(first['ilay'])}, expected {nrow} x {ncol} for layer {layer}")
            record = np.frombuffer(raw, "<f8", nrow * ncol, (layer - 1) * size + header.itemsize)
            records.append(record.reshape(nrow, ncol).copy())
            records[-1][np.abs(records[-1]) > 1e20] = np.nan
    head = np.full((nrow, ncol), np.nan)
    done = np.zeros((nrow, ncol), dtype=bool)
    for layer in range(1, layers + 1):
        active = read_grid(zf, arch["mask_member"].format(n=layer), arch) == 1
        take = active & ~done
        head[take] = (table if records is None else records[layer - 1])[take]
        done |= active
    return head * float(arch["head_factor"])


def last_active_bottom(zf: zipfile.ZipFile, arch: dict[str, Any]) -> Any:
    """Per cell, the bottom of its deepest ACTIVE layer in the model's own unit
    and datum (NaN where no layer is active)."""
    import numpy as np

    nrow, ncol = arch["shape"]
    bottom = np.full((nrow, ncol), np.nan)
    for layer in range(int(arch["layers"]), 0, -1):
        active = read_grid(zf, arch["mask_member"].format(n=layer), arch) == 1
        take = active & np.isnan(bottom)
        bottom[take] = read_grid(zf, arch["bot_member"].format(n=layer), arch)[take]
    return bottom


def head_column(zf: zipfile.ZipFile, arch: dict[str, Any]) -> tuple[Any, Any, Any]:
    """The model's head and saturated thickness per cell, and where BOTH are measured.

    ``(head, thickness, measured)``: the head of the topmost active layer in the model's own datum and
    metres, the head less the bottom of the deepest active layer in metres, and the cells where a layer is
    active and the head stands above that bottom. One mask serves the water table and the thickness, so a run
    takes both from the model over the same cells or neither."""
    import numpy as np

    head = read_head(zf, arch)
    thickness = (head - last_active_bottom(zf, arch)) * float(arch["factor"])
    measured = np.isfinite(head) & np.isfinite(thickness) & (thickness > 0.0)
    return head * float(arch["factor"]), thickness, measured.astype(int)


def datum_offset_m(spec: SourceSpec, lon: float, lat: float, frame: str) -> float:
    """What VDatum says a zero on ``frame`` reads at on NAVD88 at this point;
    its refusal is raised as this row's own, naming the point."""
    from trid3nt_server.tools import TOOL_REGISTRY

    try:
        record = TOOL_REGISTRY["fetch_vertical_datum_offset"].fn(
            point=[lon, lat], from_frame=frame.lower(), to_frame=TARGET.lower())
    except FetchError as exc:
        message = (
            f"the {frame} elevations of the model cannot be put on "
            f"{TARGET}: NOAA VDatum has no offset at ({lon:.6f}, {lat:.6f}) - "
            f"fetch_vertical_datum_offset says ({getattr(exc, 'error_code', '')}): {exc}")
        if getattr(exc, "retryable", False):
            raise router_upstream_error(spec.error_code_prefix, message) from exc
        raise router_input_error(
            spec.error_code_prefix, message, spec.input_error_suffix) from exc
    return float(record["offset_m"])


def shift_to_navd88(spec: SourceSpec, bbox: tuple[float, ...], frame: str):
    """The offset onto NAVD88 across the AOI: VDatum's offset at each of the
    AOI's four corners, bilinear between them. A datum offset is a smooth
    field, so the corners the caller named bound it without a point chosen
    for them. The offsets are asked when the shift is first applied, so a
    frame with no active cell never asks."""
    west, south, east, north = bbox

    def shift(xs: Any, ys: Any) -> Any:
        sw, se, nw, ne = (
            datum_offset_m(spec, lon, lat, frame)
            for lon, lat in ((west, south), (east, south), (west, north), (east, north)))
        u = (xs - west) / max(east - west, 1e-12)
        v = (ys - south) / max(north - south, 1e-12)
        return (1 - u) * (1 - v) * sw + u * (1 - v) * se + (1 - u) * v * nw + u * v * ne

    return shift


def masked_window_cog(
    arch: dict[str, Any],
    values: Any,
    mask: Any,
    bbox: tuple[float, ...],
    what: str,
    shift: Callable[[Any, Any], Any] | None = None,
) -> bytes:
    """``masked_window`` written as an EPSG:4326 COG."""
    out, out_t = masked_window(arch, values, mask, bbox, what, shift)
    return array_to_cog_bytes(out, out_t, "EPSG:4326")


def masked_window(
    arch: dict[str, Any],
    values: Any,
    mask: Any,
    bbox: tuple[float, ...],
    what: str,
    shift: Callable[[Any, Any], Any] | None = None,
    whole: bool = False,
) -> tuple[Any, Any]:
    """One model-grid array over the AOI as (array, EPSG:4326 transform): the
    touched window, active cells only, warped nearest, cropped to cells whose
    centres fall in the bbox. ``shift(xs, ys)`` is added to the warped cells
    (lon/lat centres in, one value per cell out). A window with no active cell
    raises ``FrameDegraded`` naming ``what``, and so does one with any cell
    unmeasured where ``whole`` asks for every cell of the box."""
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
    if whole and not np.isfinite(out).all():
        raise FrameDegraded(
            f"{label} {what} measures {int(np.isfinite(out).sum())} of {out.size} cells over this AOI; "
            "a run takes the model only where it measures every cell")
    if shift is not None:
        out = (out + shift(xs[keep_c][None, :], ys[keep_r][:, None])).astype(
            "float32"
        )
    out_t = dst_t * rasterio.Affine.translation(
        int(np.argmax(keep_c)), int(np.argmax(keep_r))
    )
    return out, out_t
