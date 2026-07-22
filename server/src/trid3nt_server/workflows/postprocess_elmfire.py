"""ELMFIRE postprocess: raw solver rasters -> fire-spread COG layer set (FIRE-3).

The ELMFIRE analogue of ``postprocess_geoclaw.py`` / ``postprocess_flood.py``.
ELMFIRE (``CONVERT_TO_GEOTIFF=.FALSE.`` — the FIRE-2 namelist) writes ESRI BIL
rasters into ``outputs/``::

    time_of_arrival_<ens>_<t>.bil   seconds from ignition (THE headline layer)
    flame_length_<ens>_<t>.bil      feet
    vs_<ens>_<t>.bil                spread rate, ft/min
    flin_<ens>_<t>.bil              fireline intensity, kW/m

The BILs carry the grid geotransform in their ``.hdr`` sidecars but NO CRS —
the FIRE-1 proof stamped it with ``gdal_translate -a_srs``. Here the stamp is
done in code: the deck's known EPSG (the deck-builder grid, default 5070) is
asserted onto the read when the file carries none (:func:`read_fire_raster` —
never a guessed CRS; the EPSG comes from the deck manifest).

Products (mirrors the GeoClaw ``(layers, metrics)`` shape so the composer and
the Phase-1 scrubber path consume it unchanged):

  - ``layers[0]``: the PRIMARY time-of-arrival COG (hours from ignition),
    ``FireSpreadLayerURI`` carrying the typed narration scalars.
  - burned-extent ANIMATION frames: the single ToA raster encodes the whole
    spread history, so postprocess thresholds it per dump hour — frame N =
    arrival-hours masked to cells burned by hour N (the extent GROWS per
    frame, the pixel value is the front age). Frame names carry the web
    ``"Burned area step N"`` token (``frames.py`` convention) so
    ``detectSequentialGroups`` forms one scrubber group.
  - flame-length COG (feet -> METRES, converted exactly once here) and
    spread-rate COG (ft/min -> m/min) as standalone context layers.

Honesty floor: an empty outputs dir is ``ELMFIRE_OUTPUT_EMPTY``; a ToA raster
with ZERO burned cells (an all-nonburnable AOI) is ``ELMFIRE_NO_SPREAD`` — a
typed result the tool narrates, never a blank "modeled ok" with empty layers.
"""

from __future__ import annotations

import logging
import math
import re
from pathlib import Path
from typing import Any

from trid3nt_contracts.elmfire_contracts import (
    ELMFIRE_FLAME_LEN_STYLE_PRESET,
    ELMFIRE_SPREAD_RATE_STYLE_PRESET,
    ELMFIRE_TOA_STYLE_PRESET,
    FireSpreadLayerURI,
)

from . import cog_io
from .cog_io import CogIoError
from .frames import (
    _select_frame_time_indices,
    frame_dest_filename,
    frame_layer_id,
    frame_name,
)
from .postprocess_flood import RUNS_BUCKET_DEFAULT

logger = logging.getLogger("trid3nt_server.workflows.postprocess_elmfire")

__all__ = [
    "PostprocessElmfireError",
    "FT_TO_M",
    "FTMIN_TO_MMIN",
    "discover_elmfire_rasters",
    "read_fire_raster",
    "toa_frame_grids",
    "postprocess_elmfire",
]

#: Unit conversions applied EXACTLY ONCE here (ELMFIRE emits imperial).
FT_TO_M: float = 0.3048
FTMIN_TO_MMIN: float = 0.3048

#: The web frame-token stem + quantity label (frames.py naming contract).
_FIRE_FRAME_STEM: str = "fire-burned"
_FIRE_QUANTITY_LABEL: str = "Burned area"

#: stage -> error_code map for cog_io failures (mirrors postprocess_geoclaw).
_ELMFIRE_STAGE_CODES: dict[str, str] = {
    "DEPENDENCY": "ELMFIRE_DEPENDENCY_MISSING",
    "WRITE": "ELMFIRE_COG_WRITE_FAILED",
    "REPROJECT": "ELMFIRE_COG_WRITE_FAILED",
    "CRS_MISMATCH": "ELMFIRE_CRS_TAG_MISMATCH",
    "UPLOAD": "ELMFIRE_COG_UPLOAD_FAILED",
}


