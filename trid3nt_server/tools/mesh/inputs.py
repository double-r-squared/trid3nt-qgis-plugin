"""The ONE typed conversion a data-valued op kwarg passes through.

An op takes the data CLASS it is defined over; a recipe names that data as
whatever address the chain produced."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any, Iterable, Mapping

from trid3nt_server.tools.mesh.meshers import MeshToolError

__all__ = ["finest_edge", "op_geometry", "op_input", "op_raster",
           "raster_cell_m"]

#: Metres per degree of latitude, for a geographic raster's cell.
_M_PER_DEG = 111_320.0


def finest_edge(stated: Any, ops: Iterable[Any],
                bbox: tuple[float, float, float, float]) -> tuple[float, str | None]:
    """The mesh's finest edge -> ``(edge_m, the sentence the mesh states about it)``.

    A stated edge stands whatever the data under it, and the bed says so where it is coarser; unstated, it is the finest cell
    of the rasters the recipe names, because the mesh library requires one and nothing finer is measured."""
    if stated not in (None, ""):
        return float(stated), None
    finest = min((cell for op in ops for cell in _cells(op, bbox)), default=None)
    if finest is None:
        raise MeshToolError(
            "MESH_EDGE_UNSTATED",
            "no resolution_m was stated and no op names a raster whose cell "
            "could set it, and the mesher needs a finest edge. State "
            "resolution_m, or name the bed with set_bed.")
    return finest[0], (f"no edge was stated, so the finest edge is "
                       f"{finest[0]:g} m, the cell of {finest[1]}")


def _cells(op: Any, bbox: tuple[float, float, float, float]
           ) -> Iterable[tuple[float, str]]:
    """The cell, in metres, of every raster one recipe op names."""
    from trid3nt_server.tools.mesh.shared.primitives import bed_raster_of
    from trid3nt_server.workflows.runtime.data import artifact_class

    for name, value in dict(op.kwargs).items():
        if op.fn == "set_bed" and name == "source":
            path = bed_raster_of(value, bbox)
        elif artifact_class(value) == "raster":
            path = op_raster(value)
        else:
            continue
        if path is not None:
            yield raster_cell_m(path), f"{op.fn} {name}"


def raster_cell_m(path: Path) -> float:
    """A raster's finer cell side, in metres."""
    import rasterio

    with rasterio.open(str(path)) as src:
        dx, dy = (abs(float(v)) for v in src.res)
        if src.crs is not None and src.crs.is_geographic:
            lat = math.radians(0.5 * (src.bounds.bottom + src.bounds.top))
            dx, dy = dx * _M_PER_DEG * math.cos(lat), dy * _M_PER_DEG
    return round(min(dx, dy), 3)


def op_input(value: Any) -> Any:
    """One op kwarg as the concrete thing the op is defined over."""
    from trid3nt_server.workflows.runtime.data import artifact_class

    if isinstance(value, Mapping) and "type" in value:
        return dict(value)
    # Two conversions, chosen by the artifact's CLASS and never guessed: raster -> readable raster, vector -> geometry document.
    # A value whose class is not knowable passes through and the op refuses it in its own words.
    kind = artifact_class(value)
    if kind == "raster":
        return op_raster(value)
    if kind == "vector":
        return op_geometry(value)
    return value


def op_raster(source: Any) -> Path:
    """A raster source -> a LOCAL readable path, whatever it arrived as."""
    from trid3nt_server.tools.cache import read_object_bytes_s3
    from trid3nt_server.inputs.geometry import source_uri

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

    A source is a typed SLOT value, inline GeoJSON, an object-store uri, a path,
    or a layer handle."""
    from trid3nt_server.tools.cache import read_object_bytes_s3
    from trid3nt_server.inputs.geometry import source_uri

    # A slot's typed value carries its own collection: reading it back off an
    # address would re-open what is already in hand, and a drawn domain has no
    # address at all.
    collection = getattr(source, "as_feature_collection", None)
    if callable(collection):
        return collection()
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
