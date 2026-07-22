"""``postprocess_pelicun`` — aggregate Pelicun per-feature damage FGB → ImpactEnvelope (Wave 4.11 P2).

This tool consumes the FlatGeobuf returned by ``run_pelicun_damage_assessment``
(a per-asset damage layer) and produces an aggregate ``ImpactEnvelope`` —
the portfolio-level damage / loss / population summary defined in
``trid3nt_contracts.impact_envelope`` (SRS Appendix B.6c, Decision N).

It is a **pure aggregation** step — it does NOT re-invoke Pelicun, sample new
hazard values, or make any network calls beyond the GCS read of the damage
FlatGeobuf.  Every numeric field on the returned envelope is a deterministic
aggregate computed from the source layer (Invariant 1).

Design reference: ``reports/inflight/wave-4-11-p2-postprocess-pelicun-design-20260609/design.md``

**Inputs**

- ``damage_layer_uri`` (str): ``gs://`` URI (or local path) to the FlatGeobuf
  emitted by ``run_pelicun_damage_assessment``. Required.
- ``flood_layer_uri`` (str | None): The hazard raster URI passed upstream to
  ``run_pelicun_damage_assessment``. Optional — carried forward into the
  envelope's ``flood_layer_uri`` provenance field. ``""`` is used when None.

**Output**

A dict produced by ``ImpactEnvelope.model_dump(mode="json")``.  Returning a
dict (rather than the pydantic model directly) keeps the ADK FunctionTool
contract simple and avoids serialization edge cases in the agent loop.

**Thresholds** (per design § 2.1 + ImpactEnvelope docstring):

- ``ds_mean >= 1.0`` ⇒ "damaged" (DS1+)
- ``ds_mean >= 3.5`` ⇒ "destroyed" (DS4)
- ``ds_mean >= 2.5`` ⇒ "at high risk" (DS3+ population)
- ``loss_ratio_mean >= 0.20`` ⇒ "displaced" (DS2+ population)

**Modal DS bins**: ``int(round(ds_mean))`` clipped to ``[0, 4]``.

**Structure inventory source** is inferred from the FGB columns: presence of
``pop2amu65`` ⇒ ``USACE_NSI``; otherwise ``MS_BUILDINGS``. The
``"USER_SUPPLIED"`` literal is reserved for the caller-supplied path but the
inference here only emits NSI or MS_BUILDINGS for v0.1 (per design OQ-P2-USER-SUPPLIED).

**Caching**: ``ttl_class="static-30d"``, ``source_class="pelicun_postprocess"``,
``cacheable=True``.  Cache key: ``sha256(damage_layer_uri | flood_layer_uri |
structure_inventory_source)[:32]``.  The cached artifact is the
``ImpactEnvelope`` JSON payload.

**Error envelope**: ``PelicunPostprocessError`` hierarchy parallels
``PelicunDamageError`` (see ``run_pelicun_damage_assessment``).
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import tempfile
from datetime import datetime, timezone
from typing import Any

import numpy as np

from trid3nt_contracts.impact_envelope import (
    ImpactEnvelope,
    OccupancyClassImpact,
)
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from . import register_tool

__all__ = [
    "postprocess_pelicun",
    "PelicunPostprocessError",
    "PelicunPostprocessInputError",
    "PelicunPostprocessIOError",
    "PelicunPostprocessEmptyError",
    "PelicunPostprocessSchemaError",
]


logger = logging.getLogger("trid3nt_server.tools.postprocess_pelicun")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 / NFR-R-1 typed-error surface).
# ---------------------------------------------------------------------------


class PelicunPostprocessError(RuntimeError):
    """Base class for ``postprocess_pelicun`` failures.

    ``error_code`` maps to the WebSocket A.6 error frame; ``retryable`` guides
    FR-AS-11 retry logic.
    """

    error_code: str = "POSTPROCESS_PELICUN_ERROR"
    retryable: bool = False


class PelicunPostprocessInputError(PelicunPostprocessError):
    """Bad input arguments (non-string URI, empty URI, etc.). Not retryable."""

    error_code = "POSTPROCESS_PELICUN_INPUT"
    retryable = False


class PelicunPostprocessIOError(PelicunPostprocessError):
    """GCS download or FlatGeobuf read failed. Retryable — transient I/O."""

    error_code = "POSTPROCESS_PELICUN_IO"
    retryable = True


class PelicunPostprocessEmptyError(PelicunPostprocessError):
    """The FlatGeobuf has zero features. Not retryable — input is well-formed
    but yields no work; the agent should pick a different damage layer."""

    error_code = "POSTPROCESS_PELICUN_EMPTY"
    retryable = False


class PelicunPostprocessSchemaError(PelicunPostprocessError):
    """Required columns (``ds_mean``, ``repair_cost_mean`` …) missing from
    the FlatGeobuf. Not retryable — schema mismatch indicates the upstream
    tool produced an incompatible artifact."""

    error_code = "POSTPROCESS_PELICUN_SCHEMA"
    retryable = False


# ---------------------------------------------------------------------------
# Thresholds + constants (sourced from the ImpactEnvelope schema docstring).
# ---------------------------------------------------------------------------


_DS_DAMAGED_THRESHOLD = 1.0       # ds_mean >= 1.0 ⇒ damaged (DS1+)
_DS_DESTROYED_THRESHOLD = 3.5     # ds_mean >= 3.5 ⇒ destroyed (DS4)
_DS_HIGH_RISK_THRESHOLD = 2.5     # ds_mean >= 2.5 ⇒ at high risk (DS3+)
_LR_DISPLACED_THRESHOLD = 0.20    # loss_ratio_mean >= 0.20 ⇒ displaced (DS2+)

# Required columns on the damage FlatGeobuf (per the upstream tool's
# documented output contract — see run_pelicun_damage_assessment.py:932-945).
_REQUIRED_COLUMNS: tuple[str, ...] = (
    "component_type_used",
    "ds_mean",
    "loss_ratio_mean",
    "repair_cost_mean",
    "repair_cost_p95",
    "replacement_value",
)

# DS-bin labels, matching the DamageStateKey Literal in impact_envelope.py.
_DS_LABELS: tuple[str, ...] = (
    "DS0_none",
    "DS1_slight",
    "DS2_moderate",
    "DS3_extensive",
    "DS4_complete",
)

# NSI population columns (present iff structure_inventory_source == USACE_NSI).
_NSI_POP_COLUMNS = ("pop2amu65", "pop2amo65")


# ---------------------------------------------------------------------------
# AtomicToolMetadata — registered once at import time.
# ---------------------------------------------------------------------------


_METADATA = AtomicToolMetadata(
    name="postprocess_pelicun",
    ttl_class="static-30d",
    source_class="pelicun_postprocess",
    cacheable=True,
    # Aggregation tool — global query is meaningless (it operates on a
    # specific damage layer URI), so opt out per layer_global_bbox_policy.
    supports_global_query=False,
)


# ---------------------------------------------------------------------------
# Validation helpers.
# ---------------------------------------------------------------------------


def _validate_uri(uri: object, field_name: str, *, required: bool) -> str:
    """Reject non-string / empty URIs; return canonicalized string."""
    if uri is None:
        if required:
            raise PelicunPostprocessInputError(
                f"{field_name} is required; got None."
            )
        return ""
    if not isinstance(uri, str):
        raise PelicunPostprocessInputError(
            f"{field_name} must be a string URI; got {type(uri).__name__}."
        )
    stripped = uri.strip()
    if not stripped:
        if required:
            raise PelicunPostprocessInputError(
                f"{field_name} must be a non-empty URI string."
            )
        return ""
    return stripped


# ---------------------------------------------------------------------------
# URI helpers (mirrors the pattern in run_pelicun_damage_assessment).
# ---------------------------------------------------------------------------


def _download_uri_to_local(
    uri: str, suffix: str, storage_client: Any | None = None
) -> str:
    """Download ``uri`` (``s3://`` or local path) into a NamedTemporaryFile.

    Returns the local path. Caller is responsible for unlinking when the input
    was remote. GCP is decommissioned: object-store reads route through boto3
    (S3); ``storage_client`` is retained for call-site compatibility but is
    ignored.

    Raises:
        ``PelicunPostprocessIOError`` on download / read failure.
    """
    del storage_client  # GCP decommissioned — S3/local only.
    # sprint-14-aws (job-0293b): s3:// staging via the shared boto3 reader
    # (NOT s3fs — instance-role lesson, job-0289). Stage to a
    # NamedTemporaryFile the caller unlinks.
    if uri.startswith("s3://"):
        from .cache import read_object_bytes_s3

        try:
            data = read_object_bytes_s3(uri)
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tf:
                tf.write(data)
                return tf.name
        except Exception as exc:  # noqa: BLE001
            raise PelicunPostprocessIOError(
                f"S3 download failed for {uri!r}: {exc}"
            ) from exc

    if not os.path.exists(uri):
        raise PelicunPostprocessIOError(
            f"local path does not exist: {uri!r}"
        )
    return uri


# ---------------------------------------------------------------------------
# Inventory-source inference.
# ---------------------------------------------------------------------------


def _infer_inventory_source(gdf_columns: list[str]) -> str:
    """Infer ``structure_inventory_source`` from the FGB column set.

    Per design § 4: presence of the NSI ``pop2amu65`` column is the canonical
    marker of an NSI-derived asset layer. If the column is absent, the upstream
    tool defaulted to the MS_BUILDINGS / synthetic-asset path.

    USER_SUPPLIED is NOT inferred — see design OQ-P2-USER-SUPPLIED; the v0.1
    inference treats it identically to MS_BUILDINGS (population fields None).
    Callers that need the USER_SUPPLIED literal must pass it explicitly when
    the tool wires through a future arg path.
    """
    if "pop2amu65" in gdf_columns and "pop2amo65" in gdf_columns:
        return "USACE_NSI"
    return "MS_BUILDINGS"


# ---------------------------------------------------------------------------
# Aggregation core.
# ---------------------------------------------------------------------------


def _check_required_columns(gdf: Any) -> None:
    """Validate the FGB carries every column the aggregation will read."""
    missing = [c for c in _REQUIRED_COLUMNS if c not in gdf.columns]
    if missing:
        raise PelicunPostprocessSchemaError(
            "damage FlatGeobuf is missing required columns: "
            f"{missing!r}. Got columns={list(gdf.columns)!r}. "
            "The upstream run_pelicun_damage_assessment tool produces these "
            "columns at the per-asset level (see its output contract docstring)."
        )


def _damage_state_distribution(
    ds_mean: np.ndarray, n_total: int
) -> dict[str, int]:
    """Bin ``ds_mean`` into the five DS labels.

    Modal DS = ``int(round(ds_mean)).clip(0, 4)``.  Returns a dict with all
    five DS labels present (zero counts included for missing bins so the
    contract dict is always full-shape).  Asserts the bin counts sum equals
    the total feature count — this is the design § 2.1 closure check.
    """
    arr = np.asarray(ds_mean, dtype=np.float64)
    # Invariant 7 (job-0300): a non-finite ds_mean (NaN/inf — only possible from a
    # malformed/foreign damage FGB) would survive .clip() and become INT64_MIN under
    # .astype(int), then index out of _DS_LABELS with a raw IndexError. Refuse to
    # fabricate a damage-state distribution from bad input — fail honestly instead.
    if arr.size and not np.all(np.isfinite(arr)):
        n_bad = int(np.count_nonzero(~np.isfinite(arr)))
        raise PelicunPostprocessSchemaError(
            f"{n_bad} of {arr.size} ds_mean values are non-finite (NaN/inf); refusing "
            "to fabricate a damage-state distribution from a malformed damage layer "
            "(Invariant 7). Re-run the Pelicun assessment to regenerate ds_mean."
        )
    modal = np.round(arr).clip(0, 4).astype(int)
    counts = {label: 0 for label in _DS_LABELS}
    if len(modal) > 0:
        unique, freq = np.unique(modal, return_counts=True)
        for u, f in zip(unique, freq, strict=False):
            counts[_DS_LABELS[int(u)]] = int(f)
    total = sum(counts.values())
    if total != n_total:
        raise PelicunPostprocessSchemaError(
            f"DS-bin closure failed: sum of damage_state_distribution "
            f"({total}) != n_structures_total ({n_total}). "
            "This indicates ds_mean values outside the expected [0, 4] range "
            "or NaN entries in the source FlatGeobuf."
        )
    return counts


def _population_fields(
    gdf: Any,
    source: str,
    *,
    ds_mean: np.ndarray,
    loss_ratio_mean: np.ndarray,
) -> tuple[int | None, int | None, int | None]:
    """Compute (population_total, population_displaced, population_at_high_risk).

    Returns ``(None, None, None)`` when ``source != USACE_NSI`` OR the NSI
    columns are missing.  When NSI confirmed, sums AM-population (under-65 +
    over-65) per feature with the design § 4 thresholds.
    """
    if source != "USACE_NSI":
        return None, None, None
    if not all(c in gdf.columns for c in _NSI_POP_COLUMNS):
        return None, None, None

    pop_under = np.asarray(gdf["pop2amu65"].fillna(0), dtype=np.float64)
    pop_over = np.asarray(gdf["pop2amo65"].fillna(0), dtype=np.float64)
    pop_total_per_row = pop_under + pop_over

    total = int(pop_total_per_row.sum())
    displaced_mask = loss_ratio_mean >= _LR_DISPLACED_THRESHOLD
    high_risk_mask = ds_mean >= _DS_HIGH_RISK_THRESHOLD
    displaced = int(pop_total_per_row[displaced_mask].sum())
    high_risk = int(pop_total_per_row[high_risk_mask].sum())
    return total, displaced, high_risk


def _per_class_breakdown(
    gdf: Any, source: str
) -> dict[str, OccupancyClassImpact]:
    """Group by ``component_type_used`` and compute OccupancyClassImpact per class.

    Population fields per-class follow the same source-gating as the top-level
    fields: None unless source is USACE_NSI and the columns exist.
    """
    out: dict[str, OccupancyClassImpact] = {}
    nsi_pop_available = source == "USACE_NSI" and all(
        c in gdf.columns for c in _NSI_POP_COLUMNS
    )

    # Group by the component_type_used column (the Pelicun-resolved class —
    # see design OQ-P2-GROUPING).
    grouped = gdf.groupby("component_type_used", dropna=False)
    for ctype, group in grouped:
        ctype_str = str(ctype) if ctype is not None else "UNKNOWN"
        ds = np.asarray(group["ds_mean"], dtype=np.float64)
        lr = np.asarray(group["loss_ratio_mean"], dtype=np.float64)
        rcm = np.asarray(group["repair_cost_mean"], dtype=np.float64)
        rcp95 = np.asarray(group["repair_cost_p95"], dtype=np.float64)

        n_total = int(len(group))
        n_dam = int((ds >= _DS_DAMAGED_THRESHOLD).sum())
        n_des = int((ds >= _DS_DESTROYED_THRESHOLD).sum())
        exp_loss = float(rcm.sum())
        p95_loss = float(rcp95.sum())

        if nsi_pop_available:
            pu = np.asarray(group["pop2amu65"].fillna(0), dtype=np.float64)
            po = np.asarray(group["pop2amo65"].fillna(0), dtype=np.float64)
            pop_per_row = pu + po
            pop_class: int | None = int(pop_per_row.sum())
            pop_displaced_class: int | None = int(
                pop_per_row[lr >= _LR_DISPLACED_THRESHOLD].sum()
            )
        else:
            pop_class = None
            pop_displaced_class = None

        out[ctype_str] = OccupancyClassImpact(
            n_structures=n_total,
            n_damaged=n_dam,
            n_destroyed=n_des,
            expected_loss_usd=exp_loss,
            loss_percentile_95_usd=p95_loss,
            population=pop_class,
            population_displaced=pop_displaced_class,
        )

    return out


def _convex_hull_area_km2(damaged_centroids: list[tuple[float, float]]) -> float:
    """Geodesic convex-hull area (km²) of a list of (lon, lat) damaged centroids.

    Per design § 5.1 — convex hull of DS1+ centroids, projected via
    ``pyproj.Geod`` (WGS84) so the area is in m² regardless of input CRS
    distortion.  Returns 0.0 when fewer than 3 points (no hull possible).
    """
    if len(damaged_centroids) < 3:
        return 0.0
    try:
        from shapely.geometry import MultiPoint  # type: ignore[import-not-found]
        from pyproj import Geod  # type: ignore[import-not-found]
    except ImportError as exc:
        raise PelicunPostprocessIOError(
            f"shapely / pyproj required for convex-hull area: {exc}"
        ) from exc

    try:
        mp = MultiPoint(damaged_centroids)
        hull = mp.convex_hull
        # If hull degenerates (collinear points), area is 0.
        if hull.geom_type not in ("Polygon", "MultiPolygon"):
            return 0.0
        geod = Geod(ellps="WGS84")
        area_m2, _perim = geod.geometry_area_perimeter(hull)
        return abs(float(area_m2)) / 1.0e6
    except Exception as exc:  # noqa: BLE001 — surface as IO error
        logger.warning("convex-hull area computation failed: %s", exc)
        return 0.0


# ---------------------------------------------------------------------------
# Cache-key helper.
# ---------------------------------------------------------------------------


def _cache_key(
    damage_layer_uri: str, flood_layer_uri: str, source: str
) -> str:
    """Per design § 7: sha256(damage_uri | flood_uri | source)[:32]."""
    seed = f"{damage_layer_uri}|{flood_layer_uri}|{source}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


def _pelicun_run_id_from_inputs(
    damage_layer_uri: str, flood_layer_uri: str
) -> str:
    """Derive a stable ULID-string from the input URIs.

    Per design § 6 + OQ-1: take sha256(damage_uri + flood_uri)[:16] as a
    16-byte ULID seed.  ``ulid.ULID.from_bytes`` accepts arbitrary 16-byte
    sequences (the timestamp prefix is just the first 6 bytes; we treat the
    full payload as the random component so identical inputs produce identical
    IDs across runs — cache-stable).
    """
    seed = hashlib.sha256(
        f"{damage_layer_uri}|{flood_layer_uri}".encode("utf-8")
    ).digest()[:16]
    try:
        from ulid import ULID  # type: ignore[import-not-found]
    except ImportError as exc:
        raise PelicunPostprocessIOError(
            f"python-ulid required for pelicun_run_id: {exc}"
        ) from exc

    return str(ULID.from_bytes(seed))


# ---------------------------------------------------------------------------
# Core aggregation — pure-Python, testable without GCS.
# ---------------------------------------------------------------------------


def _aggregate_gdf(
    gdf: Any,
    *,
    damage_layer_uri: str,
    flood_layer_uri: str,
    fragility_set: str = "hazus_flood_v6",
    realization_count: int = 100,
) -> ImpactEnvelope:
    """Compute every ImpactEnvelope field from a damage GeoDataFrame.

    Takes a geopandas GeoDataFrame (or a DataFrame-like object with the
    required columns + a ``geometry`` GeoSeries) and returns a validated
    ``ImpactEnvelope``.  Raises ``PelicunPostprocessEmptyError`` on
    zero-feature input; ``PelicunPostprocessSchemaError`` on missing columns.
    """
    if len(gdf) == 0:
        raise PelicunPostprocessEmptyError(
            "damage FlatGeobuf has zero features. The upstream run_pelicun_"
            "damage_assessment call may have raised PELICUN_NO_ASSETS_IN_HAZARD "
            "and not produced a layer."
        )

    _check_required_columns(gdf)

    columns_list = list(gdf.columns)
    source = _infer_inventory_source(columns_list)

    ds_mean = np.asarray(gdf["ds_mean"], dtype=np.float64)
    loss_ratio_mean = np.asarray(gdf["loss_ratio_mean"], dtype=np.float64)
    repair_cost_mean = np.asarray(gdf["repair_cost_mean"], dtype=np.float64)
    repair_cost_p95 = np.asarray(gdf["repair_cost_p95"], dtype=np.float64)
    replacement_value = np.asarray(gdf["replacement_value"], dtype=np.float64)

    n_total = int(len(gdf))
    n_damaged = int((ds_mean >= _DS_DAMAGED_THRESHOLD).sum())
    n_destroyed = int((ds_mean >= _DS_DESTROYED_THRESHOLD).sum())

    damage_state_distribution = _damage_state_distribution(ds_mean, n_total)

    expected_loss_usd = float(repair_cost_mean.sum())
    loss_percentile_95_usd = float(repair_cost_p95.sum())
    total_replacement_value_usd = float(replacement_value.sum())
    damaged_mask = ds_mean >= _DS_DAMAGED_THRESHOLD
    damaged_replacement_value_usd = float(replacement_value[damaged_mask].sum())

    pop_total, pop_displaced, pop_high_risk = _population_fields(
        gdf, source, ds_mean=ds_mean, loss_ratio_mean=loss_ratio_mean
    )

    # Spatial summary — bbox from full layer; impact_area_km2 from damaged
    # centroids' convex hull.
    bbox = _bbox_from_gdf(gdf)
    damaged_centroids = _damaged_centroids(gdf, damaged_mask)
    impact_area_km2 = _convex_hull_area_km2(damaged_centroids)

    per_class = _per_class_breakdown(gdf, source)

    pelicun_run_id = _pelicun_run_id_from_inputs(
        damage_layer_uri, flood_layer_uri
    )

    # Invariant 7 (job-0300): count assets whose loss figure rests on a HAZUS
    # class-default replacement value (NSI lacked a usable val_struct, or the
    # MS-buildings path which is default-by-design) so the envelope surfaces how
    # much of expected_loss_usd is default-based rather than measured. Column
    # absent on legacy/foreign FGBs -> 0 (an honest "not tracked").
    if "replacement_value_defaulted" in getattr(gdf, "columns", []):
        n_default_rv = int(
            np.asarray(gdf["replacement_value_defaulted"]).astype(bool).sum()
        )
    else:
        n_default_rv = 0

    envelope = ImpactEnvelope(
        n_structures_total=n_total,
        n_structures_damaged=n_damaged,
        n_structures_destroyed=n_destroyed,
        damage_state_distribution=damage_state_distribution,
        total_replacement_value_usd=total_replacement_value_usd,
        damaged_replacement_value_usd=damaged_replacement_value_usd,
        expected_loss_usd=expected_loss_usd,
        loss_percentile_95_usd=loss_percentile_95_usd,
        population_total=pop_total,
        population_displaced=pop_displaced,
        population_at_high_risk=pop_high_risk,
        impact_area_km2=impact_area_km2,
        bbox=bbox,
        by_occupancy_class=per_class,
        pelicun_run_id=pelicun_run_id,
        damage_layer_uri=damage_layer_uri,
        structure_inventory_source=source,  # type: ignore[arg-type]
        flood_layer_uri=flood_layer_uri or "n/a",
        fragility_set=fragility_set,
        realization_count=realization_count,
        n_assets_default_replacement_value=n_default_rv,
        generated_at=datetime.now(timezone.utc),
    )
    return envelope


def _bbox_from_gdf(gdf: Any) -> tuple[float, float, float, float]:
    """Return (minLon, minLat, maxLon, maxLat) from the gdf's total_bounds.

    Best-effort reprojects to EPSG:4326 if the CRS is set and differs.  Falls
    back to the raw total_bounds when the CRS is None (caller bears the cost
    of the assumption — matches the run_pelicun_damage_assessment convention
    which falls back to EPSG:4326).
    """
    try:
        crs = getattr(gdf, "crs", None)
        if crs is not None and str(crs).upper() not in (
            "EPSG:4326",
            "EPSG: 4326",
            "WGS 84",
            "WGS84",
        ):
            try:
                gdf_4326 = gdf.to_crs("EPSG:4326")
            except Exception:  # noqa: BLE001 — fall back to raw bounds
                gdf_4326 = gdf
        else:
            gdf_4326 = gdf
        bounds = gdf_4326.total_bounds  # [minx, miny, maxx, maxy]
        minx, miny, maxx, maxy = (
            float(bounds[0]),
            float(bounds[1]),
            float(bounds[2]),
            float(bounds[3]),
        )
        # Guard against NaN bounds (single-point degenerate).
        if not all(math.isfinite(v) for v in (minx, miny, maxx, maxy)):
            return (-180.0, -90.0, 180.0, 90.0)
        return (minx, miny, maxx, maxy)
    except Exception as exc:  # noqa: BLE001
        logger.warning("bbox extraction failed: %s; defaulting to world bbox", exc)
        return (-180.0, -90.0, 180.0, 90.0)


def _damaged_centroids(
    gdf: Any, damaged_mask: np.ndarray
) -> list[tuple[float, float]]:
    """Return (lon, lat) centroids for damaged features only.

    Reprojects to EPSG:4326 if needed.  Returns an empty list when no damaged
    features exist (impact_area_km2 will then be 0.0 per design § 5.1).
    """
    try:
        damaged_gdf = gdf[damaged_mask]
        if len(damaged_gdf) == 0:
            return []
        crs = getattr(damaged_gdf, "crs", None)
        if crs is not None and str(crs).upper() not in (
            "EPSG:4326",
            "EPSG: 4326",
            "WGS 84",
            "WGS84",
        ):
            try:
                damaged_gdf = damaged_gdf.to_crs("EPSG:4326")
            except Exception:  # noqa: BLE001
                pass
        centroids = damaged_gdf.geometry.centroid
        out: list[tuple[float, float]] = []
        for pt in centroids:
            if pt is None or pt.is_empty:
                continue
            out.append((float(pt.x), float(pt.y)))
        return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("damaged-centroid extraction failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Registered atomic tool.
# ---------------------------------------------------------------------------


@register_tool(
    _METADATA,
    # MCP annotations: read-only (no GCS writes — the cache shim is bypassed
    # for the v0.1 wiring; the envelope is returned in-process and written by
    # the caller's persistence layer), closed-world (no external API),
    # non-destructive, idempotent (deterministic aggregation of a fixed FGB).
    read_only_hint=True,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
)
async def postprocess_pelicun(
    damage_layer_uri: str,
    flood_layer_uri: str | None = None,
    # sprint-14-aws (M5.5): provenance threading. When the composer knows the
    # fragility set / realization count it actually ran upstream, it passes
    # them through so the envelope's provenance reflects the run that happened
    # rather than the hardcoded defaults. Default to None -> _aggregate_gdf's
    # back-compatible constants (hazus_flood_v6 / 100) so existing callers and
    # the LLM-facing surface are unchanged.
    fragility_set: str | None = None,
    realization_count: int | None = None,
    # job-0164: absorb LLM-invented kwargs (Tool argument normalizer ratchet).
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Aggregate a Pelicun per-asset damage FlatGeobuf into an ImpactEnvelope.

    Use this when:
        - You have just called ``run_pelicun_damage_assessment`` and need
          portfolio-level totals (n_structures_damaged, expected_loss_usd,
          population_displaced, etc.) for narration or the Case summary panel.
        - The user asks "how much damage" / "how many displaced" / "what's the
          expected loss" after a Pelicun damage run.

    Do NOT use this for:
        - Per-feature damage queries (read the FlatGeobuf directly; this tool
          collapses every per-feature column into an aggregate).
        - Recomputing damage with different inputs (call
          ``run_pelicun_damage_assessment`` instead).
        - Plain exposure counts without Pelicun (use
          ``compute_zonal_statistics``).

    Parameters:
        damage_layer_uri: gs:// URI (or local path) to the FlatGeobuf returned
            by ``run_pelicun_damage_assessment``. Required.
        flood_layer_uri: gs:// URI (or local path) to the hazard raster that
            was the upstream ``hazard_raster_uri``. Optional — carried forward
            for provenance only.
        fragility_set: the fragility set actually used in the upstream Pelicun
            run (e.g. ``"hazus_flood_v6"``). Optional — when omitted the
            envelope reports the back-compatible default. The composer threads
            the real value so the envelope provenance reflects the run.
        realization_count: the Monte-Carlo realization count actually used
            upstream. Optional — defaults to the back-compatible 100.

    Returns:
        A dict (``ImpactEnvelope.model_dump(mode="json")``) carrying:

        - ``n_structures_total`` / ``n_structures_damaged`` / ``n_structures_destroyed``
        - ``damage_state_distribution`` (DS0..DS4 counts)
        - ``expected_loss_usd`` / ``loss_percentile_95_usd``
        - ``total_replacement_value_usd`` / ``damaged_replacement_value_usd``
        - ``population_total`` / ``population_displaced`` / ``population_at_high_risk``
          (None when ``structure_inventory_source != "USACE_NSI"``)
        - ``impact_area_km2`` (convex hull of DS1+ centroids, geodesic km²)
        - ``bbox`` (full layer extent in EPSG:4326)
        - ``by_occupancy_class`` (per HAZUS occupancy class breakdown)
        - ``pelicun_run_id`` / ``damage_layer_uri`` / ``flood_layer_uri`` /
          ``structure_inventory_source`` / ``fragility_set`` /
          ``realization_count`` / ``generated_at`` (provenance)

    Raises:
        PelicunPostprocessInputError: bad URI shape / non-string argument.
        PelicunPostprocessIOError: GCS download or geopandas read failed.
        PelicunPostprocessEmptyError: FlatGeobuf has zero features.
        PelicunPostprocessSchemaError: required Pelicun-output columns
            missing from the FlatGeobuf.

    Cache: ``ttl_class="static-30d"``, ``source_class="pelicun_postprocess"``.
    Identical (``damage_layer_uri``, ``flood_layer_uri``) pairs produce
    byte-identical envelopes (deterministic ``pelicun_run_id`` seeding).
    """
    damage_uri = _validate_uri(
        damage_layer_uri, "damage_layer_uri", required=True
    )
    flood_uri = _validate_uri(
        flood_layer_uri, "flood_layer_uri", required=False
    )

    logger.info(
        "postprocess_pelicun: invoked damage_layer_uri=%s flood_layer_uri=%s",
        damage_uri,
        flood_uri or "(none)",
    )

    # Lazy geopandas import so test environments without it can still import
    # the module (and unit-test the helpers directly).
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
    except ImportError as exc:
        raise PelicunPostprocessIOError(
            f"geopandas required to read the damage FlatGeobuf: {exc}"
        ) from exc

    local_path: str | None = None
    # sprint-14-aws (job-0293b): s3:// staging also lands in a temp file the
    # finally-block must unlink — remote means either object-store scheme.
    was_remote = damage_uri.startswith(("gs://", "s3://"))
    try:
        local_path = _download_uri_to_local(damage_uri, ".fgb")
        try:
            gdf = gpd.read_file(local_path, driver="FlatGeobuf")
        except PelicunPostprocessIOError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise PelicunPostprocessIOError(
                f"geopandas failed to read FlatGeobuf {damage_uri!r}: {exc}"
            ) from exc

        # sprint-14-aws (M5.5): forward the run-provenance overrides only when
        # the caller actually supplied them; otherwise _aggregate_gdf's
        # back-compatible defaults (hazus_flood_v6 / 100) apply unchanged.
        agg_kwargs: dict[str, Any] = {}
        if fragility_set is not None:
            agg_kwargs["fragility_set"] = fragility_set
        if realization_count is not None:
            agg_kwargs["realization_count"] = realization_count
        envelope = _aggregate_gdf(
            gdf,
            damage_layer_uri=damage_uri,
            flood_layer_uri=flood_uri,
            **agg_kwargs,
        )
    finally:
        if was_remote and local_path:
            try:
                os.unlink(local_path)
            except OSError:
                pass

    return envelope.model_dump(mode="json")
