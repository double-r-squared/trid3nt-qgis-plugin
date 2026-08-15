"""worldpop raster-delegate hooks: WorldPop whole-object-download-then-window.

Folds fetch_population's WorldPop raster leg onto the ``library_delegate`` raster
mode. The WorldPop server returns HTTP 200 (full body) for range requests instead of
HTTP 206, so GDAL ``/vsicurl/`` cannot windowed-read it and ``direct_window`` /
``multi_url`` (which rely on byte-range) do not apply. The delegate owns the one
network step: resolve the country file URL, download the whole (year, country)
GeoTIFF once, windowed-read the AOI via rasterio, and return ``(array, transform,
crs)`` for the shared COG writer -- byte-for-byte the twin's
``_fetch_worldpop_population_bytes`` fetch body, minus the twin's manual COG rewrite
(the router's ``array_to_cog_bytes`` owns serialization).

The ACS (Census B01003) leg the twin also carried is DROPPED at this fold (,
NATE flag-not-copy): it was half-built (geometry=None follow-up + heuristic FIPS
tables) and Census tract population is served by the dedicated ``fetch_census_acs``
tool. An ``acs_*`` (or any non-worldpop) dataset now fails the ``validate`` gate with
the standard typed input error naming only the WorldPop surface.

The companion ``validate`` hook is the twin's pre-cache input gate: it parses +
range-checks the vintage year off the ``worldpop_<YYYY>`` dataset token BEFORE any
network call (the ``goes18`` vs ``goes-18`` identifier-format norm), so an
out-of-range or non-worldpop dataset fails LOUD and offline-testably.
"""

from __future__ import annotations

import math
import os
import tempfile
from typing import Any

import requests

from trid3nt_contracts.source_spec import SourceSpec

from ..errors import router_empty_error, router_input_error, router_upstream_error
from . import register_hook

__all__ = [
    "validate_population",
    "read_population",
]

# --------------------------------------------------------------------------- #
# Vintage window + country-envelope table (byte-identical to the twin).
# --------------------------------------------------------------------------- #

#: The WorldPop Global_2000_2020 tree publishes only the vintages 2000..2020
#: inclusive; a year outside this window composes a well-formed URL into a
#: NON-EXISTENT path -> a bare HTTP 404. Normalize-then-VALIDATE the parsed year
#: against this range so an unknown vintage fails LOUD at parse time.
_WORLDPOP_MIN_YEAR = 2000
_WORLDPOP_MAX_YEAR = 2020

