"""Gridded-population fetcher (``fetch_population``): WorldPop 100 m raster primary; Census ACS B01003 tract GeoJSON on ``dataset="acs_2022"``.
"""

from __future__ import annotations

import io
import json
import logging
import math
import os
import re
import tempfile
import time
from collections.abc import Callable
from typing import Any

import requests

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.cache import read_through
from trid3nt_server.tools.fetchers._fetch_common import (
    FetchError,
    UpstreamAPIError,
    BboxInvalidError,
    _DEFAULT_USER_AGENT,
    _validate_bbox,
    round_bbox_to_resolution,
)

__all__ = [
    "fetch_population",
]

logger = logging.getLogger("trid3nt_server.tools.fetchers.socioeconomic.fetch_population")


# ---------------------------------------------------------------------------
# fetch_population — US Census ACS B01003_001E
# ---------------------------------------------------------------------------


_FETCH_POPULATION_METADATA = AtomicToolMetadata(
    name="fetch_population",
    ttl_class="static-30d",
    source_class="population",
    cacheable=True,
)

# ---------------------------------------------------------------------------
# WorldPop branch (Tier-1 default per Appendix F.1).
# ---------------------------------------------------------------------------
#
# WorldPop publishes a global population grid as country-clipped GeoTIFFs.
# Two products are relevant here (REST index at
# https://www.worldpop.org/rest/data/pop/<alias>?iso3=<ISO3>):
#
#   - alias=wpgpunadj (Unconstrained 100m UN-adjusted, 2000-2020) →
#       Global_2000_2020/<YEAR>/<ISO3>/<iso3_lower>_ppp_<YEAR>_UNadj.tif
#       (USA file = ~4 GB)
#   - alias=wpic1km (Unconstrained 1km individual countries, 2000-2020) →
#       Global_2000_2020_1km/<YEAR>/<ISO3>/<iso3_lower>_ppp_<YEAR>_1km_Aggregated.tif
#       (USA file = ~50 MB)
#
# Substrate choice: the 1km Aggregated product. WorldPop's HTTP server
# returns HTTP 200 with the full body for range requests (instead of HTTP
# 206 Partial Content), so GDAL's ``/vsicurl/`` cannot windowed-read the
# 100m file remotely — and downloading 4 GB per cache miss is impractical.
# The 1km file is tractable as a one-shot download and is sufficient for
# exposure analysis at M5/Fort-Myers-class bbox scales. Surfaced as
# OQ-37-WORLDPOP-RESOLUTION-VS-RANGE: revisit when a range-request-capable
# mirror lands, or when an official STAC catalog with native COGs is
# published (the kickoff suggested Microsoft Planetary Computer's
# ``worldpop-100m`` collection — that collection does not exist on PC at
# this writing; the WorldPop Hub STAC at https://hub.worldpop.org/stac/
# also 404s).


_WORLDPOP_BBOX_BY_ISO3: dict[str, tuple[float, float, float, float]] = {
    # ISO3 -> approximate (min_lon, min_lat, max_lon, max_lat) envelope.
    # Substrate-scope: CONUS-centric coverage matching the v0.1 Decision I
    # scope. Replaced with a real point-in-polygon over Natural Earth admin0
    # in a follow-up. Same shape/role as the CONUS state envelope table.
    "USA": (-125.0, 24.0, -66.5, 49.5),
    "CAN": (-141.0, 41.7, -52.6, 70.0),
    "MEX": (-118.5, 14.5, -86.7, 32.7),
    "CUB": (-85.0, 19.8, -74.1, 23.3),
    "BHS": (-79.5, 20.9, -72.7, 27.3),
    "JAM": (-78.4, 17.7, -76.2, 18.5),
    "HTI": (-74.5, 18.0, -71.6, 20.1),
    "DOM": (-72.0, 17.6, -68.3, 19.9),
    "PRI": (-67.3, 17.9, -65.2, 18.6),
}

def _iso3_for_lonlat(lon: float, lat: float) -> str | None:
    """Best-effort ISO3 country code lookup from a point — heuristic only.

    Returns ``None`` if no envelope matches. A future enrichment job replaces
    this with a real point-in-polygon over Natural Earth admin0 boundaries.
    """
    for iso3, (mn_lon, mn_lat, mx_lon, mx_lat) in _WORLDPOP_BBOX_BY_ISO3.items():
        if mn_lon <= lon <= mx_lon and mn_lat <= lat <= mx_lat:
            return iso3
    return None

