"""Atomic tool ``compute_movement_trajectory`` — movement metrics from track POINTS (FR-TA-2, FR-CE-8).

This module registers one atomic tool that turns a layer of timestamped animal /
vehicle / asset track POINTS into an annotated movement-trajectory vector:

    ``compute_movement_trajectory(points_uri) -> LayerURI(layer_type="vector")``

The input is a point FlatGeobuf shaped like ``fetch_movebank_tracks`` with
``geometry_type="point"`` — one feature per telemetry fix, carrying a
``timestamp`` (ISO-8601) property and, optionally, an ``individual_id`` property
that separates one animal's track from another's. (Any point vector with a
parseable timestamp column works; the timestamp/individual column names are
auto-detected from a small set of common aliases.)

For each individual the fixes are ordered by timestamp and split into
consecutive **segments** (one LineString per adjacent pair of fixes). Each
segment is annotated with hand-rolled geodesic movement metrics:

    step_length_m   geodesic distance between the two fixes (WGS84 ellipsoid)
    duration_s      seconds between the two fix timestamps
    speed_mps       step_length_m / duration_s  (None when duration == 0)
    bearing_deg     forward azimuth of the segment, 0..360 (0 = north, 90 = east)
    turn_angle_deg  change in bearing vs the PREVIOUS segment, in (-180, 180]
                    (None for the first segment of each individual)

and each segment also carries its individual's whole-track **summary**:

    path_length_m       sum of all segment step lengths
    net_displacement_m  geodesic distance first-fix -> last-fix
    straightness        net_displacement_m / path_length_m  (0..1; 1 = a straight line)

The output is a FlatGeobuf of LineString segments in EPSG:4326 (the job-0175
inline-GeoJSON vector render path ships it to MapLibre). The per-individual
summary statistics are ALSO written to the layer ``metadata`` so a downstream
reader gets the headline numbers without re-aggregating the segments.

**CPU path.** Geodesic math is hand-rolled on ``pyproj.Geod`` (``ellps="WGS84"``)
— ``movingpandas`` is not in the agent venv, and the metric set here is small
and well-defined, so a dependency-free implementation is preferable and keeps
the result deterministic. ``geopandas`` / ``shapely`` (already on the box) read
the input and serialize the output.

**Honest empty.** An individual with < 2 timestamped fixes cannot form a
segment; it is dropped (with a logged count). If NO individual yields >= 1
segment (e.g. the input has < 2 points total, or no parseable timestamps), the
tool raises ``MovementTrajectoryError("INSUFFICIENT_POINTS", ...)`` rather than
emitting an empty layer that reads as success — a flat-out honest typed error
per the data-source-fallback norm.

**Cache.** Result FlatGeobuf cached under the FR-DC-3 shim at
``cache/static-30d/movement_trajectory/<key>.fgb``, keyed on
``(points_uri, individual_id_field, timestamp_field)``. ``static-30d`` because a
given input point layer is immutable for the life of its own cache entry.

**Cross-cutting invariants.**

- **Invariant 2 (Deterministic workflows): preserves.** Pure pyproj + numpy +
  geopandas pipeline; zero LLM calls.
- **FR-DC-6 (cacheable): honors.** ``cacheable=True``, ``ttl_class="static-30d"``,
  ``source_class="movement_trajectory"``.
- **NFR-R-1 (resilience): preserves.** Every failure surfaces as a typed
  ``MovementTrajectoryError`` with a SCREAMING_SNAKE_CASE ``error_code``; no
  silent dead-end and no empty-success.
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
from datetime import datetime, timezone
from typing import Any

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.cache import CACHE_BUCKET, read_through

__all__ = [
    "compute_movement_trajectory",
    "MovementTrajectoryError",
    "estimate_payload_mb",
]

logger = logging.getLogger("trid3nt_server.tools.processing.compute_movement_trajectory")


# ---------------------------------------------------------------------------
# Error class (NFR-R-1 typed-error surface)
# ---------------------------------------------------------------------------


class MovementTrajectoryError(RuntimeError):
    """Raised when trajectory-metric computation fails.

    ``error_code`` carries a SCREAMING_SNAKE_CASE code surfaced in the pipeline
    strip / function_response envelope.

    Codes:
    - ``DOWNLOAD_FAILED``      — the points layer could not be read from S3 / disk.
    - ``VECTOR_OPEN_FAILED``   — the points file could not be parsed as a vector.
    - ``NO_TIMESTAMP_FIELD``   — no timestamp-like column found in the points layer.
    - ``NOT_POINT_GEOMETRY``   — the layer is not point/multipoint geometry.
    - ``INSUFFICIENT_POINTS``  — no individual has >= 2 timestamped fixes (honest empty).
    - ``WRITE_FAILED``         — the annotated trajectory FlatGeobuf could not be written.
    """

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


# ---------------------------------------------------------------------------
# Tool metadata
# ---------------------------------------------------------------------------

_COMPUTE_TRAJECTORY_METADATA = AtomicToolMetadata(
    name="compute_movement_trajectory",
    ttl_class="static-30d",
    source_class="movement_trajectory",
    cacheable=True,
    payload_mb_estimator_name="estimate_payload_mb",
)


# ---------------------------------------------------------------------------
# Column-name auto-detection (tolerate Movebank + generic aliases)
# ---------------------------------------------------------------------------

#: Candidate timestamp column names, in priority order.
_TIMESTAMP_ALIASES: tuple[str, ...] = (
    "timestamp",
    "time",
    "datetime",
    "date_time",
    "t",
    "acquisition_time",
    "fix_time",
)

#: Candidate individual-identifier column names, in priority order.
_INDIVIDUAL_ALIASES: tuple[str, ...] = (
    "individual_id",
    "individual_local_identifier",
    "individual-local-identifier",
    "tag_local_identifier",
    "track_id",
    "id",
    "name",
)


def _pick_column(columns: list[str], explicit: str | None, aliases: tuple[str, ...]) -> str | None:
    """Return the first column matching ``explicit`` (exact) else an alias.

    ``explicit`` (when given) must exist or this returns ``None`` so the caller
    can raise a precise error. With no explicit name, the first present alias
    (case-insensitive) wins; ``None`` if none match.
    """
    if explicit is not None:
        for col in columns:
            if col == explicit:
                return col
        # explicit but absent -> let caller raise.
        return None
    lower = {c.lower(): c for c in columns}
    for alias in aliases:
        if alias.lower() in lower:
            return lower[alias.lower()]
    return None


# ---------------------------------------------------------------------------
# Timestamp parsing
# ---------------------------------------------------------------------------

_TS_FORMATS: tuple[str, ...] = (
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
)


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse a timestamp value into a UTC ``datetime``; ``None`` on failure.

    Accepts ``datetime`` / ``pandas.Timestamp`` objects directly, ISO-8601
    strings (with or without trailing ``Z``), and the Movebank
    ``YYYY-MM-DD HH:MM:SS.mmm`` form. Naive datetimes are assumed UTC.
    """
    if value is None:
        return None
    # Already a datetime-like (pandas Timestamp subclasses datetime).
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip()
        if not s or s.lower() in ("nan", "nat", "none", "null"):
            return None
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = None
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            for fmt in _TS_FORMATS:
                try:
                    dt = datetime.strptime(s, fmt)
                    break
                except ValueError:
                    continue
        if dt is None:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Geodesic metric helpers (hand-rolled on pyproj.Geod)
