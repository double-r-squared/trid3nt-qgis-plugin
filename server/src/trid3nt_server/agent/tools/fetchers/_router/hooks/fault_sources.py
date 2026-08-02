"""fault_sources hooks (finisher-mechanisms wave, ADR 0081): GEM active faults.

The irreducible steps the declarative surface cannot carry, all PURE:
- ``build_request`` -- ONE GET of the whole-world GEM GAF harmonized GeoJSON
  (the constant source file; the AOI never enters the URL). Fetched through the
  router's ``ingest.constant_cache`` two-tier cache so the 10.6 MB file downloads
  once per 30-day window and every AOI re-filters the same cached bytes.
- ``parse_response`` -- parse the GeoJSON, bbox-filter + kinematic-parse the
  features into fault-source records (verbatim from the twin: '(best,min,max)'
  triple parse, >=2-distinct-vertex + slip>0 gate, GEM depth/dip/rake defaults),
  and shape one LineString feature per fault. A zero-fault AOI returns ``[]`` (a
  feature-empty FGB), which the ``output.variant_by_emptiness`` switch turns into
  the honest empty-record dict -- no fabricated layer.
- ``envelope`` -- the POST-EMIT kinematic-record + legend + count read back from
  the produced FGB (-> FaultSourcesResult).
- ``empty_record`` -- the variant_by_emptiness dict for a zero-fault AOI.

Everything else -- transport, retry, the two-tier cache, payload gate, LayerURI --
is the shared router.
"""

from __future__ import annotations

import json
from typing import Any

from trid3nt_contracts.execution import LegendClass, LegendKey
from trid3nt_contracts.source_spec import SourceSpec

from .. import hooks as _hooks
from ..errors import router_upstream_error

__all__ = ["build_request", "parse_response", "envelope", "empty_record"]

#: GEM Global Active Faults, harmonized GeoJSON (worldwide; ~10.6 MB, 13696
#: faults). Versioned research artifact -> 30-day constant-cache tier.
GEM_GAF_URL = (
    "https://raw.githubusercontent.com/GEMScienceTools/"
    "gem-global-active-faults/master/geojson/"
    "gem_active_faults_harmonized.geojson"
)

#: Provenance label, mirrored on both the dict and LayerURI return shapes.
_SOURCE_LABEL = "GEM Global Active Faults (harmonized)"

#: The drawable/kinematic property columns carried on every fault feature (the
#: fault-source record fields the OpenQuake deck builder + click-inspect read).
_PROPS = (
    "name", "net_slip_rate_mm_yr", "dip_deg", "rake_deg",
    "upper_seis_depth_km", "lower_seis_depth_km", "slip_type", "catalog_name",
)


# --------------------------------------------------------------------------- #
# GEM-property parsing helpers (verbatim from the fetch_fault_sources twin).
# --------------------------------------------------------------------------- #


def first_num(v: Any, default: float | None = None) -> float | None:
    """Take the FIRST (best-estimate) value of a GEM property.

    GEM harmonized fields are strings like ``'(15.15,10.49,19.18)'`` (best,
    min, max) or ``'(38,,)'`` (best only). Tolerates plain numbers and lists.
    """
    if v is None:
        return default
    if isinstance(v, bool):  # guard: bool is an int subclass
        return default
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, (list, tuple)):
        return float(v[0]) if v and v[0] not in (None, "") else default
    if isinstance(v, str):
        head = v.strip().lstrip("(").split(",")[0].strip()
        try:
            return float(head)
        except ValueError:
            return default
    return default


def trace_coords(geometry: dict[str, Any] | None) -> list[list[float]]:
    """Flatten a fault geometry to an ordered ``[[lon, lat], ...]`` vertex list.

    Handles ``LineString`` and ``MultiLineString`` (the only shapes GEM GAF uses);
    a 3rd ``z`` ordinate is dropped. Anything else yields an empty list.
    """
    if not isinstance(geometry, dict):
        return []
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if not coords:
        return []
    if gtype == "LineString":
        pts = coords
    elif gtype == "MultiLineString":
        pts = [p for line in coords for p in line]
    else:
        return []
    out: list[list[float]] = []
    for p in pts:
        if isinstance(p, (list, tuple)) and len(p) >= 2:
            out.append([float(p[0]), float(p[1])])
    return out


def _trace_hits_bbox(pts: list[list[float]], bbox: tuple[float, float, float, float]) -> bool:
    """True iff any trace vertex falls inside the AOI bbox."""
    minlon, minlat, maxlon, maxlat = bbox
    return any(minlon <= p[0] <= maxlon and minlat <= p[1] <= maxlat for p in pts)


def _parse_fault_feature(feature: dict[str, Any]) -> dict[str, Any] | None:
    """Parse one GEM GAF feature into a fault-source record (or None to skip)."""
    if not isinstance(feature, dict):
        return None
    props = feature.get("properties") or {}
    pts = trace_coords(feature.get("geometry"))
    if len({(round(p[0], 6), round(p[1], 6)) for p in pts}) < 2:
        return None

    slip = first_num(props.get("net_slip_rate"))
    if slip is None or slip <= 0:
        return None

    dip = first_num(props.get("average_dip"), 90.0)
    rake = first_num(props.get("average_rake"), 180.0)
    usd = first_num(props.get("upper_seis_depth"), 0.0)
    lsd = first_num(props.get("lower_seis_depth"), 12.0)
    if lsd is None or usd is None or lsd <= usd:
        usd = usd if usd is not None else 0.0
        lsd = usd + 12.0

    slip_type = props.get("slip_type")
    catalog_name = props.get("catalog_name")
    return {
        "name": str(props.get("name") or "fault"),
        "geometry": pts,
        "net_slip_rate_mm_yr": float(slip),
        "dip_deg": float(dip),
        "rake_deg": float(rake),
        "upper_seis_depth_km": float(usd),
        "lower_seis_depth_km": float(lsd),
        "slip_type": str(slip_type) if slip_type else None,
        "catalog_name": str(catalog_name) if catalog_name else None,
    }