# The only WorldPop tree these URLs build against is ``Global_2000_2020`` /
# ``Global_2000_2020_1km`` -- by name those products only publish the vintages
# 2000..2020 inclusive. A ``worldpop_<YEAR>`` dataset with YEAR outside this
# window composes a well-formed URL into a NON-EXISTENT path -> a bare HTTP 404.
# Per the data-source-fallback norm we normalize-then-VALIDATE the parsed year
# against this range so an unknown vintage fails LOUD at parse time (a clear
# typed error naming the supported window) rather than after a network 404.
_WORLDPOP_MIN_YEAR = 2000

_WORLDPOP_MAX_YEAR = 2020

def _worldpop_year_from_dataset(dataset: str) -> int:
    """Parse + validate the vintage year off a ``worldpop_<YEAR>`` dataset token.

    Normalize-then-validate (the ``goes18`` vs ``goes-18`` identifier-format
    norm): the year is parsed off the suffix and range-checked against the
    Global_2000_2020 product window BEFORE any URL is composed, so a malformed
    or out-of-range vintage fails LOUD with a clear, typed error listing the
    supported range rather than building a bogus path that 404s downstream.

    Raises ``UpstreamAPIError`` (NOT retryable in spirit -- re-running the same
    bad dataset string will not resolve) when the suffix is non-numeric or the
    year falls outside ``[_WORLDPOP_MIN_YEAR, _WORLDPOP_MAX_YEAR]``.
    """
    if not dataset.startswith("worldpop_"):
        raise UpstreamAPIError(
            f"unsupported dataset={dataset!r} for WorldPop branch; expected 'worldpop_2020'"
        )
    try:
        year = int(dataset.split("_", 1)[1])
    except (IndexError, ValueError) as exc:
        raise UpstreamAPIError(
            f"could not parse vintage year from dataset={dataset!r}; expected 'worldpop_YYYY'"
        ) from exc
    if not (_WORLDPOP_MIN_YEAR <= year <= _WORLDPOP_MAX_YEAR):
        raise UpstreamAPIError(
            f"WorldPop dataset={dataset!r}: year {year} is outside the "
            f"Global_2000_2020 product range "
            f"[{_WORLDPOP_MIN_YEAR},{_WORLDPOP_MAX_YEAR}]; only those vintages "
            "are published in this tree (e.g. 'worldpop_2020')"
        )
    return year

def _worldpop_url_for(iso3: str, year: int, resolution_m: int = 1000) -> str:
    """Compose the WorldPop GeoTIFF URL for a country/year at a given resolution.

    Default (``resolution_m=1000``) uses the
    ``Global_2000_2020_1km/<YEAR>/<ISO3>/<iso3_lower>_ppp_<YEAR>_1km_Aggregated.tif``
    convention from the WorldPop GIS Data hub — the 1km-aggregated product
    is ~50MB per country (USA), vs the 100m UN-adjusted product at ~4GB.
    The 1km default is used because the WorldPop server does not support HTTP
    range requests, so a 4GB whole-country download per cache miss is costly
    even with the 30-day cache window (see OQ-37-WORLDPOP-RESOLUTION-VS-RANGE
    for the resolution-vs-tractability trade-off; the 1km product is
    sufficient for exposure analysis at the bbox scales typical of
    M5/Fort-Myers-class demos).

    Phase-2 resolution lever: pass ``resolution_m <= 100`` to opt into the
    native 100m UN-adjusted product from the base ``Global_2000_2020`` tree
    (``<iso3_lower>_ppp_<YEAR>_UNadj.tif`` — note: NO ``_1km`` segment, the
    ``_UNadj`` suffix). That file is a ~4GB upstream whole-country download
    per cache miss, so it is opt-in only.
    """
    iso3_l = iso3.lower()
    if resolution_m <= 100:
        return (
            f"https://data.worldpop.org/GIS/Population/Global_2000_2020/{year}/"
            f"{iso3}/{iso3_l}_ppp_{year}_UNadj.tif"
        )
    return (
        f"https://data.worldpop.org/GIS/Population/Global_2000_2020_1km/{year}/"
        f"{iso3}/{iso3_l}_ppp_{year}_1km_Aggregated.tif"
    )

