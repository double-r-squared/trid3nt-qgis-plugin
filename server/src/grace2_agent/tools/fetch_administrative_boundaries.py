"""``fetch_administrative_boundaries`` atomic tool — TIGER/Line 2024 polygon fetcher (job-0084).

Downloads US Census TIGER/Line 2024 administrative-boundary shapefiles from
``https://www2.census.gov/geo/tiger/TIGER2024/``, clips to the requested bbox,
and writes a FlatGeobuf to the FR-DC cache (static-30d, source_class="admin_boundaries").

Supported levels:
    "state"  — 50 US states + DC + territories (~9.5 MB ZIP nationwide)
    "county" — 3000+ US counties (~80 MB ZIP nationwide)
    "place"  — cities + towns + CDPs; per-state ZIPs (~5-10 MB each)
    "zcta"   — ZIP Code Tabulation Areas (~504 MB ZIP nationwide)

Strategy A (audit.md): download the nationwide/per-state ZIP → unzip to a temp
directory → load with geopandas (via pyogrio driver) → bbox clip → write FlatGeobuf.
Cache key is SHA-256 of (level, bbox-rounded-to-6dp, year="2024").

FR-TA-2: atomic tool, returns ``LayerURI``.
FR-CE-8 / FR-DC-3/4: routed through ``read_through`` so identical
``(level, bbox)`` calls reuse the cached FlatGeobuf.

Tier-1 free (no API key required). Year pinned to 2024 (most recent stable
release at time of authoring); surfaced as an OQ for future auto-advancement.

URL conventions (verified 2026-06-08):
    state:  .../TIGER2024/STATE/tl_2024_us_state.zip
    county: .../TIGER2024/COUNTY/tl_2024_us_county.zip
    place:  .../TIGER2024/PLACE/tl_2024_{fips2}_place.zip  (per-state)
    zcta:   .../TIGER2024/ZCTA520/tl_2024_us_zcta520.zip

Note on ZCTA: the nationwide ZCTA ZIP is ~504 MB. The 30-day cache window
makes subsequent requests fast (cache hit), but the first fetch is slow.
Surfaced as OQ-84-ZCTA-DOWNLOAD-SIZE for sprint-12 optimization.
"""

from __future__ import annotations

import logging
import math
import os
import tempfile
import zipfile
from typing import Literal, Any

import requests

from grace2_contracts.execution import LayerURI
from grace2_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from .cache import read_through

__all__ = ["fetch_administrative_boundaries"]

logger = logging.getLogger("grace2_agent.tools.fetch_administrative_boundaries")

# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class AdminBoundaryError(RuntimeError):
    """Base class for fetch_administrative_boundaries failures.

    ``error_code`` maps to the WebSocket A.6 error frame emitted by the
    agent surface. ``retryable`` guides FR-AS-11 retry logic.
    """

    error_code: str = "ADMIN_BOUNDARY_ERROR"
    retryable: bool = True


class AdminBoundaryLevelError(AdminBoundaryError):
    """Unknown or unsupported level was requested."""

    error_code = "ADMIN_BOUNDARY_LEVEL_INVALID"
    retryable = False


class AdminBoundaryUpstreamError(AdminBoundaryError):
    """Census TIGER/Line download or parsing failed."""

    error_code = "ADMIN_BOUNDARY_UPSTREAM_ERROR"
    retryable = True


class AdminBoundaryEmptyError(AdminBoundaryError):
    """No features intersect the requested bbox after clipping."""

    error_code = "ADMIN_BOUNDARY_EMPTY"
    retryable = False


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

_TIGER_BASE = "https://www2.census.gov/geo/tiger/TIGER2024"
_TIGER_YEAR = "2024"

# Levels supported by this tool.
_VALID_LEVELS = frozenset({"state", "county", "place", "zcta"})

# Bbox quantization step: 10m (matching buildings/population precedent).
# Administrative boundaries are coarser than 10m — the snap is for cache-key
# deduplication only, not data precision.
_BBOX_QUANTIZE_M = 10

# User-Agent per Census usage guidelines.
_USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)

# ---------------------------------------------------------------------------
# AtomicToolMetadata — registered once at import time.
# ---------------------------------------------------------------------------

