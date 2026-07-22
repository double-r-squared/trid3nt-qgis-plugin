# 0005 - QGIS reads COGs natively; no tile server

Context: rasters were rendered by TiTiler serving PNG tiles to the client.
Decision: the plugin is the only client and QGIS/GDAL read COGs directly, so
publish emits the raw s3:// COG URI and the plugin loads it via /vsicurl/
(same open-MinIO HTTP path vectors already used), applying its own renderer
from the envelope legend (colormap ramp table; embedded color table for
categorical). Legacy tile-template URIs in old cases are unwrapped for
back-compat.
Consequence: TiTiler removed from the stack (MinIO + agent only); users get
real data rasters - native identify, reprojection, own symbology.