# ---------------------------------------------------------------------------


def _normalize_turn_angle(deg: float) -> float:
    """Normalize a bearing difference to the half-open interval (-180, 180].

    A +90 deg result means a right turn (clockwise); -90 a left turn. Exactly
    +/-180 maps to 180 (a complete reversal).
    """
    a = (deg + 180.0) % 360.0 - 180.0
    if a == -180.0:
        a = 180.0
    return a


def _compute_segments_for_individual(
    fixes: list[tuple[float, float, datetime]],
    geod: Any,
) -> tuple[list[dict[str, Any]], list[Any], dict[str, Any]] | None:
    """Compute per-segment rows + geometries + a summary for ONE individual.

    ``fixes`` is the individual's points as ``(lon, lat, timestamp)`` tuples,
    NOT necessarily sorted. Returns ``None`` if fewer than 2 fixes (no segment
    can be formed). Otherwise returns ``(seg_rows, seg_geoms, summary)``.
    """
    from shapely.geometry import LineString  # local import — keep module load light

    pts = sorted(fixes, key=lambda f: f[2])
    n = len(pts)
    if n < 2:
        return None

    seg_rows: list[dict[str, Any]] = []
    seg_geoms: list[Any] = []
    bearings: list[float] = []
    step_lengths: list[float] = []
    durations: list[float] = []
    speeds: list[float] = []

    for i in range(n - 1):
        lon0, lat0, t0 = pts[i]
        lon1, lat1, t1 = pts[i + 1]
        az_fwd, _az_back, dist_m = geod.inv(lon0, lat0, lon1, lat1)
        dt_s = (t1 - t0).total_seconds()
        speed = (dist_m / dt_s) if dt_s > 0 else None
        bearing = az_fwd % 360.0
        bearings.append(bearing)
        step_lengths.append(dist_m)
        durations.append(dt_s)
        if speed is not None:
            speeds.append(speed)
        seg_rows.append(
            {
                "seg_index": i,
                "step_length_m": round(float(dist_m), 4),
                "duration_s": round(float(dt_s), 3),
                "speed_mps": round(float(speed), 6) if speed is not None else None,
                "bearing_deg": round(float(bearing), 4),
                "turn_angle_deg": None,  # filled below
                "t_start": pts[i][2].isoformat(),
                "t_end": pts[i + 1][2].isoformat(),
            }
        )
        seg_geoms.append(LineString([(lon0, lat0), (lon1, lat1)]))

    # Turning angle = change in bearing vs previous segment, recorded on the
    # SECOND segment of each consecutive pair (first segment stays None).
    for i in range(1, len(seg_rows)):
        turn = _normalize_turn_angle(bearings[i] - bearings[i - 1])
        seg_rows[i]["turn_angle_deg"] = round(float(turn), 4)

    path_length = float(sum(step_lengths))
    _, _, net_disp = geod.inv(pts[0][0], pts[0][1], pts[-1][0], pts[-1][1])
    net_disp = float(net_disp)
    straightness = (net_disp / path_length) if path_length > 0 else None
    total_dur = float(sum(durations))
    turns = [r["turn_angle_deg"] for r in seg_rows if r["turn_angle_deg"] is not None]

    summary = {
        "n_points": n,
        "n_segments": len(seg_rows),
        "path_length_m": round(path_length, 4),
        "net_displacement_m": round(net_disp, 4),
        "straightness": round(float(straightness), 6) if straightness is not None else None,
        "duration_s": round(total_dur, 3),
        "mean_speed_mps": round(float(sum(speeds) / len(speeds)), 6) if speeds else None,
        "max_speed_mps": round(float(max(speeds)), 6) if speeds else None,
        "mean_step_length_m": (
            round(path_length / len(step_lengths), 4) if step_lengths else None
        ),
        "mean_abs_turn_deg": (
            round(float(sum(abs(t) for t in turns) / len(turns)), 4) if turns else None
        ),
        "t_start": pts[0][2].isoformat(),
        "t_end": pts[-1][2].isoformat(),
    }

    # Stamp the per-individual summary onto every segment so the layer is
    # self-describing even without reading the layer metadata.
    for r in seg_rows:
        r["path_length_m"] = summary["path_length_m"]
        r["net_displacement_m"] = summary["net_displacement_m"]
        r["straightness"] = summary["straightness"]

    return seg_rows, seg_geoms, summary


