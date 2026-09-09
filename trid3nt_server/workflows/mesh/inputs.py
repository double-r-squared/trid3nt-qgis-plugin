"""The ONE typed conversion a data-valued op kwarg passes through.

An op takes the data CLASS it is defined over; a recipe names that data as
whatever address the chain produced."""

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
    # Two conversions and no more, and which one applies is read off the
    # artifact's CLASS, never guessed:
    #     raster -> the readable raster the op reads at the nodes
    #     vector -> the geometry document the op reads shapes out of
    # A value whose class is not knowable passes through as it was written and
    # the op refuses it in its own words if it cannot use it.
    kind = artifact_class(value)
    if kind == "raster":
        return op_raster(value)
    if kind == "vector":
        return op_geometry(value)
    return value


def op_raster(source: Any) -> Path:
    """A raster source -> a LOCAL readable path, whatever it arrived as."""
    from trid3nt_server.tools.cache import read_object_bytes_s3
    from trid3nt_server.workflows.shared.geometry import source_uri

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

    A source is inline GeoJSON, an object-store uri, a path, or a layer handle."""
    from trid3nt_server.tools.cache import read_object_bytes_s3
    from trid3nt_server.workflows.shared.geometry import source_uri

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
