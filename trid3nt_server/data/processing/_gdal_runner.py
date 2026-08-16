"""Shared GDAL runner for the terrain compute_* tools.

Owns the single GDAL-CLI binary resolution (env override -> PATH), the
PROJ/GDAL data-dir env wiring, the single subprocess invocation (timeout +
returncode -> typed error), the shared raster-bytes reader, and the in-process
COG encode. Only ``gdaldem`` and ``gdal_contour`` remain subprocess-backed
(rasterio has no gdaldem/gdal_contour equivalent); COG encoding is in-process
via the rasterio COG driver.

Callers supply their own typed-error factory to ``run_gdal`` / ``read_raster_bytes``
so each tool keeps its own error class + SCREAMING_SNAKE code.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from typing import Callable

logger = logging.getLogger(__name__)

#: subprocess ceiling; any gdaldem/gdal_contour of a reasonable DEM is seconds.
GDAL_TIMEOUT_S = 300

_GDALDEM_BIN: str | None = None
_GDAL_CONTOUR_BIN: str | None = None


def resolve_gdaldem() -> str | None:
    """Resolve ``gdaldem``: ``TRID3NT_GDALDEM_BIN`` override, else PATH. None if absent."""
    global _GDALDEM_BIN
    if _GDALDEM_BIN is not None:
        return _GDALDEM_BIN
    candidate = os.environ.get("TRID3NT_GDALDEM_BIN") or shutil.which("gdaldem")
    if candidate and os.path.isfile(candidate):
        _GDALDEM_BIN = candidate
        return candidate
    return None


def resolve_gdal_contour() -> str | None:
    """Resolve ``gdal_contour``: env override, else the gdaldem sibling, else PATH.

    ``gdal_contour`` ships next to ``gdaldem``, so a single ``TRID3NT_GDALDEM_BIN``
    override resolves both; ``TRID3NT_GDAL_CONTOUR_BIN`` overrides it directly.
    """
    global _GDAL_CONTOUR_BIN
    if _GDAL_CONTOUR_BIN is not None:
        return _GDAL_CONTOUR_BIN
    candidate = os.environ.get("TRID3NT_GDAL_CONTOUR_BIN")
    if not candidate:
        gdaldem = resolve_gdaldem()
        if gdaldem:
            sibling = os.path.join(os.path.dirname(gdaldem), "gdal_contour")
            if os.path.isfile(sibling):
                candidate = sibling
    if not candidate:
        candidate = shutil.which("gdal_contour")
    if candidate and os.path.isfile(candidate):
        _GDAL_CONTOUR_BIN = candidate
        return candidate
    return None


def gdal_env(gdal_bin: str) -> dict[str, str]:
    """Build the subprocess env, wiring PROJ/GDAL data dirs from the binary prefix.

    A bare gdaldem invocation with ``PROJ_LIB`` unset cannot find ``proj.db``
    and silently degrades the output CRS to a proj.db-less ``LOCAL_CS`` (QGIS
    then cannot reproject the layer). Deriving ``<prefix>/share/proj`` +
    ``<prefix>/share/gdal`` from the resolved binary path restores the projected
    CRS. Explicit user config wins (``setdefault``).
    """
    env = os.environ.copy()
    prefix = os.path.dirname(os.path.dirname(os.path.abspath(gdal_bin)))
    proj_dir = os.path.join(prefix, "share", "proj")
    gdal_dir = os.path.join(prefix, "share", "gdal")
    if os.path.isdir(proj_dir):
        env.setdefault("PROJ_LIB", proj_dir)
        env.setdefault("PROJ_DATA", proj_dir)
    if os.path.isdir(gdal_dir):
        env.setdefault("GDAL_DATA", gdal_dir)
    return env


def run_gdal(
    cmd: list[str],
    gdal_bin: str,
    *,
    on_unavailable: Callable[[str], Exception],
    on_failed: Callable[[str], Exception],
    timeout: int = GDAL_TIMEOUT_S,
) -> subprocess.CompletedProcess:
    """Run a GDAL CLI subprocess; map missing-binary / timeout / non-zero to typed errors.

    ``on_unavailable`` / ``on_failed`` build the caller's typed error from a
    message string, so each tool raises its own error class + code.
    """
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            check=False,
            timeout=timeout,
            env=gdal_env(gdal_bin),
        )
    except FileNotFoundError as exc:
        raise on_unavailable(f"{cmd[0]!r} not executable: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise on_failed(f"{cmd[0]} timed out after {timeout} s: {exc}") from exc
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        stdout = result.stdout.decode("utf-8", errors="replace").strip()
        raise on_failed(
            f"{cmd[0]} returned exit code {result.returncode}; "
            f"stderr={stderr!r}; stdout={stdout!r}"
        )
    return result


def read_raster_bytes(uri: str, *, on_error: Callable[[str], Exception]) -> bytes:
    """Read bytes from an ``s3://`` URI (shared boto3 reader) or a local path.

    S3 and local paths only. ``on_error`` builds the caller's typed error for a
    download / read failure.
    """
    if uri.startswith("s3://"):
        from trid3nt_server.data.cache import read_object_bytes_s3

        try:
            return read_object_bytes_s3(uri)
        except Exception as exc:  # noqa: BLE001
            raise on_error(f"S3 download failed for {uri!r}: {exc}") from exc
    try:
        with open(uri, "rb") as f:
            return f.read()
    except OSError as exc:
        raise on_error(f"Could not read local path {uri!r}: {exc}") from exc


def translate_to_cog(input_path: str, _gdal_bin: object | None = None) -> bytes:
    """Encode a flat GeoTIFF into tiled COG-with-overviews bytes (in-process rasterio).

    The rasterio ``COG`` driver tiles + builds overviews in one pass, so the
    product renders without a per-strip range request. Preserves dtype, CRS,
    transform, nodata, band color-interpretation, and a band-1 palette color
    table (paletted rasters like NLCD land cover). Best-effort: returns the
    input bytes unchanged on any failure (never raises).

    ``_gdal_bin`` is accepted for backward-compatible call sites and ignored --
    COG encoding no longer shells out to ``gdal_translate``.
    """
    out_tmp: str | None = None
    try:
        import rasterio

        with rasterio.open(input_path) as src:
            profile = {
                "driver": "COG",
                "width": src.width,
                "height": src.height,
                "count": src.count,
                "dtype": src.dtypes[0],
                "crs": src.crs,
                "transform": src.transform,
                "compress": "DEFLATE",
            }
            if src.nodata is not None:
                profile["nodata"] = src.nodata
            data = src.read()
            colorinterp = src.colorinterp
            try:
                cmap = src.colormap(1)
            except (ValueError, KeyError):
                cmap = None
            except Exception:  # noqa: BLE001 -- any read failure -> no colormap
                cmap = None
        with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as of:
            out_tmp = of.name
        with rasterio.open(out_tmp, "w", OVERVIEW_RESAMPLING="NEAREST", **profile) as dst:
            dst.write(data)
            try:
                dst.colorinterp = colorinterp
            except Exception:  # noqa: BLE001 -- colorinterp set is best-effort
                pass
            if cmap:
                try:
                    dst.write_colormap(1, cmap)
                except Exception:  # noqa: BLE001 -- colormap copy is best-effort
                    pass
        with open(out_tmp, "rb") as f:
            return f.read()
    except Exception as exc:  # noqa: BLE001 -- COG encode is best-effort
        logger.warning(
            "translate_to_cog: rasterio COG encode failed (%s: %s); returning flat bytes",
            type(exc).__name__,
            exc,
        )
        with open(input_path, "rb") as f:
            return f.read()
    finally:
        if out_tmp is not None:
            try:
                os.unlink(out_tmp)
            except OSError:
                pass