# ---------------------------------------------------------------------------
# Object-store materialization
# ---------------------------------------------------------------------------


def _materialize_points(points_uri: str, tmpdir: str) -> str:
    """Return a local file path for ``points_uri`` (download s3:// to a temp file).

    Raises ``MovementTrajectoryError("DOWNLOAD_FAILED", ...)`` on read failure.
    """
    if points_uri.startswith("s3://"):
        from trid3nt_server.tools.cache import read_object_bytes_s3

        name = points_uri.rstrip("/").rsplit("/", 1)[-1] or "points.fgb"
        sfx = ("." + name.rsplit(".", 1)[-1]) if "." in name else ".fgb"
        local = os.path.join(tmpdir, "points" + sfx)
        try:
            data = read_object_bytes_s3(points_uri)
        except Exception as exc:  # noqa: BLE001
            raise MovementTrajectoryError(
                "DOWNLOAD_FAILED",
                f"S3 download failed for points layer {points_uri!r}: {exc}",
            ) from exc
        with open(local, "wb") as f:
            f.write(data)
        return local
    if points_uri.startswith("gs://"):
        raise MovementTrajectoryError(
            "DOWNLOAD_FAILED",
            f"GCS is decommissioned; cannot read {points_uri!r}. Pass an s3:// "
            "URI or a local path.",
        )
    # Local path (test / dev convenience).
    if not os.path.exists(points_uri):
        raise MovementTrajectoryError(
            "DOWNLOAD_FAILED",
            f"points layer path does not exist: {points_uri!r}",
        )
    return points_uri


