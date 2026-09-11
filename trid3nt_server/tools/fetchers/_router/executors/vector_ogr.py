"""ogr-vector executor: a published vector layer read through a GDAL vector driver.

One access mode over three source shapes: an ArcGIS query URL, an OGC API - Features
collection, or a vector member inside a remote ZIP. The driver owns the socket, the
paging and the decode; this module owns the spec vocabulary."""

from __future__ import annotations

import json
import logging
import threading
from typing import Any
from urllib.parse import urlencode

from trid3nt_contracts.source_spec import SourceSpec

from ..errors import (
    RouterError,
    router_empty_error,
    router_input_error,
    router_upstream_error,
)
from ..hooks import resolve_hook
from ..shape_classifier import classify_response
from ..transport import TransportError, get_client, get_once, is_staged_uri
from .vector_fgb import build_where, features_to_fgb_bytes, resolve_endpoints

logger = logging.getLogger(
    "trid3nt_server.tools.fetchers._router.executors.vector_ogr"
)

__all__ = [
    "build_query", "open_path", "zip_urls", "fetch_from_endpoint", "fetch_features",
    "execute",
]

#: The read policy for every driver read in this family. The retry half is the
#: transport's own code set; GDAL's default is retry OFF.
_READ_PATH = {
    "GDAL_HTTP_MAX_RETRY": "5",
    "GDAL_HTTP_RETRY_DELAY": "1",
    "GDAL_HTTP_RETRY_CODES": "429,500,502,503,504",
}

#: Config options are process-global in libgdal, so a per-source header set has
#: to be installed and withdrawn around its own read.
_config_lock = threading.Lock()


# pyogrio links its own libgdal, so the read policy is applied through
# ``pyogrio.set_gdal_config_options``; a ``rasterio.Env`` does not reach it. Retries
# fire on 429/500/502/503/504 and the upstream STATUS is verbatim on
# ``DataSourceError``. Two measured deviations from the transport's norm hold for
# this family: GDAL discards the S3 XML ``<Code>`` body, and its backoff is
# exponential-with-jitter rather than the server's ``Retry-After``.
class _ReadPolicy:
    """Install the read policy plus this source's headers for one driver read."""

    def __init__(self, spec: SourceSpec) -> None:
        ogr = (spec.ingest or {}).get("ogr") or {}
        headers = dict(ogr.get("headers") or {})
        ua = headers.pop("User-Agent", None) or (spec.auth.user_agent if spec.auth else None)
        self._opts: dict[str, str] = dict(_READ_PATH)
        if ua:
            self._opts["GDAL_HTTP_USERAGENT"] = str(ua)
        if headers:
            self._opts["GDAL_HTTP_HEADERS"] = "\r\n".join(
                f"{k}: {v}" for k, v in headers.items()
            )

    def __enter__(self) -> None:
        import pyogrio

        _config_lock.acquire()
        self._prior = {k: pyogrio.get_gdal_config_option(k) for k in self._opts}
        pyogrio.set_gdal_config_options(self._opts)

    def __exit__(self, *exc: Any) -> None:
        import pyogrio

        pyogrio.set_gdal_config_options(self._prior)
        _config_lock.release()




def build_query(
    spec: SourceSpec,
    bbox: tuple[float, float, float, float] | None,
    *,
    where: str = "1=1",
    endpoint: Any | None = None,
    page_size: int | None = None,
) -> str:
    """The ArcGIS ``/query`` URL the ESRIJSON driver opens, carrying no offset because
    paging is the driver's. ``bbox=None`` omits the geometry envelope for a global
    sweep, and ``page_size`` pins the ``resultRecordCount`` the driver carries."""
    ingest = spec.ingest or {}
    qt = ingest.get("query_template", {})
    if endpoint is None:
        endpoint = spec.endpoints.get("data") or next(iter(spec.endpoints.values()))
    url = endpoint.url or endpoint.url_template or ""
    if is_staged_uri(url):
        raise router_upstream_error(
            spec.error_code_prefix,
            f"a staged s3:// uri is not readable by the vector query executor: {url!r}",
        )
    q: dict[str, str] = {
        "where": where,
        "outFields": str(qt.get("out_fields", "*")),
        "outSR": "4326",
        "f": "json",
        "returnGeometry": "true",
    }
    order_by = qt.get("order_by")
    if order_by:
        q["orderByFields"] = str(order_by)
    if page_size is not None:
        q["resultRecordCount"] = str(page_size)
    if bbox is not None:
        min_lon, min_lat, max_lon, max_lat = bbox
        if ingest.get("geometry_envelope") == "json":
            q["geometry"] = json.dumps({
                "xmin": min_lon, "ymin": min_lat, "xmax": max_lon, "ymax": max_lat,
                "spatialReference": {"wkid": 4326},
            })
        else:
            q["geometry"] = f"{min_lon},{min_lat},{max_lon},{max_lat}"
            q["inSR"] = "4326"
        q["geometryType"] = "esriGeometryEnvelope"
        q["spatialRel"] = "esriSpatialRelIntersects"
    for k, v in (endpoint.query or {}).items():
        q[str(k)] = str(v)
    return f"{url}?{urlencode(q)}"


def open_path(spec: SourceSpec, url: str) -> str:
    """The driver-prefixed path for this source shape."""
    driver = str(((spec.ingest or {}).get("ogr") or {}).get("driver", "ESRIJSON"))
    if driver == "ESRIJSON":
        return f"ESRIJSON:{url}"
    if driver == "OAPIF":
        return f"OAPIF:{url}"
    if driver == "vsizip":
        # One layer per TIGER-shaped archive, so the member needs no naming; a
        # source that publishes several names the one it wants.
        member = str(((spec.ingest or {}).get("ogr") or {}).get("member", ""))
        return f"/vsizip//vsicurl/{url}/{member}" if member else f"/vsizip//vsicurl/{url}"
    raise router_input_error(
        spec.error_code_prefix, f"unknown ogr driver {driver!r}", spec.input_error_suffix
    )


