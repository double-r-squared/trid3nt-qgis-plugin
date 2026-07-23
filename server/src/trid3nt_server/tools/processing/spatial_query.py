"""``spatial_query`` atomic tool - read-only SQL over the Case's vector layers.

The DuckDB spatial-query fold (NATE option 1, Phase B): ONE SQL surface
replaces the three fixed-shape analytical Q&A tools
(``summarize_layer_statistics`` / ``count_features_above_threshold`` /
``aggregate_property_within_zone``, job-0224). Their fixed result shapes were
each a single SQL query; folding them into a general read-only SELECT surface
covers the same questions plus every group-by / spatial-join / multi-layer
variant the old trio could not express, while shrinking the LLM-visible
catalog by two declarations.

How it works:

1. ``layer_refs`` maps ``{alias: layer handle}``. The parameter is NAMED
   ``layer_refs`` deliberately: the ADR-0014 dispatch seam
   (``SessionUriRegistry.resolve_params`` -> ``NESTED_REF_PARAMS``) already
   resolves every string VALUE of a ``layer_refs`` dict from an ``L<n>`` /
   layer_id handle to the exact storage URI before the tool body runs - the
   tool inherits handle resolution with zero new resolver code.
2. Each resolved vector URI is exposed as a DuckDB VIEW named ``alias`` via
   the spatial extension's ``ST_Read`` (geometry column: ``geom``).
   ``s3://`` URIs try DuckDB httpfs against the MinIO env block first
   (AWS_ENDPOINT_URL / key env vars, the offline-first object store), then
   fall back to staging bytes locally through the shared boto3 reader
   (``cache.read_object_bytes_s3`` - the job-0289 instance-role-correct
   path). Local paths are read in place.
3. The user SQL runs under a READ-ONLY guard: exactly one statement, first
   keyword ``SELECT`` (or ``WITH``), and no write/side-effect keywords
   (INSERT / UPDATE / DELETE / CREATE / COPY / ATTACH / INSTALL / SET / ...)
   anywhere outside string literals and comments - an allowlist, not a
   best-effort blocklist.
4. Results are capped at ``_ROW_CAP`` rows and returned with columns + a
   compact LLM-facing summary. Bad SQL surfaces the DuckDB error message
   VERBATIM in a typed error so the FR-AS-11 retry loop can self-correct.
5. RESULT MATERIALIZATION (NATE directive: "show me all X in Y" must PAINT,
   not just tabulate). When the SELECT result carries a geometry column and
   more than zero rows, the TOOL (not the model's SQL - the read-only
   allowlist on the user statement is untouched) writes the FULL result set
   as FlatGeobuf, persists it under the runs bucket
   (``s3://$TRID3NT_RUNS_BUCKET/spatial_query/<ulid>.fgb`` via the same
   boto3/MinIO seam the solvers use), and returns a
   ``SpatialQueryLayerURI`` - a ``LayerURI`` subclass (the
   ``FloodDepthDamageLayerURI`` pattern) carrying the compact row summary +
   a small row preview. ``isinstance(result, LayerURI)`` holds, so the
   ADR-0014 dispatch seam mints an ``L<n>`` handle and the emit seam paints
   the layer. Geometry-less or empty results keep the v1 tabular dict.
   Export path: DuckDB ``COPY ... TO ... WITH (FORMAT gdal, DRIVER
   'FlatGeobuf')`` first, geopandas/pyogrio fallback when the gdal write
   fails (e.g. wide DECIMAL columns); a materialization failure degrades
   honestly to the tabular dict with a note in ``summary``.

Rasters are OUT OF SCOPE for v1: a raster ref raises a typed error naming
``compute_zonal_statistics`` and the ``code_exec_request`` Python playground
as the alternatives (analysis-is-playground norm).

Determinism (Invariant 1): pure DuckDB over already-fetched artifacts, no LLM
calls. Caching: ``ttl_class="live-no-cache"`` - the query is CPU-cheap and
depends on the LLM-supplied SQL verbatim. Honesty floor: every failure path
is a typed ``SpatialQueryError``; nothing is fabricated.

Offline-first note: the DuckDB ``spatial`` / ``httpfs`` extensions download
from extensions.duckdb.org on FIRST ``INSTALL`` and are cached under
``~/.duckdb/extensions/<version>/`` afterwards (subsequent LOADs are
offline). A truly cold offline box degrades honestly with
``EXTENSION_UNAVAILABLE``.
"""

