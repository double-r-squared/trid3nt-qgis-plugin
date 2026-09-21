"""A QGIS layer written out as a file the agent can take.

The one export in the plugin, in both directions across the seam: the case push
sends the user's layer up, and a materialised layer-request sends the windowed
provider layer back. ``kind`` rides along because the ingest route registers a
GeoPackage and a GeoTIFF differently, and only the writer knows which it wrote.
A raster export may be asked for a pixel spacing in metres; that is the caller's
lever, so it is honoured on the way out rather than served at the native grid
under the asked name.
"""

from __future__ import annotations

import math
import os
from typing import Any, Optional, Tuple

__all__ = ["LayerExportError", "export_to_tempfile"]


class LayerExportError(Exception):
    """The layer could not be written out -- carries the writer's own message,
    or an honest local description of why there was nothing to write."""


def export_to_tempfile(
    layer: Any, resolution_m: Optional[float] = None
) -> Tuple[str, str]:
    """Export ``layer`` to a temp file, returning ``(local_path, kind)`` with
    ``kind`` either ``"vector"`` (GeoPackage) or ``"raster"`` (GeoTIFF). A
    ``None``, unsupported or unexportable layer raises. ``resolution_m`` is the
    pixel spacing a raster is written at; a vector has no pixel grid, so asking
    one for a spacing is a refusal, never a value quietly dropped. The caller
    owns the file, including deleting it."""
    from qgis.core import QgsRasterLayer, QgsVectorLayer

    if layer is None:
        raise LayerExportError(
            "no active layer -- select a layer in the QGIS Layers panel first"
        )
    if isinstance(layer, QgsVectorLayer):
        if resolution_m is not None:
            raise LayerExportError(
                f"'{layer.name()}' is a vector layer and has no pixel grid, so "
                f"the asked {resolution_m} m spacing cannot be honoured"
            )
        return _export_vector(layer), "vector"
    if isinstance(layer, QgsRasterLayer):
        return _export_raster(layer, resolution_m), "raster"
    raise LayerExportError(
        f"active layer '{layer.name()}' is not a vector or raster layer"
    )


def _export_vector(layer: Any) -> str:
    import tempfile

    from qgis.core import QgsProject, QgsVectorFileWriter

    fd, path = tempfile.mkstemp(suffix=".gpkg", prefix="trid3nt_push_")
    os.close(fd)
    os.unlink(path)  # QgsVectorFileWriter creates the file itself

    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = "GPKG"
    options.fileEncoding = "UTF-8"
    transform_context = QgsProject.instance().transformContext()
    result = QgsVectorFileWriter.writeAsVectorFormatV3(
        layer, path, transform_context, options
    )
    # writeAsVectorFormatV3 returns (WriterError, errorMessage[, ...]).
    err = result[0] if isinstance(result, tuple) else result
    if err != QgsVectorFileWriter.NoError:
        detail = result[1] if isinstance(result, tuple) and len(result) > 1 else str(err)
        raise LayerExportError(f"could not export vector layer: {detail}")
    return path


def _export_raster(layer: Any, resolution_m: Optional[float] = None) -> str:
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".tif", prefix="trid3nt_push_")
    os.close(fd)
    params = {
        "INPUT": layer,
        "TARGET_CRS": None,
        "NODATA": None,
        "COPY_SUBDATASETS": False,
        "OPTIONS": "",
        "EXTRA": "",
        "DATA_TYPE": 0,
        "OUTPUT": path,
    }
    if resolution_m is not None:
        x_res, y_res = _target_resolution(layer, float(resolution_m))
        params["EXTRA"] = f"-tr {x_res!r} {y_res!r}"
    try:
        import processing

        processing.run("gdal:translate", params)
    except Exception as exc:  # noqa: BLE001 -- surfaced, never silent
        raise LayerExportError(f"could not export raster layer: {exc}") from exc
    return path


def _target_resolution(layer: Any, resolution_m: float) -> Tuple[float, float]:
    """The asked spacing as ``(x, y)`` in the layer's OWN CRS units, which is
    what ``gdal_translate -tr`` takes. Metres pass through on both axes. A
    geographic CRS converts each axis at the layer's centre latitude, through
    the metres in a degree of longitude and a degree of latitude there, so the
    exported cell is the asked size on the ground rather than square in degrees
    and wrong in metres. Any other unit refuses by name rather than passing a
    number that means something else."""
    from qgis.core import QgsUnitTypes

    crs = layer.crs()
    units = crs.mapUnits()
    if units == QgsUnitTypes.DistanceMeters:
        return resolution_m, resolution_m
    if not crs.isGeographic():
        raise LayerExportError(
            f"'{layer.name()}' is published in {crs.authid() or 'an unnamed CRS'}, "
            f"whose units are {QgsUnitTypes.toString(units)}; a spacing asked in "
            "metres cannot be honoured on that grid"
        )
    extent = layer.extent()
    phi = math.radians((extent.yMinimum() + extent.yMaximum()) / 2.0)
    per_degree_lat = (
        111132.92
        - 559.82 * math.cos(2 * phi)
        + 1.175 * math.cos(4 * phi)
        - 0.0023 * math.cos(6 * phi)
    )
    per_degree_lon = (
        111412.84 * math.cos(phi)
        - 93.5 * math.cos(3 * phi)
        + 0.118 * math.cos(5 * phi)
    )
    if per_degree_lon <= 0.0:
        raise LayerExportError(
            f"'{layer.name()}' centres on a pole, where a degree of longitude is "
            f"no distance at all; the asked {resolution_m} m has no grid there"
        )
    return resolution_m / per_degree_lon, resolution_m / per_degree_lat