# --------------------------------------------------------------------------- #
# Hooks.
# --------------------------------------------------------------------------- #


@_hooks.register_hook("fault_sources.build_request")
def build_request(spec: SourceSpec, params: dict[str, Any]) -> list["_hooks.RequestPlan"]:
    """ONE GET of the whole-world GEM GAF file (fetched via constant_cache)."""
    endpoint = spec.endpoints.get("data") or next(iter(spec.endpoints.values()))
    url = endpoint.url or GEM_GAF_URL
    return [_hooks.RequestPlan(url=url, headers={"User-Agent": spec.auth.user_agent})]


@_hooks.register_hook("fault_sources.parse_response")
def parse_response(
    spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]
) -> list[dict[str, Any]]:
    """Parse the GEM GAF GeoJSON, bbox-filter + kinematic-parse -> fault features.

    A zero-fault AOI returns ``[]`` (a feature-empty FGB) -- the router's
    ``output.variant_by_emptiness`` switch turns that into the honest empty
    record dict. A bad body raises the source-stamped UPSTREAM error.
    """
    sc = spec.error_code_prefix
    raw = bodies[0] if bodies else b""
    try:
        collection = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise router_upstream_error(
            sc, f"GEM Global Active Faults payload was not valid GeoJSON: {exc}"
        )
    features = collection.get("features") if isinstance(collection, dict) else None
    if not isinstance(features, list):
        raise router_upstream_error(
            sc, "GEM Global Active Faults payload had no 'features' array"
        )

    bbox = tuple(float(v) for v in params["bbox"])
    out: list[dict[str, Any]] = []
    for feature in features:
        pts = trace_coords(feature.get("geometry") if isinstance(feature, dict) else None)
        if not _trace_hits_bbox(pts, bbox):  # type: ignore[arg-type]
            continue
        record = _parse_fault_feature(feature)
        if record is None:
            continue
        line = [[float(p[0]), float(p[1])] for p in record["geometry"]]
        if len(line) < 2:
            continue
        out.append(
            {
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": line},
                "properties": {k: record.get(k) for k in _PROPS},
            }
        )
    return out


def _faults_from_fgb(data: bytes) -> list[dict[str, Any]]:
    """Reconstruct the kinematic fault-source records from the produced FGB."""
    import os
    import tempfile

    import geopandas as gpd

    tmp: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False, prefix="trid3nt_fault_env_") as f:
            tmp = f.name
            f.write(data)
        gdf = gpd.read_file(tmp)
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    recs: list[dict[str, Any]] = []
    for _, row in gdf.iterrows():
        geom = row.geometry
        coords: list[list[float]] = []
        if geom is not None and geom.geom_type == "LineString":
            coords = [[float(x), float(y)] for x, y in geom.coords]
        rec: dict[str, Any] = {"geometry": coords}
        for k in _PROPS:
            v = row.get(k)
            if v is None or (isinstance(v, float) and v != v):  # drop NaN
                rec[k] = None
            elif k in ("net_slip_rate_mm_yr", "dip_deg", "rake_deg",
                       "upper_seis_depth_km", "lower_seis_depth_km"):
                rec[k] = float(v)
            else:
                rec[k] = str(v)
        recs.append(rec)
    return recs


@_hooks.register_hook("fault_sources.envelope")
def envelope(
    spec: SourceSpec, params: dict[str, Any], layer: Any, data: bytes | None
) -> dict[str, Any]:
    """The kinematic-record + legend + name envelope (-> FaultSourcesResult)."""
    faults = _faults_from_fgb(data) if data else []
    n = len(faults)
    plural = "trace" if n == 1 else "traces"
    return {
        "name": f"Active fault {plural} ({n})",
        "legend": LegendKey(
            kind="categorical",
            classes=[LegendClass(value="fault", color="#FF6A00", label="Active fault trace")],
            label="Active faults (GEM)",
        ),
        "catalog": str(params.get("catalog") or "gem"),
        "fault_count": n,
        "faults": faults,
        "source": _SOURCE_LABEL,
        "note": None,
    }


@_hooks.register_hook("fault_sources.empty_record")
def empty_record(spec: SourceSpec, params: dict[str, Any]) -> dict[str, Any]:
    """The honest zero-fault degrade: a bare record dict + typed note (no layer)."""
    q_bbox = [float(v) for v in params["bbox"]]
    note = (
        "No GEM active faults intersect this AOI. The area has no mapped "
        "active-fault sources; a fault-based PSHA cannot be built here "
        "(fall back to the synthetic area source if a hazard run is still "
        "wanted)."
    )
    return {
        "catalog": str(params.get("catalog") or "gem"),
        "bbox": q_bbox,
        "fault_count": 0,
        "faults": [],
        "note": note,
        "source": _SOURCE_LABEL,
    }