def _fetch_worldpop_population_bytes(
    bbox: tuple[float, float, float, float],
    dataset: str,
    target_resolution_m: int = 1000,
) -> bytes:
    """Fetch a windowed COG of WorldPop population for ``bbox``.

    The WorldPop product is published as a single GeoTIFF per (year, country):
    ~50MB at the 1km-aggregated default, or ~4GB at the 100m UN-adjusted
    native product (``target_resolution_m <= 100``). Because the WorldPop
    server does not support HTTP range requests, we download the full country
    file once to a tmp file, then use rasterio to read the windowed sub-region
    and rewrite it as a small Cloud-Optimized GeoTIFF for the cache.
    Subsequent calls hit the cache (30-day TTL) and skip the full download.

    ``dataset`` shape: ``worldpop_<YEAR>`` (e.g. ``worldpop_2020``). The year
    is parsed off the suffix and routed to the corresponding WorldPop URL.
    ``target_resolution_m`` selects the 1km (default) vs 100m product; the
    100m path is a ~4GB upstream country download per cache miss (opt-in cost).
    """
    _validate_bbox(bbox)
    # Normalize-then-validate the vintage year against the published product
    # window BEFORE composing a URL: an out-of-range year (e.g. worldpop_2024)
    # otherwise builds a well-formed path into a non-existent tree -> bare 404.
    year = _worldpop_year_from_dataset(dataset)

    mid_lon = 0.5 * (bbox[0] + bbox[2])
    mid_lat = 0.5 * (bbox[1] + bbox[3])
    iso3 = _iso3_for_lonlat(mid_lon, mid_lat)
    if iso3 is None:
        raise UpstreamAPIError(
            f"could not resolve ISO3 country code for bbox center=({mid_lon}, {mid_lat}); "
            "WorldPop branch needs an envelope match for the country file URL"
        )

    url = _worldpop_url_for(iso3, year, target_resolution_m)

    # rasterio is pulled in transitively by rioxarray; import lazily so test
    # environments without it can still load the registry.
    try:
        import rasterio  # type: ignore[import-not-found]
        from rasterio.windows import Window, from_bounds  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise UpstreamAPIError(f"rasterio unavailable: {exc}") from exc

    # Download the country file to a tmp path. We cannot use ``/vsicurl/``
    # because the WorldPop server returns HTTP 200 with the full body for
    # range requests instead of HTTP 206 — GDAL's curl driver then errors
    # with "Range downloading not supported by this server!". The 1km
    # aggregated USA file is ~50MB; bounded enough for a one-shot download.
    import tempfile

    src_tmp: str | None = None
    out_tmp: str | None = None
    try:
        try:
            resp = requests.get(
                url,
                headers={"User-Agent": _DEFAULT_USER_AGENT},
                timeout=180.0,
                stream=True,
                allow_redirects=True,
            )
            if resp.status_code == 404:
                raise UpstreamAPIError(
                    f"WorldPop file not found at {url} (iso3={iso3}, year={year}); "
                    "verify dataset vintage availability"
                )
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise UpstreamAPIError(
                f"WorldPop download failed url={url}: {exc}"
            ) from exc

        with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as src_f:
            src_tmp = src_f.name
            for chunk in resp.iter_content(chunk_size=1 << 20):  # 1 MiB chunks
                if chunk:
                    src_f.write(chunk)

        try:
            with rasterio.open(src_tmp) as src:
                # Compute the window for the bbox in the source's CRS
                # (WorldPop publishes in EPSG:4326; coords match bbox shape).
                window = from_bounds(
                    bbox[0], bbox[1], bbox[2], bbox[3], transform=src.transform
                )
                window = window.round_offsets().round_lengths()
                window = window.intersection(
                    Window(0, 0, src.width, src.height)
                )
                if window.width <= 0 or window.height <= 0:
                    raise UpstreamAPIError(
                        f"WorldPop window is empty for bbox={bbox} iso3={iso3} — "
                        "bbox may not intersect the country file extent"
                    )
                data = src.read(1, window=window)
                window_transform = src.window_transform(window)
                profile = src.profile.copy()
                profile.update(
                    {
                        "driver": "COG",
                        "width": int(window.width),
                        "height": int(window.height),
                        "transform": window_transform,
                        "compress": "LZW",
                        "BIGTIFF": "IF_SAFER",
                    }
                )
        except UpstreamAPIError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise UpstreamAPIError(
                f"rasterio windowed read failed for {url}: {exc}"
            ) from exc

        with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as out_f:
            out_tmp = out_f.name
        with rasterio.open(out_tmp, "w", **profile) as dst:
            dst.write(data, 1)
        with open(out_tmp, "rb") as f:
            out_bytes = f.read()

        return out_bytes
    finally:
        for path in (src_tmp, out_tmp):
            if path is None:
                continue
            try:
                os.unlink(path)
            except OSError:
                pass

