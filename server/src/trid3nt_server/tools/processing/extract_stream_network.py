"""``extract_stream_network``: D8 accumulation >= threshold cells -> stream
LineStrings vector.

Carved out of the original two-tool ``hydrology_primitives`` module in the
tools/ reorg; behavior and the registered tool surface are unchanged. Shared
pysheds/DEM plumbing lives in
``trid3nt_server.tools.processing._hydrology_common``.
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import uuid
from typing import Any

import numpy as np

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.processing._hydrology_common import (
    HydrologyAoiTooLargeError,
    HydrologyDependencyError,
    HydrologyInputError,
    HydrologyPrimitivesError,
    HydrologyUpstreamError,
    _ENGINE_NOTE,
    _MAX_AOI_DEG,
    _condition_dem,
    _import_pysheds,
    _stage_dem,
    _stage_uri_local,
    _validate_bbox,
    _write_geojson,
)

__all__ = [
    "extract_stream_network",
    "NoStreamsError",
    "StreamNetworkLayerURI",
]

logger = logging.getLogger("trid3nt_server.tools.processing.extract_stream_network")


class NoStreamsError(HydrologyPrimitivesError):
    """No cell reaches the accumulation threshold -- no stream to extract."""

    error_code = "HYDROLOGY_NO_STREAMS"
    retryable = False

class StreamNetworkLayerURI(LayerURI):
    """Stream-network line ``LayerURI`` plus extraction summary.

    Extra fields beyond ``LayerURI``: ``segment_count`` (LineString branches),
    ``accumulation_threshold`` (cells), ``total_length_km`` (approximate sum
    of branch lengths), ``notes`` (engine path + provenance).
    """

    segment_count: int = 0
    accumulation_threshold: int = 500
    total_length_km: float = 0.0
    notes: list[str] = []

#: direction value -> (row_offset, col_offset). Row 0 is the NORTH edge.
_D8_OFFSETS: dict[int, tuple[int, int]] = {
    64: (-1, 0),  # N
    128: (-1, 1),  # NE
    1: (0, 1),  # E
    2: (1, 1),  # SE
    4: (1, 0),  # S
    8: (1, -1),  # SW
    16: (0, -1),  # W
    32: (-1, -1),  # NW
}

_STREAMS_METADATA = AtomicToolMetadata(
    name="extract_stream_network",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
)

def _downstream_cell(
    fdir: np.ndarray, r: int, c: int
) -> tuple[int, int] | None:
    """The D8 downstream neighbor of ``(r, c)``, or None (pit/edge/nodata)."""
    off = _D8_OFFSETS.get(int(fdir[r, c]))
    if off is None:
        return None
    rr, cc = r + off[0], c + off[1]
    if not (0 <= rr < fdir.shape[0] and 0 <= cc < fdir.shape[1]):
        return None
    return (rr, cc)

def _trace_stream_network(
    fdir: np.ndarray, mask: np.ndarray, affine: Any
) -> list[list[tuple[float, float]]]:
    """Vectorize the channel cells into LineStrings (pure numpy D8 walk).

    Mirrors pysheds' ``extract_river_network`` segmentation (which is
    NEP-50-broken in 0.4): a segment starts at every HEADWATER (no channel
    inflow) and every JUNCTION (>= 2 channel inflows) and follows the D8
    directions downstream until the next junction / channel exit, so branches
    join exactly at confluences. Coordinates are cell centers.
    """
    in_degree = np.zeros(mask.shape, dtype=np.int32)
    rows, cols = np.nonzero(mask)
    for r, c in zip(rows.tolist(), cols.tolist()):
        ds = _downstream_cell(fdir, r, c)
        if ds is not None and mask[ds]:
            in_degree[ds] += 1
    lines: list[list[tuple[float, float]]] = []
    for r, c in zip(rows.tolist(), cols.tolist()):
        if in_degree[r, c] == 1:
            continue  # mid-segment cell -- covered by an upstream walk
        path = [(r, c)]
        cur = (r, c)
        while True:
            ds = _downstream_cell(fdir, *cur)
            if ds is None or not mask[ds]:
                break
            path.append(ds)
            if in_degree[ds] >= 2:
                break  # junction: the next segment starts there
            cur = ds
        if len(path) >= 2:
            lines.append(
                [
                    (float(x), float(y))
                    for x, y in (affine * (cc + 0.5, rr + 0.5) for rr, cc in path)
                ]
            )
    return lines

# ---------------------------------------------------------------------------
# extract_stream_network
# ---------------------------------------------------------------------------


@register_tool(
    _STREAMS_METADATA,
    # Writes only its own run artifact; open-world when fetching the DEM.
    open_world_hint=True,
)
def extract_stream_network(
    bbox: tuple[float, float, float, float],
    accumulation_threshold: int = 500,
    dem_uri: str | None = None,
    *,
    _output_dir: str | None = None,
    # job-0164: absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> StreamNetworkLayerURI:
    """Extract the stream network from a DEM by D8 flow accumulation.

    Runs the pysheds D8 chain (pit/depression filling, flat resolution, flow
    direction, flow accumulation) and traces every flow path whose upslope
    area reaches ``accumulation_threshold`` cells, returning the channel
    network as a line layer on the map.

    When to use:
        - "Where are the streams / drainage lines in this area", "trace the
          channels on this DEM", terrain-derived drainage where NHD mapping
          is missing/coarse, headwater channels below mapped rivers.
        - Upstream of ``delineate_watershed`` (streams show WHERE to put the
          pour point) or paired with a flood/erosion layer.

    When NOT to use:
        - Mapped river geometry / named rivers (use ``fetch_river_geometry``
          or ``fetch_nhdplus_nldi_navigate`` -- those are surveyed, this is
          DEM-derived).
        - The basin boundary (use ``delineate_watershed``).

    Parameters:
        bbox: ``(min_lon, min_lat, max_lon, max_lat)`` EPSG:4326, clamped to
            <= 0.3 degrees per side.
        accumulation_threshold: minimum upslope CELL COUNT for a cell to be
            channel (default 500 cells ~ 0.45 km^2 at 30 m). LOWER -> denser
            network with more headwater channels; HIGHER -> only the main
            stems.
        dem_uri: optional override DEM (s3:// or local GeoTIFF). Default:
            Copernicus GLO-30 via ``fetch_copernicus_dem``.

    Returns:
        ``StreamNetworkLayerURI`` -- the channel network as a vector layer
        (GeoJSON LineStrings, one per branch) carrying ``segment_count``,
        the threshold used, ``total_length_km`` (approximate), and honest
        ``notes`` (engine path + provenance).

    Errors (FR-AS-11): ``HydrologyAoiTooLargeError`` (bbox over the clamp),
    ``HydrologyInputError`` (bad bbox / threshold / URI), ``NoStreamsError``
    (no cell reaches the threshold -- flat AOI or threshold too high),
    ``HydrologyDependencyError`` (pysheds missing),
    ``HydrologyUpstreamError`` (fetch/write failed).
    """
    q_bbox = _validate_bbox(bbox)
    try:
        threshold = int(accumulation_threshold)
    except (TypeError, ValueError) as exc:
        raise HydrologyInputError(
            f"accumulation_threshold must be an integer cell count; "
            f"got {accumulation_threshold!r}"
        ) from exc
    if threshold < 2:
        raise HydrologyInputError(
            f"accumulation_threshold must be >= 2 cells; got {accumulation_threshold!r}"
        )

    notes: list[str] = [_ENGINE_NOTE]

    with tempfile.TemporaryDirectory(prefix="trid3nt_streams_") as tmpdir:
        dem_path = _stage_dem(q_bbox, dem_uri, tmpdir, notes)
        grid, fdir, acc = _condition_dem(dem_path)

        acc_arr = np.asarray(acc)
        if not bool((acc_arr >= threshold).any()):
            raise NoStreamsError(
                f"No cell reaches the {threshold}-cell accumulation threshold "
                f"over {q_bbox!r} (max accumulation "
                f"{int(acc_arr.max()) if acc_arr.size else 0} cells). Lower "
                "accumulation_threshold or enlarge the bbox."
            )
        try:
            lines = _trace_stream_network(
                np.asarray(fdir), acc_arr >= threshold, grid.affine
            )
        except Exception as exc:  # noqa: BLE001
            raise HydrologyUpstreamError(
                f"stream-network vectorization failed: {exc}"
            ) from exc
        if not lines:
            raise NoStreamsError(
                f"River-network extraction produced zero traceable branches "
                f"at the {threshold}-cell threshold over {q_bbox!r} (channel "
                "cells exist but form no 2+-cell path; lower the threshold)."
            )
        features = [
            {
                "type": "Feature",
                "id": idx,
                "geometry": {"type": "LineString", "coordinates": coords},
                "properties": {"branch_id": idx},
            }
            for idx, coords in enumerate(lines)
        ]
        fc: dict[str, Any] = {"type": "FeatureCollection", "features": features}
        notes.append(
            f"{len(features)} branch(es) at accumulation >= {threshold} cells."
        )

        # Approximate total length (degrees -> km at the AOI center lat for a
        # geographic grid; meters -> km for projected grids).
        total_len_km = 0.0
        lat_c = 0.5 * (q_bbox[1] + q_bbox[3])
        kx = 111.320 * max(math.cos(math.radians(lat_c)), 0.01)
        ky = 110.540
        try:
            geographic = bool(getattr(grid.crs, "is_geographic", False))
        except Exception:  # noqa: BLE001
            geographic = False
        for feat in features:
            coords = feat.get("geometry", {}).get("coordinates", [])
            for (x0, y0), (x1, y1) in zip(coords, coords[1:]):
                if geographic:
                    total_len_km += math.hypot((x1 - x0) * kx, (y1 - y0) * ky)
                else:
                    total_len_km += math.hypot(x1 - x0, y1 - y0) / 1000.0

    seed = uuid.uuid4().hex[:8]
    uri = _write_geojson(fc, "stream_network", seed, _output_dir)
    logger.info(
        "extract_stream_network: bbox=%s threshold=%d -> %d branch(es), "
        "~%.2f km",
        q_bbox,
        threshold,
        len(features),
        total_len_km,
    )
    return StreamNetworkLayerURI(
        layer_id=f"stream-network-{seed}",
        name=(
            f"Stream network (>= {threshold} cells) -- bbox "
            f"({q_bbox[0]:.2f},{q_bbox[1]:.2f},{q_bbox[2]:.2f},{q_bbox[3]:.2f})"
        ),
        layer_type="vector",
        uri=uri,
        style_preset="stream_network",
        role="primary",
        units="upslope cells",
        bbox=q_bbox,
        segment_count=len(features),
        accumulation_threshold=threshold,
        total_length_km=round(total_len_km, 3),
        notes=notes,
    )
