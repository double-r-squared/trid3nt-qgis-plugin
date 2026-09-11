"""``compute_colored_relief`` atomic tool - wraps ``gdaldem color-relief``.

The ramp file carries ``nv`` rows so no-data pixels stay transparent rather than
painting black over whatever the relief sits under.
"""
from __future__ import annotations

import logging
import os
import tempfile
from typing import Literal, Any

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.cache import read_through
from trid3nt_server.emission.cog import translate_to_cog as _translate_to_cog
from trid3nt_server.tools.derive._gdal_runner import (
    read_raster_bytes,
    resolve_gdaldem,
    run_gdal,
)

__all__ = ["compute_colored_relief"]

logger = logging.getLogger("trid3nt_server.tools.derive.compute_colored_relief.compute_colored_relief")




class ColoredReliefError(RuntimeError):
    """Color-relief computation failed. Not retryable: the cause is a missing
    binary or a corrupt DEM, neither of which a second attempt fixes.
    """

    error_code: str = "COLORED_RELIEF_ERROR"
    retryable: bool = False




def _get_gdaldem_bin() -> str:
    """Resolve ``gdaldem``; absent raises ``ColoredReliefError``."""
    binary = resolve_gdaldem()
    if binary is None:
        raise ColoredReliefError(
            "gdaldem binary not found on PATH; set TRID3NT_GDALDEM_BIN "
            "or install gdal-bin."
        )
    return binary


def _download_dem_to_local(dem_uri: str) -> str:
    """Stage an ``s3://`` DEM locally, passing local paths through; the caller
    unlinks the result whenever it differs from ``dem_uri``.
    """
    if dem_uri.startswith("s3://"):
        data = read_raster_bytes(
            dem_uri, on_error=lambda msg: ColoredReliefError(msg)
        )
        with tempfile.NamedTemporaryFile(
            suffix=".tif", delete=False, prefix="trid3nt_relief_dem_"
        ) as f:
            f.write(data)
            return f.name
    return dem_uri


# Ramp definitions.
#
# ``gdaldem color-relief`` ramp file format:
#   <elevation> <R> <G> <B>   (one entry per line; elevation in the DEM's units)
# Optional special rows:
#   nv <R> <G> <B>            (no-data / null value colour)
#
# Entries are sorted ascending by elevation; ``gdaldem`` interpolates linearly
# between adjacent rows. The special key ``nv`` sets the no-data colour.
#
# Elevations are in metres; a cell below the ramp takes the lowest colour and a
# cell above it the highest.

_RampEntry = tuple[int | str, int, int, int]  # (elevation | "nv", R, G, B)

# fmt: off
_RAMPS: dict[str, list[_RampEntry]] = {
    # natural-earth green -> brown -> white, Imhof-style
    "terrain": [
        ("nv", 0, 0, 0),      # no-data -> black (will be masked by alpha)
        (-200, 70, 130, 180),  # deep ocean / well below sea level -> steel blue
        (0,    70, 130, 180),  # sea level -> ocean blue
        (1,   110, 160,  70),  # just above sea level -> lowland green
        (200, 150, 180,  80),  # low plains -> yellow-green
        (600, 190, 160,  90),  # mid elevations -> tan/olive
        (1200, 160, 100,  60), # highlands -> brown
        (2000, 200, 140,  90), # high elevations -> light brown
        (3000, 230, 210, 180), # alpine -> pale tan
        (4000, 250, 245, 235), # very high -> near-white
        (9000, 255, 255, 255), # extreme (ice/snow) -> white
    ],

    # "elevation_blue_green" -- ocean-blue at sea level -> green -> tan -> white.
    # Best for coastal and estuarine maps where the user wants to see the
    # land-sea transition clearly.
    "elevation_blue_green": [
        ("nv", 0, 0, 0),
        (-500,   0,  20, 100),  # deep ocean -> dark navy
        (0,      0,  80, 180),  # sea level -> ocean blue
        (1,     30, 160, 100),  # land sea fringe -> green-blue
        (100,   60, 180,  80),  # coastal lowlands -> bright green
        (400,  120, 190, 100),  # low plains -> medium green
        (900,  190, 200, 130),  # highlands -> yellow-green
        (1800, 210, 190, 140),  # upper highlands -> tan
        (3000, 235, 220, 180),  # alpine -> pale tan
        (9000, 255, 255, 255),  # extreme -> white
    ],

    # "grayscale" -- monochrome; intended as the multiply-blend companion for
    # hillshade in a Swiss-style stack. Low elevation -> dark, high -> light.
    # Using a narrow band (30 - 230) rather than full 0-255 so the multiply blend
    # doesn't wash to pure black at low elevations.
    "grayscale": [
        ("nv", 0, 0, 0),
        (-500,  30,  30,  30),
        (0,     30,  30,  30),
        (1,     50,  50,  50),
        (500,  110, 110, 110),
        (1500, 170, 170, 170),
        (3000, 210, 210, 210),
        (9000, 230, 230, 230),
    ],

    # "viridis" -- perceptually-uniform; ideal for scientific / quantitative maps.
    # Sampled from the matplotlib viridis palette at 10 equidistant points.
    "viridis": [
        ("nv", 0, 0, 0),
        (-500,  68,   1,  84),   # viridis[0]
        (0,     68,   1,  84),   # same colour at sea level
        (1,     72,  40, 120),   # viridis[0.11]
        (900,   59,  82, 139),   # viridis[0.22]
        (1800,  44, 113, 142),   # viridis[0.33]
        (2700,  33, 145, 140),   # viridis[0.44]
        (3600,  39, 174, 128),   # viridis[0.56]
        (4500,  92, 200, 100),   # viridis[0.67]
        (5400, 170, 220,  50),   # viridis[0.78]
        (6300, 253, 231,  37),   # viridis[0.89]
        (9000, 253, 231,  37),   # cap at same yellow
    ],
}
# fmt: on