from __future__ import annotations

import logging
import math
import os
import re
import tempfile
from datetime import datetime, timezone
from typing import Any

from trid3nt_contracts.common import new_ulid
from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool

__all__ = [
    "spatial_query",
    "SpatialQueryError",
    "SpatialQueryLayerURI",
]

logger = logging.getLogger("trid3nt_server.tools.processing.spatial_query")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Hard cap on returned rows (wire-size + function_response safety rail).
_ROW_CAP = 5000

#: Hard cap on a single stringified cell (geometry blobs, long text).
_MAX_CELL_CHARS = 300

#: Rows carried on a MATERIALIZED result as an LLM-facing preview (the full
#: result set lives in the FlatGeobuf layer; the wire stays compact).
_PREVIEW_ROWS = 10

#: Generic vector style preset (same one ``clip_vector_to_polygon`` uses).
_RESULT_STYLE_PRESET = "affected_buildings"

#: Extensions treated as raster (out of scope v1 - typed error).
_RASTER_EXTS = {".tif", ".tiff", ".img", ".vrt", ".nc"}

#: View alias must be a plain SQL identifier (quoted at CREATE VIEW time
#: anyway, but rejecting exotic names keeps the user SQL readable and blocks
#: quote-smuggling through the alias).
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: Keywords whose presence ANYWHERE in the (literal-stripped) SQL rejects the
#: statement. Allowlist stance: the only accepted statement is a single
#: SELECT / WITH..SELECT; these tokens have no legitimate place in one.
_FORBIDDEN_KEYWORDS = frozenset(
    {
        "insert",
        "update",
        "delete",
        "drop",
        "alter",
        "create",
        "copy",
        "attach",
        "detach",
        "install",
        "force",  # FORCE INSTALL
        "export",
        "import",
        "call",
        "pragma",
        "set",
        "reset",
        "grant",
        "vacuum",
        "checkpoint",
        "truncate",
        "begin",
        "commit",
        "rollback",
        "transaction",
        "use",
        "load",
    }
)

_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


# ---------------------------------------------------------------------------
# Typed error (NFR-R-1 / FR-AS-11 surface)
# ---------------------------------------------------------------------------


