"""Slab-geometry hook: the Slab2 grids around a point or box, and the interface read at the point.

The release is read through the Slab2 seam, which downloads the tarball once; the record carries each
covering zone's window of depth, strike and dip, and for a point the interface read there.
"""

from __future__ import annotations

import logging
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ... import _slab2_grids as slab2
from ..._router.errors import router_empty_error, router_input_error
from ..._router.hooks import register_hook

logger = logging.getLogger(__name__)

__all__ = ["record"]


@register_hook("slab_geometry.record")
def record(spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]) -> dict[str, Any] | None:
    """The zones' windows over the stated place -> the record, or the typed refusal where Slab2 states no interface."""
    sc = spec.error_code_prefix
    point, box, zone = params.get("point"), params.get("bbox"), params.get("zone")
    if (point is None) == (box is None):
        raise router_input_error(sc, "state exactly one of point ([lon, lat]) and bbox ([west, south, east, north]).",
                                 spec.input_error_suffix)
    zones = slab2.read_archive(slab2.archive_bytes(spec))
    if zone is not None:
        zone = str(zone).strip().lower()
        if zone not in zones:
            raise router_input_error(sc, f"zone {zone!r} is none of the release's zones: {', '.join(zones)}.",
                                     spec.input_error_suffix)
        zones = {zone: zones[zone]}
    place = (point[0], point[1], point[0], point[1]) if point is not None else tuple(box)
    held = slab2.window(zones, place)
    out: dict[str, Any] = {"zones": slab2.record_zones(held), "source": {"archive": str(spec.endpoints["archive"].url)}}
    try:
        if point is not None:
            out["at"] = {"longitude": point[0], "latitude": point[1], **slab2.sample(list(zones.values()), point[0], point[1])}
        elif not held:
            raise slab2.SlabNoCoverage(f"Slab2 states no interface inside {tuple(round(v, 4) for v in box)}.")
    except slab2.SlabNoCoverage as exc:
        raise router_empty_error(sc, str(exc), spec.empty_error_suffix) from exc
    except slab2.SlabAmbiguous as exc:
        raise router_input_error(sc, str(exc), spec.input_error_suffix) from exc
    logger.info("slab_geometry: %d zone window(s)", len(held))
    return out
