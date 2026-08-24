"""zip_vector executor: whole-object ZIP of a multi-file vector member.

A source that publishes its vectors as a ZIP-wrapped multi-file sidecar set (a
TIGER/Line shapefile: .shp + .dbf + .shx + .prj; an NHDPlus FileGDB directory)
cannot be served by a byte-range read -- geopandas needs every sibling co-located,
so the honest shape is a WHOLE-OBJECT GET + tmp-dir extract + read + spatial
filter (the ``gzip_object`` precedent at ZIP scale). The ``build_request`` hook is
the source's PURE URL planner (nationwide file, or per-state fan-out via a FIPS
routing table); this executor owns the I/O: it ``get_zip``s each plan through the
shared transport, ``extractall``s, reads the member with geopandas, filters to the
bbox (whole intersecting features), and (for a merge source) concatenates. The
per-source quirks (FIPS logic, place-merge) live in the hook; the download +
extract + read + filter + serialize is source-agnostic here.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..errors import RouterError, router_empty_error, router_upstream_error
from ..hooks import resolve_hook
from ..transport import (
    TransportError,
    TransportNotFound,
    get_client,
    get_zip,
)

logger = logging.getLogger(
    "trid3nt_server.tools.fetchers._router.executors.zip_vector"
)

__all__ = ["execute", "gdf_to_fgb_bytes"]


def gdf_to_fgb_bytes(gdf: Any, spec: SourceSpec) -> bytes:
    """Serialize a GeoDataFrame to FlatGeobuf bytes (pyogrio), preserving its schema."""
    tmp: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="trid3nt_router_zipvec_"
        ) as f:
            tmp = f.name
        gdf.to_file(tmp, driver="FlatGeobuf", engine="pyogrio")
        with open(tmp, "rb") as f:
            data = f.read()
        logger.info(
            "router.zip_vector: FlatGeobuf = %d bytes (%d feature(s), source=%s)",
            len(data), len(gdf), spec.source_class,
        )
        return data
    except RouterError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise router_upstream_error(
            spec.error_code_prefix, f"FlatGeobuf write failed ({len(gdf)} feature(s)): {exc}")
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _download_extract_read_filter(
    spec: SourceSpec, url: str, bbox: tuple[float, float, float, float],
    zv: dict[str, Any], ua: str,
) -> Any:
    """GET the ZIP whole-object, extract, read the member with geopandas, bbox-filter.

    Returns the filtered GeoDataFrame (whole features intersecting ``bbox``; the
    twin's ``gdf[gdf.intersects(box)]`` spatial filter, NOT a geometric clip).
    Raises the source-stamped EMPTY when no feature intersects, UPSTREAM on a
    missing/corrupt object or read failure.
    """
    import geopandas as gpd
    from shapely.geometry import box as shapely_box

    member_suffix = str(zv.get("member_pattern", ".shp"))
    layer = zv.get("layer")  # a FileGDB layer name (None for a shapefile)
    reproject_to = zv.get("reproject_to", "EPSG:4326")

    try:
        zf = get_zip(get_client(), url, headers={"User-Agent": ua})
    except TransportNotFound as exc:
        raise router_upstream_error(spec.error_code_prefix, f"file not found at {url}: {exc}")
    except TransportError as exc:
        raise router_upstream_error(spec.error_code_prefix, f"download failed url={url}: {exc}")
    except Exception as exc:  # noqa: BLE001 -- BadZipFile / non-ZIP body
        raise router_upstream_error(spec.error_code_prefix, f"ZIP is corrupt url={url}: {exc}")

    tmp_dir: str | None = None
    try:
        tmp_dir = tempfile.mkdtemp(prefix="trid3nt_router_zipvec_")
        zf.extractall(tmp_dir)
        if layer:
            # A FileGDB: find the .gdb directory, read the named layer.
            gdb_path = None
            for root, dirs, _files in os.walk(tmp_dir):
                for d in dirs:
                    if d.endswith(".gdb"):
                        gdb_path = os.path.join(root, d)
                        break
                if gdb_path:
                    break
            if gdb_path is None:
                raise router_upstream_error(
                    spec.error_code_prefix, f"no .gdb directory in extracted archive {url}")
            src_path, read_kwargs = gdb_path, {"layer": layer}
        else:
            members = [
                os.path.join(r, f)
                for r, _d, files in os.walk(tmp_dir)
                for f in files
                if f.endswith(member_suffix)
            ]
            if not members:
                raise router_upstream_error(
                    spec.error_code_prefix, f"no {member_suffix} member in {url}")
            src_path, read_kwargs = members[0], {}
        try:
            gdf = gpd.read_file(src_path, engine="pyogrio", **read_kwargs)
        except Exception as exc:  # noqa: BLE001
            raise router_upstream_error(
                spec.error_code_prefix, f"geopandas read failed for {src_path}: {exc}")

        if gdf.crs is None or gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs(reproject_to)
        mask = shapely_box(bbox[0], bbox[1], bbox[2], bbox[3])
        clipped = gdf[gdf.intersects(mask)].copy()
        if clipped.empty:
            raise router_empty_error(
                spec.error_code_prefix, f"no features intersect bbox={bbox}",
                spec.empty_error_suffix)
        return clipped
    finally:
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def execute(spec: SourceSpec, params: dict[str, Any]) -> bytes:
    """Fetch the ZIP-wrapped vector member(s) and serialize to FGB (the fetch_fn body).

    ``build_request`` (pure) plans the URL(s); a source whose current level is in
    ``ingest.zip_vector.merge_levels`` fans out over several URLs and merges them,
    skipping a per-URL EMPTY (a state that clips to no feature). A single-URL
    (nationwide) source propagates its EMPTY. All-empty over a merge -> EMPTY.
    """
    import geopandas as gpd
    import pandas as pd

    ingest = spec.ingest or {}
    zv = ingest.get("zip_vector", {})
    bbox = tuple(params["bbox"])
    ua = spec.auth.user_agent if spec.auth else "trid3nt_default"

    plans = resolve_hook(spec.hooks.build_request)(spec, params)  # type: ignore[union-attr]
    merge_mode = params.get(zv.get("merge_level_param", "level")) in set(
        zv.get("merge_levels", [])
    )

    parts: list[Any] = []
    for plan in plans:
        try:
            parts.append(_download_extract_read_filter(spec, plan.url, bbox, zv, ua))
        except RouterError as exc:
            if merge_mode and getattr(exc, "error_code", "").endswith(
                spec.empty_error_suffix
            ):
                logger.debug("router.zip_vector: %s clipped to no features; skipping", plan.url)
                continue
            raise

    if not parts:
        raise router_empty_error(
            spec.error_code_prefix, f"no features intersect bbox={bbox} in any source",
            spec.empty_error_suffix)
    merged = parts[0] if len(parts) == 1 else gpd.GeoDataFrame(
        pd.concat(parts, ignore_index=True), crs=parts[0].crs
    )
    return gdf_to_fgb_bytes(merged, spec)
