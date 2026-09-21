"""A QGIS layer written out as a file the agent can take.

The one export in the plugin, in both directions across the seam: the case push
sends the user's layer up, and a materialised layer-request sends the windowed
provider layer back. ``kind`` rides along because the ingest route registers a
GeoPackage and a GeoTIFF differently, and only the writer knows which it wrote.
"""

from __future__ import annotations

import os
from typing import Any, Tuple

__all__ = ["LayerExportError", "export_to_tempfile"]


class LayerExportError(Exception):
    """The layer could not be written out -- carries the writer's own message,
    or an honest local description of why there was nothing to write."""


def export_to_tempfile(layer: Any) -> Tuple[str, str]:
    """Export ``layer`` to a temp file, returning ``(local_path, kind)`` with
    ``kind`` either ``"vector"`` (GeoPackage) or ``"raster"`` (GeoTIFF). A
    ``None``, unsupported or unexportable layer raises. The caller owns the
    file, including deleting it."""
    from qgis.core import QgsRasterLayer, QgsVectorLayer

    if layer is None:
        raise LayerExportError(
            "no active layer -- select a layer in the QGIS Layers panel first"
        )
    if isinstance(layer, QgsVectorLayer):
        return _export_vector(layer), "vector"
    if isinstance(layer, QgsRasterLayer):
        return _export_raster(layer), "raster"
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


def _export_raster(layer: Any) -> str:
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".tif", prefix="trid3nt_push_")
    os.close(fd)
    try:
        import processing

        processing.run(
            "gdal:translate",
            {
                "INPUT": layer,
                "TARGET_CRS": None,
                "NODATA": None,
                "COPY_SUBDATASETS": False,
                "OPTIONS": "",
                "EXTRA": "",
                "DATA_TYPE": 0,
                "OUTPUT": path,
            },
        )
    except Exception as exc:  # noqa: BLE001 -- surfaced, never silent
        raise LayerExportError(f"could not export raster layer: {exc}") from exc
    return path