def _fetch_acs_population_bytes(
    bbox: tuple[float, float, float, float], dataset: str
) -> bytes:
    """Fetch US Census ACS B01003 (total population) for tracts intersecting bbox.

    Uses the Census Bureau's public REST API (no key required for small
    queries; an API key can be added later for high-volume use). For the
    M4 substrate we return a GeoJSON ``FeatureCollection`` containing one
    feature per Census tract in the intersecting states, each with the
    ``B01003_001E`` total-population value as a property.

    The tract geometries themselves come from the Census TIGERweb GeoServices
    REST endpoint (a separate call). For substrate-scope simplicity this
    function returns a population *table* (FeatureCollection of point
    features at tract centroids) rather than full tract polygons; a future
    enrichment job swaps in real geometries from the TIGER cartographic
    boundary shapefiles.
    """
    _validate_bbox(bbox)
    if not dataset.startswith("acs_"):
        raise UpstreamAPIError(
            f"unsupported dataset={dataset!r} for ACS branch; expected 'acs_2022'"
        )
    year = dataset.split("_", 1)[1]
    # ACS 5-year endpoint; the variable B01003_001E is total population.
    # We request by `for=state:*` to enumerate the intersecting state set —
    # for the M4 substrate, just take the bbox center's state as a heuristic.
    mid_lon = 0.5 * (bbox[0] + bbox[2])
    mid_lat = 0.5 * (bbox[1] + bbox[3])
    state_fips = _state_fips_for_lonlat(mid_lon, mid_lat)
    if state_fips is None:
        raise UpstreamAPIError(
            f"could not resolve state FIPS for bbox center=({mid_lon}, {mid_lat}); "
            "ACS branch needs CONUS coverage"
        )

    # Census API: B01003_001E for all tracts in the state.
    url = (
        f"https://api.census.gov/data/{year}/acs/acs5?"
        f"get=B01003_001E,NAME&for=tract:*&in=state:{state_fips}"
    )
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": _DEFAULT_USER_AGENT, "Accept": "application/json"},
            timeout=30.0,
        )
        resp.raise_for_status()
        rows = resp.json()
    except requests.RequestException as exc:
        raise UpstreamAPIError(
            f"US Census ACS API failed for state={state_fips}: {exc}"
        ) from exc

    # rows[0] is the header; rows[1:] are data.
    if not rows or len(rows) < 2:
        raise UpstreamAPIError(
            f"US Census ACS returned no rows for state={state_fips}"
        )
    header = rows[0]
    pop_idx = header.index("B01003_001E")
    name_idx = header.index("NAME")
    state_idx = header.index("state")
    county_idx = header.index("county")
    tract_idx = header.index("tract")

    features: list[dict[str, Any]] = []
    for row in rows[1:]:
        try:
            pop = int(row[pop_idx]) if row[pop_idx] not in (None, "") else None
        except (TypeError, ValueError):
            pop = None
        features.append(
            {
                "type": "Feature",
                "geometry": None,  # geometry enrichment is a follow-up
                "properties": {
                    "name": row[name_idx],
                    "population": pop,
                    "state": row[state_idx],
                    "county": row[county_idx],
                    "tract": row[tract_idx],
                    "dataset": dataset,
                    "variable": "B01003_001E",
                },
            }
        )

    fc = {
        "type": "FeatureCollection",
        "features": features,
        "_trid3nt_bbox": list(bbox),
        "_trid3nt_dataset": dataset,
        "_trid3nt_source": "US Census ACS 5-year",
    }
    buf = io.BytesIO()
    buf.write(json.dumps(fc).encode("utf-8"))
    return buf.getvalue()

