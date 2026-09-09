"""fema_nfhl_zones: FEMA NFHL regulatory flood-zone polygons through pygeohydro.

The library owns the service. Reading by OBJECT ID is what this row needs: the endpoint
answers a deep page 200-WITH-ERROR, so a cursor walk silently stops early, while an
object-id read cannot lose a feature without saying so."""

# MEASURED on one bbox: the cursor walk returned 6,000 of the 9,894 polygons the
# service itself counts there and called it an answer.
#
# THE OUTPUT FORMAT IS LOAD-BEARING, so it is named here rather than left at the
# client's default. Asked for Esri JSON the client hands the rings to a converter whose
# containment test is a LINE that contains a point, which no interior ring's vertex
# lies on, so every hole comes back as another filled outer ring. MEASURED on a
# 326-polygon bbox: 18 features carry 629 holes, the Esri-JSON read returned zero of
# them, over-stating those features by 13.8 percent and the AOI by 7.8. An X-zone hole
# inside an AE polygon IS the regulatory answer, so it cannot be filled.
#
# THE BATCHES ARE READ ONE AT A TIME, which is the other half of coming back complete.
# The service throttles: MEASURED over the same bbox, batches 1-3 answered and 4-9
# returned HTTP 500 and then reset the connection outright, the ninth answering again
# after a pause. The library issues every batch in ONE gather, so a single throttled
# batch fails the whole read; read one batch per call and each one carries the shared
# shim's own backoff, under a batch size the service can actually deliver. Within a
# batch the library's own retry stays on and names any id it still could not read.
#
# A WIDE AOI IS READ TILE BY TILE, in sequence, with a pause between tiles, because one
# client cannot read a metro AOI in one pass at any batch size measured, and a refusal
# that kills the whole read is the same lost answer as a silent truncation. Every tile
# the service still refuses after its attempts is NAMED, with the polygon count it
# would have carried, in the log, on the run journal and in the run's notes.
#
# THE ZONE VOCABULARY. ``sfha_only`` and ``zone_filter`` are server-side clauses over
# the D_FLD_ZONE domain, and the domain itself is this row's data: an unknown zone code
# is refused by name rather than passed to the service to return nothing.
#
# THE REGULATORY CORE. The service publishes far more columns than a flood-zone
# question needs; the fourteen that carry the regulatory answer are the spec's.
#
# THE COMPLETENESS GUARD. When some object ids fail even after the library's own retry,
# it WARNS and returns what it has. A partial regulatory layer that looks whole is the
# failure this row exists to prevent, so a non-zero miss count is a typed upstream
# error instead.

from __future__ import annotations

import json
import logging
import math
import time
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..._router import hooks as _hooks
from ..._router.errors import RouterUpstreamError, router_input_error, router_upstream_error
from ..._router.hooks.hyriver import hyriver_call

__all__ = ["delegate", "VALID_FLOOD_ZONES"]

logger = logging.getLogger(
    "trid3nt_server.tools.fetchers.hazard.fetch_fema_nfhl_zones"
)

#: The service and the layer, both named the way the library names them.
_SERVICE = "NFHL"
_LAYER = "flood hazard zones"

#: The one format whose ring assembly keeps a zone polygon's holes; the client
#: defaults to Esri JSON, which fills them.
_OUTFORMAT = "geojson"

#: The service ADVERTISES maxRecordCount 2000 and cannot deliver it: MEASURED over a
#: Tampa Bay bbox, a 2,000-id read of these zone polygons answers HTTP 500 (both
#: formats, repeatably, direct as well as through the library) while the same ids at
#: 500 answer in ~11 s. The advertised number is the cap the request must stay under.
_MAX_IDS_PER_REQUEST = 500

#: The AOI is cut into sub-bboxes this wide, read in sequence this far apart. Both
#: numbers are the paced read the service was MEASURED to serve: 1,000 of these
#: polygons per request, 10 s apart, delivered 5,894 of the 9,894 the Tampa Bay
#: 0.4 x 0.5 deg AOI holds, while a 2,000-id request answers HTTP 500 repeatably
#: and the same AOI in one pass answers nothing at all. At the density that
#: measurement was taken at - 9,894 polygons over 0.2 deg^2, ~495 per 0.01 deg^2 -
#: a 0.1 deg tile is one servable read, so a tile is a request the service can
#: answer rather than a share of one it cannot.
_TILE_DEG = 0.1
_TILE_PAUSE_S = 10.0

