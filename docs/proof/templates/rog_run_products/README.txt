telemac_rain_on_grid run products (Coweeta showcase run 01KZHVJDBK9EVE07GVDB0E3115)
- coweeta_depth_max.tif      max water depth COG (EPSG-georeferenced raster;
                             the same layer the showcase Case loads)
                             QGIS: Layer > Add Raster Layer
- coweeta_max_fields.slf     max depth + max velocity on the mesh (SELAFIN)
                             QGIS: Layer > Add Mesh Layer
- coweeta_full_results.slf   the FULL time-stepping results - every output
                             frame, all variables; QGIS reads it natively via
                             MDAL: Add Mesh Layer, then use the temporal
                             controller to scrub the flood animation
