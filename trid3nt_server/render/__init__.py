"""Render: what a product reaches the map in, and nothing else.

The format set is exactly what QGIS opens: a raster as a COG, a vector as
GeoJSON (or GPKG, or FlatGeobuf), a mesh in any MDAL format - SELAFIN, 2dm,
UGRID, HEC-RAS 2D HDF - with the dataset files written beside it, and a series
or a profile as a chart payload. Each arrives with the style row its producer
declared, and publication is automatic: a product is published as it is made,
and a deliverability guard rather than the producing tool decides what reaches
the client.
"""
