"""The case-layer raster the two raster derives read and write.

An input is a case layer, by id or whole, carrying the datum and the quantity
its producer stated; a derive never fetches. The output is ONE raster of two bands: the values, and where each cell came from -
each input's place in the order it was laid, FILLED, or UNMEASURED.
"""

from __future__ import annotations

from typing import Any, Mapping

from trid3nt_contracts.execution import LayerURI

from trid3nt_server.inputs.user_input import UserInputError

NODATA = float("nan")
#: The band-2 codes no input's place reaches.
FILLED = 254
UNMEASURED = 255


class RasterLayerError(UserInputError):
    """A raster derive's typed refusal, by its own code."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message, code=error_code)


class RasterLayerURI(LayerURI):
    """A raster derive's output: band 1 the values, band 2 where each cell came
    from, and the share of the cells each source reached - the feedback."""

    #: Band 2's codes by place: ``sources[i]`` painted the cells coded ``i``.
    sources: list[str] = []
    coverage: list[tuple[str, float]] = []
    unmeasured_fraction: float = 0.0


def case_layer(value: Any, role: str) -> Any:
    """One input as the case layer it names - by id, with what its producer said
    about it - or a layer handed over whole; anything else refuses by name.

    A derive never fetches: a source is fetched first, and its layer named here."""
    from trid3nt_server.render.uri_registry import lookup_layer_for_handle

    if isinstance(value, str):
        layer = lookup_layer_for_handle(value)
        if layer is None:
            raise RasterLayerError(
                "RASTER_LAYER_UNKNOWN",
                f"the {role} {value!r} names no layer in this case. Fetch the "
                "source first and name the layer it produced.")
        return layer
    if isinstance(value, Mapping) and "source" in value:
        raise RasterLayerError(
            "RASTER_LAYER_UNKNOWN",
            f"the {role} names the source {value['source']!r}: a derive reads "
            "case layers and never fetches. Fetch it first and name the layer "
            "it produced.")
    return value


def sources_of(layer: Any, won: Any) -> list[str]:
    """The names band 2's codes stand for: what the layer says, else each
    input's place and ``filled``."""
    import numpy as np

    named = (layer.get("sources") if isinstance(layer, Mapping)
             else getattr(layer, "sources", None))
    if named:
        return list(named)
    codes = won[np.asarray(won) < FILLED]
    return ([f"input {code + 1}" for code in range(int(codes.max(initial=0)) + 1)]
            + (["filled"] if (np.asarray(won) == FILLED).any() else []))


def staged(layer: Any, role: str, scratch: str) -> str:
    """One input's file, local and readable, or a refusal naming it."""
    from trid3nt_server.inputs.geometry import source_uri
    from trid3nt_server.tools.derive._hydrology_common import _stage_uri_local

    uri = str(source_uri(layer) or "").strip()
    if not uri:
        raise RasterLayerError(
            "RASTER_LAYER_NO_SOURCE", f"the {role} {layer!r} names no file to read.")
    try:
        # A role is a layer's own name and reaches a FILENAME here.
        return _stage_uri_local(
            uri, scratch, "".join(c if c.isalnum() else "_" for c in role))
    except Exception as exc:  # noqa: BLE001 - every reader fault, named by source
        raise RasterLayerError(
            "RASTER_LAYER_UNREADABLE",
            f"the {role} {uri!r} could not be read ({exc}).") from exc


def label_of(layer: Any, rank: int) -> str:
    """What a derive CALLS one input: the name it carries, else its place."""
    name = (layer.get("name") if isinstance(layer, Mapping)
            else getattr(layer, "name", None))
    return str(name or "").strip() or f"input {rank + 1}"


def share(part: Any, whole: float) -> float:
    """A count as a fraction of what it is over, NEVER rounded to nothing: a
    hole the rounding erases reads as no hole everywhere downstream."""
    if not whole or not float(part):
        return 0.0
    return max(round(float(part) / float(whole), 4), 0.0001)


def percent(fraction: float) -> str:
    """One fraction as the percentage a person reads, honest at the small end."""
    if not fraction:
        return "0.0%"
    return (f"{fraction * 100.0:.1f}%" if fraction * 100.0 >= 0.1
            else "less than 0.1%")


def coverage(won: Any, sources: list[str], cells: Any = None
             ) -> tuple[list[tuple[str, float]], float]:
    """What each source reached over ``cells`` (every cell where ``None``), and
    what nothing reached."""
    import numpy as np

    inside = np.ones(won.shape, dtype=bool) if cells is None else cells
    whole = float(np.count_nonzero(inside))
    reached = [(name, share(np.count_nonzero((won == code) & inside), whole))
               for code, name in _codes(sources)]
    return reached, share(np.count_nonzero((won == UNMEASURED) & inside), whole)


