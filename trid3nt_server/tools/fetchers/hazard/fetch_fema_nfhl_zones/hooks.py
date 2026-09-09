"""fema_nfhl_zones: FEMA NFHL regulatory flood-zone polygons through pygeohydro.

``pygeohydro.NFHL`` owns the service - the layer lookup by name, the object-id
selection, the request. It also owns the thing this row could not do for itself: the
NFHL endpoint answers a deep page 200-WITH-ERROR, so a cursor walk over it silently
stops early, while an object-id read cannot lose a feature without saying so.
MEASURED on one Tampa Bay bbox: the cursor walk returned 6,000 of the 9,894 polygons
the service itself counts there and called it an answer.

THE OUTPUT FORMAT IS LOAD-BEARING, so it is named here rather than left at the
client's default. Asked for Esri JSON the client hands the rings to a converter whose
containment test is a LINE that contains a point, which no interior ring's vertex
lies on - so every hole comes back as another filled outer ring. MEASURED on a
326-polygon bbox: 18 features carry 629 holes, the Esri-JSON read returned zero of
them, over-stating those features by 13.8 percent and the AOI by 7.8. An X-zone hole
inside an AE polygon IS the regulatory answer, so it cannot be filled.

THE BATCHES ARE READ ONE AT A TIME, which is the other half of coming back complete.
The service throttles: MEASURED over the same bbox, batches 1-3 answered and 4-9
returned HTTP 500 and then reset the connection outright, the ninth answering again
after a pause. The library issues every batch in ONE gather, so a single throttled
batch fails the whole read; read one batch per call and each one carries the shared
shim's own backoff, under a batch size the service can actually deliver. Within a
batch the library's own retry stays on and names any id it still could not read.

Three more things ride here.

THE ZONE VOCABULARY. ``sfha_only`` and ``zone_filter`` are server-side clauses over
the D_FLD_ZONE domain, and the domain itself is this row's data: an unknown zone code
is refused by name rather than passed to the service to return nothing.

THE REGULATORY CORE. The service publishes far more columns than a flood-zone
question needs; the fourteen that carry the regulatory answer are the spec's.

THE COMPLETENESS GUARD. When some object ids fail even after the library's own
retry, it WARNS and returns what it has. A partial regulatory layer that looks whole
is the failure this row exists to have stopped, so a non-zero miss count is a typed
upstream error instead.
"""

from __future__ import annotations

import json
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..._router import hooks as _hooks
from ..._router.errors import router_input_error, router_upstream_error
from ..._router.hooks.hyriver import hyriver_call

__all__ = ["delegate", "VALID_FLOOD_ZONES"]

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


@_hooks.register_hook("fema_nfhl_zones.delegate")
def delegate(
    spec: SourceSpec, params: dict[str, Any], *, timeout_s: float
) -> list[dict[str, Any]]:
    """Read the flood-hazard zones in the bbox, projected to the regulatory core."""
    from pygeohydro import NFHL

    sc = spec.error_code_prefix
    bbox = tuple(float(v) for v in params["bbox"])
    clause = _sql_clause(sc, params)

    nfhl = hyriver_call(spec, f"pygeohydro.NFHL({_SERVICE!r}, {_LAYER!r})", NFHL, _SERVICE, _LAYER)
    client = nfhl.client.client
    client.outformat = _OUTFORMAT
    client.max_nrecords = _MAX_IDS_PER_REQUEST

    batches = list(
        hyriver_call(
            spec,
            f"pygeohydro.NFHL.oids_bygeom(bbox={bbox}, sql_clause={clause!r})",
            nfhl.client.oids_bygeom,
            bbox,
            geo_crs=4326,
            sql_clause=clause or None,
        )
    )

    out: list[dict[str, Any]] = []
    missing = 0
    for i, batch in enumerate(batches, 1):
        client.n_missing = 0
        pages = hyriver_call(
            spec,
            f"pygeohydro.NFHL.get_features(batch {i}/{len(batches)}, {len(batch)} ids)",
            nfhl.client.get_features,
            #: A LIST, not an iterator: a retry re-invokes with the same argument,
            #: and a consumed iterator would send an empty request instead.
            [batch],
        )
        missing += int(getattr(client, "n_missing", 0) or 0)
        for page in pages:
            for rec in page.get("features") or ():
                geom = rec.get("geometry")
                if geom is None:
                    continue
                props = rec.get("properties") or {}
                row: dict[str, Any] = {}
                for key in _PRESERVED_PROPERTIES:
                    v = props.get(key)
                    if isinstance(v, (dict, list)):
                        v = json.dumps(v)
                    row[key] = v
                out.append({"type": "Feature", "geometry": geom, "properties": row})

    if missing:
        raise router_upstream_error(
            sc,
            f"FEMA NFHL returned {len(out)} zone polygons but {missing} object id(s) "
            "the service listed could not be read, so the layer would be a partial "
            "regulatory answer; retry rather than publish it.",
        )
    return out
