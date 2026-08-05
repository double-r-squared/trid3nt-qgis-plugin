"""Municipal storm-drain GIS network -> SWMM ``.inp`` deck builder (dual-drainage
MINOR system - the real piped sewer network, the practice-verification's #1 gap).

Where :mod:`raster_cell_mesh` SYNTHESIZES a quasi-2D overland MAJOR-system mesh
from a DEM, this module imports the REAL piped MINOR system from municipal GIS
layers (nodes as point features, conduits as line features) and authors a runnable
SWMM ``.inp`` from them: JUNCTIONS + OUTFALLS from the node layer, CONDUITS (with a
CIRCULAR cross-section) from the line layer, network topology from explicit
from/to attributes OR endpoint-snapping when the GIS carries no topology (the
common real case), and a design-storm loading so the imported network actually
routes flow to its outfall.

Honest v1 scope + the labeled-degrade doctrine (ADR 0106):
  - Node/conduit ATTRIBUTES are read with a flexible, alias-aware field resolver
    (``invert_elev`` / ``InvertElev`` / ``IE`` / ``Geom1`` / ``DIAMETER`` ...),
    because no two municipal schemas agree on field names.
  - MISSING INVERTS are not fabricated silently: a node with no readable invert
    is DEM-interpolated when a DEM is supplied, else assigned a slope-consistent
    invert walked from the network's known inverts, and the count is LABELED in
    the build provenance (never a hidden default).
  - MISSING DIAMETERS fall back to a labeled demo default; MISSING TOPOLOGY
    (no from/to attrs) is recovered by snapping each conduit's endpoints to the
    nearest node within a tolerance.
  - The design-storm LOADING (per-junction subcatchment area) is a LABELED
    synthetic input - we have no sub-catchment delineation for an imported
    network, exactly as the overland path labels its synthesized drainage grid.

Determinism (invariant 1/2): no LLM in the path; given the same inputs the deck
is reproducible. swmm-api + numpy are lazy-imported so the agent service imports
this module even when SWMM is absent (only a real import/run triggers the deps).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "SWMMNetworkError",
    "ParsedNetwork",
    "NetworkBuildResult",
    "NetworkRunResult",
    "parse_network_features",
    "build_network_inp",
    "run_network_deck",
    "read_network_response",
    "network_to_geojson_4326",
    "build_dual_drainage_inp",
    "DualDrainageBuildResult",
    "DEFAULT_PIPE_ROUGHNESS",
    "DEFAULT_PIPE_DIAMETER_M",
    "MAX_NETWORK_NODES",
]

#: Hard cap on imported nodes for the in-process one-shot solve. A municipal AOI
#: block/neighbourhood network is tens-to-a-few-hundred nodes; a whole-city
#: import above this is rejected with a typed gate (retry over a smaller AOI)
#: rather than wedging the always-on box on a runaway DYNWAVE solve.
MAX_NETWORK_NODES: int = 4000

# --------------------------------------------------------------------------- #
# Labeled demo defaults (every one narrated as a demo value, never site truth).
# --------------------------------------------------------------------------- #
#: Manning n for a concrete storm-drain pipe when the GIS carries no roughness.
DEFAULT_PIPE_ROUGHNESS: float = 0.013
#: Fallback circular-pipe diameter (m) when the GIS carries no size attribute.
DEFAULT_PIPE_DIAMETER_M: float = 0.4572  # 18 in
#: Fallback junction max-depth (m, rim-to-invert) when neither rim nor depth is
#: readable - a shallow-manhole default so surcharge/flooding stays meaningful.
DEFAULT_JUNCTION_MAX_DEPTH_M: float = 3.0
#: Assumed contributing sub-area per junction (ha) for the design-storm loading.
#: LABELED synthetic - an imported nodes/conduits export has no sub-catchment
#: delineation, so each junction drains one uniform demo sub-area of the storm.
DEFAULT_JUNCTION_SUBAREA_HA: float = 0.5
#: Endpoint-snapping tolerance (m) when a conduit line carries no from/to attrs.
DEFAULT_SNAP_TOLERANCE_M: float = 5.0


# --------------------------------------------------------------------------- #
# Field-name resolution (alias-aware, case/space/underscore-insensitive).
# --------------------------------------------------------------------------- #
def _norm_key(k: str) -> str:
    return "".join(ch for ch in str(k).lower() if ch.isalnum())


# Each entry: canonical -> the set of normalized field-name aliases we accept.
_NODE_ID_ALIASES = {
    "id", "name", "node", "nodeid", "nodename", "facilityid", "assetid",
    "objectid", "structureid", "mhid", "manholeid", "gisid", "featureid",
}
_NODE_INVERT_ALIASES = {
    "invertelev", "invertelevation", "invert", "invelev", "invelevation",
    "ie", "inv", "invertel", "nodeinvert", "invertlevel", "botelev",
    "bottomelev", "flowlineelev", "flowline", "invertft", "invertm",
}
_NODE_RIM_ALIASES = {
    "rimelev", "rimelevation", "rim", "groundelev", "groundelevation",
    "surfelev", "surfaceelev", "topelev", "coverelev", "maxelev", "grndelev",
}
_NODE_MAXDEPTH_ALIASES = {
    "maxdepth", "ymax", "depth", "nodedepth", "structuredepth", "sumpdepth",
    "rimtoinvert", "totaldepth",
}
_NODE_TYPE_ALIASES = {
    "type", "nodetype", "category", "structuretype", "featuretype",
    "assettype", "kind",
}
_CONDUIT_ID_ALIASES = {
    "id", "name", "conduit", "link", "linkid", "linkname", "pipeid",
    "facilityid", "assetid", "objectid", "gisid", "featureid",
}
_CONDUIT_FROM_ALIASES = {
    "fromnode", "usnode", "upstreamnode", "inletnode", "fromid", "fromnodeid",
    "from", "upstream", "startnode", "usmh", "upnode", "unitidus",
}
_CONDUIT_TO_ALIASES = {
    "tonode", "dsnode", "downstreamnode", "outletnode", "toid", "tonodeid",
    "to", "downstream", "endnode", "dsmh", "downnode", "unitidds",
}
_CONDUIT_DIAM_ALIASES = {
    "diameter", "diam", "dia", "geom1", "size", "pipesize", "pipediam",
    "nominaldia", "widthin", "diameterin", "diammm", "diameterm",
}
_CONDUIT_ROUGHNESS_ALIASES = {
    "roughness", "manningsn", "manning", "mannings", "roughn", "nvalue",
}
_CONDUIT_LENGTH_ALIASES = {
    "length", "len", "pipelength", "shapelength", "shapeleng", "stlength",
    "lengthft", "lengthm", "conduitlength",
}
_CONDUIT_SHAPE_ALIASES = {"shape", "xsection", "crosssection", "geomtype", "pipeshape"}


def _resolve_field(props: dict[str, Any], aliases: set[str]) -> Any:
    """Return the first property value whose normalized key is in ``aliases``.

    ``None`` when no key matches or every matching value is null/blank.
    """
    if not isinstance(props, dict):
        return None
    for k, v in props.items():
        if _norm_key(k) in aliases and v is not None and str(v).strip() != "":
            return v
    return None


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


# --------------------------------------------------------------------------- #
# Typed error (shares the A.6 open-set error_code shape with SWMMMeshError).
# --------------------------------------------------------------------------- #
class SWMMNetworkError(RuntimeError):
    """Raised on a typed network-import failure.

    Codes:
      - ``SWMM_NETWORK_EMPTY`` - the node and/or conduit layer parsed to nothing.
      - ``SWMM_NETWORK_NO_OUTFALL`` - no outfall node and none inferable (a SWMM
        model must terminate at an outfall).
      - ``SWMM_NETWORK_DISCONNECTED`` - after topology recovery, no conduit
        connects to any node (nothing to route).
      - ``SWMM_NETWORK_DEPENDENCY_MISSING`` - swmm-api / numpy unavailable.
    """

    def __init__(
        self, error_code: str, *, message: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message or error_code)
        self.error_code = error_code
        self.details: dict[str, Any] = dict(details or {})


# --------------------------------------------------------------------------- #
# Parsed-network intermediate representation.
# --------------------------------------------------------------------------- #
@dataclass
class _Node:
    name: str
    x: float
    y: float
    invert: float | None
    max_depth: float | None
    is_outfall: bool


@dataclass
class _Conduit:
    name: str
    from_node: str
    to_node: str
    length: float | None
    diameter: float
    roughness: float


@dataclass(frozen=True)
class ParsedNetwork:
    """The parsed municipal network, ready to author into a SWMM ``.inp``.

    Coordinates are in a projected METRES CRS (``crs``); lengths/inverts are
    metres. ``n_inverts_filled`` / ``n_diameters_defaulted`` /
    ``n_topology_snapped`` carry the labeled-degrade counts the build provenance
    surfaces so a gap-filled network can never masquerade as fully-attributed.
    """

    junctions: list[_Node]
    outfalls: list[_Node]
    conduits: list[_Conduit]
    crs: str
    n_inverts_filled: int
    n_diameters_defaulted: int
    n_topology_snapped: int
    n_conduits_dropped: int
    diameter_units_assumed: str


def _pick_utm_epsg(lon: float, lat: float) -> int:
    zone = int((lon + 180.0) // 6.0) + 1
    return (32600 if lat >= 0 else 32700) + zone


def parse_network_features(
    nodes_fc: dict[str, Any],
    conduits_fc: dict[str, Any],
    *,
    dem_path: str | None = None,
    snap_tolerance_m: float = DEFAULT_SNAP_TOLERANCE_M,
    default_diameter_m: float = DEFAULT_PIPE_DIAMETER_M,
    default_roughness: float = DEFAULT_PIPE_ROUGHNESS,
) -> ParsedNetwork:
    """Parse node + conduit GeoJSON FeatureCollections into a ``ParsedNetwork``.

    ``nodes_fc`` features are Point geometries (junctions + outfalls); an outfall
    is detected from a ``type``-like attribute containing "outfall"/"discharge",
    or - failing that - inferred as the lowest-invert leaf node. ``conduits_fc``
    features are LineString geometries; topology comes from explicit from/to
    attributes when present, else from snapping each endpoint to the nearest node
    within ``snap_tolerance_m``.

    All coordinates are reprojected from EPSG:4326 to the AOI's UTM zone so
    lengths/inverts are in metres. Missing inverts are DEM-sampled (when
    ``dem_path`` is given) or slope-walked from known inverts, and the fill count
    is recorded. Raises :class:`SWMMNetworkError` on an empty / outfall-less /
    fully-disconnected network.
    """
    try:
        import numpy as np  # noqa: F401
        from pyproj import Transformer
    except Exception as exc:  # pragma: no cover
        raise SWMMNetworkError(
            "SWMM_NETWORK_DEPENDENCY_MISSING",
            message=f"pyproj/numpy unavailable for network import: {exc}",
        ) from exc

    node_feats = _features_of_type(nodes_fc, ("Point",))
    line_feats = _features_of_type(conduits_fc, ("LineString", "MultiLineString"))
    if not node_feats:
        raise SWMMNetworkError(
            "SWMM_NETWORK_EMPTY",
            message="the node layer contained no Point features",
        )
    if not line_feats:
        raise SWMMNetworkError(
            "SWMM_NETWORK_EMPTY",
            message="the conduit layer contained no LineString features",
        )

    # UTM projection anchored on the node centroid (lon/lat -> metres).
    lons = [c[0] for c in (_point_coords(f) for f in node_feats) if c]
    lats = [c[1] for c in (_point_coords(f) for f in node_feats) if c]
    cen_lon = sum(lons) / len(lons)
    cen_lat = sum(lats) / len(lats)
    epsg = _pick_utm_epsg(cen_lon, cen_lat)
    tf = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)

    # --- nodes ---
    nodes: dict[str, _Node] = {}
    coord_index: list[tuple[float, float, str]] = []  # (x, y, name) for snapping
    auto_id = 0
    diam_units = "meters"
    for feat in node_feats:
        ll = _point_coords(feat)
        if not ll:
            continue
        x, y = tf.transform(ll[0], ll[1])
        props = (feat or {}).get("properties") or {}
        nid = _resolve_field(props, _NODE_ID_ALIASES)
        name = _sanitize_name(nid) if nid is not None else ""
        if not name or name in nodes:
            auto_id += 1
            name = f"N{auto_id}" if not name else f"{name}_{auto_id}"
        invert = _to_float(_resolve_field(props, _NODE_INVERT_ALIASES))
        rim = _to_float(_resolve_field(props, _NODE_RIM_ALIASES))
        max_depth = _to_float(_resolve_field(props, _NODE_MAXDEPTH_ALIASES))
        if max_depth is None and rim is not None and invert is not None:
            max_depth = max(rim - invert, 0.3)
        type_val = _resolve_field(props, _NODE_TYPE_ALIASES)
        is_outfall = bool(
            type_val is not None
            and any(t in str(type_val).lower() for t in ("outfall", "discharge", "outlet"))
        )
        nodes[name] = _Node(name, float(x), float(y), invert, max_depth, is_outfall)
        coord_index.append((float(x), float(y), name))

    # --- conduits (topology: explicit attrs, else endpoint snapping) ---
    conduits: list[_Conduit] = []
    n_snapped = 0
    n_dropped = 0
    n_diam_default = 0
    auto_cid = 0
    for feat in line_feats:
        line = _line_coords(feat)
        if not line or len(line) < 2:
            n_dropped += 1
            continue
        props = (feat or {}).get("properties") or {}
        cid = _resolve_field(props, _CONDUIT_ID_ALIASES)
        auto_cid += 1
        cname = _sanitize_name(cid) if cid is not None else ""
        if not cname:
            cname = f"C{auto_cid}"
        else:
            cname = f"{cname}"
        # from/to node resolution: an explicit id that MATCHES the node layer,
        # else snap the endpoint to a nearby node-layer node, else SYNTHESIZE a
        # node at the endpoint. A conduit is never dropped just because its layer
        # uses a different node-id scheme than the node layer (the common real
        # case, e.g. gravity-main NODE_UP/NODE_DN vs manhole MHno) - the conduit
        # graph IS the authoritative topology; the node layer supplies attributes.
        f_attr = _resolve_field(props, _CONDUIT_FROM_ALIASES)
        t_attr = _resolve_field(props, _CONDUIT_TO_ALIASES)
        (ux, uy) = tf.transform(line[0][0], line[0][1])
        (dx, dy) = tf.transform(line[-1][0], line[-1][1])
        from_name, s1, y1 = _ensure_node(f_attr, ux, uy, nodes, coord_index, snap_tolerance_m, auto_id)
        auto_id += int(y1)
        to_name, s2, y2 = _ensure_node(t_attr, dx, dy, nodes, coord_index, snap_tolerance_m, auto_id)
        auto_id += int(y2)
        n_snapped += int(s1) + int(s2)
        if from_name == to_name:
            n_dropped += 1
            continue
        length = _to_float(_resolve_field(props, _CONDUIT_LENGTH_ALIASES))
        if length is None:
            length = math.hypot(dx - ux, dy - uy)
        diam_raw = _to_float(_resolve_field(props, _CONDUIT_DIAM_ALIASES))
        if diam_raw is None:
            diameter = float(default_diameter_m)
            n_diam_default += 1
        else:
            diameter, diam_units = _normalize_diameter_m(diam_raw, diam_units)
        rough = _to_float(_resolve_field(props, _CONDUIT_ROUGHNESS_ALIASES))
        conduits.append(
            _Conduit(
                name=cname,
                from_node=from_name,
                to_node=to_name,
                length=float(length) if length and length > 0 else None,
                diameter=float(diameter),
                roughness=float(rough) if rough and rough > 0 else float(default_roughness),
            )
        )

    if not conduits:
        raise SWMMNetworkError(
            "SWMM_NETWORK_DISCONNECTED",
            message=(
                "no conduit connected two distinct nodes after topology recovery "
                f"(snap tolerance {snap_tolerance_m:.0f} m) - check that the node "
                "and conduit layers overlap and share a CRS"
            ),
            details={"n_nodes": len(nodes), "n_lines": len(line_feats)},
        )

    # keep only nodes actually touched by a conduit (drop orphan points).
    touched = {c.from_node for c in conduits} | {c.to_node for c in conduits}
    live_nodes = {n: nd for n, nd in nodes.items() if n in touched}

    # --- fill missing inverts (DEM sample -> slope-walk) + record the count. ---
    n_filled = _fill_missing_inverts(
        list(live_nodes.values()), conduits, dem_path=dem_path, transformer=tf, utm_epsg=epsg
    )

    # --- outfall resolution: tagged outfalls, else the lowest-invert leaf. ---
    outfalls = [nd for nd in live_nodes.values() if nd.is_outfall]
    if not outfalls:
        leaf = _infer_outfall(live_nodes, conduits)
        if leaf is None:
            raise SWMMNetworkError(
                "SWMM_NETWORK_NO_OUTFALL",
                message=(
                    "no node is tagged as an outfall and none could be inferred "
                    "as a downstream leaf - tag one node type=outfall"
                ),
            )
        leaf.is_outfall = True
        outfalls = [leaf]

    # SWMM rule (ERROR 141/145): an OUTFALL node takes EXACTLY ONE connected link.
    # A tagged/inferred outfall that carries multiple conduits is DEMOTED to a
    # junction and a dedicated outfall is appended just below it, fed by a single
    # short connector (the raster_cell_mesh P0 pattern).
    outfalls = _ensure_single_link_outfalls(
        outfalls, list(live_nodes.values()), conduits, default_diameter_m, default_roughness
    )
    for nd in outfalls:
        live_nodes.setdefault(nd.name, nd)
    outfall_names = {nd.name for nd in outfalls}
    junctions = [nd for nd in live_nodes.values() if nd.name not in outfall_names]

    logger.info(
        "parse_network_features: %d junctions, %d outfalls, %d conduits "
        "(inverts_filled=%d diameters_defaulted=%d topology_snapped=%d dropped=%d)",
        len(junctions), len(outfalls), len(conduits),
        n_filled, n_diam_default, n_snapped, n_dropped,
    )
    return ParsedNetwork(
        junctions=junctions,
        outfalls=outfalls,
        conduits=conduits,
        crs=f"EPSG:{epsg}",
        n_inverts_filled=n_filled,
        n_diameters_defaulted=n_diam_default,
        n_topology_snapped=n_snapped,
        n_conduits_dropped=n_dropped,
        diameter_units_assumed=diam_units,
    )


# --------------------------------------------------------------------------- #
# Geometry / attribute helpers.
# --------------------------------------------------------------------------- #
def _features_of_type(fc: Any, geom_types: tuple[str, ...]) -> list[dict]:
    if not isinstance(fc, dict):
        return []
    feats = fc.get("features")
    if not isinstance(feats, list):
        # allow a bare geometry list too
        return []
    out = []
    for f in feats:
        g = (f or {}).get("geometry") or {}
        if isinstance(g, dict) and g.get("type") in geom_types:
            out.append(f)
    return out


def _point_coords(feat: dict) -> tuple[float, float] | None:
    g = (feat or {}).get("geometry") or {}
    c = g.get("coordinates")
    if isinstance(c, (list, tuple)) and len(c) >= 2:
        try:
            return float(c[0]), float(c[1])
        except (TypeError, ValueError):
            return None
    return None


def _line_coords(feat: dict) -> list[tuple[float, float]]:
    g = (feat or {}).get("geometry") or {}
    c = g.get("coordinates")
    if g.get("type") == "MultiLineString":
        # flatten the first part (real storm conduits are single-part; be lenient)
        c = (c or [[]])[0]
    out: list[tuple[float, float]] = []
    for p in c or []:
        if isinstance(p, (list, tuple)) and len(p) >= 2:
            try:
                out.append((float(p[0]), float(p[1])))
            except (TypeError, ValueError):
                continue
    return out


def _sanitize_name(raw: Any) -> str:
    """SWMM ids must be whitespace-free tokens; keep them short + unique-friendly."""
    s = str(raw).strip()
    s = "".join(ch if (ch.isalnum() or ch in "-_.") else "_" for ch in s)
    return s[:31]


def _match_node_name(attr: Any, nodes: dict[str, _Node]) -> str | None:
    if attr is None:
        return None
    cand = _sanitize_name(attr)
    if cand in nodes:
        return cand
    # tolerate an id that was auto-suffixed for uniqueness (name_<n>)
    for n in nodes:
        if n == cand or n.startswith(cand + "_"):
            return n
    return None


def _ensure_node(
    name_hint: Any, x: float, y: float, nodes: dict[str, "_Node"],
    coord_index: list[tuple[float, float, str]], tol_m: float, auto_id: int,
) -> tuple[str, bool, bool]:
    """Resolve a conduit endpoint to a node name, creating one if needed.

    Returns ``(node_name, snapped, created)`` where ``snapped`` is True iff the
    endpoint matched an existing node by GEOMETRY (not an explicit id) and
    ``created`` is True iff a new synthetic junction was minted at the endpoint.
    Order: (1) an explicit id already present -> reuse it (authoritative
    topology); (2) snap to the nearest existing node within ``tol_m`` (inherit its
    attributes); (3) synthesize a new junction at the endpoint (keeping the
    explicit id as its name when given, so a later conduit's matching id reuses it).
    """
    if name_hint is not None:
        m = _match_node_name(name_hint, nodes)
        if m is not None:
            return m, False, False
    snapped, ok = _snap_to_node(x, y, coord_index, tol_m)
    if ok and snapped is not None:
        return snapped, True, False
    base = _sanitize_name(name_hint) if name_hint is not None else ""
    name = base if (base and base not in nodes) else f"{base or 'J'}_{auto_id}"
    nodes[name] = _Node(name, float(x), float(y), None, None, False)
    coord_index.append((float(x), float(y), name))
    return name, False, True


def _ensure_single_link_outfalls(
    outfalls: list["_Node"], all_nodes: list["_Node"], conduits: list["_Conduit"],
    default_diameter: float, default_roughness: float,
) -> list["_Node"]:
    """Enforce SWMM's one-link-per-outfall rule (ERROR 141/145).

    Any outfall carrying != 1 conduit is demoted to a junction and a dedicated
    outfall (``<name>_OUT``, one demo metre below its invert) is appended, fed by a
    single short connector conduit. Returns the final outfall list.
    """
    by_name = {nd.name: nd for nd in all_nodes}
    final: list[_Node] = []
    for of in outfalls:
        incident = [c for c in conduits if c.from_node == of.name or c.to_node == of.name]
        if len(incident) == 1:
            final.append(of)
            continue
        of.is_outfall = False  # demote to junction
        inv = of.invert if of.invert is not None else 0.0
        new_name = (f"{of.name}_OUT"[:29]) or "OUTLET"
        while new_name in by_name:
            new_name = new_name[:27] + "_O"
        new_of = _Node(new_name, of.x, of.y, inv - 1.0, None, True)
        by_name[new_name] = new_of
        all_nodes.append(new_of)
        conduits.append(_Conduit(
            name=(f"OUT_{of.name}"[:31]) or "OUTLET_LINK",
            from_node=of.name, to_node=new_name, length=1.0,
            diameter=float(default_diameter), roughness=float(default_roughness),
        ))
        final.append(new_of)
    return final


def _snap_to_node(
    x: float, y: float, coord_index: list[tuple[float, float, str]], tol_m: float
) -> tuple[str | None, bool]:
    best = None
    best_d = tol_m
    for (nx, ny, name) in coord_index:
        d = math.hypot(nx - x, ny - y)
        if d <= best_d:
            best_d = d
            best = name
    return (best, best is not None)


def _normalize_diameter_m(raw: float, prior_units: str) -> tuple[float, str]:
    """Coerce a raw diameter to metres, guessing units from magnitude.

    A storm-drain diameter in metres is ~0.2-3 m; the same value in mm is
    200-3000, in inches ~8-120, in feet ~0.5-10. We treat < 10 as metres (or a
    small feet value that is still a sane metre value), 10-300 as inches,
    > 300 as mm. Deliberately conservative + labeled; a real schema should carry
    units, but municipal exports frequently do not.
    """
    if raw <= 0:
        return DEFAULT_PIPE_DIAMETER_M, prior_units
    if raw < 10.0:
        return raw, "meters"
    if raw < 300.0:  # inches
        return raw * 0.0254, "inches"
    return raw / 1000.0, "millimeters"  # mm


def _infer_outfall(nodes: dict[str, _Node], conduits: list[_Conduit]) -> _Node | None:
    """Pick the most-downstream leaf node as the outfall.

    A leaf that only ever appears as a conduit ``to_node`` (never a ``from_node``)
    is a sink; among sinks the lowest invert wins (falling back to any sink, then
    to the globally lowest-invert node). Returns ``None`` only for an empty net.
    """
    if not nodes:
        return None
    from_names = {c.from_node for c in conduits}
    sinks = [nd for name, nd in nodes.items() if name not in from_names]
    pool = sinks or list(nodes.values())

    def _key(nd: _Node) -> float:
        return nd.invert if nd.invert is not None else math.inf

    return min(pool, key=_key)


def _fill_missing_inverts(
    nodes: list[_Node], conduits: list[_Conduit], *,
    dem_path: str | None, transformer: Any, utm_epsg: int,
) -> int:
    """Fill inverts for nodes that carry none. Returns the fill count.

    Strategy, most-honest first:
      1. If a DEM is supplied, sample the ground elevation at the node and set the
         invert one demo burial-depth below grade (labeled).
      2. Else propagate along the network: a node with no invert borrows the mean
         of its conduit-neighbours' known inverts (one relaxation sweep), then any
         still-unknown node takes the global mean known invert.
      3. If NOTHING is known anywhere, assign a flat descending ramp so the deck
         still routes (labeled as fully-synthetic).
    """
    missing = [nd for nd in nodes if nd.invert is None]
    if not missing:
        return 0
    n_missing = len(missing)

    #: demo storm-drain burial depth below grade (m) for the DEM-sample fill.
    _BURIAL_M = 1.5
    ground = _sample_dem_grounds(missing, dem_path=dem_path, utm_epsg=utm_epsg) if dem_path else {}
    for nd in missing:
        g = ground.get(nd.name)
        if g is not None and math.isfinite(g):
            nd.invert = float(g) - _BURIAL_M

    known = [nd.invert for nd in nodes if nd.invert is not None]
    if not known:
        # nothing anywhere: descending ramp along conduit order.
        for k, nd in enumerate(nodes):
            nd.invert = 100.0 - 0.5 * k
        return n_missing

    global_mean = sum(known) / len(known)
    # neighbour relaxation: two sweeps is plenty for a shallow storm tree.
    neigh: dict[str, list[str]] = {}
    for c in conduits:
        neigh.setdefault(c.from_node, []).append(c.to_node)
        neigh.setdefault(c.to_node, []).append(c.from_node)
    by_name = {nd.name: nd for nd in nodes}
    for _ in range(2):
        for nd in nodes:
            if nd.invert is not None:
                continue
            vals = [by_name[m].invert for m in neigh.get(nd.name, []) if by_name.get(m) and by_name[m].invert is not None]
            if vals:
                nd.invert = sum(vals) / len(vals)
    for nd in nodes:
        if nd.invert is None:
            nd.invert = global_mean
    return n_missing


def _sample_dem_grounds(
    nodes: list[_Node], *, dem_path: str, utm_epsg: int
) -> dict[str, float]:
    """Sample DEM ground elevation (metres) at each node's UTM coord.

    Reprojects the DEM to the node UTM CRS on the fly via rasterio.sample. Best
    effort: returns {} on any read failure (the caller then slope-walks)."""
    try:
        import numpy as np
        import rasterio
        from rasterio.warp import transform as warp_transform
    except Exception:  # pragma: no cover
        return {}
    out: dict[str, float] = {}
    try:
        with rasterio.open(dem_path) as src:
            # node coords are in EPSG:<utm_epsg> metres; DEM may be geographic.
            names = [nd.name for nd in nodes]
            xs = [nd.x for nd in nodes]
            ys = [nd.y for nd in nodes]
            dst_crs = src.crs or f"EPSG:{utm_epsg}"
            if str(dst_crs).upper() not in (f"EPSG:{utm_epsg}",):
                lons, lats = warp_transform(f"EPSG:{utm_epsg}", dst_crs, xs, ys)
            else:
                lons, lats = xs, ys
            for name, val in zip(names, src.sample(list(zip(lons, lats)))):
                v = float(val[0]) if val is not None and len(val) else float("nan")
                if math.isfinite(v) and v > -1e5:
                    out[name] = v
    except Exception as exc:  # noqa: BLE001 - best effort
        logger.warning("swmm network: DEM invert sampling failed (%s)", exc)
        return {}
    return out


# --------------------------------------------------------------------------- #
# Deck authoring.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class NetworkBuildResult:
    """Result of :func:`build_network_inp` - the deck path + network provenance."""

    inp_path: str
    n_junctions: int
    n_outfalls: int
    n_conduits: int
    crs: str
    node_coords: dict[str, tuple[float, float]]
    junction_names: list[str]
    outfall_names: list[str]
    n_inverts_filled: int
    n_diameters_defaulted: int
    n_topology_snapped: int
    n_conduits_dropped: int
    diameter_units_assumed: str
    total_pipe_length_m: float
    hyetograph: Any = None


def build_network_inp(
    parsed: ParsedNetwork,
    *,
    out_inp_path: str,
    total_rain_depth_mm: float = 120.0,
    storm_duration_hr: float = 6.0,
    rain_interval_min: int = 5,
    junction_subarea_ha: float = DEFAULT_JUNCTION_SUBAREA_HA,
    infiltration_method: str = "none",
    nesting_exponent: float = 0.62,
) -> NetworkBuildResult:
    """Author a runnable SWMM ``.inp`` from a :class:`ParsedNetwork`.

    JUNCTIONS + OUTFALLS from the parsed nodes, CONDUITS (CIRCULAR cross-section)
    from the parsed lines, node COORDINATES for rendering, and a per-junction
    design-storm loading (one uniform demo sub-area draining the Atlas-14 nested
    hyetograph to each junction) so the imported network actually routes flow to
    its outfall. The sub-area is a LABELED synthetic input.

    Raises :class:`SWMMNetworkError` on a missing swmm-api dependency.
    """
    try:
        from swmm_api import SwmmInput
        from swmm_api.input_file.section_labels import OPTIONS, REPORT
        from swmm_api.input_file.sections.node import Junction, Outfall
        from swmm_api.input_file.sections.node_component import Coordinate
        from swmm_api.input_file.sections.link import Conduit
        from swmm_api.input_file.sections.link_component import CrossSection
        from swmm_api.input_file.sections.subcatch import SubCatchment, SubArea, InfiltrationHorton
        from swmm_api.input_file.sections.others import RainGage, TimeseriesData
    except Exception as exc:
        raise SWMMNetworkError(
            "SWMM_NETWORK_DEPENDENCY_MISSING",
            message=f"swmm-api unavailable: {exc}",
        ) from exc

    from trid3nt_server.agent.workflows.swmm.swmm_hyetograph import (
        build_nested_hyetograph,
    )

    hyet = build_nested_hyetograph(
        total_depth_mm=float(total_rain_depth_mm),
        storm_duration_hr=float(storm_duration_hr),
        rain_interval_min=int(rain_interval_min),
        nesting_exponent=float(nesting_exponent),
    )

    inp = SwmmInput()

    import datetime as _dt

    _start = _dt.datetime(2024, 1, 1, 0, 0, 0)
    _end = _start + _dt.timedelta(hours=int(storm_duration_hr) + 2)  # +2h drain tail
    inp[OPTIONS] = {
        "FLOW_UNITS": "CMS",
        "INFILTRATION": "HORTON",
        "FLOW_ROUTING": "DYNWAVE",
        "LINK_OFFSETS": "DEPTH",
        "MIN_SLOPE": 0,
        "ALLOW_PONDING": "YES",
        "SKIP_STEADY_STATE": "NO",
        "START_DATE": _start.strftime("%m/%d/%Y"),
        "START_TIME": _start.strftime("%H:%M:%S"),
        "REPORT_START_DATE": _start.strftime("%m/%d/%Y"),
        "REPORT_START_TIME": _start.strftime("%H:%M:%S"),
        "END_DATE": _end.strftime("%m/%d/%Y"),
        "END_TIME": _end.strftime("%H:%M:%S"),
        "SWEEP_START": "01/01",
        "SWEEP_END": "12/31",
        "DRY_DAYS": 0,
        "REPORT_STEP": "00:05:00",
        "WET_STEP": "00:01:00",
        "DRY_STEP": "00:05:00",
        "ROUTING_STEP": 5,
        "RULE_STEP": "00:00:00",
        "INERTIAL_DAMPING": "PARTIAL",
        "NORMAL_FLOW_LIMITED": "BOTH",
        "FORCE_MAIN_EQUATION": "H-W",
        "VARIABLE_STEP": 0.75,
        "LENGTHENING_STEP": 0,
        "MIN_SURFAREA": 1.0,
        "MAX_TRIALS": 8,
        "HEAD_TOLERANCE": 0.0015,
        "THREADS": 1,
    }
    inp[REPORT] = {"INPUT": "NO", "CONTROLS": "NO", "SUBCATCHMENTS": "NONE",
                   "NODES": "ALL", "LINKS": "ALL"}

    inp.add_obj(TimeseriesData(name="HYET", data=list(hyet.timeseries)))
    inp.add_obj(RainGage(name="RG", form="INTENSITY",
                         interval=f"0:{int(rain_interval_min):02d}", SCF=1.0,
                         source="TIMESERIES", timeseries="HYET"))

    node_coords: dict[str, tuple[float, float]] = {}

    # --- JUNCTIONS + their design-storm subcatchment loading ---
    for nd in parsed.junctions:
        maxd = nd.max_depth if (nd.max_depth and nd.max_depth > 0) else DEFAULT_JUNCTION_MAX_DEPTH_M
        inp.add_obj(Junction(name=nd.name, elevation=float(nd.invert),
                             depth_max=float(maxd), depth_init=0.0))
        inp.add_obj(Coordinate(node=nd.name, x=float(nd.x), y=float(nd.y)))
        node_coords[nd.name] = (nd.x, nd.y)
        scname = f"SC_{nd.name}"
        inp.add_obj(SubCatchment(name=scname, rain_gage="RG", outlet=nd.name,
                                 area=float(junction_subarea_ha), imperviousness=70.0,
                                 width=math.sqrt(float(junction_subarea_ha) * 10_000.0),
                                 slope=1.0))
        inp.add_obj(SubArea(subcatchment=scname, n_imperv=0.012, n_perv=0.1,
                            storage_imperv=1.5, storage_perv=3.0, pct_zero=25,
                            route_to="OUTLET"))
        inp.add_obj(InfiltrationHorton(subcatchment=scname, rate_max=76.2, rate_min=3.3,
                                       decay=4.14, time_dry=7.0, volume_max=0.0))

    # --- OUTFALLS ---
    for nd in parsed.outfalls:
        inp.add_obj(Outfall(name=nd.name, elevation=float(nd.invert),
                           kind=Outfall.TYPES.FREE))
        inp.add_obj(Coordinate(node=nd.name, x=float(nd.x), y=float(nd.y)))
        node_coords[nd.name] = (nd.x, nd.y)

    # --- CONDUITS (CIRCULAR) ---
    total_len = 0.0
    valid_names = {nd.name for nd in parsed.junctions} | {nd.name for nd in parsed.outfalls}
    for c in parsed.conduits:
        if c.from_node not in valid_names or c.to_node not in valid_names:
            continue
        length = c.length if (c.length and c.length > 0) else 1.0
        total_len += length
        inp.add_obj(Conduit(name=c.name, from_node=c.from_node, to_node=c.to_node,
                           length=float(length), roughness=float(c.roughness),
                           offset_upstream=0, offset_downstream=0))
        inp.add_obj(CrossSection(link=c.name, shape="CIRCULAR",
                                height=float(c.diameter), parameter_2=0))

    Path(out_inp_path).parent.mkdir(parents=True, exist_ok=True)
    inp.write_file(out_inp_path)

    logger.info(
        "build_network_inp: wrote %s (%d junctions, %d outfalls, %d conduits, "
        "%.0f m pipe)",
        out_inp_path, len(parsed.junctions), len(parsed.outfalls),
        len(parsed.conduits), total_len,
    )
    return NetworkBuildResult(
        inp_path=out_inp_path,
        n_junctions=len(parsed.junctions),
        n_outfalls=len(parsed.outfalls),
        n_conduits=len(parsed.conduits),
        crs=parsed.crs,
        node_coords=node_coords,
        junction_names=[nd.name for nd in parsed.junctions],
        outfall_names=[nd.name for nd in parsed.outfalls],
        n_inverts_filled=parsed.n_inverts_filled,
        n_diameters_defaulted=parsed.n_diameters_defaulted,
        n_topology_snapped=parsed.n_topology_snapped,
        n_conduits_dropped=parsed.n_conduits_dropped,
        diameter_units_assumed=parsed.diameter_units_assumed,
        total_pipe_length_m=total_len,
        hyetograph=hyet,
    )


# --------------------------------------------------------------------------- #
# Solve + result extraction (one-shot headless swmm5_run + .rpt summaries).
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class NetworkRunResult:
    """Result of running an imported-network deck headless.

    Scalars are the imported MINOR-system's hydraulic response to the design
    storm; the per-node / per-conduit dicts drive the network vector layer.
    """

    rpt_path: str
    out_path: str
    continuity_error_pct: float
    peak_outfall_flow_cms: float
    total_outfall_volume_m3: float
    n_flooded_nodes: int
    n_surcharged_conduits: int
    max_node_hgl_m: float
    node_max_hgl: dict[str, float]
    node_max_depth: dict[str, float]
    flooded_nodes: set[str]
    surcharged_conduits: set[str]


def run_network_deck(
    build: NetworkBuildResult,
    *,
    mass_balance_tolerance_pct: float = 10.0,
) -> NetworkRunResult:
    """Run the imported-network ``.inp`` headless (one-shot ``swmm5_run``) and
    read the ``.rpt`` summaries into a :class:`NetworkRunResult`.

    Applies the mass-balance honesty gate on the Flow Routing Continuity error
    (defaulting to a looser 10% than the overland mesh, since an imported network
    with gap-filled inverts + a demo loading is coarser). Raises
    :class:`SWMMNetworkError` on a solver failure, an unreadable continuity, or a
    continuity error over tolerance (never publishes a silently-wrong network).
    """
    try:
        from swmm_api import swmm5_run, SwmmReport
    except Exception as exc:  # pragma: no cover
        raise SWMMNetworkError(
            "SWMM_NETWORK_DEPENDENCY_MISSING",
            message=f"swmm-api unavailable for run: {exc}",
        ) from exc
    from trid3nt_server.agent.mesh.raster_cell_mesh import read_flow_routing_continuity

    inp = build.inp_path
    rpt = str(Path(inp).with_suffix(".rpt"))
    out = str(Path(inp).with_suffix(".out"))
    try:
        swmm5_run(inp, fn_rpt=rpt, fn_out=out)
    except Exception as exc:
        raise SWMMNetworkError(
            "SWMM_NETWORK_RUN_FAILED",
            message=f"swmm5_run failed on the imported network: {exc}",
            details={"inp_path": inp},
        ) from exc

    cont = read_flow_routing_continuity(rpt)
    if cont is None:
        raise SWMMNetworkError(
            "SWMM_NETWORK_CONTINUITY_UNREADABLE",
            message="no Flow Routing Continuity error in the .rpt (run did not complete)",
            details={"rpt_path": rpt},
        )
    if abs(cont) > float(mass_balance_tolerance_pct):
        raise SWMMNetworkError(
            "SWMM_MASS_BALANCE_EXCEEDED",
            message=(
                f"Flow Routing Continuity error {cont:+.3f}% exceeds tolerance "
                f"{mass_balance_tolerance_pct:.1f}% - refusing to publish a "
                f"silently-wrong network result"
            ),
            details={"continuity_error_pct": cont, "rpt_path": rpt},
        )

    resp = read_network_response(rpt, node_filter=set(build.node_coords) or None)
    logger.info(
        "run_network_deck: continuity=%+.3f%% peak_outfall=%.4g CMS vol=%.4g m3 "
        "flooded=%d surcharged=%d max_hgl=%.2f",
        cont, resp["peak_outfall_flow_cms"], resp["total_outfall_volume_m3"],
        len(resp["flooded_nodes"]), len(resp["surcharged_conduits"]), resp["max_node_hgl_m"],
    )
    return NetworkRunResult(
        rpt_path=rpt, out_path=out, continuity_error_pct=cont,
        peak_outfall_flow_cms=resp["peak_outfall_flow_cms"],
        total_outfall_volume_m3=resp["total_outfall_volume_m3"],
        n_flooded_nodes=len(resp["flooded_nodes"]),
        n_surcharged_conduits=len(resp["surcharged_conduits"]),
        max_node_hgl_m=resp["max_node_hgl_m"], node_max_hgl=resp["node_max_hgl"],
        node_max_depth=resp["node_max_depth"], flooded_nodes=resp["flooded_nodes"],
        surcharged_conduits=resp["surcharged_conduits"],
    )


def read_network_response(
    rpt_path: str, *, node_filter: set[str] | None = None,
    conduit_filter: set[str] | None = None, outfall_filter: set[str] | None = None,
) -> dict[str, Any]:
    """Read the pipe-network hydraulic response from an already-solved ``.rpt``.

    Returns a dict of per-node max HGL / depth, flooded-node + surcharged-conduit
    sets, and peak / total outfall flow. Optional filters restrict the tallies to
    a NAMED subset (the dual-drainage path passes the pipe-node / pipe-outfall
    names so the overland mesh nodes are excluded from the pipe scalars). Shared by
    :func:`run_network_deck` and the dual-drainage coupling.
    """
    from swmm_api import SwmmReport

    rep = SwmmReport(rpt_path)
    node_max_hgl: dict[str, float] = {}
    node_max_depth: dict[str, float] = {}
    try:
        nds = rep.node_depth_summary
        if nds is not None:
            for name, row in nds.iterrows():
                nm = str(name)
                if node_filter is not None and nm not in node_filter:
                    continue
                node_max_depth[nm] = float(row.get("Maximum_Depth_Meters", 0.0) or 0.0)
                node_max_hgl[nm] = float(row.get("Maximum_HGL_Meters", 0.0) or 0.0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("swmm network: node depth summary unreadable (%s)", exc)

    flooded: set[str] = set()
    try:
        nfs = rep.node_flooding_summary
        if nfs is not None:
            flooded = {str(n) for n in nfs.index
                       if node_filter is None or str(n) in node_filter}
    except Exception as exc:  # noqa: BLE001
        logger.debug("swmm network: no node flooding summary (%s)", exc)

    surcharged: set[str] = set()
    try:
        css = rep.conduit_surcharge_summary
        if css is not None:
            surcharged = {str(n) for n in css.index
                          if conduit_filter is None or str(n) in conduit_filter}
    except Exception as exc:  # noqa: BLE001
        logger.debug("swmm network: no conduit surcharge summary (%s)", exc)

    peak_flow = 0.0
    total_vol_m3 = 0.0
    try:
        ols = rep.outfall_loading_summary
        if ols is not None:
            if outfall_filter is not None:
                ols = ols[ols.index.isin(outfall_filter)]
            if len(ols):
                peak_flow = float(ols["Max_Flow_CMS"].max())
                # SWMM reports outfall volume in 10^6 litres -> m^3 (1e6 L = 1e3 m^3).
                total_vol_m3 = float(ols["Total_Volume_10^6 ltr"].sum()) * 1_000.0
    except Exception as exc:  # noqa: BLE001
        logger.warning("swmm network: outfall loading summary unreadable (%s)", exc)

    return {
        "node_max_hgl": node_max_hgl, "node_max_depth": node_max_depth,
        "flooded_nodes": flooded, "surcharged_conduits": surcharged,
        "peak_outfall_flow_cms": peak_flow, "total_outfall_volume_m3": total_vol_m3,
        "max_node_hgl_m": (max(node_max_hgl.values()) if node_max_hgl else 0.0),
    }


def network_to_geojson_4326(
    build: NetworkBuildResult, run: NetworkRunResult | None
) -> dict[str, Any]:
    """Serialize the imported network as an EPSG:4326 GeoJSON FeatureCollection.

    Nodes become Point features carrying ``role`` (junction/outfall), max depth /
    HGL, and a ``flooded`` flag; conduits become LineString features carrying a
    ``surcharged`` flag. Coordinates are reprojected from the build's UTM CRS back
    to lon/lat. This is the renderable network layer the composer publishes.
    """
    from pyproj import Transformer

    tf = Transformer.from_crs(build.crs, "EPSG:4326", always_xy=True)
    feats: list[dict[str, Any]] = []
    outfalls = set(build.outfall_names)
    for name, (x, y) in build.node_coords.items():
        lon, lat = tf.transform(x, y)
        props: dict[str, Any] = {
            "node_id": name,
            "role": "outfall" if name in outfalls else "junction",
        }
        if run is not None:
            props["max_depth_m"] = round(run.node_max_depth.get(name, 0.0), 3)
            props["max_hgl_m"] = round(run.node_max_hgl.get(name, 0.0), 3)
            props["flooded"] = name in run.flooded_nodes
        feats.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": props,
        })
    # conduit lines: straight from-node -> to-node (endpoint coords).
    for cname, frm, to in _iter_conduit_endpoints(build):
        if frm not in build.node_coords or to not in build.node_coords:
            continue
        lon0, lat0 = tf.transform(*build.node_coords[frm])
        lon1, lat1 = tf.transform(*build.node_coords[to])
        props = {"conduit_id": cname, "from_node": frm, "to_node": to}
        if run is not None:
            props["surcharged"] = cname in run.surcharged_conduits
        feats.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": [[lon0, lat0], [lon1, lat1]]},
            "properties": props,
        })
    return {"type": "FeatureCollection", "features": feats}


def _iter_conduit_endpoints(build: NetworkBuildResult):
    """Recover (name, from, to) per conduit by re-reading the staged deck."""
    try:
        from swmm_api import SwmmInput
        from swmm_api.input_file.section_labels import CONDUITS
    except Exception:  # pragma: no cover
        return
    inp = SwmmInput.read_file(build.inp_path)
    conduits = inp[CONDUITS]
    for name in conduits:
        c = conduits[name]
        yield str(name), str(c.from_node), str(c.to_node)


# --------------------------------------------------------------------------- #
# Dual-drainage coupling (Row #2): overland MAJOR system + piped MINOR system in
# ONE deck, exchanging flow at inlets. The defining dual-drainage feature.
# --------------------------------------------------------------------------- #
#: Default catchbasin/inlet capture opening (m) linking a surface cell to a
#: sewer junction. A LABELED demo value - real inlets carry a capture (rating)
#: curve; here a single fixed orifice opening captures surface flow into the
#: pipe and lets a surcharging pipe back water up onto the surface (bidirectional).
DEFAULT_INLET_OPENING_M: float = 0.6
DEFAULT_INLET_CD: float = 0.65


@dataclass(frozen=True)
class DualDrainageBuildResult:
    """Result of :func:`build_dual_drainage_inp` - the combined deck + provenance.

    Carries the OVERLAND mesh georegistration (grid_shape / crs / transform /
    resolution_m, lifted from the mesh build) so the existing overland postprocess
    can scatter the surface node depths, PLUS the pipe-network node coordinates /
    names so the network vector layer renders, PLUS the inlet-coupling count (the
    surface<->sewer exchange links) that makes it dual drainage.
    """

    inp_path: str
    grid_shape: tuple[int, int]
    crs: str
    transform: list[float]
    resolution_m: float
    n_surface_cells: int
    n_pipe_junctions: int
    n_pipe_conduits: int
    n_pipe_outfalls: int
    n_inlets: int
    pipe_node_coords: dict[str, tuple[float, float]]
    pipe_junction_names: list[str]
    pipe_outfall_names: list[str]
    pipe_conduit_endpoints: list[tuple[str, str, str]]
    total_pipe_length_m: float
    n_inverts_filled: int
    n_topology_snapped: int


def build_dual_drainage_inp(
    mesh_build: Any,
    parsed: ParsedNetwork,
    *,
    out_inp_path: str,
    inlet_opening_m: float = DEFAULT_INLET_OPENING_M,
    inlet_cd: float = DEFAULT_INLET_CD,
) -> DualDrainageBuildResult:
    """Merge an overland mesh deck (``mesh_build``) with an imported pipe network
    (``parsed``) into ONE coupled dual-drainage ``.inp``.

    The overland mesh (already built + written by
    :func:`raster_cell_mesh.build_swmm_mesh`) is the MAJOR system: it carries the
    rainfall (per-cell subcatchments) and its own boundary outfall. The imported
    pipe network is the MINOR system: its junctions/conduits/outfalls are added to
    the deck (prefixed ``P_`` to avoid name collisions with the mesh ``S_``/``C_``
    ids), WITHOUT any independent rainfall loading (the mesh owns the rain -
    no double counting). Each pipe junction is linked to the overland cell it
    falls in by a single INLET orifice (surface storage node ``S_<r>_<c>`` ->
    ``P_<junction>``): a catchbasin that captures surface flow into the pipe and,
    when the pipe surcharges, backs water up onto the surface - the bidirectional
    exchange that DEFINES dual drainage.

    The combined deck's overland ``S_<r>_<c>`` storage nodes + ``grid_shape`` are
    unchanged, so the existing overland run/postprocess (which samples only the
    ``S_`` nodes) works on it verbatim; the pipe response is read separately from
    the same ``.rpt``.
    """
    try:
        from swmm_api import SwmmInput
        from swmm_api.input_file.section_labels import STORAGE
        from swmm_api.input_file.sections.node import Junction, Outfall
        from swmm_api.input_file.sections.node_component import Coordinate
        from swmm_api.input_file.sections.link import Conduit, Orifice
        from swmm_api.input_file.sections.link_component import CrossSection
    except Exception as exc:
        raise SWMMNetworkError(
            "SWMM_NETWORK_DEPENDENCY_MISSING",
            message=f"swmm-api unavailable: {exc}",
        ) from exc
    from affine import Affine
    from rasterio.transform import rowcol
    from rasterio.warp import transform as warp_transform

    inp = SwmmInput.read_file(mesh_build.inp_path)
    storage = inp[STORAGE]
    aff = Affine(*list(mesh_build.transform)[:6])
    mesh_crs = str(mesh_build.crs)
    reproject = parsed.crs != mesh_crs

    def _cell_of(x: float, y: float) -> str | None:
        if reproject:
            xs, ys = warp_transform(parsed.crs, mesh_crs, [x], [y])
            x, y = xs[0], ys[0]
        try:
            r, c = rowcol(aff, x, y)
        except Exception:  # noqa: BLE001
            return None
        name = f"S_{int(r)}_{int(c)}"
        return name if name in storage else None

    pipe_node_coords: dict[str, tuple[float, float]] = {}
    n_inlets = 0
    for nd in parsed.junctions:
        pname = f"P_{nd.name}"[:31]
        inp.add_obj(Junction(name=pname, elevation=float(nd.invert),
                             depth_max=(nd.max_depth if (nd.max_depth and nd.max_depth > 0)
                                        else DEFAULT_JUNCTION_MAX_DEPTH_M),
                             depth_init=0.0))
        inp.add_obj(Coordinate(node=pname, x=float(nd.x), y=float(nd.y)))
        pipe_node_coords[pname] = (nd.x, nd.y)
        cell = _cell_of(nd.x, nd.y)
        if cell is not None:
            oname = f"INLET_{nd.name}"[:31]
            inp.add_obj(Orifice(name=oname, from_node=cell, to_node=pname,
                               orientation="SIDE", offset=0.0,
                               discharge_coefficient=float(inlet_cd),
                               has_flap_gate=False, hours_to_open=0))
            inp.add_obj(CrossSection(link=oname, shape="RECT_CLOSED",
                                    height=float(inlet_opening_m),
                                    parameter_2=float(inlet_opening_m)))
            n_inlets += 1

    for nd in parsed.outfalls:
        pname = f"P_{nd.name}"[:31]
        inp.add_obj(Outfall(name=pname, elevation=float(nd.invert), kind=Outfall.TYPES.FREE))
        inp.add_obj(Coordinate(node=pname, x=float(nd.x), y=float(nd.y)))
        pipe_node_coords[pname] = (nd.x, nd.y)

    endpoints: list[tuple[str, str, str]] = []
    total_len = 0.0
    valid = {f"P_{nd.name}"[:31] for nd in parsed.junctions} | {f"P_{nd.name}"[:31] for nd in parsed.outfalls}
    for c in parsed.conduits:
        frm, to = f"P_{c.from_node}"[:31], f"P_{c.to_node}"[:31]
        if frm not in valid or to not in valid:
            continue
        length = c.length if (c.length and c.length > 0) else 1.0
        total_len += length
        cn = f"P_{c.name}"[:31]
        inp.add_obj(Conduit(name=cn, from_node=frm, to_node=to, length=float(length),
                           roughness=float(c.roughness), offset_upstream=0, offset_downstream=0))
        inp.add_obj(CrossSection(link=cn, shape="CIRCULAR", height=float(c.diameter), parameter_2=0))
        endpoints.append((cn, frm, to))

    Path(out_inp_path).parent.mkdir(parents=True, exist_ok=True)
    inp.write_file(out_inp_path)

    logger.info(
        "build_dual_drainage_inp: %d surface cells + %d pipe junctions / %d "
        "conduits / %d outfalls coupled by %d inlets -> %s",
        int(getattr(mesh_build, "n_active_cells", 0) or 0), len(parsed.junctions),
        len(endpoints), len(parsed.outfalls), n_inlets, out_inp_path,
    )
    return DualDrainageBuildResult(
        inp_path=out_inp_path,
        grid_shape=tuple(mesh_build.grid_shape),
        crs=mesh_crs,
        transform=list(mesh_build.transform)[:6],
        resolution_m=float(mesh_build.resolution_m),
        n_surface_cells=int(getattr(mesh_build, "n_active_cells", 0) or 0),
        n_pipe_junctions=len(parsed.junctions),
        n_pipe_conduits=len(endpoints),
        n_pipe_outfalls=len(parsed.outfalls),
        n_inlets=n_inlets,
        pipe_node_coords=pipe_node_coords,
        pipe_junction_names=[f"P_{nd.name}"[:31] for nd in parsed.junctions],
        pipe_outfall_names=[f"P_{nd.name}"[:31] for nd in parsed.outfalls],
        pipe_conduit_endpoints=endpoints,
        total_pipe_length_m=total_len,
        n_inverts_filled=parsed.n_inverts_filled,
        n_topology_snapped=parsed.n_topology_snapped,
    )


def dual_drainage_network_to_geojson_4326(
    build: DualDrainageBuildResult, response: dict[str, Any] | None
) -> dict[str, Any]:
    """Serialize the coupled deck's PIPE network as an EPSG:4326 FeatureCollection
    (nodes coloured by max HGL / flooding, conduits by surcharge) - the minor-system
    overlay drawn over the overland depth raster."""
    from pyproj import Transformer

    tf = Transformer.from_crs(build.crs, "EPSG:4326", always_xy=True)
    outfalls = set(build.pipe_outfall_names)
    resp = response or {}
    node_hgl = resp.get("node_max_hgl", {})
    node_depth = resp.get("node_max_depth", {})
    flooded = resp.get("flooded_nodes", set())
    surcharged = resp.get("surcharged_conduits", set())
    feats: list[dict[str, Any]] = []
    for name, (x, y) in build.pipe_node_coords.items():
        lon, lat = tf.transform(x, y)
        props = {"node_id": name, "role": "outfall" if name in outfalls else "junction"}
        if response is not None:
            props["max_depth_m"] = round(node_depth.get(name, 0.0), 3)
            props["max_hgl_m"] = round(node_hgl.get(name, 0.0), 3)
            props["flooded"] = name in flooded
        feats.append({"type": "Feature",
                      "geometry": {"type": "Point", "coordinates": [lon, lat]},
                      "properties": props})
    for cname, frm, to in build.pipe_conduit_endpoints:
        if frm not in build.pipe_node_coords or to not in build.pipe_node_coords:
            continue
        lon0, lat0 = tf.transform(*build.pipe_node_coords[frm])
        lon1, lat1 = tf.transform(*build.pipe_node_coords[to])
        props = {"conduit_id": cname, "from_node": frm, "to_node": to}
        if response is not None:
            props["surcharged"] = cname in surcharged
        feats.append({"type": "Feature",
                      "geometry": {"type": "LineString", "coordinates": [[lon0, lat0], [lon1, lat1]]},
                      "properties": props})
    return {"type": "FeatureCollection", "features": feats}
