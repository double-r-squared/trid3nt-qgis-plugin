"""Shared GDAL runner for the terrain compute_* tools.

Owns the single GDAL-CLI binary resolution (env override -> PATH), the
PROJ/GDAL data-dir env wiring, the single subprocess invocation (timeout +
returncode -> typed error), and the shared raster-bytes reader. Only ``gdaldem`` and ``gdal_contour``
remain subprocess-backed (rasterio has no equivalent).

COG ENCODING IS NOT HERE. Turning a flat GeoTIFF into a tiled
COG-with-overviews is a PUBLICATION concern - it is what makes a raster
renderable rather than what makes it correct - so it lives with the rest of
emission (``trid3nt_server/emission/cog.py``). It used to sit here, and
``emission/publish.py`` reached backwards into a terrain TOOL to get at it.

Callers supply their own typed-error factory to ``run_gdal`` / ``read_raster_bytes``
so each tool keeps its own error class + SCREAMING_SNAKE code.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
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
        from trid3nt_server.tools.cache import read_object_bytes_s3

        try:
            return read_object_bytes_s3(uri)
        except Exception as exc:  # noqa: BLE001
            raise on_error(f"S3 download failed for {uri!r}: {exc}") from exc
    try:
        with open(uri, "rb") as f:
            return f.read()
    except OSError as exc:
        raise on_error(f"Could not read local path {uri!r}: {exc}") from exc
