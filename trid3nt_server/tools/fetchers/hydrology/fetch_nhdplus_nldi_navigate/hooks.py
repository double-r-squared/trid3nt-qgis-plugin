"""NLDI network traversal: the NHDPlus flowlines connected to a seed reach.

A seed_point snaps to a COMID (``nldi.get_features``), or a comid is taken as given;
``get_flowlines`` then navigates UM/UT/DM/DD to the distance. Zero flowlines is the
typed empty."""

from __future__ import annotations

import logging
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..._router.errors import router_empty_error, router_input_error, router_upstream_error
from ..._router.hooks import register_hook
from ..._router.hooks.dataretrieval import retrieve

__all__ = ["validate", "features"]

logger = logging.getLogger(__name__)

#: CONUS envelope and the flowline cap for the navigate service.
_NLDI_CONUS: tuple[float, float, float, float] = (-130.0, 20.0, -60.0, 55.0)
_NLDI_MAX_FLOWLINES = 5000


def _nldi_snap(spec: SourceSpec, lon: float, lat: float) -> int:
    """Snap (lon, lat) to the nearest NHDPlus COMID via NLDI /comid/position."""
    import dataretrieval.nldi as nldi

    prefix = spec.error_code_prefix
    # NLDI /comid/position 404s or errors an off-network point; any HTTP error on
    # the snap call is a typed upstream error.
    gf = retrieve(spec, nldi.get_features, input_on_400=False, lat=lat, long=lon)
    if gf is None or len(gf) == 0 or "comid" not in getattr(gf, "columns", []):
        raise router_empty_error(
            prefix,
            f"NLDI could not snap ({lon}, {lat}) to any NHDPlus reach "
            f"(likely offshore or outside the NHDPlus network)",
            spec.empty_error_suffix,
        )
    try:
        return int(gf.iloc[0]["comid"])
    except (TypeError, ValueError, KeyError) as exc:
        raise router_upstream_error(prefix, f"NLDI snap returned a non-integer COMID: {exc}")


@register_hook("nldi.validate")
def validate(spec: SourceSpec, params: dict[str, Any]) -> None:
    """Exactly one of seed_point and comid, a seed inside CONUS, a positive comid."""
    prefix = spec.error_code_prefix
    sfx = spec.input_error_suffix
    seed = params.get("seed_point")
    comid = params.get("comid")
    if (seed is None) == (comid is None):
        raise router_input_error(
            prefix,
            f"exactly one of seed_point or comid must be provided; got "
            f"seed_point={seed!r}, comid={comid!r}",
            sfx,
        )
    if seed is not None:
        lon, lat = float(seed[0]), float(seed[1])
        w, s, e, n = _NLDI_CONUS
        if not (w <= lon <= e and s <= lat <= n):
            raise router_input_error(
                prefix, f"seed_point ({lon}, {lat}) is outside NLDI's CONUS coverage {_NLDI_CONUS}", sfx
            )
    elif isinstance(comid, bool) or not isinstance(comid, int) or comid <= 0:
        raise router_input_error(prefix, f"comid must be a positive integer; got {comid!r}", sfx)


@register_hook("nldi.features")
def features(spec: SourceSpec, params: dict[str, Any], *,
             timeout_s: float) -> list[dict[str, Any]]:
    """The NLDI flowline LineStrings from the seed reach (seed_point XOR comid)."""
    import dataretrieval.nldi as nldi

    prefix = spec.error_code_prefix
    seed = params.get("seed_point")
    direction = params.get("direction") or "DM"
    distance_km = params.get("distance_km")
    if seed is not None:
        seed_comid = _nldi_snap(spec, float(seed[0]), float(seed[1]))
    else:
        seed_comid = int(params["comid"])

    # Navigate the connected flowlines: get_flowlines(as_json) returns the raw NLDI
    # GeoJSON FeatureCollection, LineStrings tagged with nhdplus_comid.
    fc = retrieve(
        spec, nldi.get_flowlines, input_on_400=False,
        navigation_mode=str(direction),
        # The distance travels as the FLOAT it was declared as. Rounding it
        # to a whole kilometre is not a rounding at all below 1 km: NLDI
        # returns whole reaches until the cumulative distance is EXCEEDED, so
        # a 0.5 km ask rounded to 0 returns the seed reach alone - one
        # flowline where the ask means four, and a shorter river than the
        # caller asked to model. NLDI accepts the fraction.
        distance=float(distance_km),
        comid=seed_comid,
        as_json=True,
    )

    raw_feats = (fc or {}).get("features", []) if isinstance(fc, dict) else []
    # A raw-empty navigate is a typed EMPTY; a raw-non-empty result whose features
    # filter to zero LineStrings is an honest header-only FGB instead.
    if not raw_feats:
        raise router_empty_error(
            prefix,
            f"NLDI navigate returned zero flowlines for seed COMID={seed_comid} "
            f"direction={direction} distance_km={distance_km} (network terminus, "
            f"stub reach, or distance shorter than the next reach)",
            spec.empty_error_suffix,
        )
    feats: list[dict[str, Any]] = []
    for feat in raw_feats:
        if not isinstance(feat, dict):
            continue
        geom = feat.get("geometry")
        if not isinstance(geom, dict) or geom.get("type") != "LineString":
            continue
        props = feat.get("properties") or {}
        cid = props.get("nhdplus_comid") or props.get("comid") or feat.get("id")
        try:
            cid_int = int(cid) if cid is not None else None
        except (TypeError, ValueError):
            cid_int = None
        feats.append(
            {"type": "Feature", "geometry": geom, "properties": {"nhdplus_comid": cid_int}}
        )

    if len(feats) > _NLDI_MAX_FLOWLINES:
        logger.warning(
            "nldi: %d flowlines > cap %d; truncating",
            len(feats), _NLDI_MAX_FLOWLINES,
        )
        feats = feats[:_NLDI_MAX_FLOWLINES]
    return feats
