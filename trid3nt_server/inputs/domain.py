"""THE DOMAIN: the closed polygon the equations are solved over.

One slot, geometry only. A polygon drawn on the canvas, the user's own layer, a
fetched waterbody and a reach section between two ends all enter through
``domain`` and read the same after, so nothing downstream branches on which way
it was filled. A body of water that exists in no fetcher is filled the same way.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping

from .boundary import RUN_TYPES, WALL, BoundaryRun, boundary_runs
from .geometry import flatten_geometries, read_geometry_doc
from .user_input import UserInputError, polygon_ring

__all__ = ["Domain", "domain", "domain_ring"]

logger = logging.getLogger("trid3nt_server.inputs.domain")

_CODE = "DOMAIN_INVALID"
_LAYER_SCHEMES = ("s3://", "gs://", "file://", "/", "./")


@dataclass(frozen=True, slots=True)
class Domain:
    """One closed polygon in EPSG:4326, the name it was given, and where it came
    from as an ADDRESS - never as a branch: a consumer reads the geometry.

    ``geometry`` is GeoJSON Polygon or MultiPolygon."""

    geometry: dict[str, Any]
    name: str | None = None
    uri: str | None = None
    #: The stretches of this domain's edge its PRODUCER measured, where one did.
    #: A reach fetcher returns the section and the two faces it was cut between
    #: as one artifact, and a run the user draws lands the same way.
    runs: tuple[BoundaryRun, ...] = ()

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        """The lon/lat box this polygon spans."""
        xs, ys = [], []
        for ring in _rings(self.geometry):
            xs += [float(v[0]) for v in ring]
            ys += [float(v[1]) for v in ring]
        return (min(xs), min(ys), max(xs), max(ys))

    def as_feature_collection(self) -> dict[str, Any]:
        """This domain as the one collection every geometry reader opens."""
        return {"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {"role": "domain",
                                               "name": self.name},
             "geometry": dict(self.geometry)}]}


def domain(value: Any, *, label: str = "domain",
           code: str = _CODE) -> Domain | None:
    """THE ingestion: a drawing, a layer, a fetched polygon, a ring -> Domain.

    ``None`` only when nothing came; anything that is not a closed polygon
    refuses typed rather than being squared off into one."""
    if value is None or isinstance(value, Domain):
        return value
    uri = getattr(value, "uri", None) or (value if isinstance(value, str) else None)
    if isinstance(uri, str) and uri.strip().startswith(_LAYER_SCHEMES):
        doc = read_geometry_doc(uri.strip())
        logger.info("%s: read from %s", label, uri)
        return Domain(_one_polygon(doc, label, code), _named(value), uri.strip(),
                      _runs_of(value, label, code) or _prescribed(doc, label, code))
    if isinstance(value, Mapping):
        return Domain(_one_polygon(value, label, code), _named(value),
                      _uri_of(value), _runs_of(value, label, code)
                      or _prescribed(value, label, code))
    ring = polygon_ring(value, label=label, code=code)
    return Domain(_closed(ring or []))


def domain_ring(dom: Domain) -> list[list[float]]:
    """The OUTER ring of the domain, open - what a mesher's extent reads."""
    rings = _rings(dom.geometry)
    return [[float(v[0]), float(v[1])] for v in rings[0][:-1]]


def _rings(geometry: Mapping[str, Any]) -> list[list[Any]]:
    kind = str(geometry.get("type") or "")
    coords = geometry.get("coordinates") or []
    if kind == "Polygon":
        return [list(ring) for ring in coords]
    if kind == "MultiPolygon":
        return [list(ring) for polygon in coords for ring in polygon]
    return []


def _one_polygon(doc: Any, label: str, code: str) -> dict[str, Any]:
    """The polygon a document carries; several are the one they cover together."""
    polygons = [g for g in flatten_geometries(read_geometry_doc(doc))
                if str(g.get("type")) in ("Polygon", "MultiPolygon")]
    if not polygons:
        raise UserInputError(
            f"the {label} carries no polygon geometry. A domain is the CLOSED "
            "outline the equations are solved over: draw it, name a polygon "
            "layer, or let the template's own producer find one.", code=code)
    if len(polygons) == 1:
        return dict(polygons[0])
    # SEVERAL polygons are one domain with parts - a lake with islands cut out,
    # a two-basin harbour - and the mesher meshes the union. Picking the largest
    # would silently drop the rest.
    return {"type": "MultiPolygon",
            "coordinates": [g["coordinates"] if str(g["type"]) == "Polygon"
                            else part
                            for g in polygons
                            for part in ([g["coordinates"]]
                                         if str(g["type"]) == "Polygon"
                                         else g["coordinates"])]}


def _closed(ring: list[list[float]]) -> dict[str, Any]:
    """A typed or drawn OPEN ring as a closed GeoJSON polygon.

    How few vertices a ring may have is the ingestion's own rule, stated where
    every drawn shape passes it."""
    return {"type": "Polygon", "coordinates": [[*ring, list(ring[0])]]}


def _runs_of(value: Any, label: str, code: str) -> tuple[BoundaryRun, ...]:
    """The boundary runs a producer stated beside its polygon, or none.

    A producer that measured the edge says so ON the artifact; a drawn outline
    says nothing, and nothing is an answer."""
    stated = (value.get("runs") if isinstance(value, Mapping)
              else getattr(value, "runs", None))
    return boundary_runs(stated, label=f"{label} runs", code=code)


def _prescribed(doc: Any, label: str, code: str) -> tuple[BoundaryRun, ...]:
    """The run rows a producer wrote INTO the artifact beside its polygon.

    A fetcher that cut a polygon between two faces returns those faces as rows
    of the same document, each row naming which stretch it is; a row naming a
    stretch that prescribes nothing is the wall the edge already is, and is not
    a run. The row's own word for which row it is - ``part`` on a multi-part
    artifact - is what names it, because a slot reads the producer's vocabulary
    rather than asking the producer to speak its own."""
    if not isinstance(doc, Mapping) or doc.get("type") != "FeatureCollection":
        return ()
    rows = []
    for feature in (doc.get("features") or ()):
        properties = dict((feature or {}).get("properties") or {})
        kind = str(properties.get("type") or properties.get("part") or "").strip()
        geometry = (feature or {}).get("geometry") or {}
        if kind in RUN_TYPES and kind != WALL \
                and str(geometry.get("type")) == "LineString":
            rows.append({**feature, "properties": {**properties, "type": kind}})
    return boundary_runs(rows, label=f"{label} runs", code=code) if rows else ()


def _named(value: Any) -> str | None:
    name = value.get("name") if isinstance(value, Mapping) else getattr(
        value, "name", None)
    text = str(name).strip() if name is not None else ""
    return text or None


def _uri_of(value: Mapping[str, Any]) -> str | None:
    uri = value.get("uri")
    return str(uri) if uri else None
