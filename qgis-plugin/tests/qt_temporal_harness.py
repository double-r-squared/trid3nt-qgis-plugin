"""Real-QGIS harness for ``layers.stamp_temporal`` -- run as a SUBPROCESS by
``test_temporal.TestQtTemporalStamp`` (needs qgis.core; the pure test venv
does not have it, so the parent test probes the system interpreter and skips
honestly when absent).

Builds three tiny GeoTIFFs named like an exported flood-frame stack
(``Flood_depth_step_1..3``) plus one non-frame raster, loads them as real
``QgsRasterLayer`` objects under an offscreen ``QgsApplication``, stamps
them, and asserts:

  * every frame layer's temporalProperties() is active, in
    FixedTemporalRange mode, with contiguous 1-hour ranges;
  * the non-frame raster is left untouched;
  * the project temporal range spans the whole group;
  * the dock note text is present.

Exits 0 and prints QT-TEMPORAL-OK on success; asserts (nonzero) otherwise.
"""

from __future__ import annotations

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from qgis.core import QgsApplication, QgsProject, QgsRasterLayer  # noqa: E402


def _write_tif(path: str, fill: float) -> None:
    from osgeo import gdal, osr

    ds = gdal.GetDriverByName("GTiff").Create(path, 2, 2, 1, gdal.GDT_Float32)
    ds.SetGeoTransform([0.0, 0.001, 0.0, 0.0, 0.0, -0.001])
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    ds.GetRasterBand(1).Fill(fill)
    ds.FlushCache()


def main() -> int:
    app = QgsApplication([], False)
    app.initQgis()
    try:
        from trid3nt.render.layers import stamp_temporal

        tmp = tempfile.mkdtemp(prefix="trid3nt_temporal_harness_")
        names = [f"Flood_depth_step_{i}" for i in (1, 2, 3)] + ["Peak_flood_depth"]
        layers = []
        for i, name in enumerate(names):
            path = os.path.join(tmp, f"{name}.tif")
            _write_tif(path, float(i))
            layer = QgsRasterLayer(path, name, "gdal")
            assert layer.isValid(), f"harness raster {name} did not load"
            layers.append(layer)

        notes = stamp_temporal(layers)
        print("notes:", notes, flush=True)
        assert any(
            "3-frame sequence 'flood depth' stamped for the Temporal Controller"
            in n
            for n in notes
        ), f"expected stamp note missing: {notes}"

        frames, non_frame = layers[:3], layers[3]
        ranges = []
        for layer in frames:
            props = layer.temporalProperties()
            assert props.isActive(), f"{layer.name()} temporal props not active"
            rng = props.fixedTemporalRange()
            assert rng.begin().isValid() and rng.end().isValid(), (
                f"{layer.name()} has an invalid fixed range"
            )
            ranges.append(rng)
        for prev, nxt in zip(ranges, ranges[1:]):
            assert prev.end() == nxt.begin(), (
                "frame ranges not contiguous: "
                f"{prev.end().toString()} != {nxt.begin().toString()}"
            )
            assert prev.begin().secsTo(prev.end()) == 3600, "range is not 1 hour"

        assert not non_frame.temporalProperties().isActive(), (
            "non-frame raster must stay untouched"
        )

        project_range = QgsProject.instance().timeSettings().temporalRange()
        assert project_range.begin() == ranges[0].begin(), "project range begin"
        assert project_range.end() == ranges[-1].end(), "project range end"

        # Idempotence: a per-case counts dict suppresses the replay re-stamp.
        counts = {"flood depth": 3}
        assert stamp_temporal(layers, counts) == [], "replay was not idempotent"

        print("QT-TEMPORAL-OK", flush=True)
        # Release the Python-owned layers BEFORE exitQgis -- letting the
        # interpreter GC them after QGIS teardown segfaults the process.
        del layer, frames, non_frame, ranges
        layers.clear()
        return 0
    finally:
        app.exitQgis()


if __name__ == "__main__":
    sys.exit(main())