#: Properties preserved from each NFHL feature (the regulatory-flood semantic core).
_PRESERVED_PROPERTIES: tuple[str, ...] = (
    "FLD_ZONE", "ZONE_SUBTY", "SFHA_TF", "STATIC_BFE", "V_DATUM", "DEPTH",
    "LEN_UNIT", "VELOCITY", "VEL_UNIT", "DFIRM_ID", "FLD_AR_ID", "STUDY_TYP",
    "SOURCE_CIT", "GFID",
)

#: Canonical FEMA flood-zone designations accepted by zone_filter (D_FLD_ZONE domain).
VALID_FLOOD_ZONES: frozenset[str] = frozenset({
    "A", "AE", "AH", "AO", "AR", "A99", "V", "VE", "X", "D", "B", "C",
    "AREA NOT INCLUDED", "OPEN WATER",
})


def _validate_zone_filter(sc: str, raw: Any) -> list[str] | None:
    """Uppercase + validate zone_filter against VALID_FLOOD_ZONES; raise on unknown."""
    if not raw:
        return None
    if not isinstance(raw, (list, tuple)):
        raise router_input_error(
            sc, f"zone_filter must be a list[str] or None; got {type(raw).__name__}",
            "INPUT_INVALID",
        )
    out: list[str] = []
    for z in raw:
        if not isinstance(z, str):
            raise router_input_error(
                sc, f"zone_filter entries must be str; got {type(z).__name__}",
                "INPUT_INVALID",
            )
        zu = z.upper()
        if zu not in VALID_FLOOD_ZONES:
            raise router_input_error(
                sc, f"zone_filter entry {z!r} not in known NFHL zone codes "
                    f"{sorted(VALID_FLOOD_ZONES)}",
                "INPUT_INVALID",
            )
        if zu not in out:
            out.append(zu)
    return sorted(out) or None


def _sql_clause(sc: str, params: dict[str, Any]) -> str:
    """The server-side selection: [SFHA_TF='T'] [AND FLD_ZONE IN (...)]."""
    parts: list[str] = []
    if not isinstance(params.get("sfha_only", False), bool):
        raise router_input_error(sc, "sfha_only must be bool", "INPUT_INVALID")
    if params.get("sfha_only"):
        parts.append("SFHA_TF='T'")
    zones = _validate_zone_filter(sc, params.get("zone_filter"))
    if zones:
        parts.append("FLD_ZONE IN (" + ",".join(f"'{z}'" for z in zones) + ")")
    return " AND ".join(parts)


def _tiles(bbox: tuple[float, ...]) -> list[tuple[float, float, float, float]]:
    """The AOI cut into equal sub-bboxes no wider than one servable read."""
    x0, y0, x1, y1 = bbox
    #: The epsilon keeps a bbox that IS one tile wide from splitting on the
    #: rounding of its own span: 0.1 deg of longitude subtracts to 0.09999999999.
    nx = max(1, math.ceil((x1 - x0) / _TILE_DEG - 1e-9))
    ny = max(1, math.ceil((y1 - y0) / _TILE_DEG - 1e-9))
    dx, dy = (x1 - x0) / nx, (y1 - y0) / ny
    return [(x0 + i * dx, y0 + j * dy, x0 + (i + 1) * dx, y0 + (j + 1) * dy)
            for j in range(ny) for i in range(nx)]


def _tile_oids(spec: SourceSpec, nfhl: Any, tile: tuple[float, ...],
               clause: str) -> list[tuple[str, ...]]:
    """The tile's object ids in servable batches; a tile holding no zones is empty."""
    from pygeoogc.exceptions import ZeroMatchedError

    def read() -> list[tuple[str, ...]]:
        try:
            return list(nfhl.client.oids_bygeom(tile, geo_crs=4326, sql_clause=clause or None))
        except ZeroMatchedError as exc:
            #: The library raises the same class for an empty tile and for a service
            #: error body; only its own no-match text means the tile is empty.
            if "No matched records" in str(exc):
                return []
            raise

    return hyriver_call(
        spec, f"pygeohydro.NFHL.oids_bygeom(bbox={tile}, sql_clause={clause!r})", read)