#: ISO3 -> approximate (min_lon, min_lat, max_lon, max_lat) envelope. Substrate-scope
#: CONUS-centric + Gulf/Caribbean coverage; a point-in-polygon over Natural Earth
#: admin0 is the follow-up. Same shape/role as the twin's table.
_WORLDPOP_BBOX_BY_ISO3: dict[str, tuple[float, float, float, float]] = {
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
    """Best-effort ISO3 country code lookup from a point -- heuristic only."""
    for iso3, (mn_lon, mn_lat, mx_lon, mx_lat) in _WORLDPOP_BBOX_BY_ISO3.items():
        if mn_lon <= lon <= mx_lon and mn_lat <= lat <= mx_lat:
            return iso3
    return None


def _worldpop_year_from_dataset(spec: SourceSpec, dataset: str) -> int:
    """Parse + validate the vintage year off a ``worldpop_<YEAR>`` dataset token.

    Raises the source's typed input error (pre-cache) when the token is not a
    ``worldpop_*`` dataset, the suffix is non-numeric, or the year falls outside
    the published Global_2000_2020 window. An ``acs_*`` dataset (the dropped leg)
    lands here as an unsupported-dataset input error.
    """
    sc = spec.error_code_prefix
    sfx = spec.input_error_suffix
    if not dataset.startswith("worldpop_"):
        raise router_input_error(
            sc,
            f"unsupported dataset={dataset!r}; fetch_population serves only WorldPop "
            "rasters (e.g. 'worldpop_2020'). For US Census tract population use "
            "fetch_census_acs.",
            sfx,
        )
    try:
        year = int(dataset.split("_", 1)[1])
    except (IndexError, ValueError) as exc:
        raise router_input_error(
            sc,
            f"could not parse vintage year from dataset={dataset!r}; expected "
            "'worldpop_YYYY'",
            sfx,
        ) from exc
    if not (_WORLDPOP_MIN_YEAR <= year <= _WORLDPOP_MAX_YEAR):
        raise router_input_error(
            sc,
            f"WorldPop dataset={dataset!r}: year {year} is outside the "
            f"Global_2000_2020 product range "
            f"[{_WORLDPOP_MIN_YEAR},{_WORLDPOP_MAX_YEAR}]; only those vintages are "
            "published in this tree (e.g. 'worldpop_2020')",
            sfx,
        )
    return year


def _worldpop_url_for(iso3: str, year: int, resolution_m: int = 1000) -> str:
    """Compose the WorldPop GeoTIFF URL for a country/year at a given resolution.

    Default (``resolution_m=1000``) uses the 1km-aggregated product (~50 MB/country);
    ``resolution_m <= 100`` opts into the native 100m UN-adjusted product (~4 GB
    whole-country download per cache miss).
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


# --------------------------------------------------------------------------- #
# validate: the pre-cache input gate (byte-identical to the twin's parse).
# --------------------------------------------------------------------------- #


@register_hook("worldpop.validate")
def validate_population(spec: SourceSpec, params: dict[str, Any]) -> None:
    """Parse + range-check the vintage year BEFORE read_through (offline-testable).

    Rejects a non-worldpop dataset (incl. the dropped ``acs_*`` leg) and an
    out-of-range / malformed vintage with the source's typed input error, so a
    malformed identifier never reaches the network as a bare 404.
    """
    dataset = str(params.get("dataset", "worldpop_2020"))
    _worldpop_year_from_dataset(spec, dataset)


# --------------------------------------------------------------------------- #
# read: whole-object download-then-window (the sanctioned delegate socket).
# --------------------------------------------------------------------------- #


@register_hook("worldpop.read")
def read_population(
    spec: SourceSpec, params: dict[str, Any], *, timeout_s: float
) -> tuple[Any, Any, Any]:
    """Download the WorldPop country GeoTIFF, windowed-read the AOI -> (array, transform, crs).

    Because the WorldPop server does not support HTTP range requests, the whole
    (year, country) file is downloaded once to a tmp path, then rasterio reads only
    the bbox window. Nodata is masked to NaN for the shared COG writer. Raises the
    source's typed empty error for an off-country / empty window and the typed
    upstream error for a download / read failure.
    """
    import numpy as np

    sc = spec.error_code_prefix
    bbox = tuple(float(v) for v in params["bbox"])
    dataset = str(params.get("dataset", "worldpop_2020"))
    target_resolution_m = int(params.get("target_resolution_m", 1000))

    year = _worldpop_year_from_dataset(spec, dataset)

    mid_lon = 0.5 * (bbox[0] + bbox[2])
    mid_lat = 0.5 * (bbox[1] + bbox[3])
    iso3 = _iso3_for_lonlat(mid_lon, mid_lat)
    if iso3 is None:
        raise router_upstream_error(
            sc,
            f"could not resolve ISO3 country code for bbox center=({mid_lon}, "
            f"{mid_lat}); WorldPop needs an envelope match for the country file URL",
        )

    url = _worldpop_url_for(iso3, year, target_resolution_m)

    try:
        import rasterio  # type: ignore[import-not-found]
        from rasterio.windows import Window, from_bounds  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise router_upstream_error(sc, f"rasterio unavailable: {exc}")

    src_tmp: str | None = None
    try:
        try:
            resp = requests.get(
                url,
                headers={"User-Agent": spec.auth.user_agent},
                timeout=timeout_s,
                stream=True,
                allow_redirects=True,
            )
            if resp.status_code == 404:
                raise router_empty_error(
                    sc,
                    f"WorldPop file not found at {url} (iso3={iso3}, year={year}); "
                    "verify dataset vintage availability",
                    spec.empty_error_suffix,
                )
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise router_upstream_error(sc, f"WorldPop download failed url={url}: {exc}")

        with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as src_f:
            src_tmp = src_f.name
            for chunk in resp.iter_content(chunk_size=1 << 20):
                if chunk:
                    src_f.write(chunk)

        with rasterio.open(src_tmp) as src:
            window = from_bounds(bbox[0], bbox[1], bbox[2], bbox[3], transform=src.transform)
            window = window.round_offsets().round_lengths()
            window = window.intersection(Window(0, 0, src.width, src.height))
            if window.width <= 0 or window.height <= 0:
                raise router_empty_error(
                    sc,
                    f"WorldPop window is empty for bbox={bbox} iso3={iso3} -- bbox may "
                    "not intersect the country file extent",
                    spec.empty_error_suffix,
                )
            data = src.read(1, window=window).astype("float32")
            window_transform = src.window_transform(window)
            crs = src.crs
            nodata = src.nodata
    finally:
        if src_tmp is not None:
            try:
                os.unlink(src_tmp)
            except OSError:
                pass

    if nodata is not None and not (isinstance(nodata, float) and math.isnan(nodata)):
        try:
            data = np.where(data == float(nodata), np.nan, data)
        except (TypeError, ValueError):
            pass
    return data, window_transform, crs