_METADATA = AtomicToolMetadata(
    name="fetch_administrative_boundaries",
    ttl_class="static-30d",
    source_class="admin_boundaries",
    cacheable=True,
)


# ---------------------------------------------------------------------------
# FIPS lookup helpers.
# ---------------------------------------------------------------------------

# State FIPS → approximate WGS84 bounding box (min_lon, min_lat, max_lon, max_lat).
# Used by "place" level to identify which per-state ZIP(s) to download.
# Covers 50 states + DC + PR + VI. Envelopes are intentionally generous (~10km
# buffer) so bbox queries near state borders include the correct state file.
# Alaska's western Aleutian Islands cross the 180th meridian into the eastern
# hemisphere (Attu Island ~ +173 lon). A single WGS84 envelope cannot span the
# antimeridian, so Alaska gets TWO envelopes that are OR-ed together below: the
# main body (negative lon, down to -180) plus the trans-antimeridian Aleutian
# tail (positive lon, +172 .. +180). See ``_state_fips_for_bbox``.
_ALASKA_FIPS = "02"
_ALASKA_ANTIMERIDIAN_BBOX: tuple[float, float, float, float] = (
    172.0, 51.0, 180.0, 53.5
)

_STATE_FIPS_BBOXES: dict[str, tuple[float, float, float, float]] = {
    "01": (-88.5, 30.1, -84.9, 35.0),   # Alabama
    # Alaska main body (negative-lon hemisphere). The trans-antimeridian
    # Aleutian tail is handled separately via _ALASKA_ANTIMERIDIAN_BBOX so a
    # query over the western Aleutians (e.g. Attu, lon ~ +173) still routes to
    # the AK ("02") per-state PLACE ZIP instead of dead-ending.
    "02": (-180.0, 51.0, -129.9, 71.5),  # Alaska (main body; W. Aleutians: see below)
    "04": (-114.8, 31.3, -109.0, 37.0),  # Arizona
    "05": (-94.6, 33.0, -89.7, 36.5),   # Arkansas
    "06": (-124.5, 32.5, -114.1, 42.0),  # California
    "08": (-109.1, 36.9, -102.0, 41.0),  # Colorado
    "09": (-73.7, 40.9, -71.8, 42.1),   # Connecticut
    "10": (-75.8, 38.4, -75.0, 39.8),   # Delaware
    "11": (-77.1, 38.8, -76.9, 39.0),   # DC
    "12": (-87.6, 24.4, -80.0, 31.0),   # Florida
    "13": (-85.6, 30.3, -80.8, 35.0),   # Georgia
    "15": (-160.3, 18.9, -154.8, 22.2), # Hawaii
    "16": (-117.2, 42.0, -111.0, 49.0), # Idaho
    "17": (-91.5, 36.9, -87.0, 42.5),   # Illinois
    "18": (-88.1, 37.8, -84.8, 41.8),   # Indiana
    "19": (-96.6, 40.4, -90.1, 43.5),   # Iowa
    "20": (-102.1, 36.9, -94.6, 40.0),  # Kansas
    "21": (-89.6, 36.5, -82.0, 39.1),   # Kentucky
    "22": (-94.0, 28.9, -89.0, 33.0),   # Louisiana
    "23": (-71.1, 43.0, -67.0, 47.5),   # Maine
    "24": (-79.5, 37.9, -75.0, 39.7),   # Maryland
    "25": (-73.5, 41.2, -69.9, 42.9),   # Massachusetts
    "26": (-90.4, 41.7, -82.4, 48.3),   # Michigan
    "27": (-97.2, 43.5, -89.5, 49.4),   # Minnesota
    "28": (-91.7, 30.1, -88.1, 35.0),   # Mississippi
    "29": (-95.8, 35.9, -89.1, 40.6),   # Missouri
    "30": (-116.1, 44.4, -104.0, 49.0), # Montana
    "31": (-104.1, 40.0, -95.3, 43.0),  # Nebraska
    "32": (-120.0, 35.0, -114.0, 42.0), # Nevada
    "33": (-72.6, 42.7, -70.6, 45.3),   # New Hampshire
    "34": (-75.6, 38.9, -73.9, 41.4),   # New Jersey
    "35": (-109.1, 31.3, -103.0, 37.0), # New Mexico
    "36": (-79.8, 40.5, -71.9, 45.0),   # New York
    "37": (-84.4, 33.8, -75.4, 36.6),   # North Carolina
    "38": (-104.1, 45.9, -96.6, 49.0),  # North Dakota
    "39": (-84.8, 38.4, -80.5, 42.3),   # Ohio
    "40": (-103.0, 33.6, -94.4, 37.0),  # Oklahoma
    "41": (-124.6, 41.9, -116.5, 46.3), # Oregon
    "42": (-80.5, 39.7, -74.7, 42.3),   # Pennsylvania
    "44": (-71.9, 41.1, -71.1, 42.0),   # Rhode Island
    "45": (-83.4, 32.0, -78.5, 35.2),   # South Carolina
    "46": (-104.1, 42.5, -96.4, 45.9),  # South Dakota
    "47": (-90.3, 35.0, -81.7, 36.7),   # Tennessee
    "48": (-106.7, 25.8, -93.5, 36.5),  # Texas
    "49": (-114.1, 37.0, -109.0, 42.0), # Utah
    "50": (-73.4, 42.7, -71.5, 45.0),   # Vermont
    "51": (-83.7, 36.5, -75.2, 39.5),   # Virginia
    "53": (-124.8, 45.5, -116.9, 49.0), # Washington
    "54": (-82.6, 37.2, -77.7, 40.6),   # West Virginia
    "55": (-92.9, 42.5, -86.8, 47.1),   # Wisconsin
    "56": (-111.1, 40.9, -104.1, 45.0), # Wyoming
    "72": (-67.3, 17.9, -65.2, 18.6),   # Puerto Rico
    "78": (-65.1, 17.6, -64.5, 18.5),   # Virgin Islands
}