# ---------------------------------------------------------------------------
# Core compute (cache miss)
# ---------------------------------------------------------------------------


def _build_trajectory_fgb(
    points_uri: str,
    individual_id_field: str | None,
    timestamp_field: str | None,
) -> tuple[bytes, dict[str, Any]]:
    """Read the point layer, compute metrics, return (fgb_bytes, summary_dict).

    ``summary_dict`` carries a per-individual breakdown plus an overall rollup;
    it is attached to the LayerURI metadata.
    """
    import geopandas as gpd  # local import — keep module load light
    import pandas as pd
    from pyproj import Geod
    from shapely.geometry import LineString  # noqa: F401  (used in helper)

    geod = Geod(ellps="WGS84")

    with tempfile.TemporaryDirectory() as tmpdir:
        local = _materialize_points(points_uri, tmpdir)
        try:
            gdf = gpd.read_file(local)
        except Exception as exc:  # noqa: BLE001
            raise MovementTrajectoryError(
                "VECTOR_OPEN_FAILED",
                f"could not read points layer {points_uri!r} as a vector: {exc}",
            ) from exc

    if gdf.empty:
        raise MovementTrajectoryError(
            "INSUFFICIENT_POINTS",
            f"points layer {points_uri!r} has no features; need >= 2 timestamped "
            "fixes (with the same individual_id) to form a trajectory.",
        )

    # Ensure EPSG:4326 lon/lat (geodesic math + output render path both want it).
    try:
        if gdf.crs is not None and str(gdf.crs).upper() not in ("EPSG:4326", "WGS84"):
            gdf = gdf.to_crs("EPSG:4326")
    except Exception as exc:  # noqa: BLE001
        raise MovementTrajectoryError(
            "VECTOR_OPEN_FAILED",
            f"could not reproject points layer to EPSG:4326: {exc}",
        ) from exc

    # Geometry must be point-like.
    geom_types = set(gdf.geom_type.dropna().unique())
    if not geom_types.issubset({"Point", "MultiPoint"}):
        raise MovementTrajectoryError(
            "NOT_POINT_GEOMETRY",
            f"compute_movement_trajectory needs point geometry; layer "
            f"{points_uri!r} has geometry types {sorted(geom_types)}. Pass a "
            "point track (e.g. fetch_movebank_tracks geometry_type='point').",
        )

    columns = list(gdf.columns)
    ts_col = _pick_column(columns, timestamp_field, _TIMESTAMP_ALIASES)
    if ts_col is None:
        if timestamp_field is not None:
            raise MovementTrajectoryError(
                "NO_TIMESTAMP_FIELD",
                f"timestamp_field={timestamp_field!r} not found in points layer "
                f"columns {columns}.",
            )
        raise MovementTrajectoryError(
            "NO_TIMESTAMP_FIELD",
            f"no timestamp column found in points layer columns {columns}; "
            f"tried aliases {list(_TIMESTAMP_ALIASES)}. Pass timestamp_field "
            "explicitly.",
        )
    ind_col = _pick_column(columns, individual_id_field, _INDIVIDUAL_ALIASES)
    if individual_id_field is not None and ind_col is None:
        raise MovementTrajectoryError(
            "VECTOR_OPEN_FAILED",
            f"individual_id_field={individual_id_field!r} not found in points "
            f"layer columns {columns}.",
        )

    # Group fixes by individual (single bucket when no id column present).
    from collections import defaultdict

    grouped: dict[str, list[tuple[float, float, datetime]]] = defaultdict(list)
    skipped_bad_ts = 0
    skipped_bad_geom = 0
    for geom, row in zip(gdf.geometry, gdf.to_dict(orient="records")):
        if geom is None or geom.is_empty:
            skipped_bad_geom += 1
            continue
        # MultiPoint -> use its centroid / first point representative.
        try:
            if geom.geom_type == "MultiPoint":
                rep = list(geom.geoms)[0]
                lon, lat = float(rep.x), float(rep.y)
            else:
                lon, lat = float(geom.x), float(geom.y)
        except Exception:  # noqa: BLE001
            skipped_bad_geom += 1
            continue
        if not (math.isfinite(lon) and math.isfinite(lat)):
            skipped_bad_geom += 1
            continue
        ts = _parse_timestamp(row.get(ts_col))
        if ts is None:
            skipped_bad_ts += 1
            continue
        ind = str(row.get(ind_col)) if ind_col is not None else "track"
        grouped[ind].append((lon, lat, ts))

    if skipped_bad_ts:
        logger.info(
            "compute_movement_trajectory: skipped %d fix(es) with unparseable timestamps",
            skipped_bad_ts,
        )
    if skipped_bad_geom:
        logger.info(
            "compute_movement_trajectory: skipped %d fix(es) with bad/empty geometry",
            skipped_bad_geom,
        )

    all_seg_rows: list[dict[str, Any]] = []
    all_seg_geoms: list[Any] = []
    per_individual: dict[str, dict[str, Any]] = {}
    dropped_short: list[str] = []

    for ind, fixes in grouped.items():
        out = _compute_segments_for_individual(fixes, geod)
        if out is None:
            dropped_short.append(ind)
            continue
        seg_rows, seg_geoms, summary = out
        for r in seg_rows:
            r["individual_id"] = ind
        all_seg_rows.extend(seg_rows)
        all_seg_geoms.extend(seg_geoms)
        per_individual[ind] = summary

    if dropped_short:
        logger.info(
            "compute_movement_trajectory: dropped %d individual(s) with < 2 fixes: %s",
            len(dropped_short),
            dropped_short[:10],
        )

    if not all_seg_rows:
        # Honest typed empty — no individual produced a segment.
        raise MovementTrajectoryError(
            "INSUFFICIENT_POINTS",
            f"points layer {points_uri!r} yielded no trajectory segment: no "
            "individual has >= 2 fixes with parseable timestamps. (need >= 2 "
            "timestamped points per individual to compute movement metrics.)",
        )

    # Serialize segments to FlatGeobuf in EPSG:4326.
    seg_df = pd.DataFrame(all_seg_rows)
    # Stable column order for a clean schema.
    col_order = [
        "individual_id",
        "seg_index",
        "step_length_m",
        "duration_s",
        "speed_mps",
        "bearing_deg",
        "turn_angle_deg",
        "t_start",
        "t_end",
        "path_length_m",
        "net_displacement_m",
        "straightness",
    ]
    seg_df = seg_df[[c for c in col_order if c in seg_df.columns]]
    seg_gdf = gpd.GeoDataFrame(seg_df, geometry=all_seg_geoms, crs="EPSG:4326")

    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="trid3nt_trajectory_"
        ) as out_f:
            out_path = out_f.name
        try:
            seg_gdf.to_file(out_path, driver="FlatGeobuf", engine="pyogrio")
            with open(out_path, "rb") as f:
                fgb_bytes = f.read()
        finally:
            try:
                os.unlink(out_path)
            except OSError:
                pass
    except Exception as exc:  # noqa: BLE001
        raise MovementTrajectoryError(
            "WRITE_FAILED",
            f"could not write trajectory FlatGeobuf: {exc}",
        ) from exc

    # Layer extent for camera fly-to (best-effort).
    try:
        minx, miny, maxx, maxy = (float(v) for v in seg_gdf.total_bounds)
        bbox_4326 = (minx, miny, maxx, maxy)
    except Exception:  # noqa: BLE001
        bbox_4326 = None

    # Overall rollup across individuals.
    total_path = sum(s["path_length_m"] for s in per_individual.values())
    summary_meta: dict[str, Any] = {
        "n_individuals": len(per_individual),
        "n_segments": len(all_seg_rows),
        "total_path_length_m": round(float(total_path), 4),
        "per_individual": per_individual,
        "bbox": list(bbox_4326) if bbox_4326 is not None else None,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }

    logger.info(
        "compute_movement_trajectory: %d individual(s) -> %d segment(s), "
        "total_path=%.1fm",
        len(per_individual),
        len(all_seg_rows),
        total_path,
    )
    return fgb_bytes, summary_meta


