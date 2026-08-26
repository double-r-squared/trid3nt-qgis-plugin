"""Reading the geometry a CONTEXT SLOT was filled with, whatever form it arrived in.

A producer-less ``Data`` slot declares the SHAPE it accepts and nothing about
where the thing comes from, so exactly one function has to cope with all the ways
one can be satisfied:

  * a LAYER the caller already has - a ``LayerURI`` handle or a bare object-store
    uri, typically the output of a fetcher the user ran first;
  * a SKETCH - the draw gate's reply, or a typed list of vertices;
  * nothing, which is legal on an ``.optional()`` slot and is the caller's answer,
    not an error.

The in-memory shapes normalize through the user-input species
(``workflows/lib/user_input.py``) so a drawn line and a typed line are the same
value by the time anything models them. Only the READ of a stored vector lives
here, because that is I/O and the species is pure.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from trid3nt_server.workflows.lib import user_input

logger = logging.getLogger("trid3nt_server.workflows.shared.supplied_geometry")

__all__ = ["supplied_polylines"]

_URI_SCHEMES = ("s3://", "gs://", "file://", "/")


def _uri_of(value: Any) -> str | None:
    """The object-store uri behind a supplied artifact, or ``None`` if it is data."""
    uri = getattr(value, "uri", None) or (value if isinstance(value, str) else None)
    if not isinstance(uri, str):
        return None
    return uri if uri.startswith(_URI_SCHEMES) else None


def _local_copy(uri: str) -> tuple[str, bool]:
    """A LOCAL path for ``uri``, plus whether it is a temporary copy to unlink.

    An object-store uri is fetched with boto3 rather than handed to GDAL's
    ``/vsis3``: boto3 reads the endpoint the rest of this process reads (a MinIO
    deployment is not AWS), and GDAL's own S3 driver would authenticate against a
    different one and fail with an access-key error that has nothing to do with
    the layer.
    """
    if not uri.startswith(("s3://", "gs://")):
        return uri, False
    import tempfile

    from trid3nt_server.workflows.solver.solver import _get_s3_client

    bucket, _, key = uri.split("://", 1)[1].partition("/")
    suffix = os.path.splitext(key)[1] or ".fgb"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as fh:
        fh.write(_get_s3_client().get_object(Bucket=bucket, Key=key)["Body"].read())
        return fh.name, True


def _read_vector_lines(uri: str, *, code: str) -> list[list[list[float]]]:
    """Every LineString in a stored vector layer, as ``[[lon, lat], ...]`` lists.

    Reprojected to EPSG:4326 when the file says otherwise, because every consumer
    of this species works in lon/lat and a silently-UTM line would model a
    structure on the other side of the world.
    """
    import geopandas as gpd

    path, temporary = _local_copy(uri)
    try:
        frame = gpd.read_file(path)
    finally:
        if temporary:
            os.unlink(path)
    if frame.crs is not None and frame.crs.to_epsg() != 4326:
        frame = frame.to_crs(4326)
    out: list[list[list[float]]] = []
    for geom in frame.geometry:
        if geom is None or geom.is_empty:
            continue
        parts = list(geom.geoms) if geom.geom_type.startswith("Multi") else [geom]
        for part in parts:
            if part.geom_type != "LineString":
                continue
            coords = [[float(x), float(y)] for x, y in part.coords]
            if len(coords) >= 2:
                out.append(coords)
    if not out:
        raise user_input.UserInputError(
            f"the layer supplied at {uri} carries no line geometry, so there is "
            "nothing to model as a structure. Supply a line layer, sketch one, or "
            "omit the slot and the run solves without it.", code=code)
    return out


def supplied_polylines(value: Any, *, label: str = "structure",
                       code: str = "SUPPLIED_GEOMETRY_INVALID"
                       ) -> list[list[list[float]]] | None:
    """The lines a polyline-shaped context slot was filled with; ``None`` if unfilled.

    A stored layer is READ; a sketch or a typed value is NORMALIZED. Both routes
    end in the same list of lon/lat vertex lists, which is the no-double-
    middleware law applied to our own front door: the run cannot tell, and must
    not be able to tell, which way the geometry arrived.
    """
    if value is None:
        return None
    uri = _uri_of(value)
    if uri is not None:
        lines = _read_vector_lines(uri, code=code)
        logger.info("supplied %s: %d line(s) read from %s", label, len(lines), uri)
        return lines
    lines = user_input.polyline_set(value, label=label, code=code)
    if lines:
        logger.info("supplied %s: %d line(s) normalized from a sketched/typed value",
                    label, len(lines))
    return lines