def _bbox_intersects(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> bool:
    """Return True iff axis-aligned bboxes ``a`` and ``b`` overlap (inclusive).

    Bboxes intersect iff neither is entirely to one side of the other on
    either axis. Both are ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326
    and assumed NOT to cross the antimeridian (callers split such bboxes first).
    """
    a_min_lon, a_min_lat, a_max_lon, a_max_lat = a
    b_min_lon, b_min_lat, b_max_lon, b_max_lat = b
    return (
        a_min_lon <= b_max_lon and a_max_lon >= b_min_lon
        and a_min_lat <= b_max_lat and a_max_lat >= b_min_lat
    )


def _state_fips_for_bbox(bbox: tuple[float, float, float, float]) -> list[str]:
    """Return all state FIPS codes whose bounding boxes intersect ``bbox``.

    Uses a simple bbox-vs-bbox intersection test against ``_STATE_FIPS_BBOXES``.
    Alaska is special-cased to also match the trans-antimeridian Aleutian tail
    (``_ALASKA_ANTIMERIDIAN_BBOX``, positive longitudes) so the western
    Aleutians route to the AK ("02") PLACE ZIP instead of silently missing.

    A future enrichment job can replace this with a real point-in-polygon over
    TIGER state polygons for better accuracy near state borders (TODO).

    Returns at least one FIPS code if the bbox is within CONUS/territories.
    Returns an empty list if no state envelope matches (bbox outside coverage).
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    results: list[str] = []
    for fips, env in _STATE_FIPS_BBOXES.items():
        if _bbox_intersects(bbox, env):
            results.append(fips)
    # Alaska antimeridian tail: a western-Aleutian bbox uses positive longitudes
    # (e.g. Attu ~ +173) and so misses the main "02" envelope above. Add AK if
    # the query overlaps the trans-antimeridian Aleutian box (and avoid a dup).
    if _ALASKA_FIPS not in results and _bbox_intersects(bbox, _ALASKA_ANTIMERIDIAN_BBOX):
        results.append(_ALASKA_FIPS)
    return results


# ---------------------------------------------------------------------------
# TIGER/Line URL builders.
# ---------------------------------------------------------------------------


def _tiger_url(level: str, state_fips: str | None = None) -> str:
    """Return the TIGER/Line 2024 download URL for ``level``.

    ``state_fips`` is required for per-state levels ("place").
    """
    if level == "state":
        return f"{_TIGER_BASE}/STATE/tl_{_TIGER_YEAR}_us_state.zip"
    if level == "county":
        return f"{_TIGER_BASE}/COUNTY/tl_{_TIGER_YEAR}_us_county.zip"
    if level == "zcta":
        return f"{_TIGER_BASE}/ZCTA520/tl_{_TIGER_YEAR}_us_zcta520.zip"
    if level == "place":
        if not state_fips:
            raise AdminBoundaryLevelError(
                "state_fips is required to build the place URL"
            )
        return f"{_TIGER_BASE}/PLACE/tl_{_TIGER_YEAR}_{state_fips}_place.zip"
    raise AdminBoundaryLevelError(
        f"unknown level={level!r}; allowed: {sorted(_VALID_LEVELS)}"
    )


# ---------------------------------------------------------------------------
# bbox helpers.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    """Raise ``AdminBoundaryError`` if bbox is invalid."""
    if len(bbox) != 4:
        raise AdminBoundaryError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    min_lon, min_lat, max_lon, max_lat = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise AdminBoundaryError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise AdminBoundaryError(f"bbox lon out of [-180,180]: {bbox!r}")
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise AdminBoundaryError(f"bbox lat out of [-90,90]: {bbox!r}")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise AdminBoundaryError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )


def _round_bbox_to_6dp(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Round bbox coordinates to 6 decimal places (~0.1m) for cache-key stability.

    Matching the audit.md cache-key spec: "SHA256 of (level, bbox-rounded-to-6dp, year)".
    """
    return tuple(round(v, 6) for v in bbox)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Core download + clip function.
# ---------------------------------------------------------------------------


def _download_and_clip_zip(
    url: str,
    bbox: tuple[float, float, float, float],
    layer_name_hint: str,
) -> bytes:
    """Download a TIGER/Line ZIP, unzip to a temp dir, clip to bbox, return FlatGeobuf bytes.

    Args:
        url: TIGER/Line ZIP URL.
        bbox: ``(min_lon, min_lat, max_lon, max_lat)`` clip extent in EPSG:4326.
        layer_name_hint: used only in log messages for diagnostics.

    Returns:
        FlatGeobuf bytes of the clipped features.

    Raises:
        ``AdminBoundaryUpstreamError``: download, unzip, or geopandas I/O failure.
        ``AdminBoundaryEmptyError``: no features intersect bbox after clip.
    """
    # Lazy imports so test environments that mock the network can still import
    # the module without installing geopandas/shapely.
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
        from shapely.geometry import box as shapely_box  # type: ignore[import-not-found]
    except ImportError as exc:
        raise AdminBoundaryUpstreamError(
            f"geopandas / shapely not available: {exc}"
        ) from exc

    tmp_zip: str | None = None
    tmp_dir: str | None = None
    tmp_fgb: str | None = None

    try:
        # 1. Download ZIP to a temp file.
        logger.info(
            "fetch_administrative_boundaries: downloading %s for %s", url, layer_name_hint
        )
        try:
            resp = requests.get(
                url,
                headers={"User-Agent": _USER_AGENT},
                timeout=300.0,    # large files (county ~80MB, ZCTA ~504MB) need time
                stream=True,
                allow_redirects=True,
            )
            if resp.status_code == 404:
                raise AdminBoundaryUpstreamError(
                    f"TIGER/Line 2024 file not found at {url}"
                )
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise AdminBoundaryUpstreamError(
                f"TIGER/Line download failed url={url}: {exc}"
            ) from exc

        with tempfile.NamedTemporaryFile(
            suffix=".zip", delete=False, prefix="grace2_tiger_"
        ) as zf:
            tmp_zip = zf.name
            for chunk in resp.iter_content(chunk_size=1 << 20):  # 1 MiB chunks
                if chunk:
                    zf.write(chunk)

        logger.info(
            "fetch_administrative_boundaries: downloaded %d bytes to %s",
            os.path.getsize(tmp_zip),
            tmp_zip,
        )

        # 2. Unzip to a temp directory.
        tmp_dir = tempfile.mkdtemp(prefix="grace2_tiger_")
        try:
            with zipfile.ZipFile(tmp_zip, "r") as zf_obj:
                zf_obj.extractall(tmp_dir)
        except zipfile.BadZipFile as exc:
            raise AdminBoundaryUpstreamError(
                f"TIGER/Line ZIP is corrupt or not a ZIP: {url}: {exc}"
            ) from exc

        # 3. Find the .shp file in the extracted directory.
        shp_files = [
            os.path.join(tmp_dir, f)
            for f in os.listdir(tmp_dir)
            if f.endswith(".shp")
        ]
        if not shp_files:
            raise AdminBoundaryUpstreamError(
                f"No .shp file found after unzipping {url}"
            )
        shp_path = shp_files[0]

        # 4. Load shapefile with geopandas and clip to bbox.
        logger.info(
            "fetch_administrative_boundaries: reading %s", shp_path
        )
        try:
            gdf = gpd.read_file(shp_path, engine="pyogrio")
        except Exception as exc:  # noqa: BLE001
            raise AdminBoundaryUpstreamError(
                f"geopandas.read_file failed for {shp_path}: {exc}"
            ) from exc

        # Ensure CRS is EPSG:4326 (TIGER files are always in geographic coords but
        # check to be safe — a future TIGER release might ship in a different CRS).
        if gdf.crs is None or gdf.crs.to_epsg() != 4326:
            logger.warning(
                "fetch_administrative_boundaries: CRS is %s, reprojecting to EPSG:4326",
                gdf.crs,
            )
            try:
                gdf = gdf.to_crs(epsg=4326)
            except Exception as exc:  # noqa: BLE001
                raise AdminBoundaryUpstreamError(
                    f"CRS reprojection failed: {exc}"
                ) from exc

        # Clip to bbox using a shapely box as the mask.
        clip_geom = shapely_box(bbox[0], bbox[1], bbox[2], bbox[3])
        try:
            clipped = gdf[gdf.intersects(clip_geom)].copy()
        except Exception as exc:  # noqa: BLE001
            raise AdminBoundaryUpstreamError(
                f"geopandas bbox intersection failed: {exc}"
            ) from exc

        if clipped.empty:
            raise AdminBoundaryEmptyError(
                f"No {layer_name_hint} features intersect bbox={bbox}"
            )

        logger.info(
            "fetch_administrative_boundaries: %d feature(s) after clip to %s",
            len(clipped),
            bbox,
        )

        # 5. Write clipped features to FlatGeobuf via a temp file.
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="grace2_tiger_clip_"
        ) as fgb_f:
            tmp_fgb = fgb_f.name

        try:
            clipped.to_file(tmp_fgb, driver="FlatGeobuf", engine="pyogrio")
        except Exception as exc:  # noqa: BLE001
            raise AdminBoundaryUpstreamError(
                f"geopandas FlatGeobuf write failed: {exc}"
            ) from exc

        with open(tmp_fgb, "rb") as f:
            fgb_bytes = f.read()

        logger.info(
            "fetch_administrative_boundaries: FlatGeobuf = %d bytes", len(fgb_bytes)
        )
        return fgb_bytes

    except (AdminBoundaryError, AdminBoundaryUpstreamError, AdminBoundaryEmptyError):
        raise
    except Exception as exc:  # noqa: BLE001
        raise AdminBoundaryUpstreamError(
            f"unexpected error fetching {url}: {exc}"
        ) from exc
    finally:
        # Clean up all temp paths.
        for path in (tmp_zip, tmp_fgb):
            if path is None:
                continue
            try:
                os.unlink(path)
            except OSError:
                pass
        if tmp_dir is not None:
            import shutil as _shutil
            try:
                _shutil.rmtree(tmp_dir, ignore_errors=True)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Fetch function — builds the bytes callable for read_through.