_VALID_RAMPS = frozenset(_RAMPS)


def _write_ramp_file(ramp: str, path: str) -> None:
    """Write the named ramp to ``path`` in ``gdaldem color-relief`` CSV format;
    the caller owns the file.
    """
    if ramp not in _RAMPS:
        raise ColoredReliefError(
            f"unknown ramp={ramp!r}; allowed: {sorted(_VALID_RAMPS)}"
        )
    lines: list[str] = []
    for entry in _RAMPS[ramp]:
        elev, r, g, b = entry
        lines.append(f"{elev} {r} {g} {b}\n")
    with open(path, "w") as fh:
        fh.writelines(lines)


def _write_normalized_ramp_file(
    ramp: str, path: str, dem_min: float, dem_max: float
) -> None:
    """Write ``ramp`` rescaled onto ``[dem_min, dem_max]``; a degenerate span
    falls back to the canonical ramp, and ``nv`` passes through untouched.
    """
    # The presets span a canonical 0-9000 m domain, so an inland scene at
    # 1600-2800 m lands inside one band of it and renders as a near-uniform
    # brown sheet. Rescaling the non-negative anchors onto the scene's own span
    # stretches the whole progression across it; negative anchors clamp to
    # dem_min so a coastal scene keeps water at the bottom colour. The stats
    # come from the input, so the result stays deterministic per DEM.
    if ramp not in _RAMPS:
        raise ColoredReliefError(
            f"unknown ramp={ramp!r}; allowed: {sorted(_VALID_RAMPS)}"
        )
    span = dem_max - dem_min
    if not (span > 1.0):
        _write_ramp_file(ramp, path)
        return
    positives = [e[0] for e in _RAMPS[ramp] if isinstance(e[0], (int, float)) and e[0] >= 0]
    domain_max = max(positives) if positives else 9000.0
    lines: list[str] = []
    for entry in _RAMPS[ramp]:
        elev, r, g, b = entry
        if elev == "nv":
            scaled: float | str = "nv"
        elif elev < 0:
            scaled = dem_min
        else:
            scaled = dem_min + (elev / domain_max) * span
        lines.append(f"{scaled} {r} {g} {b}\n")
    with open(path, "w") as fh:
        fh.writelines(lines)


def _dem_min_max(local_path: str) -> tuple[float, float] | None:
    """The DEM's ``(min, max)`` elevation, or None on any failure."""
    try:
        import numpy as np
        import rasterio

        with rasterio.open(local_path) as ds:
            band = ds.read(1, masked=True)
            if band.mask.all():
                return None
            return float(np.min(band)), float(np.max(band))
    except Exception:  # noqa: BLE001 -- stats are an enhancement, never fatal
        logger.warning("DEM min/max read failed; using canonical ramp", exc_info=True)
        return None



_COMPUTE_COLORED_RELIEF_METADATA = AtomicToolMetadata(
    name="compute_colored_relief",
    ttl_class="static-30d",   # DEM-derived; stable
    source_class="colored_relief",
    cacheable=True,
)




