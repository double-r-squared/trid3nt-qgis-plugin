"""TRID3NT mesh worker (oceanmesh coastal_tin generator, GPL-3 isolated).

See Dockerfile: this package runs ONLY inside the mesh worker image. It holds no
server/trid3nt imports; the server-side agent/mesh/coastal_tin.py composes the
manifest + dispatches this worker + reads the mesh geojson back through the
shared mesh_preview component.
"""
