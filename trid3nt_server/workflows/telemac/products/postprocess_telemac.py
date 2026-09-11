"""A solved open-water result SELAFIN -> the peak raster COGs and their scalars.

The wave, agitation, 3D and coastal readers. Each emits ONE peak
COG as the map anchor and narration carrier; the time animation rides the result
SELAFIN itself, published as a mesh layer, so NO per-frame COGs are written."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from trid3nt_contracts.telemac_contracts import (
    TELEMAC3D_SIGNED_STYLE,
    TELEMAC3D_STRATIFICATION_STYLE,
    TELEMAC_AGITATION_STYLE,
    TELEMAC_COASTAL_DEPTH_STYLE,
    TELEMAC_WAVE_STYLE,
    ArtemisAgitationLayerURI,
    Telemac3dLayerURI,
    TelemacCoastalLayerURI,
    TelemacWaveLayerURI,
)
from trid3nt_server.emission import presets
from trid3nt_server.workflows.publishing import cog as cog_io
from trid3nt_server.workflows.publishing import raster
from trid3nt_server.workflows.publishing.cog import RUNS_BUCKET_DEFAULT, CogIoError
from trid3nt_server.workflows.telemac.modules.outputs import read_selafin


__all__ = [
    "PostprocessTelemacError",
    "postprocess_tomawac",
    "postprocess_artemis",
    "postprocess_telemac3d",
    "postprocess_coastal",
    "TELEMAC_WAVE_STYLE",
    "TELEMAC_AGITATION_STYLE",
    "TELEMAC_TARGET_GROUND_RES_M",
    "TELEMAC_WSE_WET_DEPTH_M",
]

logger = logging.getLogger("trid3nt_server.workflows.telemac.products.postprocess_telemac")

TELEMAC_TARGET_GROUND_RES_M: float = raster.TARGET_GROUND_RES_M

#: Water-depth floor (m) above which a node counts as WET for the max-WSE raster.
#: TELEMAC's FREE SURFACE equals the BED elevation at a dry node (depth 0), so an
#: unmasked max-over-time of FREE SURFACE would paint dry terrain as a water
#: surface. We take the peak FREE SURFACE only over frames where WATER DEPTH
#: exceeds this floor, so a never-wetted node reads NaN (no water), never its bed
#: elevation. 1 cm mirrors the flood engines' wet threshold.
TELEMAC_WSE_WET_DEPTH_M: float = 0.01


class PostprocessTelemacError(RuntimeError):
    """Raised on read / rasterize / COG-write / upload failures.

    ``error_code`` is open-set, so the emitter renders a typed error frame."""
    # TELEMAC_OUTPUT_READ_FAILED   -- could not parse the SELAFIN.
    # TELEMAC_OUTPUT_EMPTY         -- no time steps, or no wet node on a
    #                                 free-surface read; a wholly dry DEPTH
    #                                 field is a result, not empty.
    # TELEMAC_DEPENDENCY_MISSING   -- numpy / scipy / rasterio not importable.
    # TELEMAC_COG_WRITE_FAILED     -- rasterio could not write the COG.
    # TELEMAC_CRS_TAG_MISMATCH     -- the COG CRS tag did not round-trip.
    # TELEMAC_COG_UPLOAD_FAILED    -- the runs-bucket upload failed.

    error_code: str = "POSTPROCESS_TELEMAC_FAILED"

    def __init__(
        self,
        error_code: str,
        *,
        message: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message or error_code)
        self.error_code = error_code
        self.details: dict[str, Any] = dict(details or {})


#: Water-depth variable names (English + French) the wet mask is built from.
_DEPTH_VAR_KEYS: tuple[str, ...] = ("WATER DEPTH", "HAUTEUR D'EAU", "HAUTEUR D EAU")
#: Static bed-elevation variable names (English + French). Read to reproduce the
#: worker's own ``bed > initial water line`` discrimination on the raster.
_BED_VAR_KEYS: tuple[str, ...] = ("BOTTOM", "FOND")


def _pick_named_var(varnames: list[str], keys: tuple[str, ...], letter: str) -> str | None:
    """First variable whose (upper, trimmed) name contains any of ``keys``.

    Falls back to an EXACT mnemonic match, else ``None``; it never guesses."""
    for v in varnames:
        u = v.strip().upper()
        for k in keys:
            if k in u:
                return v
    for v in varnames:
        if v.strip().upper() == letter:
            return v
    return None


def _reraise_cogio(exc: CogIoError) -> "PostprocessTelemacError":
    codes = {
        "DEPENDENCY": "TELEMAC_DEPENDENCY_MISSING",
        "WRITE": "TELEMAC_COG_WRITE_FAILED",
        "REPROJECT": "TELEMAC_COG_WRITE_FAILED",
        "CRS_MISMATCH": "TELEMAC_CRS_TAG_MISMATCH",
        "UPLOAD": "TELEMAC_COG_UPLOAD_FAILED",
    }
    return PostprocessTelemacError(
        codes.get(exc.stage, "POSTPROCESS_TELEMAC_FAILED"),
        message=exc.message,
        details=dict(exc.details),
    )


#: Hs (m) below which a wet node is treated as "flat water" for the extent
#: metrics / detection floor. Tiny absolute floor separates a real wave field
#: from a genuinely empty solve.
_HS_WET_FLOOR: float = 1e-3


def _local_mesh_origin(domain_bbox: Any, utm_epsg: int, *,
                       required: bool = False,
                       context: str = "this postprocess") -> tuple[float, float]:
    """The UTM corner a LOCAL-coordinate mesh was built from. The ONE origin.

    ABSENCE and MALFORMATION differ: a present but malformed bbox refuses."""
    # Every open-water build lays its grid with node 0 at the AOI's SW corner, so
    # the result SELAFIN carries local metres and the corner has to be added back
    # before reprojection. Getting it wrong does not fail - it silently lands the
    # field at the UTM zone's false origin, thousands of km from the domain - so
    # the arithmetic lives in one place. ``required=True`` says this reader cannot
    # place its mesh without a corner and refuses instead of guessing.
    if domain_bbox is None:
        if required:
            raise PostprocessTelemacError(
                "TELEMAC_PARAMS_INVALID",
                message=f"{context} needs the 4326 domain bbox (min_lon, min_lat, "
                "max_lon, max_lat) to place the local-coordinate mesh; none was "
                f"supplied for utm_epsg={utm_epsg}.",
                details={"utm_epsg": utm_epsg, "domain_bbox": None},
            )
        return (0.0, 0.0)

    corners = tuple(domain_bbox)
    try:
        if len(corners) != 4:
            raise ValueError(f"{len(corners)} corners, expected 4")
        west, south, east, north = (float(v) for v in corners)
    except (TypeError, ValueError) as exc:
        raise PostprocessTelemacError(
            "TELEMAC_PARAMS_INVALID",
            message=f"{context} was handed a malformed domain bbox "
            f"{domain_bbox!r}: {exc}. It must be four numeric 4326 corners "
            "(min_lon, min_lat, max_lon, max_lat).",
            details={"utm_epsg": utm_epsg, "domain_bbox": repr(domain_bbox)},
        ) from exc

    from pyproj import Transformer

    fwd = Transformer.from_crs(4326, int(utm_epsg), always_xy=True)
    x0, y0 = fwd.transform(west, south)
    x1, y1 = fwd.transform(east, north)
    return (min(x0, x1), min(y0, y1))


def postprocess_tomawac(
    slf_path: str | Path,
    *,
    run_id: str,
    utm_epsg: int,
    reach_name: str = "wave_field",
    wave_mode: str = "fetch_growth",
    domain_bbox: Sequence[float] | None = None,
    runs_bucket: str | None = None,
    target_ground_res_m: float = 30.0,
) -> tuple[list[TelemacWaveLayerURI], dict[str, Any]]:
    """Rasterize a solved TOMAWAC result into ONE significant-wave-height COG.

    The FINAL frame is the steady sea state; ``domain_bbox`` georeferences it."""
    # The wave worker builds its grid with node 0 at the AOI's SW corner and only
    # offsets by that corner when it samples the bed, so without the bbox those
    # metres reproject as absolute UTM and the Hs COG lands at the zone's false
    # origin while the bed COG beside it sits correctly on the water. An idealized
    # basin has no geographic footprint, passes no bbox, and stays where the
    # geography-free grid puts it - which its own label says.
    try:
        import numpy as np
        from pyproj import Transformer  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        raise PostprocessTelemacError(
            "TELEMAC_DEPENDENCY_MISSING",
            message=f"numpy/pyproj unavailable for TOMAWAC postprocess: {exc}",
        ) from exc

    slf = Path(slf_path)
    try:
        mesh = read_selafin(slf)
    except Exception as exc:  # noqa: BLE001
        raise PostprocessTelemacError(
            "TELEMAC_OUTPUT_READ_FAILED",
            message=f"could not parse SELAFIN {slf.name}: {exc}",
            details={"slf": str(slf)},
        ) from exc

    import numpy as np

    hs_var = None
    for v in mesh["varnames"]:
        u = v.strip().upper()
        if "HM0" in u or "WAVE HEIGHT" in u:
            hs_var = v
            break
    if hs_var is None or mesh["data"].get(hs_var) is None or mesh["data"][hs_var].size == 0:
        raise PostprocessTelemacError(
            "TELEMAC_OUTPUT_EMPTY",
            message=f"no WAVE HEIGHT HM0 field / no time steps in {slf.name} "
            f"(vars={mesh['varnames']})",
            details={"slf": str(slf), "varnames": mesh["varnames"]},
        )

    hs = np.asarray(mesh["data"][hs_var])          # (nframes, npoin)
    node_hs = hs[-1]                                # final frame = steady sea state
    x_utm = np.asarray(mesh["x"])
    y_utm = np.asarray(mesh["y"])
    finite = np.isfinite(node_hs)
    hs_max = float(np.nanmax(node_hs[finite])) if finite.any() else 0.0
    hs_mean = float(np.nanmean(node_hs[finite & (node_hs > _HS_WET_FLOOR)])) \
        if (finite & (node_hs > _HS_WET_FLOOR)).any() else 0.0
    if hs_max < _HS_WET_FLOOR:
        raise PostprocessTelemacError(
            "TELEMAC_OUTPUT_EMPTY",
            message=f"Hs never exceeded {_HS_WET_FLOOR} m anywhere in {slf.name} "
            f"(peak {hs_max:.4g}) -- a dry/zero-wave solve?",
            details={"hs_max_m": hs_max},
        )

    from pyproj import Transformer

    x_org, y_org = _local_mesh_origin(domain_bbox, utm_epsg)
    back = Transformer.from_crs(int(utm_epsg), 4326, always_xy=True)
    lon, lat = back.transform(x_utm + x_org, y_utm + y_org)
    lon = np.asarray(lon)
    lat = np.asarray(lat)

    pad = 0.0009
    bbox = (
        float(lon.min() - pad), float(lat.min() - pad),
        float(lon.max() + pad), float(lat.max() + pad),
    )
    shape = raster.grid_shape(bbox, target_ground_res_m)
    try:
        # barycentric over the wave mesh's own elements: a ~3 km TOMAWAC grid under
        # a nearest-node halo published isolated pixels, not an Hs field.
        # wet_floor tiny so a small-Hs run is not clipped; NaN nodes drop out.
        grid = raster.rasterize_elements(
            lon, lat, mesh["ikle"], node_hs, bbox, shape, wet_floor=_HS_WET_FLOOR)
    except Exception as exc:  # noqa: BLE001
        raise PostprocessTelemacError(
            "TELEMAC_OUTPUT_READ_FAILED",
            message=f"Hs rasterization failed: {exc}",
        ) from exc

    from rasterio.transform import from_bounds

    transform = from_bounds(bbox[0], bbox[1], bbox[2], bbox[3], shape[1], shape[0])
    try:
        cog = cog_io.write_cog_4326_from_grid(
            grid, src_crs="EPSG:4326", src_transform=transform,
            reproject=False, crs_roundtrip_guard=True,
            dst_suffix="_tomawac_hs_4326.tif",
        )
    except CogIoError as exc:
        raise _reraise_cogio(exc) from exc
    try:
        uri = cog_io.upload_cog(
            cog, run_id, runs_bucket,
            dest_filename="tomawac_hs.tif",
            content_type="image/tiff", gs_backend="fsspec",
            gs_fallback_to_file=False, runs_bucket_default=RUNS_BUCKET_DEFAULT,
            log_label="TOMAWAC Hs COG",
        )
    except CogIoError as exc:
        raise _reraise_cogio(exc) from exc
    finally:
        cog_io.safe_unlink(cog)

    vmax = round(max(hs_max, _HS_WET_FLOOR), 4)
    legend = presets.legend_key(TELEMAC_WAVE_STYLE, value_range=(0.0, vmax),
                                label="Significant wave height Hs (m)")
    honesty = (
        "Spectral-wave screening (TOMAWAC WAM4 physics): significant wave height "
        "Hs over the domain. A planning-grade wave field driven by a prescribed "
        "steady wind / boundary swell, not a calibrated hindcast."
    )
    layer = TelemacWaveLayerURI(
        layer_id=f"tomawac-hs-{run_id}",
        name=f"Significant wave height ({reach_name})",
        layer_type="raster",
        uri=uri,
        style=TELEMAC_WAVE_STYLE,
        quantity="significant_wave_height",
        role="primary",
        units="m",
        bbox=bbox,
        legend=legend,
        fallback_note=honesty,
        hs_max_m=round(hs_max, 4),
        hs_mean_m=round(hs_mean, 4),
        wave_mode=wave_mode,
    )
    metrics: dict[str, Any] = {
        "hs_var": hs_var.strip(),
        "hs_max_m": round(hs_max, 4),
        "hs_mean_m": round(hs_mean, 4),
        "wave_mode": wave_mode,
        "npoin": int(mesh["npoin"]),
        "nelem": int(mesh["nelem"]),
        "utm_epsg": int(utm_epsg),
        "bbox": list(bbox),
        "crs": "EPSG:4326",
        "honesty_label": honesty,
    }
    logger.info(
        "postprocess_tomawac run_id=%s hs_var=%s hs_max=%.4g m mode=%s -> %s",
        run_id, hs_var.strip(), hs_max, wave_mode, uri,
    )
    return [layer], metrics


#: Kd (agitation coefficient) below which a wet node is treated as "flat water"
#: for the detection floor. Tiny absolute floor separates a real agitation field
#: from a genuinely empty solve.
_KD_WET_FLOOR: float = 1e-3


def postprocess_artemis(
    *,
    run_id: str,
    utm_epsg: int,
    x: Any,
    y: Any,
    ikle: Any,
    hs: Any,
    incident_hs_m: float,
    reach_name: str = "harbor_agitation",
    runs_bucket: str | None = None,
    target_ground_res_m: float = 20.0,
) -> tuple[list[ArtemisAgitationLayerURI], dict[str, Any]]:
    """The solved agitation field -> ONE Kd (Hs/H0) COG on the map.

    An authored mesh carries TRUE eastings, so there is no origin to add back."""
    # The field is drawn by the solver's own P1 representation over the element
    # table rather than by a nearest-node halo: an authored mesh spaces its
    # offshore nodes hundreds of metres apart, and a halo sized for the harbour
    # publishes a lattice of isolated pixels out there instead of a field.
    import numpy as np

    hs = np.asarray(hs, dtype="float64")
    h0 = max(float(incident_hs_m), 1e-6)
    kd = hs / h0
    finite = np.isfinite(kd)
    kd_max = float(np.nanmax(kd[finite])) if finite.any() else 0.0
    if kd_max < _KD_WET_FLOOR:
        raise PostprocessTelemacError(
            "TELEMAC_OUTPUT_EMPTY",
            message=f"Kd never exceeded {_KD_WET_FLOOR} anywhere in the solved "
                    f"field (peak {kd_max:.4g}) -- a dry/zero-agitation solve?",
            details={"kd_max": kd_max},
        )

    from pyproj import Transformer

    back = Transformer.from_crs(int(utm_epsg), 4326, always_xy=True)
    lon, lat = back.transform(np.asarray(x, dtype="float64"),
                              np.asarray(y, dtype="float64"))
    lon = np.asarray(lon)
    lat = np.asarray(lat)
    pad = 0.0009
    bbox = (float(lon.min() - pad), float(lat.min() - pad),
            float(lon.max() + pad), float(lat.max() + pad))
    shape = raster.grid_shape(bbox, target_ground_res_m)

    try:
        grid = raster.rasterize_elements(lon, lat, ikle, kd, bbox, shape,
                                       wet_floor=_KD_WET_FLOOR)
    except Exception as exc:  # noqa: BLE001
        raise PostprocessTelemacError(
            "TELEMAC_OUTPUT_READ_FAILED",
            message=f"Kd rasterization failed: {exc}",
        ) from exc

    from rasterio.transform import from_bounds

    transform = from_bounds(bbox[0], bbox[1], bbox[2], bbox[3], shape[1], shape[0])
    try:
        cog = cog_io.write_cog_4326_from_grid(
            grid, src_crs="EPSG:4326", src_transform=transform,
            reproject=False, crs_roundtrip_guard=True,
            dst_suffix="_artemis_agitation.tif",
        )
    except CogIoError as exc:
        raise _reraise_cogio(exc) from exc
    try:
        uri = cog_io.upload_cog(
            cog, run_id, runs_bucket,
            dest_filename="artemis_agitation.tif",
            content_type="image/tiff", gs_backend="fsspec",
            gs_fallback_to_file=False, runs_bucket_default=RUNS_BUCKET_DEFAULT,
            log_label="ARTEMIS Kd COG",
        )
    except CogIoError as exc:
        raise _reraise_cogio(exc) from exc
    finally:
        cog_io.safe_unlink(cog)

    # legend vmax: a robust cap at the 99.5th percentile of the wet field so a
    # single spurious hotspot (a coastline reflection / focus caustic) does not
    # wash the readable 0..~2 agitation range off the ramp. The layer's kd_max
    # metric still carries the TRUE peak; this only styles the COG.
    kd_wet = kd[finite & (kd > _KD_WET_FLOOR)]
    kd_p995 = float(np.percentile(kd_wet, 99.5)) if kd_wet.size else kd_max
    vmax = round(max(min(kd_max, max(kd_p995, 1.0)), 1.0), 3)
    legend = presets.legend_key(TELEMAC_AGITATION_STYLE, value_range=(0.0, vmax),
                                label="Agitation coefficient Kd = Hs / H0")
    honesty = (
        "Phase-resolving harbour-agitation screening (ARTEMIS elliptic mild-slope "
        "/ Berkhoff): agitation coefficient Kd = Hs/H0 (how much the incident wave "
        "is amplified or sheltered). A planning-grade field driven by a prescribed "
        "monochromatic incident wave, not a calibrated hindcast. kd_max is often a "
        "standing wave against the domain's own open (seaward) boundary rather "
        "than a harbour answer; the sheltering ratio (kd_sheltered / kd_exposed) "
        "is the number that answers whether the structure shelters."
    )
    layer = ArtemisAgitationLayerURI(
        layer_id=f"artemis-agitation-{run_id}",
        name=f"Wave agitation Kd ({reach_name})",
        layer_type="raster",
        uri=uri,
        style=TELEMAC_AGITATION_STYLE,
        quantity="agitation_coefficient",
        role="primary",
        units="Kd",
        bbox=bbox,
        legend=legend,
        fallback_note=honesty,
        kd_max=round(kd_max, 3),
        hs_max_m=round(float(np.nanmax(hs[finite])), 4) if finite.any() else None,
        wave_mode="diffraction",
    )
    metrics: dict[str, Any] = {
        "kd_max": round(kd_max, 3),
        "npoin": int(np.asarray(x).shape[0]),
        "utm_epsg": int(utm_epsg),
        "bbox": list(bbox),
        "valid_pixel_fraction": round(
            float(np.isfinite(grid).sum()) / float(max(grid.size, 1)), 4),
        "honesty_label": honesty,
    }
    logger.info("postprocess_artemis run_id=%s kd_max=%.3g -> %s",
                run_id, kd_max, uri)
    return [layer], metrics


def _rasterize_t3d_plane(
    x, y, ikle, node_vals, *, run_id, utm_epsg, dest_filename, dst_suffix,
    log_label, runs_bucket, target_ground_res_m,
):
    """One sigma plane of the solved column -> a 4326 COG, uploaded.

    NO value masking: a temperature or a velocity can be negative and valid."""
    # ``valid_frac`` is the fraction of output pixels carrying a value, the number
    # that separates a FIELD from a dot lattice. The nodes carry true eastings and
    # northings, so the reprojection to 4326 is the whole of the georeferencing.
    import numpy as np

    node_vals = np.asarray(node_vals, dtype="float64")
    finite = np.isfinite(node_vals)
    if not finite.any():
        raise PostprocessTelemacError(
            "TELEMAC_OUTPUT_EMPTY",
            message=f"the {log_label} plane carried no finite values",
            details={"run_id": run_id},
        )
    node_min = float(np.nanmin(node_vals[finite]))
    node_max = float(np.nanmax(node_vals[finite]))
    node_mean = float(np.nanmean(node_vals[finite]))

    from pyproj import Transformer

    back = Transformer.from_crs(int(utm_epsg), 4326, always_xy=True)
    lon, lat = back.transform(np.asarray(x, dtype="float64"),
                              np.asarray(y, dtype="float64"))
    lon = np.asarray(lon)
    lat = np.asarray(lat)
    pad = 0.0009
    bbox = (float(lon.min() - pad), float(lat.min() - pad),
            float(lon.max() + pad), float(lat.max() + pad))
    shape = raster.grid_shape(bbox, target_ground_res_m)

    try:
        # barycentric over the RESULT triangulation: an open-water mesh spaces its
        # offshore nodes hundreds of metres apart, so a nearest-node halo publishes
        # a lattice of isolated pixels instead of a field. The element fill is the
        # solver's own P1 representation.
        grid = raster.rasterize_elements(
            lon, lat, ikle, node_vals, bbox, shape, wet_floor=-1e30)
    except Exception as exc:  # noqa: BLE001
        raise PostprocessTelemacError(
            "TELEMAC_OUTPUT_READ_FAILED",
            message=f"TELEMAC-3D field rasterization failed: {exc}",
        ) from exc

    valid_frac = float(np.isfinite(grid).sum()) / float(max(grid.size, 1))

    from rasterio.transform import from_bounds

    transform = from_bounds(bbox[0], bbox[1], bbox[2], bbox[3], shape[1], shape[0])
    try:
        cog = cog_io.write_cog_4326_from_grid(
            grid, src_crs="EPSG:4326", src_transform=transform,
            reproject=False, crs_roundtrip_guard=True, dst_suffix=dst_suffix,
        )
    except CogIoError as exc:
        raise _reraise_cogio(exc) from exc
    try:
        uri = cog_io.upload_cog(
            cog, run_id, runs_bucket, dest_filename=dest_filename,
            content_type="image/tiff", gs_backend="fsspec",
            gs_fallback_to_file=False, runs_bucket_default=RUNS_BUCKET_DEFAULT,
            log_label=log_label,
        )
    except CogIoError as exc:
        raise _reraise_cogio(exc) from exc
    finally:
        cog_io.safe_unlink(cog)
    return uri, bbox, node_min, node_max, node_mean, valid_frac


def postprocess_telemac3d(
    *,
    run_id: str,
    utm_epsg: int,
    x: Any,
    y: Any,
    ikle: Any,
    surface: Any,
    bottom: Any,
    measured: dict[str, Any],
    reach_name: str = "stratified_flow",
    runs_bucket: str | None = None,
    target_ground_res_m: float = 40.0,
) -> tuple[list[Telemac3dLayerURI], dict[str, Any]]:
    """The solved column's top and bed planes -> the PAIR of COGs on the map.

    TWO LAYERS, ONE ANSWER; ``measured`` is read off the same 3D field."""
    units = measured.get("variable_units") or ""
    var_label = measured.get("variable_label") or "Surface field"
    metric = float(measured.get("stratification_metric") or 0.0)

    s_uri, s_bbox, s_min, s_max, s_mean, s_frac = _rasterize_t3d_plane(
        x, y, ikle, surface, run_id=run_id, utm_epsg=utm_epsg,
        dest_filename="telemac3d_surface.tif", dst_suffix="_t3d_surface.tif",
        log_label="TELEMAC-3D surface COG", runs_bucket=runs_bucket,
        target_ground_res_m=target_ground_res_m)
    b_uri, b_bbox, b_min, b_max, b_mean, b_frac = _rasterize_t3d_plane(
        x, y, ikle, bottom, run_id=run_id, utm_epsg=utm_epsg,
        dest_filename="telemac3d_bottom.tif", dst_suffix="_t3d_bottom.tif",
        log_label="TELEMAC-3D bottom COG", runs_bucket=runs_bucket,
        target_ground_res_m=target_ground_res_m)

    # shared legend over the combined surface+bottom range so the two layers read
    # on ONE ramp: the surface-vs-bottom contrast is the point.
    lo = round(min(s_min, b_min), 5)
    hi = round(max(s_max, b_max), 5)
    if hi <= lo:
        hi = lo + 1e-3
    # a signed field (velocity) reads on a diverging ramp centred on 0; a strictly
    # positive field (temperature) reads on a sequential ramp.
    signed = lo < 0.0 < hi
    vext = round(max(abs(lo), abs(hi)), 5)
    legend_common = dict(
        units=units or None,
        label=f"{var_label} ({units})" if units else var_label)

    honesty = (
        "TELEMAC-3D vertical-structure screening: the surface and bottom planes of "
        "a baroclinic field (what a 2D depth-averaging cannot resolve). A "
        "planning-grade prescribed-forcing field, not a calibrated site study."
        # The run carries no surface heat exchange, so nothing can remove heat.
        " The run carries NO surface heat exchange: heat is CONSERVED, so a "
        "falling surface temperature is the warm layer MIXING DOWNWARD, not the "
        "lake cooling."
    )
    if measured.get("vertical_resolution_label"):
        honesty += f" Vertical fidelity: {measured['vertical_resolution_label']}."

    style = TELEMAC3D_SIGNED_STYLE if signed else TELEMAC3D_STRATIFICATION_STYLE

    def _mk(uri, bbox, role, is_surface):
        legend = presets.legend_key(
            style, value_range=((-vext, vext) if signed else (lo, hi)),
            **legend_common)
        which = "Surface" if is_surface else "Bottom"
        return Telemac3dLayerURI(
            layer_id=f"telemac3d-{'surface' if is_surface else 'bottom'}-{run_id}",
            name=f"{which} {var_label.split(' ', 1)[-1] if ' ' in var_label else var_label} ({reach_name})",
            layer_type="raster",
            uri=uri,
            style=style,
            role=role,
            units=units or None,
            bbox=bbox,
            legend=legend,
            fallback_note=honesty,
            stratification_metric=metric,
            flow_mode="stratification",
            variable_label=var_label,
            variable_units=units or None,
            stratification_dt=measured.get("stratification_dt"),
            u_surface=measured.get("u_surface"),
            u_bottom=measured.get("u_bottom"),
            depth_avg_u=measured.get("depth_avg_u"),
            surface_value_mean=round(s_mean, 5),
            bottom_value_mean=round(b_mean, 5),
            nplan=measured.get("nplan"),
            wind_speed_mps=measured.get("wind_speed_mps"),
            mesh_size_m=measured.get("mesh_size_m"),
            mesh_resolution_label=measured.get("mesh_resolution_label"),
        )

    surface_layer = _mk(s_uri, s_bbox, "primary", True)
    bottom_layer = _mk(b_uri, b_bbox, "context", False)

    metrics: dict[str, Any] = {
        "stratification_metric": metric,
        "variable_label": var_label,
        "variable_units": units,
        "surface_value_range": [s_min, s_max],
        "bottom_value_range": [b_min, b_max],
        "surface_value_mean": round(s_mean, 5),
        "bottom_value_mean": round(b_mean, 5),
        "utm_epsg": utm_epsg,
        "surface_bbox": list(s_bbox),
        "surface_valid_pixel_fraction": round(s_frac, 4),
        "bottom_valid_pixel_fraction": round(b_frac, 4),
        "honesty_label": honesty,
    }
    logger.info(
        "postprocess_telemac3d run_id=%s metric=%.4g surf=[%.3g,%.3g] "
        "bot=[%.3g,%.3g] valid_px=%.1f%%/%.1f%% -> %s , %s",
        run_id, metric, s_min, s_max, b_min, b_max,
        100.0 * s_frac, 100.0 * b_frac, s_uri, b_uri,
    )
    return [surface_layer, bottom_layer], metrics


def _initially_dry_mask(mesh: Any, depth: Any, init_wl_m: Any) -> tuple[Any, str]:
    """The t=0 wet/dry mask: True where a node was DRY before the tide arrived.

    Returned with the LABEL of the route that ran, so a reader can check it."""
    # Two routes to the same discrimination, in preference order, because the
    # answer layer has to mean the same thing as ``flooded_land_km2``:
    #   1. the worker's own rule, ``BOTTOM > init_wl``, reproduced from the
    #      result's static bed and the datum-corrected initial water line the
    #      worker cold-started from - the definition the scalar already uses;
    #   2. frame 0 of WATER DEPTH, when the result carries no bed or the run
    #      reported no initial stage. TELEMAC cold-starts ``H = max(0, init_wl -
    #      B)``, so a dry-at-t0 node is exactly one whose first frame is at the
    #      dry floor - the same discrimination read off the field.
    import numpy as np

    bed_var = _pick_named_var(mesh["varnames"], _BED_VAR_KEYS, "B")
    bed = mesh["data"].get(bed_var) if bed_var else None
    if bed is not None and getattr(bed, "size", 0) and init_wl_m is not None:
        bed0 = np.asarray(bed)[0]
        return (bed0 > float(init_wl_m),
                f"bed above the {float(init_wl_m):.4g} m initial water line "
                "(the worker's own flooded-land rule)")
    return (np.asarray(depth)[0] <= TELEMAC_WSE_WET_DEPTH_M,
            f"WATER DEPTH at t=0 at or below the {TELEMAC_WSE_WET_DEPTH_M} m dry "
            "floor (the result carried no bed / no initial stage)")


def postprocess_coastal(
    slf_path: str | Path,
    *,
    run_id: str,
    utm_epsg: int,
    domain_bbox: Sequence[float],
    reach_name: str = "coast",
    worker_metrics: dict[str, Any] | None = None,
    runs_bucket: str | None = None,
    target_ground_res_m: float = 30.0,
) -> tuple[list[TelemacCoastalLayerURI], dict[str, Any]]:
    """Rasterize a solved COASTAL result into an INUNDATION layer and its context.

    TWO products: peak depth over land DRY at t=0, and the full depth field."""
    # The coastal worker writes LOCAL (origin-shifted) mesh coordinates into the
    # result, so ``domain_bbox`` is REQUIRED to recover the UTM origin added back
    # before reprojection; without it the COG lands at the UTM false origin. The
    # flooded-land discriminant is computed inside the worker and folded in from
    # ``worker_metrics``.
    try:
        import numpy as np
        from pyproj import Transformer  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        raise PostprocessTelemacError(
            "TELEMAC_DEPENDENCY_MISSING",
            message=f"numpy/pyproj unavailable for coastal postprocess: {exc}",
        ) from exc

    slf = Path(slf_path)
    try:
        mesh = read_selafin(slf)
    except Exception as exc:  # noqa: BLE001
        raise PostprocessTelemacError(
            "TELEMAC_OUTPUT_READ_FAILED",
            message=f"could not parse SELAFIN {slf.name}: {exc}",
            details={"slf": str(slf)},
        ) from exc

    import numpy as np

    depth_var = _pick_named_var(mesh["varnames"], _DEPTH_VAR_KEYS, "H")
    if depth_var is None or mesh["data"].get(depth_var) is None \
            or mesh["data"][depth_var].size == 0:
        raise PostprocessTelemacError(
            "TELEMAC_OUTPUT_EMPTY",
            message=f"no WATER DEPTH variable / no time steps in {slf.name} "
            f"(vars={mesh['varnames']})",
            details={"slf": str(slf), "varnames": mesh["varnames"]},
        )

    depth = np.asarray(mesh["data"][depth_var])          # (nframes, npoin), metres
    times = np.asarray(mesh["times"])
    x_utm = np.asarray(mesh["x"])
    y_utm = np.asarray(mesh["y"])

    # per-node peak inundation depth over ONLY the wet frames; never-wet -> NaN.
    import warnings

    wet = depth > TELEMAC_WSE_WET_DEPTH_M
    masked = np.where(wet, depth, np.nan)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        node_peak = np.nanmax(masked, axis=0) if masked.shape[0] else np.full(
            x_utm.size, np.nan)
    finite = np.isfinite(node_peak)
    if not finite.any():
        raise PostprocessTelemacError(
            "TELEMAC_OUTPUT_EMPTY",
            message=f"no wet node in {slf.name}: WATER DEPTH never exceeded "
            f"{TELEMAC_WSE_WET_DEPTH_M} m anywhere (dry solve?)",
            details={"slf": str(slf), "wet_depth_m": TELEMAC_WSE_WET_DEPTH_M},
        )
    peak_depth = float(np.nanmax(node_peak[finite]))

    from pyproj import Transformer

    # the coastal SELAFIN carries LOCAL (0-origin) mesh coordinates; add back the
    # UTM origin (min easting/northing over the AOI corners, matching the build)
    # before reprojecting, else the COG lands at the UTM false-origin.
    x_org, y_org = _local_mesh_origin(
        domain_bbox, int(utm_epsg), required=True, context="postprocess_coastal")
    back = Transformer.from_crs(int(utm_epsg), 4326, always_xy=True)
    lon, lat = back.transform(x_utm + x_org, y_utm + y_org)
    lon = np.asarray(lon)
    lat = np.asarray(lat)

    pad = 0.0009
    bbox = (
        float(lon.min() - pad), float(lat.min() - pad),
        float(lon.max() + pad), float(lat.max() + pad),
    )
    wm = worker_metrics or {}
    # The ANSWER field: peak depth over land that was dry before the tide arrived.
    # Initially-wet nodes go NaN, so the interpolator drops every element they
    # touch and the permanent bay is nodata rather than a painted "inundation".
    dry0, dry0_basis = _initially_dry_mask(mesh, depth, wm.get("init_wl_m"))
    inundation = np.where(dry0, node_peak, np.nan)
    n_inundated = int(np.isfinite(inundation).sum())

    shape = raster.grid_shape(bbox, target_ground_res_m)

    def _grid_of(values: Any, label: str) -> Any:
        # barycentric over the coastal mesh's own elements (a ~250 m grid under a
        # nearest-node halo published a dot lattice, not an inundation field).
        # Values are passed UNFILTERED so the element table still indexes them:
        # a masked node is NaN, and the interpolator drops the elements it
        # touches - the dry rim is nodata, never an interpolated depth.
        try:
            return raster.rasterize_elements(
                lon, lat, mesh["ikle"], values, bbox, shape,
                wet_floor=TELEMAC_WSE_WET_DEPTH_M)
        except Exception as exc:  # noqa: BLE001
            raise PostprocessTelemacError(
                "TELEMAC_OUTPUT_READ_FAILED",
                message=f"coastal {label} rasterization failed: {exc}",
            ) from exc

    from rasterio.transform import from_bounds

    transform = from_bounds(bbox[0], bbox[1], bbox[2], bbox[3], shape[1], shape[0])

    def _cog_of(grid: Any, suffix: str, dest: str, label: str) -> str:
        try:
            cog = cog_io.write_cog_4326_from_grid(
                grid, src_crs="EPSG:4326", src_transform=transform,
                reproject=False, crs_roundtrip_guard=True, dst_suffix=suffix,
            )
        except CogIoError as exc:
            raise _reraise_cogio(exc) from exc
        try:
            return cog_io.upload_cog(
                cog, run_id, runs_bucket, dest_filename=dest,
                content_type="image/tiff", gs_backend="fsspec",
                gs_fallback_to_file=False, runs_bucket_default=RUNS_BUCKET_DEFAULT,
                log_label=label,
            )
        except CogIoError as exc:
            raise _reraise_cogio(exc) from exc
        finally:
            cog_io.safe_unlink(cog)

    inundation_uri = _cog_of(
        _grid_of(inundation, "inundation depth"), "_coastal_inundation_4326.tif",
        "coastal_inundation.tif", "TELEMAC coastal inundation COG")
    uri = _cog_of(
        _grid_of(node_peak, "water depth"), "_coastal_depth_4326.tif",
        "coastal_depth_max.tif", "TELEMAC coastal water-depth COG")

    flooded_land_km2 = float(wm.get("flooded_land_km2") or 0.0)
    wet_area_km2 = wm.get("wet_peak_km2")
    peak_wl_m = wm.get("peak_wl_max_m")
    sl_peak_m = wm.get("sl_max_m")
    series_type = wm.get("series_type")
    ocean_edge = wm.get("ocean_edge")

    inundation_peak = (float(np.nanmax(inundation[np.isfinite(inundation)]))
                       if n_inundated else 0.0)
    mesh_label = (
        f"real NOAA DEM_all topobathy grid {wm.get('dx_m', target_ground_res_m):g} m"
        + (" (coarsened under node budget)" if wm.get("coarsened") else ""))
    scalars: dict[str, Any] = dict(
        peak_depth_m=round(peak_depth, 4),
        flooded_land_km2=round(flooded_land_km2, 5),
        wet_area_km2=round(float(wet_area_km2), 5) if wet_area_km2 is not None else None,
        peak_wl_m=round(float(peak_wl_m), 4) if peak_wl_m is not None else None,
        sl_peak_m=round(float(sl_peak_m), 4) if sl_peak_m is not None else None,
        inundation_peak_depth_m=round(inundation_peak, 4),
        inundation_basis=dry0_basis,
        series_type=series_type,
        series_datum=wm.get("series_datum"),
        datum_offset_m=wm.get("datum_offset_m"),
        station_id=wm.get("station_id"),
        station_name=wm.get("station_name"),
        ocean_edge=ocean_edge,
        mesh_size_m=wm.get("dx_m"),
        mesh_resolution_label=mesh_label,
    )
    shared = (
        "Coastal tidal/surge (TELEMAC-2D SAINT-VENANT + TIDAL FLATS): an open-water "
        "domain driven at the seaward boundary by a NOAA CO-OPS / GTSM water-level "
        "series through the LIQUID BOUNDARIES FILE. Planning-grade screening (real "
        "topobathy + observed stage), not a calibrated hindcast; the tide datum is "
        "reconciled to the DEM by a labeled offset."
    )
    honesty = (
        "Peak water depth over land that was DRY at t=0 - the flooding the tide "
        f"CAUSED, on the same discrimination ({dry0_basis}) that "
        f"flooded_land_km2 counts. Permanently submerged water is nodata here; it "
        "is published beside this as the total water-depth context layer. " + shared
    )
    context_honesty = (
        "TOTAL peak water depth, INCLUDING the permanently submerged bay - this is "
        "where the water is, not where the tide went. The planning answer is the "
        "inundation layer beside it; read this one for the whole water column. "
        + shared
    )
    inundation_layer = TelemacCoastalLayerURI(
        layer_id=f"telemac-coastal-inundation-{run_id}",
        name=f"Peak inundation depth over initially-dry land ({reach_name})",
        layer_type="raster",
        uri=inundation_uri,
        style=TELEMAC_COASTAL_DEPTH_STYLE,
        quantity="inundation_depth",
        role="primary",
        units="m",
        bbox=bbox,
        legend=presets.legend_key(
            TELEMAC_COASTAL_DEPTH_STYLE,
            value_range=(0.0, round(max(inundation_peak, TELEMAC_WSE_WET_DEPTH_M), 4)),
            label="Peak inundation depth over initially-dry land (m)"),
        fallback_note=honesty,
        **scalars,
    )
    water_depth_layer = TelemacCoastalLayerURI(
        layer_id=f"telemac-coastal-depth-{run_id}",
        name=f"Total water depth at peak ({reach_name})",
        layer_type="raster",
        uri=uri,
        style=TELEMAC_COASTAL_DEPTH_STYLE,
        quantity="water_depth",
        role="context",
        units="m",
        bbox=bbox,
        legend=presets.legend_key(
            TELEMAC_COASTAL_DEPTH_STYLE,
            value_range=(0.0, round(max(peak_depth, TELEMAC_WSE_WET_DEPTH_M), 4)),
            label="Total water depth at peak (m)"),
        fallback_note=context_honesty,
        **scalars,
    )
    metrics: dict[str, Any] = {
        "depth_var": depth_var.strip(),
        "peak_depth_m": round(peak_depth, 4),
        "inundation_peak_depth_m": round(inundation_peak, 4),
        "inundation_basis": dry0_basis,
        "flooded_land_km2": round(flooded_land_km2, 5),
        "n_frames": int(times.size),
        "n_wet_nodes": int(finite.sum()),
        "n_inundated_nodes": n_inundated,
        "npoin": int(mesh["npoin"]),
        "nelem": int(mesh["nelem"]),
        "utm_epsg": int(utm_epsg),
        "bbox": list(bbox),
        "crs": "EPSG:4326",
        "honesty_label": honesty,
    }
    logger.info(
        "postprocess_coastal run_id=%s depth_var=%s peak_depth=%.4g m "
        "inundation_peak=%.4g m flooded_land=%.4g km^2 n_wet=%d/%d "
        "n_inundated=%d -> %s + %s",
        run_id, depth_var.strip(), peak_depth, inundation_peak, flooded_land_km2,
        int(finite.sum()), int(x_utm.size), n_inundated, inundation_uri, uri,
    )
    return [inundation_layer, water_depth_layer], metrics
