"""Library-route bodies in miniature, shared by the parsing test and the Qt
harness so both halves read ONE fixture of the wire shape."""

#: ``GET /api/library``: two subsystems, one data class with one kind.
LIBRARY_BODY = {
    "generated_at": "2026-09-21T00:00:00Z",
    "subsystems": [
        {"name": "fetchers", "tools": [
            {"name": "fetch_dem", "description": "Fetch elevation for a bbox.",
             "facts": {"source_class": "elevation", "cacheable": True}},
            {"name": "fetch_landcover", "description": "Fetch land cover.",
             "facts": {"source_class": "landcover"}},
        ]},
        {"name": "derives", "tools": [
            {"name": "compute_slope", "description": "Slope from elevation.",
             "facts": {"tier": "general"}},
        ]},
    ],
    "classes": [
        {"name": "elevation", "kinds": [
            {"name": "raster", "rows": [
                {"name": "3dep", "fetcher": "fetch_dem",
                 "description": "USGS 3DEP elevation.",
                 "facts": {"resolution_m": 10, "extent": "conus"}},
                {"name": "copernicus_dem", "fetcher": "fetch_dem",
                 "description": "Copernicus global DEM.",
                 "facts": {"resolution_m": 30}},
            ]},
        ]},
    ],
}

#: ``GET /api/library/search``: hits across both halves, ranked by the server.
SEARCH_BODY = {
    "query": "where is the water",
    "hits": [
        {"name": "fetch_dem", "kind": "tool", "group": "fetchers",
         "description": "Fetch elevation for a bbox.", "score": 8.4},
        {"name": "3dep", "kind": "row", "group": "elevation / raster",
         "fetcher": "fetch_dem", "description": "USGS 3DEP elevation.",
         "score": 5.1},
    ],
}
