"""Reproducible authoring script for the canonical M2 sample QGIS project.

Generates ``services/workers/pyqgis/sample_project/grace2-sample.qgs`` — a
single-layer EPSG:4326 project covering CONUS, intended as the canonical
``.qgs`` consumed by:

- the live QGIS Server Cloud Run service (FR-QS-2: read via ``/vsigs/`` from
  ``gs://grace-2-hazard-prod-qgs/grace2-sample.qgs``),
- the PyQGIS worker round-trip in job-0020,
- the M2 acceptance suite in job-0023.

Run inside the ``grace2`` conda env (QGIS 3.40.3-Bratislava, Python 3.12,
see ``infra/conda/environment.yml``)::

    conda activate grace2
    python services/workers/pyqgis/sample_project/build_sample_project.py

The script is idempotent — re-running overwrites the file with bit-stable
content (modulo QGIS' internal save timestamp). The accompanying
``styles/basemap.qml`` is *not* embedded into the ``.qgs`` itself — the
QGIS Server image bakes it into ``/opt/styles/`` at container build time
(job-0018 mechanism) and the worker (job-0020) applies it via
``apply_style_preset`` against the layer name.

Layer name: ``basemap-osm-conus`` (matches the ``.qml`` preset by name).
"""
from __future__ import annotations

import sys
from pathlib import Path

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsDataSourceUri,
    QgsProject,
    QgsRasterLayer,
    QgsReferencedRectangle,
    QgsRectangle,
)

LAYER_NAME = "basemap-osm-conus"
CONUS_EXTENT = QgsRectangle(-125.0, 24.0, -66.0, 50.0)  # lon_min, lat_min, lon_max, lat_max
TARGET_CRS = QgsCoordinateReferenceSystem("EPSG:4326")

# OSM XYZ tile URL — must be URL-encoded for QGIS' XYZ provider URI grammar.
# The ``type=xyz`` provider expects the template URL in a QgsDataSourceUri.
OSM_TILE_TEMPLATE = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"


def build_xyz_uri(url_template: str, zmin: int = 0, zmax: int = 19) -> str:
    """Compose a QGIS ``type=xyz`` data-source URI for an XYZ tile endpoint."""
    uri = QgsDataSourceUri()
    uri.setParam("type", "xyz")
    uri.setParam("url", url_template)
    uri.setParam("zmin", str(zmin))
    uri.setParam("zmax", str(zmax))
    # http-header passthrough is the standard "be a good citizen" signature.
    # QGIS Server inside Cloud Run egresses on its own SA; OSM tile policy
    # applies — at smoke scale this is fine; pre-MVP only.
    return bytes(uri.encodedUri()).decode("ascii")


def main(output_path: Path) -> int:
    qgs_app = QgsApplication([], False)
    qgs_app.initQgis()
    try:
        project = QgsProject.instance()
        project.clear()
        project.setCrs(TARGET_CRS)
        project.setTitle("M2 sample (CONUS basemap)")

        xyz_uri = build_xyz_uri(OSM_TILE_TEMPLATE)
        layer = QgsRasterLayer(xyz_uri, LAYER_NAME, "wms")
        if not layer.isValid():
            print(
                f"ERROR: XYZ layer failed to initialize. URI={xyz_uri!r}",
                file=sys.stderr,
            )
            return 2

        # Pin the layer CRS — XYZ tiles are Web Mercator (EPSG:3857) natively;
        # QGIS will reproject on the fly to the project CRS (EPSG:4326).
        layer.setCrs(QgsCoordinateReferenceSystem("EPSG:3857"))

        project.addMapLayer(layer)

        # Lock the project default extent to CONUS so first WMS request
        # without an explicit BBOX still hits the intended area.
        project.viewSettings().setDefaultViewExtent(
            QgsReferencedRectangle(CONUS_EXTENT, TARGET_CRS)
        )
        # QGIS Server reads the project's ``WMSExtent`` advertised CRS extent.
        # Set both the project map-canvas extent and the WMS service extent.
        project.writeEntry("WMSExtent", "/", [
            str(CONUS_EXTENT.xMinimum()),
            str(CONUS_EXTENT.yMinimum()),
            str(CONUS_EXTENT.xMaximum()),
            str(CONUS_EXTENT.yMaximum()),
        ])
        # Advertise WMS-served CRSes so clients can request EPSG:4326 directly.
        project.writeEntry("WMSCrsList", "/", ["EPSG:4326", "EPSG:3857"])
        # Make the layer queryable / published.
        project.writeEntry("WMSServiceTitle", "/", "sample WMS")
        project.writeEntry(
            "WMSServiceAbstract",
            "/",
            "M2 smoke sample — single OSM XYZ basemap covering CONUS.",
        )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        if not project.write(str(output_path)):
            print(f"ERROR: QgsProject.write({output_path}) returned False", file=sys.stderr)
            return 3

        print(f"Wrote {output_path}")
        print(f"  QGIS version: {Qgis.QGIS_VERSION}")
        print(f"  Layers: {[l.name() for l in project.mapLayers().values()]}")
        print(f"  Project CRS: {project.crs().authid()}")
        print(f"  WMS extent (CONUS): {CONUS_EXTENT.toString()}")
        return 0
    finally:
        QgsProject.instance().clear()
        qgs_app.exitQgis()


if __name__ == "__main__":
    here = Path(__file__).resolve().parent
    out = here / "grace2-sample.qgs"
    raise SystemExit(main(out))