# ---------------------------------------------------------------------------


def _fetch_admin_boundaries_bytes(
    level: str,
    bbox: tuple[float, float, float, float],
) -> bytes:
    """Download, clip, and serialize TIGER/Line boundaries for ``level``.

    For "place" (per-state), downloads all state ZIPs that intersect the bbox
    and merges the clipped features into a single FlatGeobuf. For all other
    levels, downloads the single nationwide ZIP.

    Raises:
        ``AdminBoundaryLevelError``: unknown level.
        ``AdminBoundaryUpstreamError``: download or I/O failure.
        ``AdminBoundaryEmptyError``: no features in the bbox.
    """
    if level not in _VALID_LEVELS:
        raise AdminBoundaryLevelError(
            f"unknown level={level!r}; allowed: {sorted(_VALID_LEVELS)}"
        )

    if level == "place":
        # Per-state: identify intersecting states, download each, merge.
        state_fips_list = _state_fips_for_bbox(bbox)
        if not state_fips_list:
            # No TIGER state envelope routes this bbox. This is NOT an upstream
            # (census.gov) failure -- nothing was even fetched -- so raise a
            # routing/input error with actionable guidance instead of a
            # misleading UPSTREAM error (the "goes18 vs goes-18" class of bug:
            # a heuristic-built routing token that does not match coverage and
            # dead-ends as a cryptic/wrong-category error).
            raise AdminBoundaryLevelError(
                f"bbox={bbox} is not routable to a TIGER state for level='place'; "
                "the per-state PLACE ZIPs require a bbox over US land within a "
                "state/territory envelope. Use level='county' (nationwide file, "
                "no per-state routing) or a bbox over CONUS/AK/HI/PR/VI."
            )

        try:
            import geopandas as gpd  # type: ignore[import-not-found]
        except ImportError as exc:
            raise AdminBoundaryUpstreamError(
                f"geopandas not available: {exc}"
            ) from exc

        parts: list[object] = []  # list[gpd.GeoDataFrame]
        for fips in state_fips_list:
            url = _tiger_url("place", state_fips=fips)
            logger.info(
                "fetch_administrative_boundaries: place fetch for state FIPS=%s", fips
            )
            # Download and clip individual state file to a temp FlatGeobuf.
            try:
                state_bytes = _download_and_clip_zip(
                    url, bbox, f"place/state={fips}"
                )
                # Load back from FlatGeobuf bytes to merge.
                import io as _io
                tmp_state: str | None = None
                try:
                    import tempfile as _tf
                    with _tf.NamedTemporaryFile(suffix=".fgb", delete=False) as tf:
                        tmp_state = tf.name
                        tf.write(state_bytes)
                    parts.append(gpd.read_file(tmp_state, engine="pyogrio"))
                finally:
                    if tmp_state:
                        try:
                            os.unlink(tmp_state)
                        except OSError:
                            pass
            except AdminBoundaryEmptyError:
                # This state file had no intersecting features — OK for multi-state
                # queries where the bbox clips into a state but misses all places.
                logger.debug(
                    "fetch_administrative_boundaries: no place features in state FIPS=%s",
                    fips,
                )

        if not parts:
            raise AdminBoundaryEmptyError(
                f"No place features intersect bbox={bbox} in any state"
            )

        # Merge all per-state GeoDataFrames.
        import tempfile as _tf
        merged = gpd.pd.concat(parts, ignore_index=True)  # type: ignore[attr-defined]
        if hasattr(merged, "set_crs"):
            # already a GeoDataFrame from concat
            merged_gdf = gpd.GeoDataFrame(merged, crs="EPSG:4326")
        else:
            merged_gdf = gpd.GeoDataFrame(merged, crs="EPSG:4326")

        tmp_merged: str | None = None
        try:
            with _tf.NamedTemporaryFile(suffix=".fgb", delete=False) as mf:
                tmp_merged = mf.name
            merged_gdf.to_file(tmp_merged, driver="FlatGeobuf", engine="pyogrio")
            with open(tmp_merged, "rb") as f:
                return f.read()
        except Exception as exc:  # noqa: BLE001
            raise AdminBoundaryUpstreamError(
                f"failed to merge/write place FlatGeobuf: {exc}"
            ) from exc
        finally:
            if tmp_merged:
                try:
                    os.unlink(tmp_merged)
                except OSError:
                    pass

    else:
        # Nationwide: state, county, zcta.
        url = _tiger_url(level)
        return _download_and_clip_zip(url, bbox, level)