class SpatialQueryError(RuntimeError):
    """Raised when ``spatial_query`` cannot produce a result.

    ``error_code`` values:

    - ``SQL_NOT_ALLOWED``       - the read-only guard rejected the statement
      (not a single SELECT, or a forbidden keyword present). Not retryable
      with the same shape; the message says exactly what to change.
    - ``SQL_ERROR``             - DuckDB rejected the SQL; the DuckDB error
      message is carried VERBATIM so the LLM retry loop can self-correct
      (retryable).
    - ``BAD_LAYER_REF``         - ``layer_refs`` malformed (bad alias, non-string
      ref, unsupported scheme).
    - ``RASTER_UNSUPPORTED``    - a raster URI was referenced; v1 is vector-only.
    - ``LAYER_OPEN_FAILED``     - ``ST_Read`` could not open a resolved vector.
    - ``DOWNLOAD_FAILED``       - the s3/MinIO staging read failed.
    - ``EXTENSION_UNAVAILABLE`` - the DuckDB spatial extension could not be
      installed/loaded (cold offline box).
    - ``DUCKDB_UNAVAILABLE``    - the duckdb package itself is not importable.
    """

    def __init__(self, error_code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable


# ---------------------------------------------------------------------------
# Read-only guard
# ---------------------------------------------------------------------------


def _strip_literals_and_comments(sql: str) -> str:
    """Return ``sql`` with string literals, quoted identifiers and comments
    blanked out (replaced by spaces) so the keyword scan cannot be fooled by
    ``'DROP'`` inside a string or ``-- create`` in a comment.

    Handles: ``'...'`` (with ``''`` escapes), ``"..."`` quoted identifiers,
    ``-- line`` comments, ``/* block */`` comments (non-nested).
    """
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'" or ch == '"':
            quote = ch
            out.append(" ")
            i += 1
            while i < n:
                if sql[i] == quote:
                    if i + 1 < n and sql[i + 1] == quote:  # escaped '' / ""
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
        elif ch == "-" and i + 1 < n and sql[i + 1] == "-":
            while i < n and sql[i] != "\n":
                i += 1
        elif ch == "/" and i + 1 < n and sql[i + 1] == "*":
            i += 2
            while i + 1 < n and not (sql[i] == "*" and sql[i + 1] == "/"):
                i += 1
            i = min(i + 2, n)
            out.append(" ")
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _validate_read_only(sql: str) -> None:
    """Reject anything that is not a single SELECT / WITH..SELECT statement."""
    if not isinstance(sql, str) or not sql.strip():
        raise SpatialQueryError(
            "SQL_NOT_ALLOWED", "sql must be a non-empty SELECT statement."
        )
    stripped = _strip_literals_and_comments(sql).strip()
    # Single statement: at most one trailing semicolon.
    body = stripped.rstrip()
    if body.endswith(";"):
        body = body[:-1]
    if ";" in body:
        raise SpatialQueryError(
            "SQL_NOT_ALLOWED",
            "Only a SINGLE statement is allowed (found an inner ';'). "
            "Combine work into one SELECT (CTEs via WITH are fine).",
        )
    tokens = [t.lower() for t in _WORD_RE.findall(body)]
    if not tokens or tokens[0] not in ("select", "with"):
        raise SpatialQueryError(
            "SQL_NOT_ALLOWED",
            "Only read-only SELECT statements are allowed (the statement must "
            "start with SELECT or WITH).",
        )
    bad = sorted(set(tokens) & _FORBIDDEN_KEYWORDS)
    if bad:
        raise SpatialQueryError(
            "SQL_NOT_ALLOWED",
            f"Statement contains disallowed keyword(s) {bad}; spatial_query is "
            "READ-ONLY (single SELECT over the layer views; no DDL/DML/COPY/"
            "ATTACH/INSTALL/SET).",
        )


# ---------------------------------------------------------------------------
# DuckDB session + layer views
# ---------------------------------------------------------------------------


def _open_connection() -> Any:
    """In-memory DuckDB connection with the spatial extension loaded."""
    try:
        import duckdb
    except ImportError as exc:
        raise SpatialQueryError(
            "DUCKDB_UNAVAILABLE",
            "The duckdb package is not installed in this environment; "
            "spatial_query is unavailable. The code_exec_request Python "
            "playground (geopandas) covers the same vector analysis.",
        ) from exc
    con = duckdb.connect()
    try:
        con.execute("INSTALL spatial; LOAD spatial;")
    except Exception as exc:  # noqa: BLE001
        con.close()
        raise SpatialQueryError(
            "EXTENSION_UNAVAILABLE",
            f"The DuckDB spatial extension could not be installed/loaded: {exc}. "
            "On an offline box the extension must be pre-cached under "
            "~/.duckdb/extensions. The code_exec_request Python playground "
            "(geopandas) covers the same vector analysis.",
        ) from exc
    return con


def _try_configure_httpfs(con: Any) -> bool:
    """Best-effort httpfs + MinIO/S3 settings from the env block.

    Primary s3 path per the fold design: DuckDB httpfs reads ``s3://`` URIs
    directly (the spatial extension bridges DuckDB filesystems into GDAL, so
    ``ST_Read('s3://...')`` routes through these settings). Uses the same env
    the daemon's boto3 clients use - AWS_ENDPOINT_URL[_S3] (MinIO),
    AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_REGION. Returns False on
    any failure; the caller falls back to boto3 staging.
    """
    try:
        con.execute("INSTALL httpfs; LOAD httpfs;")
        endpoint = os.environ.get("AWS_ENDPOINT_URL_S3") or os.environ.get(
            "AWS_ENDPOINT_URL"
        )
        if endpoint:
            use_ssl = endpoint.startswith("https://")
            host = re.sub(r"^https?://", "", endpoint).rstrip("/")
            con.execute(f"SET s3_endpoint='{host}';")
            con.execute(f"SET s3_use_ssl={'true' if use_ssl else 'false'};")
            con.execute("SET s3_url_style='path';")
        access_key = os.environ.get("AWS_ACCESS_KEY_ID")
        secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY")
        if access_key and secret_key:
            con.execute(f"SET s3_access_key_id='{access_key}';")
            con.execute(f"SET s3_secret_access_key='{secret_key}';")
        region = os.environ.get("AWS_REGION")
        if region:
            con.execute(f"SET s3_region='{region}';")
        return True
    except Exception as exc:  # noqa: BLE001
        logger.info("spatial_query: httpfs unavailable (%s); using boto3 staging", exc)
        return False


def _stage_s3_locally(uri: str, tmpdir: str, alias: str) -> str:
    """Fallback s3 path: stage bytes via the shared boto3 reader."""
    from trid3nt_server.tools.cache import read_object_bytes_s3

    name = uri.rstrip("/").rsplit("/", 1)[-1] or f"{alias}.bin"
    local_path = os.path.join(tmpdir, f"{alias}_{name}")
    try:
        data = read_object_bytes_s3(uri)
    except Exception as exc:  # noqa: BLE001
        raise SpatialQueryError(
            "DOWNLOAD_FAILED",
            f"Object-store read failed for {uri!r}: {exc}",
            retryable=True,
        ) from exc
    with open(local_path, "wb") as f:
        f.write(data)
    return local_path


def _is_raster_uri(uri: str) -> bool:
    ext = os.path.splitext(uri.split("?")[0].rstrip("/"))[-1].lower()
    return ext in _RASTER_EXTS


def _create_view(con: Any, alias: str, target: str) -> None:
    quoted = target.replace("'", "''")
    con.execute(
        f'CREATE OR REPLACE VIEW "{alias}" AS SELECT * FROM ST_Read(\'{quoted}\')'
    )


def _attach_layer_views(
    con: Any, layer_refs: dict[str, str], tmpdir: str
) -> dict[str, str]:
    """Expose every resolved ref as a DuckDB view; return {alias: uri} used."""
    attached: dict[str, str] = {}
    httpfs_ready: bool | None = None  # probed lazily on the first s3 ref
    for alias, uri in layer_refs.items():
        if not isinstance(alias, str) or not _IDENT_RE.match(alias):
            raise SpatialQueryError(
                "BAD_LAYER_REF",
                f"layer_refs alias {alias!r} is not a valid SQL identifier "
                "([A-Za-z_][A-Za-z0-9_]*).",
            )
        if not isinstance(uri, str) or not uri.strip():
            raise SpatialQueryError(
                "BAD_LAYER_REF",
                f"layer_refs[{alias!r}] must be a layer handle / URI string; "
                f"got {uri!r}.",
            )
        uri = uri.strip()
        if _is_raster_uri(uri):
            raise SpatialQueryError(
                "RASTER_UNSUPPORTED",
                f"layer_refs[{alias!r}] -> {uri!r} is a RASTER; spatial_query "
                "v1 queries VECTOR layers only. For raster statistics use "
                "compute_zonal_statistics, or the code_exec_request Python "
                "playground (rasterio/numpy) for ad-hoc raster analysis.",
            )
        if uri.startswith("s3://"):
            if httpfs_ready is None:
                httpfs_ready = _try_configure_httpfs(con)
            if httpfs_ready:
                try:
                    _create_view(con, alias, uri)
                    attached[alias] = uri
                    continue
                except Exception as exc:  # noqa: BLE001
                    logger.info(
                        "spatial_query: httpfs ST_Read failed for %s (%s); "
                        "falling back to boto3 staging",
                        uri,
                        exc,
                    )
            local = _stage_s3_locally(uri, tmpdir, alias)
            _create_view_or_raise(con, alias, local, uri)
            attached[alias] = uri
            continue
        if "://" in uri:
            raise SpatialQueryError(
                "BAD_LAYER_REF",
                f"layer_refs[{alias!r}] has unsupported scheme in {uri!r}; "
                "pass the layer's L<n>/layer_id HANDLE (preferred - the server "
                "resolves it) or an s3:// / local-path URI.",
            )
        _create_view_or_raise(con, alias, uri, uri)
        attached[alias] = uri
    return attached


def _create_view_or_raise(con: Any, alias: str, target: str, display_uri: str) -> None:
    try:
        _create_view(con, alias, target)
    except SpatialQueryError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise SpatialQueryError(
            "LAYER_OPEN_FAILED",
            f"ST_Read could not open layer_refs alias {alias!r} "
            f"({display_uri!r}): {exc}",
        ) from exc


# ---------------------------------------------------------------------------
# Result shaping
# ---------------------------------------------------------------------------


def _json_safe(value: Any) -> Any:
    """Coerce a DuckDB cell to a compact JSON-safe value."""
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        if len(value) > _MAX_CELL_CHARS:
            return value[: _MAX_CELL_CHARS - 3] + "..."
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<blob {len(bytes(value))} bytes; use ST_AsText(geom) for WKT>"
    iso = getattr(value, "isoformat", None)
    if callable(iso):
        try:
            return iso()
        except Exception:  # noqa: BLE001
            pass
    text = str(value)
    if len(text) > _MAX_CELL_CHARS:
        text = text[: _MAX_CELL_CHARS - 3] + "..."
    return text


def _summarize(columns: list[str], rows: list[list[Any]], truncated: bool) -> str:
    """One compact line for the LLM (the honest headline, never invented)."""
    if not rows:
        return "0 rows (empty result)."
    if len(rows) == 1:
        pairs = ", ".join(f"{c}={v!r}" for c, v in zip(columns, rows[0]))
        if len(pairs) > 400:
            pairs = pairs[:397] + "..."
        return f"1 row: {pairs}"
    head = f"{len(rows)} rows x {len(columns)} columns ({', '.join(columns[:8])}"
    head += ", ..." if len(columns) > 8 else ""
    head += ")"
    if truncated:
        head += f"; TRUNCATED at the {_ROW_CAP}-row cap - aggregate or add LIMIT."
    return head


# ---------------------------------------------------------------------------
# Result materialization (geometry results paint, not just tabulate)
# ---------------------------------------------------------------------------


class SpatialQueryLayerURI(LayerURI):
    """A materialized query-result layer + the compact tabular summary.

    Returned INSTEAD of the tabular dict when the SELECT result carries a
    geometry column and >0 rows. Extends ``LayerURI`` field-for-field (the
    ``FloodDepthDamageLayerURI`` pattern) so the ADR-0014 dispatch seam mints
    an ``L<n>`` handle for the layer and the emit seam paints it, while the
    LLM narrates from typed fields:

    - ``columns`` / ``row_count`` / ``truncated`` / ``row_cap`` - the same
      tabular contract the dict path carries (``row_count`` is capped at
      ``row_cap``; the LAYER always carries the FULL result set).
    - ``preview_rows`` - the first few JSON-safe rows (never the full set).
    - ``feature_count`` - total rows written to the FlatGeobuf layer.
    - ``summary`` - the honest one-line headline incl. the layer reference.
    - ``layer_views`` - {alias: resolved source uri} provenance.
    - ``computed_at`` - iso8601 UTC stamp.
    """

    columns: list[str]
    row_count: int
    truncated: bool
    row_cap: int
    preview_rows: list[list[Any]]
    feature_count: int
    summary: str
    layer_views: dict[str, str]
    computed_at: str


def _sql_body(sql: str) -> str:
    """User SQL with any single trailing semicolon stripped, safe to embed as
    ``COPY (<body>) TO ...`` / ``FROM (<body>)`` (the guard already proved the
    statement is a single SELECT/WITH)."""
    body = sql.strip()
    if body.endswith(";"):
        body = body[:-1].rstrip()
    return body


def _geometry_columns(con: Any, body: str) -> list[str]:
    """Names of GEOMETRY-typed result columns (binder-only DESCRIBE; cheap).

    Best-effort: any failure returns ``[]`` so the caller keeps the tabular
    path (the query itself already succeeded).
    """
    try:
        described = con.execute(f"DESCRIBE {body}").fetchall()
        return [
            str(row[0])
            for row in described
            if str(row[1]).upper().startswith("GEOMETRY")
        ]
    except Exception:  # noqa: BLE001
        logger.info("spatial_query: DESCRIBE failed; skipping materialization", exc_info=True)
        return []


def _copy_via_duckdb_gdal(con: Any, body: str, out_path: str) -> None:
    """Tool-side DuckDB ``COPY (<body>) TO out_path`` as FlatGeobuf via GDAL.

    This is NOT the model's SQL - the read-only allowlist on the user
    statement is untouched; the tool composes and runs this COPY itself.
    ``SRS 'EPSG:4326'`` stamps the pipeline-wide CRS (every vector layer the
    tools produce/consume is EPSG:4326).
    """
    quoted = out_path.replace("'", "''")
    con.execute(
        f"COPY ({body}) TO '{quoted}' "
        "WITH (FORMAT gdal, DRIVER 'FlatGeobuf', SRS 'EPSG:4326')"
    )


def _export_via_geopandas(
    con: Any, body: str, geom_cols: list[str], out_path: str
) -> None:
    """Fallback FlatGeobuf export: WKB rewrite -> fetchdf -> geopandas/pyogrio.

    The first geometry column becomes THE layer geometry; any additional
    geometry columns are dropped (FlatGeobuf carries one geometry per
    feature). Raises on failure - the caller degrades to tabular.
    """
    import geopandas as gpd  # type: ignore[import-not-found]

    primary = geom_cols[0]
    replacements = ", ".join(f'ST_AsWKB("{c}") AS "{c}"' for c in geom_cols)
    df = con.execute(
        f"SELECT * REPLACE ({replacements}) FROM ({body}) AS __sq_sub"
    ).fetchdf()
    geometry = gpd.GeoSeries.from_wkb(
        df[primary].map(lambda v: bytes(v) if v is not None else None)
    )
    gdf = gpd.GeoDataFrame(
        df.drop(columns=geom_cols), geometry=geometry, crs="EPSG:4326"
    )
    gdf.to_file(out_path, driver="FlatGeobuf", engine="pyogrio")


def _export_fgb(con: Any, body: str, geom_cols: list[str], out_path: str) -> str:
    """Write the FULL result set as FlatGeobuf; return the export path used
    (``"duckdb-gdal"`` or ``"geopandas"``)."""
    if len(geom_cols) == 1:
        try:
            _copy_via_duckdb_gdal(con, body, out_path)
            return "duckdb-gdal"
        except Exception as exc:  # noqa: BLE001
            logger.info(
                "spatial_query: duckdb-gdal FlatGeobuf COPY failed (%s); "
                "falling back to geopandas export",
                exc,
            )
    # Multi-geometry results skip straight to geopandas (drops secondaries).
    _export_via_geopandas(con, body, geom_cols, out_path)
    return "geopandas"


def _result_count_and_bbox(
    con: Any, body: str, geom_col: str
) -> tuple[int | None, tuple[float, float, float, float] | None]:
    """(full row count, EPSG:4326 bbox) of the result set - best-effort."""
    try:
        q = (
            f'SELECT count(*), min(ST_XMin("{geom_col}")), '
            f'min(ST_YMin("{geom_col}")), max(ST_XMax("{geom_col}")), '
            f'max(ST_YMax("{geom_col}")) FROM ({body}) AS __sq_sub'
        )
        n, xmin, ymin, xmax, ymax = con.execute(q).fetchone()
        bbox = None
        coords = (xmin, ymin, xmax, ymax)
        if all(
            isinstance(v, (int, float)) and math.isfinite(float(v)) for v in coords
        ):
            bbox = tuple(round(float(v), 6) for v in coords)
        return int(n), bbox
    except Exception:  # noqa: BLE001
        logger.info("spatial_query: result count/bbox aggregate failed", exc_info=True)
        return None, None


def _persist_fgb(local_path: str, output_dir: str | None) -> str:
    """Persist the FlatGeobuf; return its URI.

    ``output_dir`` set (tests): copy into it and return the local path - the
    ``compute_flood_depth_damage._write_output`` local-file scheme. Otherwise
    upload to ``s3://$TRID3NT_RUNS_BUCKET/spatial_query/<ulid>.fgb`` through
    the solver's shared boto3 seam (MinIO env block / ``set_s3_client``).
    """
    filename = f"{new_ulid()}.fgb"
    if output_dir is not None:
        dest = os.path.join(output_dir, filename)
        with open(local_path, "rb") as src, open(dest, "wb") as out:
            out.write(src.read())
        return dest
    from trid3nt_server.tools.simulation.solver import (
        _get_runs_bucket,
        _get_s3_client,
    )

    bucket = _get_runs_bucket()
    key = f"spatial_query/{filename}"
    with open(local_path, "rb") as f:
        _get_s3_client().put_object(
            Bucket=bucket,
            Key=key,
            Body=f.read(),
            ContentType="application/octet-stream",
        )
    return f"s3://{bucket}/{key}"


def _materialize_result(
    con: Any,
    sql: str,
    tmpdir: str,
    *,
    columns: list[str],
    rows: list[list[Any]],
    truncated: bool,
    attached: dict[str, str],
    result_name: str | None,
    output_dir: str | None,
) -> "SpatialQueryLayerURI | None":
    """Materialize a geometry-bearing result set as a painted vector layer.

    Returns ``None`` when the result carries no geometry column (tabular
    path). Raises on export/upload failure - the caller degrades to tabular
    with an honest note.
    """
    body = _sql_body(sql)
    geom_cols = _geometry_columns(con, body)
    if not geom_cols:
        return None

    local_path = os.path.join(tmpdir, "spatial_query_result.fgb")
    export_path = _export_fgb(con, body, geom_cols, local_path)
    full_count, bbox = _result_count_and_bbox(con, body, geom_cols[0])
    feature_count = full_count if full_count is not None else len(rows)
    uri = _persist_fgb(local_path, output_dir)
    logger.info(
        "spatial_query: materialized %d feature(s) via %s -> %s",
        feature_count,
        export_path,
        uri,
    )

    name = (result_name or "").strip() or f"Query result ({feature_count} features)"
    summary = _summarize(columns, rows, truncated)
    summary += (
        f"; full result ({feature_count} features) written as vector layer "
        f"{name!r} and loaded on the map"
    )
    return SpatialQueryLayerURI(
        layer_id=f"spatial-query-{new_ulid().lower()}",
        name=name,
        layer_type="vector",
        uri=uri,
        style_preset=_RESULT_STYLE_PRESET,
        role="primary",
        units=None,
        bbox=bbox,
        columns=columns,
        row_count=len(rows),
        truncated=truncated,
        row_cap=_ROW_CAP,
        preview_rows=rows[:_PREVIEW_ROWS],
        feature_count=feature_count,
        summary=summary,
        layer_views=attached,
        computed_at=datetime.now(timezone.utc).isoformat(),
    )


# ---------------------------------------------------------------------------
# Tool metadata + registration
# ---------------------------------------------------------------------------

_METADATA = AtomicToolMetadata(
    name="spatial_query",
    ttl_class="live-no-cache",
    source_class=None,
    cacheable=False,
)


@register_tool(
    _METADATA,
    read_only_hint=True,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
)
def spatial_query(
    sql: str,
    layer_refs: dict[str, str] | None = None,
    result_name: str | None = None,
    *,
    _output_dir: str | None = None,
    **_extra_ignored: Any,
) -> dict[str, Any] | SpatialQueryLayerURI:
    """Summary statistics, counts, averages, sums and feature selection over
    the Case's VECTOR layers - one READ-ONLY SQL (DuckDB + spatial) surface.

    Use this for any quantitative or analytical question about vector layers
    (buildings, parcels, zones, features): summarize a layer's attribute
    statistics ("summary statistics for the building layer - min, max, mean,
    sum"), count features / answer "how many" ("how many buildings have flood
    depth over 1 meter"), average or total an attribute, aggregate within
    zones ("total replacement cost inside the flood-zone polygons"), spatial
    joins, group-bys, and multi-layer joins. Also SELECTS features: "show me
    all X in Y" queries whose result includes the geometry column return the
    matching features as a NEW painted map layer.

    Usage pattern: (1) fetch the data first - every ``layer_refs`` alias needs
    an existing layer HANDLE, so run the fetcher(s) that produce it before
    calling this tool; (2) if unsure which DuckDB spatial SQL function to use,
    call ``search_spatial_functions`` with a free-text ask (e.g. "distance
    between two points") to get the exact function name + signature; (3)
    compose the SQL and call this tool.

    Do NOT use for: rasters (v1 is vector-only - use compute_zonal_statistics
    or the code_exec_request playground); rendering an EXISTING layer
    (publish_layer); charts (generate_histogram / generate_time_series);
    looking up a DuckDB spatial function by name (use
    ``search_spatial_functions``, not this docstring).

    Params:
        sql: ONE read-only SELECT (WITH/CTEs allowed). Each layer_refs alias
            is a table (view); the geometry column is ``geom``. The full
            DuckDB ``spatial`` extension function set is available (ST_*) -
            call ``search_spatial_functions`` for an unfamiliar one rather
            than guessing. Results cap at 5000 rows.
        layer_refs: dict {alias: layer HANDLE}. Use the L<n>/layer_id handles
            from prior tool results (PREFERRED - the server resolves them to
            storage URIs; never construct s3:// paths by hand). Example:
            {"buildings": "L2", "zones": "L5"}.
        result_name: optional display name for the materialized result layer
            (geometry results only). Default: "Query result (<n> features)".

    Example queries (the folded analytical surface, as SQL):
        - Layer summary stats:
          SELECT count(*) AS n, min(value_usd), max(value_usd),
                 avg(value_usd), sum(value_usd) FROM buildings
        - Count features above a threshold ("how many buildings ..."):
          SELECT count(*) AS n_damaged FROM buildings WHERE damage_ratio >= 0.5
        - Select the matching features AS A MAP LAYER (keep geom in the
          SELECT list; the result paints):
          SELECT * FROM buildings WHERE flood_depth_m > 1.0
        - Aggregate a property within zone polygons (centroid-in-zone):
          SELECT sum(b.value_usd) AS total FROM buildings b, zones z
          WHERE ST_Within(ST_Centroid(b.geom), z.geom)
        - Spatial join, per-zone counts:
          SELECT z.zone_id, count(*) AS n FROM pts p JOIN zones z
          ON ST_Within(p.geom, z.geom) GROUP BY z.zone_id ORDER BY n DESC

    Returns:
        Geometry-less (or empty) results - the tabular dict:
        {"columns": [...], "rows": [[...], ...], "row_count": int,
         "truncated": bool, "row_cap": 5000, "summary": str,
         "layer_views": {alias: resolved_uri}, "computed_at": iso8601}

        Results carrying a geometry column with >0 rows - a
        ``SpatialQueryLayerURI``: the full result set is written as a
        FlatGeobuf vector layer (runs bucket) and PAINTED on the map; the
        response carries the layer reference plus the compact summary,
        ``columns``, ``feature_count`` and ``preview_rows`` (first
        10 rows) instead of the full row table.

    Raises:
        SpatialQueryError with a typed ``error_code``:
        SQL_NOT_ALLOWED (read-only guard - single SELECT only),
        SQL_ERROR (DuckDB's message verbatim - fix the SQL and retry),
        BAD_LAYER_REF / RASTER_UNSUPPORTED / LAYER_OPEN_FAILED /
        DOWNLOAD_FAILED / EXTENSION_UNAVAILABLE / DUCKDB_UNAVAILABLE.
    """
    _validate_read_only(sql)

    refs = layer_refs or {}
    if not isinstance(refs, dict):
        raise SpatialQueryError(
            "BAD_LAYER_REF",
            f"layer_refs must be a dict of alias -> layer handle; got "
            f"{type(refs).__name__}.",
        )

    con = _open_connection()  # typed DUCKDB_UNAVAILABLE / EXTENSION_UNAVAILABLE
    import duckdb  # importable - _open_connection succeeded; needed for except
    try:
        with tempfile.TemporaryDirectory(prefix="spatial_query_") as tmpdir:
            attached = _attach_layer_views(con, refs, tmpdir)

            try:
                cur = con.execute(sql)
                columns = [d[0] for d in (cur.description or [])]
                raw_rows = cur.fetchmany(_ROW_CAP + 1)
            except duckdb.Error as exc:
                # VERBATIM DuckDB message - the LLM retry loop self-corrects
                # from it (unknown column names, function typos, etc).
                raise SpatialQueryError("SQL_ERROR", str(exc), retryable=True) from exc

            truncated = len(raw_rows) > _ROW_CAP
            rows = [[_json_safe(v) for v in row] for row in raw_rows[:_ROW_CAP]]

            # Materialize geometry-bearing non-empty results as a painted
            # layer (still INSIDE the tmpdir block - staged s3 views + the
            # local FlatGeobuf live there). Empty / geometry-less results
            # fall through to the v1 tabular dict.
            materialize_note = ""
            if rows:
                try:
                    layer = _materialize_result(
                        con,
                        sql,
                        tmpdir,
                        columns=columns,
                        rows=rows,
                        truncated=truncated,
                        attached=attached,
                        result_name=result_name,
                        output_dir=_output_dir,
                    )
                except Exception as exc:  # noqa: BLE001 - degrade to tabular
                    logger.warning(
                        "spatial_query: result-layer materialization failed; "
                        "returning tabular only: %s",
                        exc,
                    )
                    materialize_note = (
                        "; result-layer materialization FAILED "
                        f"({str(exc)[:160]}) - tabular result only, no layer "
                        "was painted"
                    )
                else:
                    if layer is not None:
                        return layer

        return {
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "truncated": truncated,
            "row_cap": _ROW_CAP,
            "summary": _summarize(columns, rows, truncated) + materialize_note,
            "layer_views": attached,
            "computed_at": datetime.now(timezone.utc).isoformat(),
        }
    finally:
        con.close()
