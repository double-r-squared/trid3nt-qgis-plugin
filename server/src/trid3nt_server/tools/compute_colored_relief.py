"""``compute_colored_relief`` atomic tool — wraps ``gdaldem color-relief`` (job-0080).

Color-tints a DEM by elevation using one of four built-in ramp presets. The
result is a single-band-per-channel RGB GeoTIFF (3-band) cached under
``cache/static-30d/colored_relief/<key>.tif`` in the project cache bucket.

FR-TA-2: atomic tool, returns ``LayerURI``.
FR-CE-8 / FR-DC-3/4: routed through ``read_through`` so identical
``(dem_uri, ramp)`` calls reuse the cached artifact.

``gdaldem color-relief`` is already present in the deployment environment
(job-0063 confirmed GDAL availability in ``.venv-agent``).

Ramp definitions live inline as Python dicts and are written to a temp file
(the CSV format ``gdaldem color-relief`` requires). Each ramp entry is a
4-tuple ``(elevation_m, R, G, B)`` where all channel values are 0-255.

The tool uses ``nv`` (no-data) rows at the top of the ramp file to keep
``gdaldem``'s no-data pixels transparent so they don't paint black over the
flood layer.

FR-TA-3 docstring discipline: one-sentence summary, "Use this when:",
"Do NOT use this for:", full param + return descriptions.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from typing import Literal, Any

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from .cache import read_through

# job-0269: single source for the binary resolution + PROJ/GDAL data-dir env
# (the job-0257 CRS fix). compute_colored_relief shipped with a bare
# ``"gdaldem"`` argv — FileNotFoundError in any env where gdaldem is not on
# PATH (live failure 2026-06-10, Boulder colored relief) — and no PROJ env,
# which silently degrades the output CRS to LOCAL_CS exactly as hillshade did.
# job-0271: + COG conversion (flat gdaldem GTiffs render too slowly via WMS).
from .compute_hillshade import _gdaldem_subprocess_env, _translate_to_cog

__all__ = ["compute_colored_relief"]

logger = logging.getLogger("trid3nt_server.tools.compute_colored_relief")


# ---------------------------------------------------------------------------
# Error type (mirrors data_fetch pattern).
# ---------------------------------------------------------------------------


class ColoredReliefError(RuntimeError):
    """Raised when color-relief computation fails.

    ``error_code`` is stable for FR-AS-11 mapping; ``retryable`` is False
    because failures are almost always a missing binary or a corrupt DEM.
    """

    error_code: str = "COLORED_RELIEF_ERROR"
    retryable: bool = False


# --------------------------------------------------------------------------- #
# gdaldem binary resolution (job-0269 — mirrors compute_slope/_aspect)
# --------------------------------------------------------------------------- #

_GDALDEM_BIN: str | None = None


def _get_gdaldem_bin() -> str:
    """Resolve the ``gdaldem`` binary path, with env-var override support.

    Checks ``TRID3NT_GDALDEM_BIN`` first, then PATH (via ``shutil.which``),
    then the known conda-env path from the dev environment. Raises
    ``ColoredReliefError`` if not found.
    """
    global _GDALDEM_BIN
    if _GDALDEM_BIN is not None:
        return _GDALDEM_BIN

    import shutil

    candidate = (
        os.environ.get("TRID3NT_GDALDEM_BIN")
        or shutil.which("gdaldem")
        or _conda_grace2_gdaldem()
    )
    if candidate is None or not os.path.isfile(candidate):
        raise ColoredReliefError(
            "gdaldem binary not found on PATH; set TRID3NT_GDALDEM_BIN "
            "or install gdal-bin / activate the grace2 conda env."
        )
    _GDALDEM_BIN = candidate
    return _GDALDEM_BIN


def _conda_grace2_gdaldem() -> str | None:
    """Return the grace2 conda-env gdaldem path if it exists."""
    candidate = os.path.expanduser("~/miniforge3/envs/grace2/bin/gdaldem")
    return candidate if os.path.isfile(candidate) else None


def _download_dem_to_local(dem_uri: str) -> str:
    """Stage an ``s3://`` DEM to a local temp file; pass local paths through.

    job-0269: replaces the ``/vsis3/`` input path — the subprocess gdaldem
    has no guaranteed object-store auth context, while the agent process holds
    the EC2 instance-role credentials boto3 resolves via IMDS. Mirrors the
    compute_slope/_aspect staging pattern. Caller owns cleanup of the returned
    temp file (only when it differs from ``dem_uri``).
    """
    # sprint-14-aws (job-0290b): s3:// staging — download to a local temp file.
    if dem_uri.startswith("s3://"):
        from .cache import read_object_bytes_s3
        try:
            with tempfile.NamedTemporaryFile(
                suffix=".tif", delete=False, prefix="trid3nt_relief_dem_"
            ) as f:
                f.write(read_object_bytes_s3(dem_uri))
                return f.name
        except ColoredReliefError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ColoredReliefError(
                f"S3 download failed for {dem_uri!r}: {exc}"
            ) from exc
    # Local path — pass through (test / dev convenience).
    return dem_uri


# ---------------------------------------------------------------------------
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
# Elevations are in metres (NAVD88 / WGS84 ellipsoidal depending on the DEM
# source); the ramps cover the full practical range for CONUS DEMs (~-86 m to
# ~4418 m) but are read by ``gdaldem`` relative to the actual cell values, so
# below-ramp cells get the lowest colour and above-ramp cells get the highest.
# ---------------------------------------------------------------------------

# Type alias for ramp entries.
_RampEntry = tuple[int | str, int, int, int]  # (elevation | "nv", R, G, B)

# fmt: off
_RAMPS: dict[str, list[_RampEntry]] = {
    # "terrain" — natural-earth green→brown→white (Imhof-style).
    # Inspired by the GRASS r.color terrain preset; suitable for general maps.
    "terrain": [
        ("nv", 0, 0, 0),      # no-data → black (will be masked by alpha)
        (-200, 70, 130, 180),  # deep ocean / well below sea level → steel blue
        (0,    70, 130, 180),  # sea level → ocean blue
        (1,   110, 160,  70),  # just above sea level → lowland green
        (200, 150, 180,  80),  # low plains → yellow-green
        (600, 190, 160,  90),  # mid elevations → tan/olive
        (1200, 160, 100,  60), # highlands → brown
        (2000, 200, 140,  90), # high elevations → light brown
        (3000, 230, 210, 180), # alpine → pale tan
        (4000, 250, 245, 235), # very high → near-white
        (9000, 255, 255, 255), # extreme (ice/snow) → white
    ],

    # "elevation_blue_green" — ocean-blue at sea level → green → tan → white.
    # Best for coastal and estuarine maps where the user wants to see the
    # land-sea transition clearly.
    "elevation_blue_green": [
        ("nv", 0, 0, 0),
        (-500,   0,  20, 100),  # deep ocean → dark navy
        (0,      0,  80, 180),  # sea level → ocean blue
        (1,     30, 160, 100),  # land sea fringe → green-blue
        (100,   60, 180,  80),  # coastal lowlands → bright green
        (400,  120, 190, 100),  # low plains → medium green
        (900,  190, 200, 130),  # highlands → yellow-green
        (1800, 210, 190, 140),  # upper highlands → tan
        (3000, 235, 220, 180),  # alpine → pale tan
        (9000, 255, 255, 255),  # extreme → white
    ],

    # "grayscale" — monochrome; intended as the multiply-blend companion for
    # hillshade in a Swiss-style stack. Low elevation → dark, high → light.
    # Using a narrow band (30–230) rather than full 0-255 so the multiply blend
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

    # "viridis" — perceptually-uniform; ideal for scientific / quantitative maps.
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
    """Write the named ramp to ``path`` in ``gdaldem color-relief`` CSV format.

    Args:
        ramp: one of the four preset names in ``_RAMPS``.
        path: filesystem path to write (caller is responsible for cleanup).
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
    """Write ``ramp`` rescaled to the DEM's actual elevation span (job-0273b).

    The preset ramps span a canonical 0–9000 m domain. An inland scene like
    Boulder (~1600–2800 m) lands entirely in one band of that domain, so the
    relief renders as a near-uniform brown sheet (user-observed). Rescaling
    the non-negative anchor elevations linearly onto ``[dem_min, dem_max]``
    stretches the full green→brown→white progression across the scene's own
    span. Negative anchors (ocean blues) clamp to ``dem_min`` so coastal
    scenes keep water at the ramp's bottom color; ``nv`` passes through.
    Deterministic for a given DEM (stats derive from the input), so the
    cache contract is unchanged. Falls back to the canonical ramp when the
    span is degenerate (flat tile).
    """
    if ramp not in _RAMPS:
        raise ColoredReliefError(
            f"unknown ramp={ramp!r}; allowed: {sorted(_VALID_RAMPS)}"
        )
    span = dem_max - dem_min
    if not (span > 1.0):  # degenerate / flat — canonical ramp is fine
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
    """Read the DEM's (min, max) elevation; None on any failure (fallback)."""
    try:
        import numpy as np
        import rasterio

        with rasterio.open(local_path) as ds:
            band = ds.read(1, masked=True)
            if band.mask.all():
                return None
            return float(np.min(band)), float(np.max(band))
    except Exception:  # noqa: BLE001 — stats are an enhancement, never fatal
        logger.warning("DEM min/max read failed; using canonical ramp", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# AtomicToolMetadata — registered once at import time.
# ---------------------------------------------------------------------------

_COMPUTE_COLORED_RELIEF_METADATA = AtomicToolMetadata(
    name="compute_colored_relief",
    ttl_class="static-30d",   # DEM-derived; stable
    source_class="colored_relief",
    cacheable=True,
)


# ---------------------------------------------------------------------------
# Fetch function (cache-miss path).
# ---------------------------------------------------------------------------


def _run_colored_relief(dem_uri: str, ramp: str) -> bytes:
    """Download ``dem_uri`` from GCS, run ``gdaldem color-relief``, return COG bytes.

    Args:
        dem_uri: ``gs://…`` URI of the input DEM (COG/GeoTIFF).
        ramp: one of the four preset names.

    Returns:
        Bytes of a 3-band Cloud-Optimized GeoTIFF (RGB), preserving the
        DEM's CRS and extent.

    Raises:
        ``ColoredReliefError`` on any subprocess or file I/O failure.
    """
    ramp_file: str | None = None
    dem_local: str | None = None
    out_file: str | None = None

    try:
        # job-0269: stage gs:// DEMs to a local temp file (agent-process ADC)
        # instead of /vsigs/ — the gdaldem subprocess has no guaranteed GCS
        # auth. Local paths (tests / dev) pass straight through.
        gdal_dem_path = _download_dem_to_local(dem_uri)
        if gdal_dem_path != dem_uri:
            dem_local = gdal_dem_path  # mark for cleanup in finally

        # Write ramp to a named temp file — gdaldem needs a real path.
        # job-0273b: rescale the ramp anchors onto the DEM's actual span so
        # inland scenes use the full color progression (Boulder rendered as
        # one near-uniform brown band of the canonical 0-9000m ramp).
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, prefix="trid3nt_ramp_"
        ) as rf:
            ramp_file = rf.name
        stats = _dem_min_max(gdal_dem_path)
        if stats is not None:
            _write_normalized_ramp_file(ramp, ramp_file, stats[0], stats[1])
        else:
            _write_ramp_file(ramp, ramp_file)

        # Output temp file for gdaldem.
        with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as out_f:
            out_file = out_f.name

        # Build gdaldem command.
        # -alpha: add an alpha channel so no-data pixels are transparent.
        # -compute_edges: avoids edge artefacts (no black border).
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
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=180,
            check=False,
            env=_gdaldem_subprocess_env(gdaldem_bin),  # job-0257 PROJ/GDAL dirs
        )
        if result.returncode != 0:
            stderr_txt = result.stderr.decode("utf-8", errors="replace").strip()
            raise ColoredReliefError(
                f"gdaldem color-relief failed (rc={result.returncode}): {stderr_txt}"
            )

        # job-0271: serve a real COG (tiled + overviews). The flat
        # strip-organized gdaldem output rendered slower than the 60s WMS
        # gateway limit over /vsigs/ and triggered the cold-load layer
        # poisoning the job-0270 verifier isolated.
        return _translate_to_cog(out_file, gdaldem_bin)

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


