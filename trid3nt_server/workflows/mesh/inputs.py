"""The ONE typed conversion a data-valued op kwarg passes through.

An op takes the data CLASS it is defined over, and a recipe names that data as
whatever the chain produced - a DATA row's layer handle, an object-store uri, a
path, inline GeoJSON. Two conversions and no more, both explicit:

    raster -> the readable raster the op reads at the nodes
    layer  -> the geometry document the op reads shapes out of

Which one applies is read off the artifact's CLASS, which is the repo's one
answer to "what kind of thing is this uri" (``runtime.data.artifact_class``, by
suffix). A value whose class is not knowable is NOT guessed at: it passes
through as it was written, and the op refuses it in its own words if it cannot
use it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

from trid3nt_server.workflows.mesh.meshers import MeshToolError

__all__ = ["op_geometry", "op_input", "op_raster"]


def op_input(value: Any) -> Any:
    """One op kwarg as the concrete thing the op is defined over."""
    from trid3nt_server.workflows.runtime.data import artifact_class

    if isinstance(value, Mapping) and "type" in value:
        return dict(value)
    kind = artifact_class(value)
    if kind == "raster":
        return op_raster(value)
    if kind == "vector":
        return op_geometry(value)
    return value


def op_raster(source: Any) -> Path:
    """A raster source -> a LOCAL readable raster, whatever it arrived as.

    Local because a raster op reads it through the grid's own georeferencing -
    rasterio opens a path, not a handle - and the object store is not a file
    system.
    """
    from trid3nt_server.tools.cache import read_object_bytes_s3
    from trid3nt_server.tools.processing._geometry_common import source_uri

    uri = str(source_uri(source) or "").strip()
    if not uri:
        raise MeshToolError(
            "MESH_OP_INPUT_UNREADABLE",
            f"the raster {source!r} names no readable address.")
    if uri.startswith("s3://"):
        from trid3nt_contracts import new_ulid

        local = _scratch() / f"raster-{new_ulid()}{Path(uri).suffix or '.tif'}"
        local.write_bytes(read_object_bytes_s3(uri))
        return local
    path = Path(uri)
    if not path.exists():
        raise MeshToolError(
            "MESH_OP_INPUT_UNREADABLE",
            f"the raster {uri!r} is neither an object-store uri nor a file on "
            "disk.")
    return path


def op_geometry(source: Any) -> dict[str, Any]:
    """A geometry source -> GeoJSON, whatever vector format it arrived in.

    A source is an address the recipe records and can re-read, so a drawn
    polygon, a fetched water layer and a file on disk all enter the same way. A
    LAYER a chain produced enters the same way too: refusing the object while
    accepting the ``.uri`` it carries would make a chain depend on the author
    remembering to write it.
    """
    from trid3nt_server.tools.cache import read_object_bytes_s3
    from trid3nt_server.tools.processing._geometry_common import source_uri

    resolved = source_uri(source)
    if isinstance(resolved, Mapping):
        return dict(resolved)
    text = str(resolved).strip()
    if text.startswith("{"):
        return json.loads(text)
    if text.startswith("s3://"):
        from trid3nt_contracts import new_ulid

        raw = read_object_bytes_s3(text)
        suffix = Path(text).suffix.lower()
        if suffix in (".geojson", ".json"):
            return json.loads(raw.decode("utf-8"))
        local = _scratch() / f"geom-{new_ulid()}{suffix}"
        local.write_bytes(raw)
        text = str(local)
    path = Path(text)
    if not path.exists():
        raise MeshToolError(
            "MESH_OP_INPUT_UNREADABLE",
            f"the geometry {source!r} could not be read: it is neither inline "
            "GeoJSON, an object-store uri, nor a file on disk.")
    if path.suffix.lower() in (".geojson", ".json"):
        return json.loads(path.read_text())
    import geopandas as gpd

    return json.loads(gpd.read_file(path).to_crs(4326).to_json())


def _scratch() -> Path:
    return Path(os.environ.get("TRID3NT_RUNS_DIR", "/tmp"))
