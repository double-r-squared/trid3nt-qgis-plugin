# grace2-qgis — headless QGIS Processing worker + QGIS Server base.
# job-0308 / sprint-16 / SRS M12 + Decision Q (QGIS Processing as the primary
# agentic compute substrate). Built on the EC2 agent box:
#   docker build -t grace2-qgis:ltr services/workers/qgis
#
# Provides `qgis_process` (695 algorithms incl. the GRASS hydrology suite —
# r.watershed / r.water.outlet / r.stream.extract / r.fill.dir for HEC-HMS
# basin prep) and the QGIS install used for QGIS Server (WMS) render.
# SAGA NextGen / TauDEM are follow-up plugins (GRASS already covers watershed).
FROM qgis/qgis:ltr

# GRASS 8.x + SAGA 9.x binaries (the Processing providers shell out to these).
RUN apt-get update \
 && apt-get install -y --no-install-recommends grass saga \
 && rm -rf /var/lib/apt/lists/*

ENV QT_QPA_PLATFORM=offscreen

# Activate the bundled GRASS Processing provider so qgis_process exposes the
# grass:* algorithms. grassprovider ships in qgis/qgis:ltr but is NOT loaded by
# default — without this the catalog is 388 (native+gdal+qgis) algorithms; with
# it, 695 including the full GRASS hydrology suite. The enable persists in the
# default profile baked into this image layer.
RUN qgis_process plugins enable grassprovider || true