def zip_urls(spec: SourceSpec, params: dict[str, Any], endpoint: Any) -> list[str]:
    """The ZIP URLs this request spans: the source's own planner, else the endpoint. A
    nationwide archive is one URL; a per-state one fans out over every state the bbox
    reaches, which is a routing table only the source holds."""
    if spec.hooks is not None and spec.hooks.build_request:
        return [p.url for p in resolve_hook(spec.hooks.build_request)(spec, params)]
    return [endpoint.url or endpoint.url_template or ""]


def _verbatim_upstream(spec: SourceSpec, url: str, exc: Exception) -> RouterError:
    """The upstream's own message for a read the driver could only call malformed, via
    ONE un-retried GET: the driver already spent this family's retry budget. Only a
    query URL is re-read, and an unrecognized body keeps the driver's own text."""
    driver = str(((spec.ingest or {}).get("ogr") or {}).get("driver", "ESRIJSON"))
    if driver != "ESRIJSON":
        return router_upstream_error(spec.error_code_prefix, f"read failed url={url}: {exc}")
    try:
        body, _status = get_once(get_client(), url)
        verdict = classify_response(body.decode("utf-8", "replace"))
    except (TransportError, Exception):  # noqa: BLE001 -- the driver's text stands
        return router_upstream_error(spec.error_code_prefix, f"read failed url={url}: {exc}")
    if verdict.kind == "error_envelope":
        return router_upstream_error(
            spec.error_code_prefix, f"error envelope url={url}: {verdict.error_message}"
        )
    return router_upstream_error(spec.error_code_prefix, f"read failed url={url}: {exc}")


def fetch_from_endpoint(
    spec: SourceSpec, endpoint: Any, params: dict[str, Any]
) -> list[dict[str, Any]]:
    """One driver read of ONE endpoint -> GeoJSON features."""
    import pyogrio

    ingest = spec.ingest or {}
    ogr = ingest.get("ogr") or {}
    driver = str(ogr.get("driver", "ESRIJSON"))
    bbox = params.get("bbox")
    bbox = tuple(bbox) if bbox is not None else None
    max_features = spec.gates.max_features or 30000
    # A caller may CAP the sweep at its own count, and that is a single-shot
    # request rather than a paging hint: the service page and the ceiling move
    # together, so the read returns exactly the cap in server order.
    capped = params.get("max_records")
    page_size = int(capped) if capped is not None else None
    if capped is not None:
        max_features = int(capped)

    read_kwargs: dict[str, Any] = {"max_features": max_features}
    if driver == "ESRIJSON":
        if page_size is None and ogr.get("page_size"):
            page_size = int(ogr["page_size"])
        urls = [build_query(
            spec, bbox, where=build_where(spec, params), endpoint=endpoint,
            page_size=page_size,
        )]
    else:
        if bbox is not None:
            read_kwargs["bbox"] = bbox
        if driver == "OAPIF":
            read_kwargs["layer"] = str(ogr.get("layer", ""))
            urls = [endpoint.url or endpoint.url_template or ""]
        else:
            urls = zip_urls(spec, params, endpoint)

    out: list[dict[str, Any]] = []
    for url in urls:
        try:
            with _ReadPolicy(spec):
                df = pyogrio.read_dataframe(open_path(spec, url), **read_kwargs)
        except RouterError:
            raise
        except Exception as exc:  # noqa: BLE001 -- pyogrio DataSourceError and kin
            raise _verbatim_upstream(spec, url, exc)
        if df.crs is not None and df.crs.to_epsg() != 4326:
            df = df.to_crs("EPSG:4326")
        out.extend(
            {"type": "Feature", "geometry": geom, "properties": props}
            for geom, props in zip(
                (None if g is None else g.__geo_interface__ for g in df.geometry),
                df.drop(columns=[df.geometry.name]).to_dict(orient="records"),
            )
        )
    logger.info(
        "router.vector_ogr: %s read %d feature(s) from %d url(s) (source=%s)",
        driver, len(out), len(urls), spec.source_class,
    )
    if not out and ogr.get("empty_is_typed_error"):
        # A published administrative archive covers what it covers: a bbox it does
        # not reach is out of coverage, not an empty answer about it.
        raise router_empty_error(
            spec.error_code_prefix, f"no features intersect bbox={bbox}",
            spec.empty_error_suffix,
        )
    return out


def fetch_features(spec: SourceSpec, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Fetch across the resolved endpoint chain. Every mirror publishes the SAME
    dataset, so the hop is silent under the loudness floor; a single endpoint is one
    read with no mirror."""
    chain = resolve_endpoints(spec, params)
    last_exc: Exception | None = None
    for i, endpoint in enumerate(chain):
        try:
            return fetch_from_endpoint(spec, endpoint, params)
        except Exception as exc:  # noqa: BLE001 -- try the next mirror
            last_exc = exc
            if i < len(chain) - 1:
                logger.warning(
                    "router.vector_ogr: endpoint %d/%d failed (%s); trying fallback",
                    i + 1, len(chain), exc,
                )
    assert last_exc is not None
    if len(chain) > 1:
        raise router_upstream_error(
            spec.error_code_prefix,
            f"all {len(chain)} endpoints failed; last error: {last_exc}",
        )
    raise last_exc


def execute(spec: SourceSpec, params: dict[str, Any]) -> bytes:
    """Read through the driver and serialize to FGB bytes (the ``fetch_fn`` body)."""
    return features_to_fgb_bytes(fetch_features(spec, params), spec, params)