def _run_colored_relief(dem_uri: str, ramp: str) -> bytes:
    """Stage ``dem_uri``, run ``gdaldem color-relief`` and return RGB COG bytes
    on the DEM's own CRS and extent; any failure raises ``ColoredReliefError``.
    """
    ramp_file: str | None = None
    dem_local: str | None = None
    out_file: str | None = None

    try:
        # gdaldem color-relief reads a file path, so an s3:// DEM is staged.
        gdal_dem_path = _download_dem_to_local(dem_uri)
        if gdal_dem_path != dem_uri:
            dem_local = gdal_dem_path  # mark for cleanup in finally

        # gdaldem needs the ramp as a real path.
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, prefix="trid3nt_ramp_"
        ) as rf:
            ramp_file = rf.name
        stats = _dem_min_max(gdal_dem_path)
        if stats is not None:
            _write_normalized_ramp_file(ramp, ramp_file, stats[0], stats[1])
        else:
            _write_ramp_file(ramp, ramp_file)

        with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as out_f:
            out_file = out_f.name

        # -alpha keeps no-data pixels transparent; -compute_edges keeps the
        # tile from gaining a black border.
        gdaldem_bin = _get_gdaldem_bin()
        cmd = [
            gdaldem_bin,
            "color-relief",
            gdal_dem_path,
            ramp_file,
            out_file,
            "-alpha",
            "-compute_edges",
            "-of", "GTiff",
        ]

        logger.info("compute_colored_relief: running %s", " ".join(cmd))
        run_gdal(
            cmd, gdaldem_bin,
            on_unavailable=lambda msg: ColoredReliefError(msg),
            on_failed=lambda msg: ColoredReliefError(msg),
            timeout=180,
        )

        # gdaldem writes strip-organized output, which would force a range
        # request per strip on render; the COG rewrite is tiled with overviews.
        return _translate_to_cog(out_file)

    except ColoredReliefError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ColoredReliefError(
            f"compute_colored_relief failed for dem_uri={dem_uri!r} ramp={ramp!r}: {exc}"
        ) from exc
    finally:
        for path in (ramp_file, dem_local, out_file):
            if path is None:
                continue
            try:
                os.unlink(path)
            except OSError:
                pass




@register_tool(
    _COMPUTE_COLORED_RELIEF_METADATA,
    # Annotations: readOnlyHint=True (reads input raster/vector; writes cache
    # artifact only via the read-through shim), openWorldHint=False (all
    # computation is local GDAL/numpy; no external API calls),
    # destructiveHint=False, idempotentHint=True (deterministic transform;
    # same inputs always produce the same output pixels).
)
def compute_colored_relief(
    dem_uri: str,
    ramp: Literal["terrain", "elevation_blue_green", "grayscale", "viridis"] = "terrain",
    # absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> LayerURI:
    """Color-tint a DEM by elevation using ``gdaldem color-relief``.

    Use this when: producing a colored elevation basemap under flood/
    habitat/hazard overlays, a Swiss-style shaded-relief stack (grayscale
    ramp x ``compute_hillshade``), or the user asks for "colored elevation"/
    "terrain colormap". Do NOT use for: shadow visualization
    (``compute_hillshade``); slope/aspect (``compute_slope``/
    ``compute_aspect``); quantitative stats (the code_exec playground).

    Params:
        dem_uri: input DEM GeoTIFF (elevation in metres), typically from
            ``fetch_dem``.
        ramp: ``"terrain"`` (default, green->brown->white), ``"grayscale"``
            (multiply-blend companion for hillshade), ``"viridis"``
            (scientific/quantitative), or ``"elevation_blue_green"``
            (coastal/sea-level, ocean-blue->green->tan->white).

    Returns an RGB COG on the DEM's CRS and extent - 0-255 bands, not elevation.
    """
    if ramp not in _VALID_RAMPS:
        raise ColoredReliefError(
            f"unknown ramp={ramp!r}; allowed: {sorted(_VALID_RAMPS)}"
        )

    params = {"dem_uri": dem_uri, "ramp": ramp}
    result = read_through(
        metadata=_COMPUTE_COLORED_RELIEF_METADATA,
        params=params,
        ext="tif",
        fetch_fn=lambda: _run_colored_relief(dem_uri, ramp),
    )
    assert result.uri is not None, (
        "compute_colored_relief is cacheable; uri must be set by read_through"
    )

    # Derive a human-readable layer name from the ramp.
    ramp_labels = {
        "terrain": "Terrain",
        "elevation_blue_green": "Elevation (Blue-Green)",
        "grayscale": "Elevation (Grayscale)",
        "viridis": "Elevation (Viridis)",
    }
    ramp_label = ramp_labels.get(ramp, ramp)

    return LayerURI(
        layer_id=f"colored-relief-{ramp}-{abs(hash(dem_uri)) % 100_000:05d}",
        name=f"Colored Relief -- {ramp_label}",
        layer_type="raster",
        uri=result.uri,
        style={"kind": "continuous", "ramp": "gray", "units": "m", "label": "Elevation"},
        role="context",
        units="rgb",
    )