# ---------------------------------------------------------------------------
# Registered atomic tool.
# ---------------------------------------------------------------------------


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
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """Color-tint a DEM by elevation using ``gdaldem color-relief``.

    Applies a named color ramp to a single-band elevation GeoTIFF and returns a
    3-band (RGB) Cloud-Optimized GeoTIFF. Four presets (terrain, elevation_blue_green,
    grayscale, viridis) cover the common terrain visualization needs. Result is cached
    for 30 days and suitable as a QGIS Server WMS basemap layer.

    When to use:
        - Producing a colored elevation basemap for display beneath flood, habitat,
          or hazard overlays.
        - Building a Swiss-style shaded-relief stack: grayscale colored relief +
          ``compute_hillshade`` output → multiply-blended cartographic base.
        - User asks for "colored elevation", "terrain colormap", or "elevation
          visualization".
        - Coastal or sea-level scenarios requiring the elevation-blue-green ramp.

    When NOT to use:
        - Hillshade or shadow visualization (use ``compute_hillshade``).
        - Slope or aspect analysis (use ``compute_slope`` / ``compute_aspect``).
        - Computing quantitative elevation statistics (use ``compute_zonal_statistics``).
        - Animated or time-varying elevation (output is a static single-time raster).

    Ramp presets:
        "terrain": natural-earth green → brown → white (low → high). Default.
            Best for general-purpose terrain maps.
        "elevation_blue_green": ocean-blue at sea-level → green → tan → white
            at high elevations. Best for coastal / estuarine / sea-level maps
            where the land-sea transition matters.
        "grayscale": monochrome (low=dark, high=light). Ideal as a
            multiply-blend companion for hillshade in a Swiss-style stack —
            the grayscale colorramp multiplied by the hillshade produces a
            cartographically pleasing shaded-relief base.
        "viridis": perceptually-uniform colour ramp (purple → blue → green →
            yellow). Best when the user wants scientific / quantitative
            emphasis where equal visual distances represent equal elevation
            differences.

    LLM guidance:
        - "terrain" for general natural maps (the safe default)
        - "grayscale" when stacking with hillshade in a multiply blend
        - "viridis" when the user asks for a scientific or quantitative view
        - "elevation_blue_green" when the user mentions ocean / sea / coastal

    Params:
        dem_uri: ``gs://…`` URI of the input DEM. Must be a GeoTIFF (COG or
            standard) with elevation values in metres. Typically the ``uri``
            from a preceding ``fetch_dem`` call.
        ramp: one of the four preset names above. Defaults to ``"terrain"``.

    Returns:
        A ``LayerURI`` pointing at a 3- or 4-band Cloud-Optimized GeoTIFF in
        the cache bucket:
        ``s3://trid3nt-cache/cache/static-30d/colored_relief/<key>.tif``.
        The output shares the DEM's CRS and spatial extent; the units are RGB
        colour channels (0-255 per band), not elevation metres.

    FR-CE-8: The computation is routed through ``read_through`` so identical
    ``(dem_uri, ramp)`` calls reuse the cached artefact. Cache key is
    SHA-256 of ``{dem_uri, ramp, ttl_vintage}``; the 30-day TTL matches the
    DEM's own cache class since the colorramp output is fully determined by
    the DEM + ramp choice.

    Cross-tool dependencies:
        Upstream (consumes):
        - ``fetch_dem`` — primary source of ``dem_uri``; pass ``LayerURI.uri``
          (gs:// COG) directly as ``dem_uri``.
        Downstream (feeds):
        - ``publish_layer`` — pass the returned ``LayerURI`` as ``layer_uri``
          to register the colored relief with QGIS Server WMS.
        - ``compute_hillshade`` — combine for a Swiss-style shaded-relief
          stack (grayscale colored relief × hillshade multiply blend).
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
        name=f"Colored Relief — {ramp_label}",
        layer_type="raster",
        uri=result.uri,
        style_preset="continuous_dem",  # closest existing preset; colored-relief preset is follow-up
        role="context",
        units="rgb",
    )