# ---------------------------------------------------------------------------
# Registered atomic tool.
# ---------------------------------------------------------------------------


@register_tool(
    _METADATA,
    # Annotations: readOnlyHint=True (read-only; no state mutation),
    # openWorldHint=True (calls external public API endpoint),
    # destructiveHint=False, idempotentHint=True (cache shim deduplicates).
    open_world_hint=True,
)
def fetch_administrative_boundaries(
    level: Literal["state", "county", "place", "zcta"],
    bbox: tuple[float, float, float, float],
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """Fetch US Census TIGER/Line 2024 administrative-boundary polygons clipped to a bbox.

    **What it does:** Downloads the TIGER/Line 2024 shapefile for the requested
    administrative level from ``census.gov``, clips it to the requested bbox
    via geopandas, and returns a FlatGeobuf vector layer. Cached ``static-30d``
    (boundaries change at most once per census year). Four levels supported:
    state, county, place (cities/CDPs), and ZIP Code Tabulation Areas (zcta).

    **When to use:**
    - Agent needs administrative outlines for spatial context alongside a
      hazard layer (e.g. county boundaries over a flood inundation surface).
    - Workflow must aggregate or label results by jurisdiction — state, county,
      city, or ZIP code.
    - User asks for a geographic boundary before calling
      ``clip_raster_to_polygon`` or ``compute_zonal_statistics``.
    - ``geocode_location`` returned a bbox but the workflow needs the actual
      polygon for precise clipping.

    **When NOT to use:**
    - Parcel-level or cadastral boundaries (county assessor data; not in scope).
    - Congressional or voting districts (different TIGER layers, not added).
    - International administrative boundaries (TIGER is US + territories only).
    - Simple place-name → bbox resolution (use ``geocode_location`` instead).

    **Parameters:**
    - ``level`` (str): one of ``"state"`` (50 + DC + territories), ``"county"``
      (3000+ counties), ``"place"`` (cities/CDPs; per-state ZIPs; only states
      intersecting bbox are fetched), or ``"zcta"`` (ZIP Code Tabulation Areas;
      ~504 MB download — subsequent calls hit 30-day cache).
    - ``bbox`` (tuple): ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326.
      Example: ``(-82.2, 26.3, -81.5, 26.8)`` for Lee County FL.

    **Returns:**
    ``LayerURI(layer_type="vector", role="context", units=None)`` pointing at a
    FlatGeobuf with TIGER standard fields (GEOID, NAME, STATEFP, etc.) clipped
    to the requested bbox.

    **Cross-tool dependencies:**
    - Upstream of: ``clip_raster_to_polygon``, ``compute_zonal_statistics``,
      overlay display.
    - Pairs with: ``geocode_location`` (resolve name → bbox first), then
      ``fetch_administrative_boundaries`` for the actual polygon.
    - Feeds into: jurisdictional labeling in any flood/wildfire impact summary.
    """
    if level not in _VALID_LEVELS:
        raise AdminBoundaryLevelError(
            f"unknown level={level!r}; allowed: {sorted(_VALID_LEVELS)}"
        )
    _validate_bbox(bbox)

    # Quantize bbox to 6dp for cache-key stability (audit.md spec).
    q_bbox = _round_bbox_to_6dp(bbox)

    params = {
        "level": level,
        "bbox": list(q_bbox),
        "year": _TIGER_YEAR,
    }

    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="fgb",
        fetch_fn=lambda: _fetch_admin_boundaries_bytes(level, q_bbox),
    )
    assert result.uri is not None, (
        "fetch_administrative_boundaries is cacheable; uri must be set by read_through"
    )

    level_labels = {
        "state": "States",
        "county": "Counties",
        "place": "Places / CDPs",
        "zcta": "ZIP Code Tabulation Areas",
    }
    level_label = level_labels.get(level, level.capitalize())

    return LayerURI(
        layer_id=f"admin-{level}-{q_bbox[0]:.4f}-{q_bbox[1]:.4f}",
        name=f"Admin Boundaries — {level_label} (TIGER 2024)",
        layer_type="vector",
        uri=result.uri,
        style_preset="admin_boundaries",
        role="context",
        units=None,
    )