def _codes(sources: list[str]) -> list[tuple[int, str]]:
    """Band 2's codes and the names they stand for."""
    return [(FILLED if name == "filled" else rank, name)
            for rank, name in enumerate(sources)]


def stated(reached: list[tuple[str, float]], blank: float) -> str:
    """The coverage feedback, said once."""
    measured = "; ".join(f"{name} {percent(part)}" for name, part in reached if part)
    return (f"{measured or 'nothing'} reached its cells, and {percent(blank)} "
            "of them nothing reached.")


def read(path: str) -> tuple[Any, Any, Any, Any]:
    """``(values, provenance, crs, transform)`` off one raster; provenance is
    ``None`` on a single-band raster nothing composed."""
    import rasterio

    with rasterio.open(path) as src:
        values = src.read(1, masked=True).filled(NODATA).astype("float32")
        provenance = (src.read(2).astype("uint8") if src.count >= 2 else None)
        return values, provenance, src.crs, src.transform


def written(values: Any, provenance: Any, *, crs: Any, transform: Any,
            prefix: str, seed: str, output_dir: str | None) -> str:
    """The two bands written as one raster -> its uri."""
    import numpy as np

    from trid3nt_server.tools.derive._hydrology_common import write_cog

    return write_cog(np.stack([values.astype("float32"),
                               provenance.astype("float32")]),
                     crs=crs, transform=transform, prefix=prefix, seed=seed,
                     output_dir=output_dir, code="RASTER_LAYER_WRITE_FAILED",
                     nodata=NODATA)


def bbox_4326(crs: Any, transform: Any, width: int, height: int
              ) -> tuple[float, float, float, float]:
    """The lon/lat box a grid spans, so the camera can fly to it."""
    from rasterio.warp import transform_bounds

    west, north = transform.c, transform.f
    return tuple(float(v) for v in transform_bounds(
        crs, "EPSG:4326", west, north + height * transform.e,
        west + width * transform.a, north, densify_pts=21))


def polygons(doc: Any) -> list[dict[str, Any]]:
    """The polygons a geometry document or layer carries, in lon/lat."""
    from trid3nt_server.inputs.geometry import flatten_geometries, read_geometry_doc

    return [g for g in flatten_geometries(read_geometry_doc(doc))
            if str(g.get("type")) in ("Polygon", "MultiPolygon")]


#: The most cells a coverage read builds over a cut; a finer raster is read on
#: a coarser grid for the count, never for its values.
_COVERAGE_CELLS = 4_000_000


def over_the_cut(won: Any, crs: Any, transform: Any, polygon: Any
                 ) -> tuple[Any, Any]:
    """``won`` on a grid reaching over the raster AND the polygon -> ``(won,
    wet)``, ``wet`` ``None`` where the polygon carries none.

    Ground inside the polygon past the raster's own edge is ground nothing
    measured, so it counts as UNMEASURED rather than out of sight."""
    import math

    import numpy as np
    from rasterio.features import geometry_mask
    from rasterio.transform import array_bounds, from_origin
    from rasterio.warp import Resampling, reproject, transform_geom
    from shapely.geometry import shape

    shapes = [transform_geom("EPSG:4326", crs, g) for g in polygons(polygon)]
    if not shapes:
        return won, None
    bounds = [shape(g).bounds for g in shapes]
    west, south, east, north = array_bounds(won.shape[0], won.shape[1], transform)
    west = min([west, *(b[0] for b in bounds)])
    south = min([south, *(b[1] for b in bounds)])
    east = max([east, *(b[2] for b in bounds)])
    north = max([north, *(b[3] for b in bounds)])
    cell = abs(float(transform.a))
    count = (east - west) * (north - south) / (cell * cell)
    if count > _COVERAGE_CELLS:
        cell *= math.sqrt(count / _COVERAGE_CELLS)
    width = max(1, int(math.ceil((east - west) / cell)))
    height = max(1, int(math.ceil((north - south) / cell)))
    grid = from_origin(west, north, cell, cell)
    out = np.full((height, width), UNMEASURED, dtype="uint8")
    reproject(source=won, destination=out, src_transform=transform, src_crs=crs,
              dst_transform=grid, dst_crs=crs, src_nodata=UNMEASURED,
              dst_nodata=UNMEASURED, resampling=Resampling.nearest)
    wet = geometry_mask(shapes, out_shape=out.shape, transform=grid, invert=True)
    return out, wet


__all__ = ["FILLED", "NODATA", "RasterLayerError", "RasterLayerURI",
           "UNMEASURED", "bbox_4326", "coverage", "label_of", "over_the_cut",
           "percent", "polygons", "read", "case_layer", "share", "sources_of",
           "staged", "stated", "written"]