class PostprocessElmfireError(RuntimeError):
    """Raised on any ELMFIRE postprocess failure (typed ``error_code``)."""

    def __init__(
        self,
        error_code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.details = details or {}


def _reraise_cogio(exc: CogIoError) -> PostprocessElmfireError:
    code = _ELMFIRE_STAGE_CODES.get(exc.stage, "POSTPROCESS_ELMFIRE_FAILED")
    return PostprocessElmfireError(code, exc.message, dict(exc.details))


# --------------------------------------------------------------------------- #
# Output discovery.
# --------------------------------------------------------------------------- #

#: solver output family -> filename-stem regex (ensemble + time suffixes vary).
_RASTER_PATTERNS: dict[str, re.Pattern[str]] = {
    "time_of_arrival": re.compile(r"^time_of_arrival.*\.(bil|tif)$", re.I),
    "flame_length": re.compile(r"^flame_length.*\.(bil|tif)$", re.I),
    "spread_rate": re.compile(r"^vs.*\.(bil|tif)$", re.I),
    "fireline_intensity": re.compile(r"^flin.*\.(bil|tif)$", re.I),
}


def discover_elmfire_rasters(out_dir: str | Path) -> dict[str, Path | None]:
    """Locate the per-family solver rasters under ``out_dir`` (or ``outputs/``).

    Returns ``{family: Path | None}`` for the four known families. When a
    family produced multiple dumps the LEXICALLY LAST file wins (ELMFIRE
    encodes the simulation time in the name, so the last dump is the
    end-of-run state — the full spread history for ToA).
    """
    out = Path(out_dir)
    search_dirs = [out / "outputs", out]
    found: dict[str, Path | None] = {k: None for k in _RASTER_PATTERNS}
    for family, pattern in _RASTER_PATTERNS.items():
        candidates: list[Path] = []
        for d in search_dirs:
            if not d.is_dir():
                continue
            candidates.extend(
                p for p in d.iterdir() if p.is_file() and pattern.match(p.name)
            )
            if candidates:
                break  # prefer outputs/ over the flat dir
        if candidates:
            found[family] = sorted(candidates)[-1]
    return found


# --------------------------------------------------------------------------- #
# Raster read with the CRS stamp (the FIRE-1 gdal_translate -a_srs step).
# --------------------------------------------------------------------------- #
def read_fire_raster(
    path: str | Path, *, epsg: int
) -> tuple[Any, Any, str, float]:
    """Read one solver raster; return ``(array, transform, crs, cellsize_m)``.

    The array is float64 with the -9999 nodata (and any recorded nodata)
    mapped to NaN. When the file carries NO CRS (the BIL case) the deck's
    known ``epsg`` is stamped on — in-code equivalent of the FIRE-1 proof's
    ``gdal_translate -a_srs EPSG:<epsg>`` step. A file that DOES carry a CRS
    keeps it (never silently overridden).
    """
    import numpy as np
    import rasterio
    from rasterio.errors import RasterioIOError

    p = Path(path)
    try:
        ds = rasterio.open(p)
    except RasterioIOError as exc:
        raise PostprocessElmfireError(
            "ELMFIRE_OUTPUT_READ_FAILED",
            f"could not open solver raster {p}: {exc}",
            details={"path": str(p)},
        ) from exc
    with ds:
        arr = ds.read(1).astype("float64")
        nodata = ds.nodata
        transform = ds.transform
        crs = str(ds.crs) if ds.crs is not None else f"EPSG:{int(epsg)}"
        cellsize_m = float(abs(transform.a))
    if nodata is not None:
        arr[arr == float(nodata)] = np.nan
    arr[arr == -9999.0] = np.nan
    return arr, transform, crs, cellsize_m


# --------------------------------------------------------------------------- #
# ToA -> burned-extent frame grids (threshold per dump hour).
# --------------------------------------------------------------------------- #
def toa_frame_grids(toa_s: Any, duration_s: float) -> list[tuple[float, Any]]:
    """Threshold the time-of-arrival grid into hourly burned-extent frames.

    Returns ``[(hour, grid), ...]`` ascending; ``grid`` is arrival time in
    HOURS on cells burned by ``hour`` (NaN elsewhere) — the burned extent
    GROWS frame to frame while the pixel value encodes the front age, so one
    colormap tells both the where and the when. Hourly cadence matches the
    deck's ``DTDUMP=3600``; frames are evenly subsampled to
    ``MAX_FLOOD_FRAMES`` via the shared ``frames.py`` selector (endpoints
    kept, never silent).
    """
    import numpy as np

    toa = np.asarray(toa_s, dtype="float64")
    n_hours = max(int(math.ceil(max(float(duration_s), 0.0) / 3600.0)), 1)
    hours = [float(h) for h in range(1, n_hours + 1)]
    kept = _select_frame_time_indices(len(hours))
    frames: list[tuple[float, Any]] = []
    toa_hr = toa / 3600.0
    for idx in kept:
        h = hours[idx]
        grid = np.where(
            np.isfinite(toa) & (toa <= h * 3600.0), toa_hr, np.nan
        )
        frames.append((h, grid))
    return frames


# --------------------------------------------------------------------------- #
# COG write + upload shims (cog_io — mirrors postprocess_geoclaw).
# --------------------------------------------------------------------------- #
def _write_fire_cog_4326(grid: Any, src_crs: str, src_transform: Any) -> Path:
    """Warp a projected-source fire grid to an EPSG:4326 COG.

    NEAREST resampling on purpose: ToA/burned-extent carries a hard
    burned/unburned boundary and the flame/spread rasters share the same
    footprint — bilinear would smear values across the fire front into
    unburned nodata cells.
    """
    from rasterio.warp import Resampling

    try:
        return cog_io.write_cog_4326_from_grid(
            grid,
            src_crs=src_crs,
            src_transform=src_transform,
            reproject=True,
            resampling=Resampling.nearest,
            crs_roundtrip_guard=True,
            dst_suffix="_elmfire_4326.tif",
        )
    except CogIoError as exc:
        raise _reraise_cogio(exc) from exc


def _upload_cog_to_runs_bucket(
    local_cog: Path,
    run_id: str,
    runs_bucket: str | None = None,
    *,
    dest_filename: str,
) -> str:
    try:
        return cog_io.upload_cog(
            local_cog,
            run_id,
            runs_bucket,
            dest_filename=dest_filename,
            content_type="image/tiff",
            gs_backend="fsspec",
            gs_fallback_to_file=False,
            runs_bucket_default=RUNS_BUCKET_DEFAULT,
            log_label="ELMFIRE fire COG",
        )
    except CogIoError as exc:
        raise _reraise_cogio(exc) from exc


def _safe_unlink(p: Path) -> None:
    cog_io.safe_unlink(p)


# --------------------------------------------------------------------------- #
# Top-level postprocess.
# --------------------------------------------------------------------------- #
def postprocess_elmfire(
    out_dir: str | Path,
    bbox: tuple[float, float, float, float],
    *,
    run_id: str,
    duration_s: float,
    epsg: int = 5070,
    runs_bucket: str | None = None,
    ignition_lonlat: tuple[float, float] | None = None,
) -> tuple[list[FireSpreadLayerURI], dict[str, Any]]:
    """Turn a solved ELMFIRE outputs dir into the fire-spread COG layer set.

    Args:
        out_dir: directory holding the solver rasters (or an ``outputs/``
            subdir) — the downloaded run outputs.
        bbox: the AOI ``(min_lon, min_lat, max_lon, max_lat)`` EPSG:4326
            (echoed onto every layer as the zoom-to extent).
        run_id: the run the COGs are keyed under in the runs bucket.
        duration_s: the simulated burn duration (drives the hourly frame
            thresholds).
        epsg: the deck grid EPSG stamped onto CRS-less BIL reads (the deck
            manifest's ``grid.epsg``; FIRE-track canon 5070).
        runs_bucket: optional runs-bucket override.
        ignition_lonlat: echoed onto the layers (self-describing result).

    Returns:
        ``(layers, metrics)`` — ``layers[0]`` the PRIMARY ToA
        ``FireSpreadLayerURI``; then the contiguous ``"Burned area step N"``
        animation frames (>= 2 or none); then the flame-length / spread-rate
        context COGs when the solver produced them. ``metrics`` carries the
        typed aggregates (``burned_area_km2`` computed on the SOURCE projected
        grid where the cell size is known in metres).

    Raises:
        PostprocessElmfireError: ``ELMFIRE_OUTPUT_EMPTY`` (no ToA raster),
            ``ELMFIRE_NO_SPREAD`` (zero burned cells — an honest typed result,
            e.g. an all-nonburnable AOI or an ignition on bare ground), and
            the read/write/upload failure codes.
    """
    try:
        import numpy as np
    except Exception as exc:  # noqa: BLE001
        raise PostprocessElmfireError(
            "ELMFIRE_DEPENDENCY_MISSING",
            f"numpy unavailable for ELMFIRE postprocess: {exc}",
        ) from exc

    out = Path(out_dir)
    rasters = discover_elmfire_rasters(out)
    toa_path = rasters.get("time_of_arrival")
    if toa_path is None:
        raise PostprocessElmfireError(
            "ELMFIRE_OUTPUT_EMPTY",
            f"no time_of_arrival raster found under {out}",
            details={"out_dir": str(out)},
        )

    toa_s, transform, crs, cellsize_m = read_fire_raster(toa_path, epsg=epsg)
    burned = np.isfinite(toa_s)
    n_burned = int(burned.sum())
    if n_burned == 0:
        raise PostprocessElmfireError(
            "ELMFIRE_NO_SPREAD",
            "the solver completed but NO cell ignited/burned (all-nonburnable "
            "fuels over the AOI, or the ignition landed on a nonburnable "
            "cell). This is a typed zero-spread result, not a rendering "
            "failure — try a different ignition point or AOI.",
            details={"toa_raster": str(toa_path)},
        )

    # --- Typed metrics on the SOURCE projected grid (cell size in metres). ---
    burned_area_km2 = float(n_burned) * (cellsize_m * cellsize_m) / 1.0e6
    fire_arrival_max_hr = float(np.nanmax(toa_s)) / 3600.0
    metrics: dict[str, Any] = {
        "burned_area_km2": burned_area_km2,
        "burned_cell_count": n_burned,
        "fire_arrival_max_hr": fire_arrival_max_hr,
        "cellsize_m": cellsize_m,
        "crs": crs,
        "max_flame_length_m": None,
        "max_spread_rate_m_min": None,
    }
    duration_hours = max(float(duration_s), 1.0) / 3600.0

    def _mk_layer(
        *,
        layer_id: str,
        name: str,
        uri: str,
        style_preset: str,
        role: str,
        units: str,
        frame_burned_km2: float | None = None,
    ) -> FireSpreadLayerURI:
        return FireSpreadLayerURI(
            layer_id=layer_id,
            name=name,
            layer_type="raster",
            uri=uri,
            style_preset=style_preset,
            role=role,  # type: ignore[arg-type]
            units=units,
            bbox=tuple(bbox),
            burned_area_km2=(
                frame_burned_km2
                if frame_burned_km2 is not None
                else burned_area_km2
            ),
            fire_arrival_max_hr=fire_arrival_max_hr,
            max_flame_length_m=metrics["max_flame_length_m"],
            max_spread_rate_m_min=metrics["max_spread_rate_m_min"],
            duration_hours=duration_hours,
            ignition_lonlat=ignition_lonlat,
        )

    # --- Flame length / spread rate (converted ONCE, before the layers). ----
    aux_specs: list[tuple[str, str, str, str, str, float]] = []
    # (family, layer name, id stem, style preset, units, conversion factor)
    for family, lname, stem, preset, units, factor in (
        (
            "flame_length", "Flame length", "fire-flame-length",
            ELMFIRE_FLAME_LEN_STYLE_PRESET, "m", FT_TO_M,
        ),
        (
            "spread_rate", "Spread rate", "fire-spread-rate",
            ELMFIRE_SPREAD_RATE_STYLE_PRESET, "m/min", FTMIN_TO_MMIN,
        ),
    ):
        if rasters.get(family) is not None:
            aux_specs.append((family, lname, stem, preset, units, factor))

    aux_data: list[tuple[str, str, str, str, str, Any, Any, str]] = []
    for family, lname, stem, preset, units, factor in aux_specs:
        arr, a_transform, a_crs, _cs = read_fire_raster(
            rasters[family], epsg=epsg  # type: ignore[arg-type]
        )
        arr = arr * factor  # feet -> metres / ft/min -> m/min, exactly once
        key = (
            "max_flame_length_m" if family == "flame_length"
            else "max_spread_rate_m_min"
        )
        metrics[key] = (
            float(np.nanmax(arr)) if np.isfinite(arr).any() else 0.0
        )
        aux_data.append(
            (family, lname, stem, preset, units, arr, a_transform, a_crs)
        )

    logger.info(
        "postprocess_elmfire run_id=%s burned_area_km2=%.4f burned_cells=%d "
        "arrival_max_hr=%.2f flame_max_m=%s spread_max_m_min=%s (crs=%s)",
        run_id,
        burned_area_km2,
        n_burned,
        fire_arrival_max_hr,
        metrics["max_flame_length_m"],
        metrics["max_spread_rate_m_min"],
        crs,
    )

    # --- PRIMARY: the ToA COG in HOURS (the headline layer). ----------------
    toa_hr = np.where(burned, toa_s / 3600.0, np.nan)
    primary_cog = _write_fire_cog_4326(toa_hr, crs, transform)
    try:
        primary_uri = _upload_cog_to_runs_bucket(
            primary_cog, run_id, runs_bucket, dest_filename="elmfire_toa.tif"
        )
    finally:
        _safe_unlink(primary_cog)

    layers: list[FireSpreadLayerURI] = [
        _mk_layer(
            layer_id=f"fire-arrival-{run_id}",
            name="Fire arrival time",
            uri=primary_uri,
            style_preset=ELMFIRE_TOA_STYLE_PRESET,
            role="primary",
            units="hours",
        )
    ]

    # --- Burned-extent animation frames (per-hour ToA threshold). -----------
    frame_grids = toa_frame_grids(toa_s, duration_s)
    frame_layers: list[FireSpreadLayerURI] = []
    written: list[Path] = []
    cell_km2 = (cellsize_m * cellsize_m) / 1.0e6
    try:
        for frame_no, (_hour, grid) in enumerate(frame_grids, start=1):
            cog = _write_fire_cog_4326(grid, crs, transform)
            written.append(cog)
            frame_uri = _upload_cog_to_runs_bucket(
                cog,
                run_id,
                runs_bucket,
                dest_filename=frame_dest_filename("elmfire_burned", frame_no),
            )
            _safe_unlink(cog)
            written.pop()
            frame_layers.append(
                _mk_layer(
                    layer_id=frame_layer_id(_FIRE_FRAME_STEM, frame_no, run_id),
                    name=frame_name(frame_no, _FIRE_QUANTITY_LABEL),
                    uri=frame_uri,
                    style_preset=ELMFIRE_TOA_STYLE_PRESET,
                    role="context",
                    units="hours",
                    frame_burned_km2=float(
                        np.isfinite(np.asarray(grid)).sum()
                    ) * cell_km2,
                )
            )
    except PostprocessElmfireError as exc:
        # Corrupt-frame guard: degrade to primary-only, never sink the run.
        logger.warning(
            "postprocess_elmfire: a frame COG write/upload failed (%s); "
            "degrading to the primary ToA layer only (no animation group).",
            exc,
        )
        for p in written:
            _safe_unlink(p)
        frame_layers = []

    # "< 2 never groups": a lone frame can never form a web scrubber group.
    if len(frame_layers) >= 2:
        layers.extend(frame_layers)
    elif frame_layers:
        logger.info(
            "postprocess_elmfire: only %d burned-extent frame — emitting the "
            "primary ToA layer without an animation group (run_id=%s)",
            len(frame_layers),
            run_id,
        )

    # --- Flame length / spread rate context COGs. ----------------------------
    for family, lname, stem, preset, units, arr, a_transform, a_crs in aux_data:
        if not np.isfinite(arr).any():
            logger.info(
                "postprocess_elmfire: %s raster is all-nodata — honestly "
                "skipped (run_id=%s)",
                family,
                run_id,
            )
            continue
        cog = _write_fire_cog_4326(arr, a_crs, a_transform)
        try:
            uri = _upload_cog_to_runs_bucket(
                cog, run_id, runs_bucket, dest_filename=f"elmfire_{family}.tif"
            )
        finally:
            _safe_unlink(cog)
        layers.append(
            _mk_layer(
                layer_id=f"{stem}-{run_id}",
                name=lname,
                uri=uri,
                style_preset=preset,
                role="context",
                units=units,
            )
        )

    logger.info(
        "postprocess_elmfire run_id=%s emitted %d layers (primary + %d frames "
        "+ %d aux)",
        run_id,
        len(layers),
        len(frame_layers) if len(frame_layers) >= 2 else 0,
        len(aux_data),
    )
    return layers, metrics