# ---------------------------------------------------------------------------
# Payload estimator (FR-DC-9 / Wave-1.5 chat-warning gate)
# ---------------------------------------------------------------------------

#: A LineString segment FlatGeobuf feature with ~12 scalar attrs is small;
#: this is a coarse per-segment byte estimate for the advisory gate.
_BYTES_PER_SEGMENT_ESTIMATE = 320


def estimate_payload_mb(**args: Any) -> float:
    """Coarse payload estimate (MB) for the chat-warning gate.

    The true size depends on the INPUT point count, which is not known without
    reading the layer; the estimator is advisory only. We return a small fixed
    estimate (the output is geometry-light: one short LineString + ~12 numeric
    attributes per segment, and a typical animal track is hundreds-to-low-tens-
    of-thousands of fixes). The signature accepts ``**args`` per the Wave-1.5
    convention (the gate passes the tool kwargs unchanged).
    """
    # Assume a generous ~50k segments worst case before the user would ever be
    # surprised; 50k * 320 bytes ~= 15 MB.
    return float(50_000 * _BYTES_PER_SEGMENT_ESTIMATE) / (1024 * 1024)


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


@register_tool(
    _COMPUTE_TRAJECTORY_METADATA,
    payload_mb_estimator_name="estimate_payload_mb",
    # Annotations: readOnlyHint=True (reads the input point layer; writes a
    # cache artifact only via the read-through shim), openWorldHint=False (all
    # computation is local pyproj/geopandas — no external API call),
    # destructiveHint=False, idempotentHint=True (deterministic transform; the
    # same input points + field names always produce the same segments).
    read_only_hint=True,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
)
def compute_movement_trajectory(
    points_uri: str,
    individual_id_field: str | None = None,
    timestamp_field: str | None = None,
    *,
    _bucket: str | None = None,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """Compute movement-trajectory metrics from a layer of timestamped track POINTS.

    Use this (not fetch_dem) when you already have timestamped GPS/track POINTS and want movement metrics (speed, step length, turning angle).

    Takes a point vector (one feature per telemetry fix, carrying a timestamp)
    and returns an annotated **trajectory** vector: one LineString SEGMENT per
    adjacent pair of fixes, each labelled with movement metrics, plus a
    per-individual summary. Use it to quantify HOW an animal / vehicle / asset
    moved — speed, turning, path length, directness — from raw GPS fixes.

    When to use:
        - The user has animal-tracking fixes (e.g. ``fetch_movebank_tracks`` with
          ``geometry_type="point"``) and asks "how fast / how far / how straight
          did it move", "show its speed along the track", "compute step lengths /
          turning angles", or "trajectory metrics".
        - Deriving a movement-annotated path layer to drape on a hazard surface
          (e.g. migration speed vs a wildfire footprint, displacement vs flood
          extent).

    When NOT to use:
        - You only want the raw line of the track with no metrics — use
          ``fetch_movebank_tracks`` with ``geometry_type="linestring"``.
        - The points have no timestamp — movement metrics are undefined without
          time; this tool raises ``NO_TIMESTAMP_FIELD``.
        - Aggregating a raster within a zone — use ``compute_zonal_statistics``.

    Pairs with:
        - ``fetch_movebank_tracks`` (``geometry_type="point"``) — the canonical
          upstream point source.

    Params:
        points_uri: ``s3://`` URI or local path of a point vector (FlatGeobuf /
            GeoJSON / GPKG). Each feature is one timestamped fix. Typically
            ``fetch_movebank_tracks(..., geometry_type="point").uri``.
        individual_id_field: name of the column that separates one track from
            another (so multi-animal studies produce one trajectory per animal).
            When ``None`` the column is auto-detected from common aliases
            (``individual_id``, ``individual_local_identifier``, ``track_id``,
            ``id``, ...); if none is present, ALL points are treated as one track.
        timestamp_field: name of the timestamp column. When ``None`` it is
            auto-detected from common aliases (``timestamp``, ``time``,
            ``datetime``, ...). ISO-8601 and the Movebank
            ``YYYY-MM-DD HH:MM:SS.mmm`` form are both parsed.

    Returns:
        A ``LayerURI`` (``layer_type="vector"``, ``role="context"``,
        ``style_preset="movement_trajectory"``, ``units="m"``) pointing at a
        FlatGeobuf of LineString segments in EPSG:4326. Each segment feature
        carries:

            individual_id       (str)
            seg_index           (int)    0-based index within the individual
            step_length_m       (float)  geodesic length of the segment
            duration_s          (float)  seconds between the two fixes
            speed_mps           (float|null) step_length_m / duration_s
            bearing_deg         (float)  forward azimuth 0..360 (0=N, 90=E)
            turn_angle_deg      (float|null) change in bearing vs prior segment, (-180,180]
            t_start, t_end      (str)    ISO-8601 fix timestamps
            path_length_m       (float)  the individual's whole-track path length
            net_displacement_m  (float)  geodesic first-fix -> last-fix distance
            straightness        (float)  net_displacement_m / path_length_m (0..1)

        The LayerURI ``metadata`` carries the full per-individual summary
        (n_points, path_length_m, net_displacement_m, straightness, duration_s,
        mean/max speed, mean step length, mean abs turn angle) plus an overall
        rollup, so a downstream reader gets headline numbers without
        re-aggregating segments. ``bbox`` is set to the segment extent for
        camera fly-to.

    FR-CE-8: routed through ``read_through`` keyed on
    ``(points_uri, individual_id_field, timestamp_field)`` so repeat calls reuse
    the cached trajectory. TTL is 30 days (a given point layer is immutable for
    the life of its cache entry).

    Raises:
        MovementTrajectoryError: with a typed ``error_code`` when the points
            layer cannot be read (``DOWNLOAD_FAILED`` / ``VECTOR_OPEN_FAILED``),
            has no timestamp column (``NO_TIMESTAMP_FIELD``), is not point
            geometry (``NOT_POINT_GEOMETRY``), yields no segment because no
            individual has >= 2 timestamped fixes (``INSUFFICIENT_POINTS`` — the
            honest empty path), or cannot be written (``WRITE_FAILED``).
    """
    if not isinstance(points_uri, str) or not points_uri.strip():
        raise MovementTrajectoryError(
            "VECTOR_OPEN_FAILED",
            f"points_uri must be a non-empty string; got {points_uri!r}",
        )
    if individual_id_field is not None and not isinstance(individual_id_field, str):
        raise MovementTrajectoryError(
            "VECTOR_OPEN_FAILED",
            f"individual_id_field must be a string or None; got "
            f"{type(individual_id_field).__name__}",
        )
    if timestamp_field is not None and not isinstance(timestamp_field, str):
        raise MovementTrajectoryError(
            "NO_TIMESTAMP_FIELD",
            f"timestamp_field must be a string or None; got "
            f"{type(timestamp_field).__name__}",
        )

    effective_bucket = _bucket or CACHE_BUCKET

    # Capture the summary the fetch computed so the LayerURI metadata is accurate
    # even on a cache HIT (where _fetch does not run — we then read it from the
    # cached bytes' sibling is not available, so we re-attach a minimal note).
    captured: dict[str, Any] = {"summary": None}

    def _fetch() -> bytes:
        fgb_bytes, summary_meta = _build_trajectory_fgb(
            points_uri=points_uri,
            individual_id_field=individual_id_field,
            timestamp_field=timestamp_field,
        )
        captured["summary"] = summary_meta
        return fgb_bytes

    params = {
        "points_uri": points_uri,
        "individual_id_field": individual_id_field,
        "timestamp_field": timestamp_field,
    }

    result = read_through(
        metadata=_COMPUTE_TRAJECTORY_METADATA,
        params=params,
        ext="fgb",
        fetch_fn=_fetch,
        bucket=effective_bucket,
    )
    assert result.uri is not None, (
        "compute_movement_trajectory is cacheable; uri must be set by read_through"
    )

    summary_meta = captured["summary"]
    bbox_4326: tuple[float, float, float, float] | None = None
    if summary_meta is not None:
        b = summary_meta.get("bbox")
        if b and len(b) == 4:
            bbox_4326 = (float(b[0]), float(b[1]), float(b[2]), float(b[3]))
        # The per-segment + per-individual metrics live IN the layer features
        # (stamped onto each segment). The full summary is logged for the
        # session telemetry; it is not attached to LayerURI (extra="forbid").
        logger.info(
            "compute_movement_trajectory summary: %s",
            json.dumps(
                {k: v for k, v in summary_meta.items() if k != "per_individual"}
            ),
        )

    n_ind = (
        summary_meta.get("n_individuals") if summary_meta is not None else None
    )
    name = "Movement Trajectory"
    if n_ind:
        name = f"Movement Trajectory ({n_ind} track{'s' if n_ind != 1 else ''})"

    layer_id = (
        "movement-trajectory-"
        + (json.dumps(params, sort_keys=True).encode("utf-8").hex()[:16])
    )

    return LayerURI(
        layer_id=layer_id,
        name=name,
        layer_type="vector",
        uri=result.uri,
        style_preset="movement_trajectory",
        role="context",
        units="m",
        bbox=bbox_4326,
    )