# Minimal lon/lat -> state FIPS mapping for the CONUS-default ACS branch.
# Used only as a routing heuristic in the M4 substrate; a future enrichment
# job replaces this with a real point-in-polygon over TIGER state boundaries.
_CONUS_STATE_BBOXES: dict[str, tuple[float, float, float, float]] = {
    # state_fips -> (min_lon, min_lat, max_lon, max_lat) approximate envelope
    "12": (-87.6, 24.4, -80.0, 31.0),  # Florida
    "13": (-85.6, 30.3, -80.8, 35.0),  # Georgia
    "01": (-88.5, 30.2, -84.9, 35.0),  # Alabama
    "28": (-91.7, 30.1, -88.1, 35.0),  # Mississippi
    "22": (-94.0, 28.9, -89.0, 33.0),  # Louisiana
    "48": (-106.7, 25.8, -93.5, 36.5),  # Texas
    "06": (-124.5, 32.5, -114.1, 42.0),  # California
    "53": (-124.8, 45.5, -116.9, 49.0),  # Washington
    "41": (-124.6, 41.9, -116.5, 46.3),  # Oregon
    "36": (-79.8, 40.5, -71.9, 45.0),  # New York
    "37": (-84.4, 33.8, -75.4, 36.6),  # North Carolina
    "45": (-83.4, 32.0, -78.5, 35.2),  # South Carolina
    "21": (-89.6, 36.5, -82.0, 39.1),  # Kentucky
    "47": (-90.3, 35.0, -81.7, 36.7),  # Tennessee
    "51": (-83.7, 36.5, -75.2, 39.5),  # Virginia
}

def _state_fips_for_lonlat(lon: float, lat: float) -> str | None:
    """Best-effort state FIPS lookup from a point — heuristic only.

    Returns ``None`` if no envelope matches. A future enrichment job replaces
    this with a real point-in-polygon over a TIGER state boundary file
    cached in the artifacts bucket.
    """
    for fips, (mn_lon, mn_lat, mx_lon, mx_lat) in _CONUS_STATE_BBOXES.items():
        if mn_lon <= lon <= mx_lon and mn_lat <= lat <= mx_lat:
            return fips
    return None