def _report(sc: str, tiles: list[tuple[float, ...]],
            lost: dict[tuple[float, ...], int | None], kept: int) -> None:
    """Name every refused tile and what it would have carried. Never silent."""
    if not lost:
        return
    detail = "; ".join(
        "[{:.4f},{:.4f},{:.4f},{:.4f}] {}".format(
            *t, f"{n} zone polygons" if n is not None else "polygon count unread")
        for t, n in lost.items())
    counted = sum(n for n in lost.values() if n is not None)
    text = (f"FEMA NFHL: {len(lost)} of {len(tiles)} tiles were refused after their "
            f"attempts, so {kept} zone polygons were read and at least {counted} were "
            f"not - {detail}")
    logger.warning("%s", text)
    if not kept:
        raise router_upstream_error(sc, text)
    from trid3nt_server.workflows.runtime.journal import journal_note

    journal_note(text)


@_hooks.register_hook("fema_nfhl_zones.delegate")
def delegate(
    spec: SourceSpec, params: dict[str, Any], *, timeout_s: float
) -> list[dict[str, Any]]:
    """Read the bbox tile by tile, merged by object id, projected to the core."""
    from pygeohydro import NFHL

    sc = spec.error_code_prefix
    bbox = tuple(float(v) for v in params["bbox"])
    clause = _sql_clause(sc, params)

    nfhl = hyriver_call(spec, f"pygeohydro.NFHL({_SERVICE!r}, {_LAYER!r})", NFHL, _SERVICE, _LAYER)
    client = nfhl.client.client
    client.outformat = _OUTFORMAT
    client.max_nrecords = _MAX_IDS_PER_REQUEST

    tiles = _tiles(bbox)
    out: list[dict[str, Any]] = []
    seen: set[Any] = set()
    lost: dict[tuple[float, ...], int | None] = {}
    missing = 0
    for t, tile in enumerate(tiles):
        if t:
            time.sleep(_TILE_PAUSE_S)
        where = f"tile {t + 1}/{len(tiles)} {tile}"
        try:
            batches = _tile_oids(spec, nfhl, tile, clause)
        except RouterUpstreamError as exc:
            lost[tile] = None
            logger.warning("fetch_fema_nfhl_zones: %s could not be listed: %s", where, exc)
            continue
        for i, batch in enumerate(batches, 1):
            client.n_missing = 0
            try:
                pages = hyriver_call(
                    spec,
                    f"pygeohydro.NFHL.get_features({where}, "
                    f"batch {i}/{len(batches)}, {len(batch)} ids)",
                    nfhl.client.get_features,
                    #: A LIST, not an iterator: a retry re-invokes with the same
                    #: argument, and a consumed iterator would send an empty request.
                    [batch],
                )
            except RouterUpstreamError as exc:
                lost[tile] = (lost.get(tile) or 0) + len(batch)
                logger.warning("fetch_fema_nfhl_zones: %s batch %d/%d refused after its "
                               "attempts, %d zone polygons not read: %s",
                               where, i, len(batches), len(batch), exc)
                continue
            missing += int(getattr(client, "n_missing", 0) or 0)
            for page in pages:
                for rec in page.get("features") or ():
                    geom = rec.get("geometry")
                    if geom is None:
                        continue
                    props = rec.get("properties") or {}
                    #: Tiles overlap at their edges by construction - a polygon that
                    #: straddles one intersects both - so the object id the read was
                    #: keyed on is what makes the merge a union rather than a double.
                    oid = props.get("OBJECTID")
                    if oid is not None:
                        if oid in seen:
                            continue
                        seen.add(oid)
                    row: dict[str, Any] = {}
                    for key in _PRESERVED_PROPERTIES:
                        v = props.get(key)
                        if isinstance(v, (dict, list)):
                            v = json.dumps(v)
                        row[key] = v
                    out.append({"type": "Feature", "geometry": geom, "properties": row})

    _report(sc, tiles, lost, len(out))
    if missing:
        raise router_upstream_error(
            sc,
            f"FEMA NFHL returned {len(out)} zone polygons but {missing} object id(s) "
            "the service listed could not be read, so the layer would be a partial "
            "regulatory answer; retry rather than publish it.",
        )
    return out