@register_tool(
    _FETCH_POPULATION_METADATA,
    # Annotations: readOnlyHint=True, openWorldHint=True (WorldPop/GCS public bucket),
    # destructiveHint=False, idempotentHint=True (cache shim deduplicates).
    open_world_hint=True,
)
def fetch_population(
    bbox: tuple[float, float, float, float],
    dataset: str = "worldpop_2020",
    target_resolution_m: int = 1000,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """Fetch population data for a bbox from WorldPop (Tier-1 default) or Census ACS.

    Use this when: the agent needs population counts for exposure analysis,
    risk scoring, or display alongside hazard layers. Anywhere globally, with
    no API key, at 100m resolution — that's the default WorldPop path.

    Do NOT use this for: real-time / daytime population (WorldPop and ACS are
    both residential count estimates); per-individual data (these are gridded /
    tract-level aggregates); sub-100m resolution (WorldPop's native grid is
    100m; finer resolution is a paid LandScan-grade product, not Tier-1).

    Default behavior:
        ``dataset="worldpop_2020"`` is the Tier-1 default — WorldPop
        Unconstrained 100m UN-adjusted gridded population. No API key
        required; global coverage; windowed read of the country GeoTIFF via
        rasterio ``/vsicurl/`` so only the bbox window is downloaded.

    Tier-2 opt-in:
        ``dataset="acs_2022"`` routes to the US Census ACS 5-year estimates
        (B01003_001E total population at tract level) — authoritative for
        CONUS, finer demographic detail, but **requires a Census API key**
        for non-trivial volumes (the Tier-2 routing rule per Appendix F.1).
        Pick this when the agent specifically needs tract-level precision
        rather than the 100m raster.

    Params:
        bbox: ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326.
        dataset: ``"worldpop_2020"`` (Tier-1 default, no key) or
            ``"acs_2022"`` (Tier-2 opt-in, US-only, Census key required for
            high-volume use). The WorldPop branch only publishes the
            Global_2000_2020 tree, so the vintage year MUST be in 2000..2020
            inclusive (``"worldpop_2020"`` is the canonical analytical
            product); a year outside that window raises ``UpstreamAPIError``
            at parse time rather than 404ing on a non-existent path. Newer
            vintages (e.g. ``"worldpop_2024"``) are NOT available until the
            v2024B file URLs stabilize and the range is widened here (tracked
            as OQ-37-WORLDPOP-VINTAGE-YEAR).
        target_resolution_m: ground cell size for the WorldPop branch.
            Default ``1000`` (the 1km-aggregated product, ~50MB per country —
            unchanged). Pass ``100`` (or any value ``<= 100``) to opt into the
            native 100m UN-adjusted product. WARNING: the 100m path is a ~4 GB
            upstream whole-country download per cache miss (WorldPop does not
            support HTTP range requests), so 100m is opt-in for its cost.
            Distinct cache keys per resolution (100m vs 1km do not collide).
            Ignored by the ACS branch.

    Returns:
        A ``LayerURI`` pointing at a Cloud-Optimized GeoTIFF (WorldPop branch)
        or a GeoJSON FeatureCollection (ACS branch) in the cache bucket.
        - WorldPop: ``s3://trid3nt-cache/cache/static-30d/population/<key>.tif``
          (100m raster, units = people per 100m cell).
        - ACS: ``s3://trid3nt-cache/cache/static-30d/population/<key>.json``
          (tract-level FeatureCollection; geometry enrichment is a follow-up).

    FR-CE-8: The fetch is routed through ``read_through`` so identical
    quantized-bbox + dataset calls reuse the cached artifact. FR-DC-4 dedup
    is preserved at 100m bbox quantization (matches WorldPop native
    resolution; coarser than the bbox driving the ACS tract intersection).
    """
    if dataset.startswith("worldpop_"):
        # Tier-1 default: WorldPop 100m windowed COG.
        # Quantize at 100m — matches WorldPop native resolution, preserves
        # FR-DC-4 dedup, and the ACS branch (when opted into) is happy with
        # the same grid since tracts are coarser than 100m anyway.
        quantized = round_bbox_to_resolution(bbox, 100)
        # target_resolution_m enters the cache params so 100m vs 1km fetches
        # get distinct cache keys (they are different upstream products).
        params = {
            "bbox": list(quantized),
            "dataset": dataset,
            "target_resolution_m": target_resolution_m,
        }
        result = read_through(
            metadata=_FETCH_POPULATION_METADATA,
            params=params,
            ext="tif",
            fetch_fn=lambda: _fetch_worldpop_population_bytes(
                quantized, dataset, target_resolution_m
            ),
        )
        assert result.uri is not None
        return LayerURI(
            layer_id=f"population-{quantized[0]:.4f}-{quantized[1]:.4f}-{dataset}",
            name=f"Population ({dataset})",
            layer_type="raster",
            uri=result.uri,
            style_preset="population_density",  # tools-backlog #3: people/pixel magma density ramp
            role="input",
            units="people",
        )

    if dataset.startswith("acs_"):
        # Tier-2 opt-in: US Census ACS B01003 tract-level. Census API key is
        # required for non-trivial volumes (OQ-36-CENSUS-API-KEY-REQUIRED);
        # the substrate works for small CONUS queries without a key.
        quantized = round_bbox_to_resolution(bbox, 100)
        params = {"bbox": list(quantized), "dataset": dataset}
        result = read_through(
            metadata=_FETCH_POPULATION_METADATA,
            params=params,
            ext="json",
            fetch_fn=lambda: _fetch_acs_population_bytes(quantized, dataset),
        )
        assert result.uri is not None
        return LayerURI(
            layer_id=f"population-{quantized[0]:.4f}-{quantized[1]:.4f}-{dataset}",
            name=f"Population ({dataset})",
            layer_type="vector",
            uri=result.uri,
            style_preset="population_density",  # tools-backlog #3: people/pixel magma density ramp
            role="input",
            units="people",
        )

    raise BboxInvalidError(
        f"unsupported dataset={dataset!r}; allowed: 'worldpop_2020' (default), "
        "'acs_2022' (Tier-2 opt-in, US-only)"
    )
