"""``run_pelicun_damage_assessment`` — Pelicun-backed fragility damage assessment (job-0120).

**Wave 2 composer** — replaces the job-0098 stub with the real Pelicun-backed
runtime. The LLM-visible API contract (name, parameters, allowed enums, return
type) is unchanged from Wave 1; only the body is swapped.

**What this tool computes**

For each asset feature in ``assets_uri``:

1. Sample the hazard raster value at the asset centroid (after CRS alignment).
2. Look up the HAZUS v6.1 loss function for the asset's
   ``component_type`` (occupancy class, default ``RES1``).
3. Interpolate the deterministic mean loss ratio at the sampled inundation
   depth (converting metres → feet for the HAZUS curves).
4. Monte-Carlo sample ``realization_count`` loss-ratio realizations from a
   bounded lognormal aleatory model centred on the deterministic mean,
   with HAZUS-standard ``σ_lnD = 0.4`` (truncated to ``[0, 0.6]`` per the
   HAZUS depth-damage cap; outside [0,1] clipping).
5. Bin each realization into HAZUS damage states ``DS0..DS4`` (none, slight,
   moderate, extensive, complete) by loss-ratio thresholds
   ``[0, 0.05, 0.20, 0.50, 0.80]``; aggregate to mean + p05/p95 damage state.
6. Multiply per-asset replacement value (USD, defaults by occupancy class)
   by the realized loss ratios to derive ``repair_cost_mean`` /
   ``repair_cost_p95``.

The output FlatGeobuf carries the original asset geometry + every numeric
field the LLM might narrate. **Invariant 1**: no LLM-generated numbers — every
narrated quantity reads from a typed property on this layer.

**Why HAZUS v6.1 flood loss functions, not classical fragility curves**

The Pelicun 3.9 DamageAndLossModelLibrary ships HAZUS v6.1 flood depth-damage
data as piecewise-linear *loss functions* (loss-ratio vs. peak inundation
height), not as classical fragility curves with damage-state probabilities.
We compute damage states by binning realized loss ratios into the HAZUS DS
ladder — the canonical convention used by HAZUS-MH itself when post-processing
loss-function outputs. The aleatory dispersion ``σ_lnD = 0.4`` is the
standard HAZUS depth-damage dispersion (see HAZUS Flood Technical Manual §3.3,
also Tate et al. 2015, Wing et al. 2020).

**OQ-8-RESOLVED-V0.1**: bundled HAZUS curves it is. FEMA P-58 swap (component-
level fragility for seismic + finer-grain flood) is sprint-13+ work.

**Geographic-correctness gate (codified lesson from job-0086)**

Damage states MUST be monotonically non-decreasing with sampled hazard
intensity. Test ``test_geographic_correctness_higher_depth_higher_damage``
asserts this against a synthetic raster where the western half is dry and the
eastern half is flooded — assets in the eastern half MUST come back with
higher ``ds_mean`` than assets in the western half. A sampling bug that maps
every asset to the wrong raster pixel would fail this test.

**Cache**

``ttl_class="static-30d"``, ``source_class="pelicun_damage"``,
``cacheable=True``. The Monte-Carlo loop is seeded deterministically per
``(asset_id, component_type)`` so identical
``(hazard_raster_uri, assets_uri, fragility_set, component_types,
realization_count)`` calls reuse the cached FlatGeobuf for 30 days.

**Live verification gate**

Acceptance run (``GRACE2_TEST_LIVE_PELICUN=1``): job-0086 Y-flip-fixed flood
COG (``gs://grace-2-hazard-prod-runs/01KTJX71NKGDMXB9TN0DV75JWK/flood_depth_peak_0086.tif``)
+ Fort Myers place polygons from ``fetch_administrative_boundaries(level='place', bbox=...)``
→ FlatGeobuf with populated ``ds_mean``/``repair_cost_mean`` per asset,
saved to ``reports/inflight/job-0120-engine-20260608/evidence/``.

FR-TA-2 / FR-AS-3 / FR-CE-8 / FR-DC-3/4 invariants honored as documented in
the per-section comments below.
"""

from __future__ import annotations

import hashlib
import io
import logging
import math
import os
import tempfile
from typing import Any, Literal

import numpy as np

from grace2_contracts.execution import LayerURI, LegendClass, LegendKey
from grace2_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from .cache import read_through

__all__ = [
    "run_pelicun_damage_assessment",
    "PelicunDamageError",
    "PelicunInputError",
    "PelicunRuntimeError",
    "PelicunFragilityDataError",
    "PelicunNoAssetsError",
]

logger = logging.getLogger("grace2_agent.tools.run_pelicun_damage_assessment")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 / NFR-R-1 typed-error surface).
# ---------------------------------------------------------------------------


class PelicunDamageError(RuntimeError):
    """Base class for ``run_pelicun_damage_assessment`` failures.

    ``error_code`` maps to the WebSocket A.6 error frame emitted by the
    agent surface; ``retryable`` guides FR-AS-11 retry logic.
    """

    error_code: str = "PELICUN_DAMAGE_ERROR"
    retryable: bool = True


class PelicunInputError(PelicunDamageError):
    """Bad ``hazard_raster_uri`` / ``assets_uri`` / ``fragility_set`` / etc.

    Not retryable — input validation failures are deterministic given the same
    inputs, so the agent's retry loop should not re-invoke.
    """

    error_code = "PELICUN_INPUT_INVALID"
    retryable = False


class PelicunRuntimeError(PelicunDamageError):
    """The Pelicun runtime or geospatial I/O failed during damage assessment.

    Examples: cannot open hazard raster, asset file unreadable, CRS
    reprojection failure. Retryable — transient I/O might succeed on retry.
    """

    error_code = "PELICUN_RUNTIME_ERROR"
    retryable = True


class PelicunFragilityDataError(PelicunDamageError):
    """Pelicun's bundled HAZUS data is missing / corrupt / unparseable.

    This is an install-state defect; retrying won't help.
    """

    error_code = "PELICUN_FRAGILITY_DATA_MISSING"
    retryable = False


class PelicunNoAssetsError(PelicunDamageError):
    """Asset file has zero features overlapping the hazard raster footprint.

    Not retryable — the inputs are well-formed but yield no work; the agent's
    planning loop should pick a different bbox or asset source.
    """

    error_code = "PELICUN_NO_ASSETS_IN_HAZARD"
    retryable = False


# ---------------------------------------------------------------------------
# Allowed enum values — locked at Wave 1, preserved here.
# ---------------------------------------------------------------------------

FragilitySet = Literal["hazus_flood_v6", "fema_hazus_eq_2020"]
_VALID_FRAGILITY_SETS: frozenset[str] = frozenset(
    {"hazus_flood_v6", "fema_hazus_eq_2020"}
)

# Default HAZUS occupancy class for assets that don't declare one.
_DEFAULT_COMPONENT_TYPE = "RES1"


# ---------------------------------------------------------------------------
# HAZUS damage-state bins.
#
# Standard HAZUS-MH convention: loss-ratio thresholds map a continuous
# loss ratio to one of five damage states.
# DS0 = None      : LR <  0.05
# DS1 = Slight    : 0.05 ≤ LR < 0.20
# DS2 = Moderate  : 0.20 ≤ LR < 0.50
# DS3 = Extensive : 0.50 ≤ LR < 0.80
# DS4 = Complete  : LR ≥  0.80
# ---------------------------------------------------------------------------

_DS_LOSS_RATIO_BREAKS = np.array([0.05, 0.20, 0.50, 0.80])
_DS_LABELS = ("DS0_none", "DS1_slight", "DS2_moderate", "DS3_extensive", "DS4_complete")

# DATA-DRIVEN LEGEND for the per-asset ``ds_mean`` choropleth. Pelicun emits a
# VECTOR FlatGeobuf whose features carry ``ds_mean`` (the mean HAZUS damage state
# in 0..4); the legend KEY tells the frontend to drive the fill from that field
# generically (replacing the brittle ``pelicun_damage`` exact-string sentinel that
# never matched the real ``pelicun_damage_state`` style_preset). Five graduated
# buckets centered on DS0..DS4 along a green(no damage)->yellow->red(complete)
# damage ramp -- the canonical HAZUS damage palette. ``vmin=0``/``vmax=4`` is the
# CANONICAL fixed scale for damage state (not a percentile read): DS labels are
# fixed categories, so the legend is pinned, not data-derived.
_DS_LEGEND_COLORS = (
    "#1a9850",  # DS0 none      - green
    "#a6d96a",  # DS1 slight    - light green
    "#fee08b",  # DS2 moderate  - yellow
    "#fc8d59",  # DS3 extensive - orange
    "#d73027",  # DS4 complete  - red
)
_DS_HUMAN_LABELS = ("None", "Slight", "Moderate", "Extensive", "Complete")


def _build_damage_state_legend() -> LegendKey:
    """Build the categorical/graduated ``LegendKey`` for the ``ds_mean`` choropleth.

    One swatch per HAZUS damage state DS0..DS4, each covering the half-unit bucket
    centered on the integer state (so a continuous ``ds_mean`` of e.g. 1.7 lands in
    the DS2 bucket). ``value_field="ds_mean"`` tells the generic web vector path
    which feature property drives the fill; ``vmin=0``/``vmax=4`` is the canonical
    damage-state scale. Built fresh per call (Pydantic models are cheap; avoids a
    shared-mutable-default footgun).
    """
    classes = [
        LegendClass(
            value_min=float(i) - 0.5,
            value_max=float(i) + 0.5,
            color=_DS_LEGEND_COLORS[i],
            label=f"DS{i} {_DS_HUMAN_LABELS[i]}",
        )
        for i in range(5)
    ]
    return LegendKey(
        kind="categorical",
        classes=classes,
        value_field="ds_mean",
        vmin=0.0,
        vmax=4.0,
        units="damage_state",
        label="Damage state",
    )

# Standard HAZUS flood depth-damage aleatory dispersion (σ in ln-space).
# Source: HAZUS Flood Technical Manual §3.3 + Tate et al. 2015.
_HAZUS_FLOOD_SIGMA_LND = 0.4

# Loss-ratio cap from the HAZUS v6.1 piecewise functions (curves saturate at
# 0.6 = 60% loss ratio). Used as the truncation cap for Monte Carlo samples
# so realizations cannot exceed the curve's documented saturation.
_HAZUS_FLOOD_LOSS_CAP = 0.6


# ---------------------------------------------------------------------------
# Replacement-value defaults per HAZUS occupancy class (USD).
#
# These are coarse v0.1 defaults from HAZUS-MH 4.2 reference tables
# (Table 16-1 General Building Stock Replacement Costs) scaled to 2024 USD
# using BLS construction-cost CPI factor ~1.45 from 2018 base.
#
# Surfaced as OQ-0120-REPLACEMENT-VALUE for sprint-13+ refinement: real
# parcel data carries per-asset replacement value; v0.1 falls back to
# class defaults so the tool produces a populated repair_cost field even
# when the asset layer lacks the property.
# ---------------------------------------------------------------------------

_REPLACEMENT_VALUE_DEFAULTS_USD: dict[str, float] = {
    "RES1": 250_000.0,   # single-family residential
    "RES2": 80_000.0,    # mobile home
    "RES3": 350_000.0,   # multi-family residential
    "RES3A": 300_000.0,
    "RES3B": 380_000.0,
    "RES4": 4_500_000.0,  # temporary lodging
    "RES5": 8_000_000.0,  # institutional dormitory
    "RES6": 6_000_000.0,  # nursing home
    "COM1": 1_400_000.0,  # retail
    "COM2": 2_000_000.0,  # wholesale
    "COM3": 1_200_000.0,  # personal/repair services
    "COM4": 2_800_000.0,  # professional/technical
    "COM5": 3_500_000.0,  # bank/finance
    "COM6": 8_000_000.0,  # hospital
    "COM7": 2_200_000.0,  # medical office
    "COM8": 1_500_000.0,  # entertainment / recreation
    "COM9": 3_600_000.0,  # theaters
    "COM10": 600_000.0,   # parking
    "IND1": 2_500_000.0,
    "IND2": 1_500_000.0,
    "IND3": 1_800_000.0,
    "IND4": 1_400_000.0,
    "IND5": 1_200_000.0,
    "IND6": 1_500_000.0,
    "INDX": 1_500_000.0,
    "AGR1": 800_000.0,
    "EDU1": 5_500_000.0,
    "EDU2": 12_000_000.0,
    "EDU": 6_000_000.0,
    "REL1": 1_800_000.0,
    "GOV1": 3_500_000.0,
    "GOV2": 8_000_000.0,
}

# Default for unknown component types (median-ish).
_REPLACEMENT_VALUE_FALLBACK_USD = 500_000.0


# ---------------------------------------------------------------------------
# AtomicToolMetadata — registered once at import time.
# ---------------------------------------------------------------------------

_METADATA = AtomicToolMetadata(
    name="run_pelicun_damage_assessment",
    ttl_class="static-30d",
    source_class="pelicun_damage",
    cacheable=True,
)


# ---------------------------------------------------------------------------
# Input-validation helpers (unchanged from Wave 1 — same typed errors).
# ---------------------------------------------------------------------------


def _validate_uri(uri: object, field_name: str) -> str:
    """Reject non-string, empty, or obviously-malformed URIs."""
    if uri is None:
        raise PelicunInputError(
            f"{field_name} is required; got None. "
            "Pass a gs:// URI (or local path) to a "
            f"{'COG raster' if 'hazard' in field_name else 'FlatGeobuf vector'}."
        )
    if not isinstance(uri, str):
        raise PelicunInputError(
            f"{field_name} must be a string URI; got {type(uri).__name__}."
        )
    if not uri.strip():
        raise PelicunInputError(
            f"{field_name} must be a non-empty URI string."
        )
    return uri


def _validate_fragility_set(fragility_set: str) -> str:
    """Reject ``fragility_set`` values outside the allowed enum."""
    if not isinstance(fragility_set, str):
        raise PelicunInputError(
            f"fragility_set must be a string; got {type(fragility_set).__name__}."
        )
    if fragility_set not in _VALID_FRAGILITY_SETS:
        raise PelicunInputError(
            f"fragility_set={fragility_set!r} is not in the allowed set "
            f"{sorted(_VALID_FRAGILITY_SETS)}. v0.1 ships only 'hazus_flood_v6' "
            "(FEMA HAZUS-MH flood depth-damage curves) and 'fema_hazus_eq_2020' "
            "(HAZUS earthquake curves — sprint-13+ when the seismic engine lands)."
        )
    return fragility_set


def _validate_component_types(component_types: object) -> list[str] | None:
    """Reject ``component_types`` values outside ``list[str] | None``."""
    if component_types is None:
        return None
    if not isinstance(component_types, (list, tuple)):
        raise PelicunInputError(
            "component_types must be a list of strings or None; "
            f"got {type(component_types).__name__}."
        )
    if len(component_types) == 0:
        raise PelicunInputError(
            "component_types is an empty list; pass None to include all "
            "components in the fragility set, or pass a non-empty list of "
            "component codes (e.g. ['RES1', 'COM1'])."
        )
    out: list[str] = []
    for idx, ct in enumerate(component_types):
        if not isinstance(ct, str) or not ct.strip():
            raise PelicunInputError(
                f"component_types[{idx}] must be a non-empty string; "
                f"got {ct!r}."
            )
        out.append(ct.strip())
    return out


def _validate_realization_count(realization_count: object) -> int:
    """Reject ``realization_count`` outside positive int range."""
    if isinstance(realization_count, bool) or not isinstance(realization_count, int):
        raise PelicunInputError(
            "realization_count must be a positive integer; "
            f"got {type(realization_count).__name__}."
        )
    if realization_count <= 0:
        raise PelicunInputError(
            f"realization_count={realization_count} must be > 0. "
            "Default 100 is suitable for most assessments; raise for "
            "tighter confidence intervals."
        )
    return realization_count


# ---------------------------------------------------------------------------
# HAZUS loss-function loader.
#
# We load the bundled Pelicun HAZUS v6.1 flood loss_repair.csv once at first
# use (module-level cache) and parse the piecewise loss-ratio vs. peak
# inundation height curves keyed by (component_type, category).
#
# For v0.1 each component_type collapses to a single canonical "structural"
# curve picked by majority-rules variant selection:
#   - one_floor, no_basement, a_zone (the most common SFD configuration for
#     RES1 in FEMA flood-hazard mapping)
#   - FIA depth-damage methodology (the FEMA-issued standard, not Modified)
# This is documented as OQ-0120-CURVE-VARIANT — a sprint-13+ refinement could
# infer foundation type from building footprint attrs.
# ---------------------------------------------------------------------------


class _HazusLossCurve:
    """A single piecewise-linear HAZUS loss function.

    Attributes:
        component_type: HAZUS occupancy class (e.g. ``"RES1"``).
        depths_ft: 1-D ``np.ndarray`` of breakpoint depths in **feet**.
        loss_ratios: 1-D ``np.ndarray`` of mean loss ratios at each breakpoint
            (0.0–0.6 typically; 0.6 is the HAZUS saturation).
        curve_id: source ID string from the HAZUS CSV (for provenance).
    """

    __slots__ = ("component_type", "depths_ft", "loss_ratios", "curve_id")

    def __init__(
        self,
        component_type: str,
        depths_ft: np.ndarray,
        loss_ratios: np.ndarray,
        curve_id: str,
    ) -> None:
        self.component_type = component_type
        self.depths_ft = depths_ft
        self.loss_ratios = loss_ratios
        self.curve_id = curve_id

    def mean_loss_ratio_at(self, depth_ft: float) -> float:
        """Interpolate the mean loss ratio at ``depth_ft``.

        Linear interpolation between breakpoints; below the minimum depth
        returns 0; above the maximum returns the curve's saturation value.
        """
        if not math.isfinite(depth_ft):
            return 0.0
        return float(np.interp(depth_ft, self.depths_ft, self.loss_ratios))


# Module-level cache for the parsed HAZUS curve dict.
_HAZUS_FLOOD_CURVES: dict[str, _HazusLossCurve] | None = None


def _hazus_flood_loss_csv_path() -> str:
    """Return the absolute path to Pelicun's bundled HAZUS v6.1 flood CSV.

    Raises:
        ``PelicunFragilityDataError`` if Pelicun isn't installed or the bundled
        file is missing.
    """
    try:
        import pelicun  # noqa: F401 — used only for __file__
    except ImportError as exc:
        raise PelicunFragilityDataError(
            "pelicun is not installed; cannot load HAZUS fragility curves. "
            "Install with `pip install pelicun`."
        ) from exc

    pelicun_dir = os.path.dirname(pelicun.__file__)
    flood_csv = os.path.join(
        pelicun_dir,
        "resources",
        "DamageAndLossModelLibrary",
        "flood",
        "building",
        "portfolio",
        "Hazus v6.1",
        "loss_repair.csv",
    )
    if not os.path.isfile(flood_csv):
        raise PelicunFragilityDataError(
            f"Pelicun HAZUS v6.1 flood loss_repair.csv not found at {flood_csv!r}. "
            "Pelicun's DLML data may not have been downloaded yet — "
            "import pelicun in an interactive session to trigger the one-time download."
        )
    return flood_csv


def _parse_loss_function_field(field: str) -> tuple[np.ndarray, np.ndarray]:
    """Parse a HAZUS CSV ``LossFunction-Theta_0`` field.

    The field is shaped like ``"<lrs>|<depths>"`` where ``<lrs>`` and
    ``<depths>`` are each comma-separated floats. Returns
    ``(depths_ft, loss_ratios)`` as parallel numpy arrays sorted by depth.

    Raises:
        ``PelicunFragilityDataError`` on malformed payload.
    """
    if "|" not in field:
        raise PelicunFragilityDataError(
            f"LossFunction-Theta_0 field has no '|' separator: {field[:80]!r}"
        )
    lrs_part, depths_part = field.split("|", 1)
    try:
        loss_ratios = np.fromiter(
            (float(x.strip()) for x in lrs_part.split(",")), dtype=np.float64
        )
        depths_ft = np.fromiter(
            (float(x.strip()) for x in depths_part.split(",")), dtype=np.float64
        )
    except (ValueError, TypeError) as exc:
        raise PelicunFragilityDataError(
            f"LossFunction-Theta_0 parse failure: {exc}; field={field[:120]!r}"
        ) from exc
    if len(loss_ratios) != len(depths_ft):
        raise PelicunFragilityDataError(
            f"LossFunction-Theta_0 has mismatched lengths "
            f"(loss_ratios={len(loss_ratios)}, depths={len(depths_ft)})"
        )
    # Sort by depth for safe np.interp.
    order = np.argsort(depths_ft)
    return depths_ft[order], loss_ratios[order]


def _load_hazus_flood_curves() -> dict[str, _HazusLossCurve]:
    """Load + parse the HAZUS v6.1 flood loss curves keyed by component_type.

    Module-level memoized so repeated tool invocations don't re-parse the CSV.

    Curve-selection rule per component_type: the first ``structural.*`` row
    whose ID parts are ``("FIA", "one_floor", "no_basement", "a_zone")`` —
    the FEMA-issued canonical SFD configuration. If that exact variant is
    not present, falls back to the first ``structural.*`` row for the
    component type (e.g. some commercial codes don't have basement variants).

    Returns:
        ``{component_type: _HazusLossCurve}`` for every component type with
        at least one structural loss function.

    Raises:
        ``PelicunFragilityDataError`` if Pelicun isn't installed or the file
        is malformed.
    """
    global _HAZUS_FLOOD_CURVES
    if _HAZUS_FLOOD_CURVES is not None:
        return _HAZUS_FLOOD_CURVES

    csv_path = _hazus_flood_loss_csv_path()

    # Lazy import pandas so the module load is fast in test environments.
    try:
        import pandas as pd
    except ImportError as exc:
        raise PelicunFragilityDataError(
            f"pandas required to load HAZUS curves: {exc}"
        ) from exc

    try:
        df = pd.read_csv(csv_path)
    except Exception as exc:  # noqa: BLE001
        raise PelicunFragilityDataError(
            f"failed to read HAZUS CSV {csv_path!r}: {exc}"
        ) from exc

    if "ID" not in df.columns or "LossFunction-Theta_0" not in df.columns:
        raise PelicunFragilityDataError(
            f"HAZUS CSV missing required columns; got {list(df.columns)}"
        )

    # Parse: ID format = "{category}.{idx}.{ctype}.{methodology}.{floors}.{basement}.{zone}-Cost"
    # We want structural rows only (not contents, not inventory).
    curves: dict[str, _HazusLossCurve] = {}
    for _, row in df.iterrows():
        id_str = str(row["ID"])
        if not id_str.startswith("structural."):
            continue
        try:
            head, _ = id_str.split("-", 1)
        except ValueError:
            continue
        parts = head.split(".")
        if len(parts) < 7:
            continue
        category, _idx, ctype, methodology, floors, basement, zone = parts[:7]
        if category != "structural":
            continue

        # Prefer canonical variant; fall back to first structural row per ctype.
        is_canonical = (
            methodology == "FIA"
            and floors == "one_floor"
            and basement == "no_basement"
            and zone == "a_zone"
        )
        if ctype in curves and not is_canonical:
            continue

        try:
            depths_ft, loss_ratios = _parse_loss_function_field(
                str(row["LossFunction-Theta_0"])
            )
        except PelicunFragilityDataError as exc:
            logger.warning(
                "skipping malformed HAZUS curve for id=%s: %s", id_str, exc
            )
            continue

        # If we already have this ctype and the new one is canonical, replace.
        if is_canonical or ctype not in curves:
            curves[ctype] = _HazusLossCurve(
                component_type=ctype,
                depths_ft=depths_ft,
                loss_ratios=loss_ratios,
                curve_id=id_str,
            )

    if not curves:
        raise PelicunFragilityDataError(
            f"No structural HAZUS curves parsed from {csv_path!r}; "
            "the CSV may be malformed or use an unexpected ID schema."
        )

    logger.info(
        "loaded %d HAZUS v6.1 flood loss curves (component types: %s)",
        len(curves),
        sorted(curves.keys()),
    )
    _HAZUS_FLOOD_CURVES = curves
    return curves


# ---------------------------------------------------------------------------
# URI helpers — download GCS object bytes to a local temp file.
# ---------------------------------------------------------------------------


def _download_uri_to_local(uri: str, suffix: str, storage_client: Any | None = None) -> str:
    """Download ``uri`` (``s3://`` or local path) into a NamedTemporaryFile.

    Returns the local path. Caller is responsible for unlinking. GCP is
    decommissioned: object-store reads route through boto3 (S3); the
    ``storage_client`` argument is retained for call-site compatibility but is
    ignored.

    Raises:
        ``PelicunRuntimeError`` on download / read failure.
    """
    del storage_client  # GCP decommissioned — S3/local only.
    if uri.startswith(("http://", "https://")) and "LAYERS=" in uri:
        # job-0255 (OQ-0255-PELICUN-WMS-URI): the LLM copies the published
        # layer's QGIS WMS GetMap URL verbatim — which IS the LayerURI.uri
        # field per OQ-62 (the s3:// COG never appears in its context).
        # Reverse-map the WMS layer id back to the runs-bucket COG:
        # LAYERS=flood-depth-peak-<run_id>  ->  s3://<runs>/<run_id>/flood_depth_peak.tif
        # LAYERS=plume-concentration-<run_id> -> .../plume_concentration_4326.tif
        from urllib.parse import parse_qs, urlparse

        layers = parse_qs(urlparse(uri).query).get("LAYERS", [])
        runs_bucket = os.environ.get(
            "GRACE2_RUNS_BUCKET", "grace-2-hazard-prod-runs"
        )
        # GCP decommissioned: the reverse-mapped runs-bucket COG is always s3://.
        for layer_id in layers:
            for prefix, fname in (
                ("flood-depth-peak-", "flood_depth_peak.tif"),
                ("plume-concentration-", "plume_concentration_4326.tif"),
            ):
                if layer_id.startswith(prefix):
                    run_id = layer_id[len(prefix):]
                    mapped = f"s3://{runs_bucket}/{run_id}/{fname}"
                    logger.warning(
                        "hazard URI was a WMS GetMap URL; reverse-mapped "
                        "LAYERS=%s -> %s (job-0255 guard)",
                        layer_id,
                        mapped,
                    )
                    return _download_uri_to_local(mapped, suffix)
        raise PelicunRuntimeError(
            f"hazard_raster_uri is a WMS URL with an unmapped layer id "
            f"({uri!r}); pass the s3:// COG URI from the producing tool"
        )

    # sprint-14-aws (job-0293b): s3:// staging via the shared boto3 reader
    # (NOT s3fs — instance-role lesson, job-0289). Stage to a
    # NamedTemporaryFile the caller unlinks, with the job-0253 last-two-segment
    # retry for LLM path-mangled URIs.
    if uri.startswith("s3://"):
        from .cache import read_object_bytes_s3

        try:
            try:
                data = read_object_bytes_s3(uri)
            except Exception as first_exc:  # noqa: BLE001
                bucket_name, _, obj_key = uri[len("s3://"):].partition("/")
                parts = obj_key.split("/")
                if len(parts) > 2:
                    repaired = "/".join(parts[-2:])
                    logger.warning(
                        "s3:// download failed for %r; retrying suffix-repaired "
                        "path s3://%s/%s (LLM path-mangle guard, job-0253)",
                        uri,
                        bucket_name,
                        repaired,
                    )
                    data = read_object_bytes_s3(f"s3://{bucket_name}/{repaired}")
                else:
                    raise first_exc
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tf:
                tf.write(data)
                return tf.name
        except Exception as exc:  # noqa: BLE001
            raise PelicunRuntimeError(
                f"S3 download failed for {uri!r}: {exc}"
            ) from exc

    if not os.path.exists(uri):
        raise PelicunRuntimeError(
            f"local path does not exist: {uri!r}"
        )
    return uri


# ---------------------------------------------------------------------------
# Core Monte-Carlo + damage-state binning.
# ---------------------------------------------------------------------------


def _mc_loss_ratio_realizations(
    mean_lr: float,
    realization_count: int,
    rng: np.random.Generator,
) -> np.ndarray:
    """Monte-Carlo loss-ratio realizations around ``mean_lr``.

    Uses a bounded lognormal aleatory model with the standard HAZUS dispersion
    ``σ_lnD = 0.4``. The lognormal is centred so its **mean** equals ``mean_lr``
    (μ_lnD = ln(mean_lr) − σ²/2). Realizations are clipped to ``[0, 0.6]``
    (the HAZUS saturation cap).

    When ``mean_lr`` is 0 (asset is dry), all realizations are exactly 0 — no
    fictitious damage from numeric tail effects.
    """
    if mean_lr <= 0.0 or not math.isfinite(mean_lr):
        return np.zeros(realization_count, dtype=np.float64)

    sigma = _HAZUS_FLOOD_SIGMA_LND
    mu = math.log(mean_lr) - 0.5 * sigma * sigma
    samples = rng.lognormal(mean=mu, sigma=sigma, size=realization_count)
    np.clip(samples, 0.0, _HAZUS_FLOOD_LOSS_CAP, out=samples)
    return samples


def _bin_to_damage_state(loss_ratios: np.ndarray) -> np.ndarray:
    """Map loss-ratio realizations to damage states ``DS0..DS4`` (0..4).

    Vectorized via ``np.searchsorted``.
    """
    return np.searchsorted(_DS_LOSS_RATIO_BREAKS, loss_ratios, side="right").astype(
        np.int32
    )


def _seed_for_asset(
    asset_id: object,
    component_type: str,
    hazard_uri: str,
    realization_count: int,
) -> int:
    """Deterministic per-asset RNG seed.

    Ensures byte-identical FlatGeobuf output across runs with the same inputs
    (cache invariant); independent across assets (no MC coupling).
    """
    salt = f"{asset_id}|{component_type}|{hazard_uri}|{realization_count}"
    digest = hashlib.sha256(salt.encode("utf-8")).digest()
    # Take the first 8 bytes → 64-bit unsigned int.
    return int.from_bytes(digest[:8], "big") & ((1 << 63) - 1)


# ---------------------------------------------------------------------------
# Per-asset hazard sampling + assessment.
# ---------------------------------------------------------------------------


def _sample_raster_at_point(
    src: Any,  # rasterio.DatasetReader
    raster_array: np.ndarray,
    point_x: float,
    point_y: float,
    nodata: float | None,
) -> float:
    """Sample a single raster value at ``(point_x, point_y)`` in raster CRS.

    Returns ``np.nan`` for out-of-bounds / nodata points (treated as zero
    hazard downstream).
    """
    try:
        row, col = src.index(point_x, point_y)
    except Exception:  # noqa: BLE001 — rasterio raises various index errors
        return float("nan")
    if row < 0 or col < 0 or row >= raster_array.shape[0] or col >= raster_array.shape[1]:
        return float("nan")
    val = float(raster_array[row, col])
    if nodata is not None and val == nodata:
        return float("nan")
    if not math.isfinite(val):
        return float("nan")
    return val


def _assets_centroids_in_raster_crs(
    gdf: Any,  # geopandas.GeoDataFrame
    target_crs: Any,
) -> Any:  # geopandas.GeoSeries
    """Return asset centroids reprojected into ``target_crs``.

    Pre-reprojects the entire frame to the raster CRS first (preserves
    sub-pixel accuracy near boundaries vs. point-by-point reproject).
    """
    if gdf.crs is None:
        # Tolerate missing CRS by assuming EPSG:4326 — TIGER and most
        # admin-boundary tools emit 4326. Log loudly.
        logger.warning(
            "asset layer has no CRS — assuming EPSG:4326 for reprojection"
        )
        gdf = gdf.set_crs("EPSG:4326")
    if target_crs is None or (
        hasattr(gdf.crs, "to_epsg")
        and hasattr(target_crs, "to_epsg")
        and gdf.crs.to_epsg() == target_crs.to_epsg()
    ):
        return gdf.geometry.centroid
    reprojected = gdf.to_crs(target_crs)
    return reprojected.geometry.centroid


def _assess_assets(
    hazard_raster_path: str,
    assets_path: str,
    component_types_filter: list[str] | None,
    realization_count: int,
    hazard_uri_for_seed: str,
) -> Any:  # geopandas.GeoDataFrame
    """Run the damage assessment loop and return the enriched GeoDataFrame.

    The returned frame carries the original asset geometry + the
    Pelicun-computed property columns documented in the docstring.

    Raises:
        ``PelicunRuntimeError`` on I/O failure / CRS mismatch.
        ``PelicunNoAssetsError`` if zero assets survive the filter or all
        assets fall outside the raster footprint.
        ``PelicunFragilityDataError`` if curves cannot be loaded.
    """
    try:
        import geopandas as gpd
        import rasterio
    except ImportError as exc:
        raise PelicunRuntimeError(
            f"geopandas/rasterio required: {exc}"
        ) from exc

    curves = _load_hazus_flood_curves()

    try:
        gdf = gpd.read_file(assets_path)
    except Exception as exc:  # noqa: BLE001
        raise PelicunRuntimeError(
            f"failed to read assets {assets_path!r}: {exc}"
        ) from exc

    if len(gdf) == 0:
        raise PelicunNoAssetsError(
            f"assets layer {assets_path!r} is empty (zero features)"
        )

    # Apply component_types filter if supplied. Use the asset's
    # ``component_type`` property if present; otherwise the asset takes the
    # default and the filter falls through to "include" if default matches.
    if component_types_filter is not None:
        if "component_type" in gdf.columns:
            mask = gdf["component_type"].isin(component_types_filter)
            gdf = gdf[mask].copy()
        else:
            # No per-asset component_type column → only include if the default
            # is in the filter.
            if _DEFAULT_COMPONENT_TYPE not in component_types_filter:
                gdf = gdf.iloc[0:0].copy()

    if len(gdf) == 0:
        raise PelicunNoAssetsError(
            f"zero assets remain after component_types filter "
            f"({component_types_filter!r})"
        )

    # Reset index so positional lookups against the centroid GeoSeries align
    # one-to-one with the iteration order. Without this, a filtered frame
    # keeps its sparse original labels and ``centroids.iloc[pos]`` no longer
    # corresponds to ``gdf.iloc[pos]``.
    gdf = gdf.reset_index(drop=True)

    # Open hazard raster and read once.
    try:
        with rasterio.open(hazard_raster_path) as src:
            raster_array = src.read(1)
            raster_crs = src.crs
            raster_nodata = src.nodata
            raster_units = (
                src.tags().get("units")
                or (src.units[0] if src.units else None)
                or "meters"  # default assumption for flood depth COGs
            )

            # Reproject asset centroids to raster CRS once.
            try:
                centroids = _assets_centroids_in_raster_crs(gdf, raster_crs)
            except Exception as exc:  # noqa: BLE001
                raise PelicunRuntimeError(
                    f"asset CRS reprojection failed: {exc}"
                ) from exc

            # Per-asset assessment loop.
            depth_unit_is_meters = (
                "meter" in str(raster_units).lower()
                or "metre" in str(raster_units).lower()
                or raster_units == "m"
            )

            ds_means: list[float] = []
            ds_p05s: list[float] = []
            ds_p95s: list[float] = []
            lr_means: list[float] = []
            lr_p95s: list[float] = []
            repair_means: list[float] = []
            repair_p95s: list[float] = []
            repair_replacements: list[float] = []
            replacement_value_defaulted: list[bool] = []
            depth_samples: list[float] = []
            curve_ids: list[str] = []
            component_used: list[str] = []
            in_hazard_footprint = 0

            for pos in range(len(gdf)):
                asset = gdf.iloc[pos]
                # Determine component_type (default to RES1).
                ctype = (
                    str(asset.get("component_type", _DEFAULT_COMPONENT_TYPE))
                    if "component_type" in gdf.columns
                    else _DEFAULT_COMPONENT_TYPE
                )
                if ctype not in curves:
                    ctype = _DEFAULT_COMPONENT_TYPE
                curve = curves[ctype]
                component_used.append(ctype)
                curve_ids.append(curve.curve_id)

                # Determine replacement value. Invariant 7 (job-0300): record when
                # we fall back to a HAZUS class default (NSI lacked a usable
                # val_struct) so the envelope can surface how many loss figures
                # rest on defaults rather than measured per-asset values.
                rv = asset.get("replacement_value")
                rv_defaulted = (
                    rv is None
                    or not isinstance(rv, (int, float))
                    or not math.isfinite(rv)
                    or rv <= 0
                )
                if rv_defaulted:
                    rv = _REPLACEMENT_VALUE_DEFAULTS_USD.get(
                        ctype, _REPLACEMENT_VALUE_FALLBACK_USD
                    )
                rv = float(rv)
                repair_replacements.append(rv)
                replacement_value_defaulted.append(bool(rv_defaulted))

                # Sample hazard at centroid (positional lookup against the
                # parallel reset-index centroid series).
                centroid = centroids.iloc[pos]
                if centroid is None or centroid.is_empty:
                    depth = float("nan")
                else:
                    depth = _sample_raster_at_point(
                        src, raster_array, centroid.x, centroid.y, raster_nodata
                    )

                if math.isnan(depth):
                    # Outside hazard footprint → record zero damage.
                    depth_samples.append(0.0)
                    ds_means.append(0.0)
                    ds_p05s.append(0.0)
                    ds_p95s.append(0.0)
                    lr_means.append(0.0)
                    lr_p95s.append(0.0)
                    repair_means.append(0.0)
                    repair_p95s.append(0.0)
                    continue

                in_hazard_footprint += 1

                # Convert depth to feet for HAZUS curves.
                depth_ft = depth * 3.28084 if depth_unit_is_meters else depth
                depth_samples.append(depth)

                mean_lr = curve.mean_loss_ratio_at(depth_ft)

                # Monte-Carlo around mean_lr.
                seed = _seed_for_asset(
                    asset.get("id", pos), ctype, hazard_uri_for_seed, realization_count
                )
                rng = np.random.default_rng(seed)
                lr_samples = _mc_loss_ratio_realizations(
                    mean_lr, realization_count, rng
                )
                ds_samples = _bin_to_damage_state(lr_samples)

                ds_means.append(float(np.mean(ds_samples)))
                ds_p05s.append(float(np.percentile(ds_samples, 5)))
                ds_p95s.append(float(np.percentile(ds_samples, 95)))
                lr_means.append(float(np.mean(lr_samples)))
                lr_p95s.append(float(np.percentile(lr_samples, 95)))
                repair_means.append(float(np.mean(lr_samples) * rv))
                repair_p95s.append(float(np.percentile(lr_samples, 95) * rv))

            if in_hazard_footprint == 0:
                raise PelicunNoAssetsError(
                    "No assets intersect the hazard raster footprint. "
                    "Check that the asset bbox overlaps the raster extent."
                )

    except (PelicunDamageError,):
        raise
    except Exception as exc:  # noqa: BLE001
        raise PelicunRuntimeError(
            f"damage-assessment loop failed: {exc}"
        ) from exc

    # Attach computed columns to the GeoDataFrame.
    gdf = gdf.copy()
    gdf["component_type_used"] = component_used
    gdf["fragility_curve_id"] = curve_ids
    gdf["hazard_depth_sampled"] = depth_samples
    gdf["ds_mean"] = ds_means
    gdf["ds_p05"] = ds_p05s
    gdf["ds_p95"] = ds_p95s
    gdf["loss_ratio_mean"] = lr_means
    gdf["loss_ratio_p95"] = lr_p95s
    gdf["repair_cost_mean"] = repair_means
    gdf["repair_cost_p95"] = repair_p95s
    gdf["replacement_value"] = repair_replacements
    gdf["replacement_value_defaulted"] = replacement_value_defaulted

    return gdf


# ---------------------------------------------------------------------------
# FlatGeobuf serialization.
# ---------------------------------------------------------------------------


def _gdf_to_fgb_bytes(gdf: Any) -> bytes:
    """Serialize a GeoDataFrame to in-memory FlatGeobuf bytes."""
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as tf:
        tmp_path = tf.name
    try:
        # Ensure GeoSeries with valid CRS — fall back to 4326 if missing.
        if gdf.crs is None:
            gdf = gdf.set_crs("EPSG:4326")
        gdf.to_file(tmp_path, driver="FlatGeobuf", engine="pyogrio")
        with open(tmp_path, "rb") as fh:
            return fh.read()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Cacheable fetch wrapper.
# ---------------------------------------------------------------------------


def _fetch_pelicun_damage_bytes(
    hazard_raster_uri: str,
    assets_uri: str,
    fragility_set: str,
    component_types: list[str] | None,
    realization_count: int,
) -> bytes:
    """The cache-miss path: download inputs, run assessment, return FGB bytes."""
    # Branch on fragility_set BEFORE any I/O so the deferred-feature error
    # surface is independent of whether the URIs resolve. v0.1 wires only
    # ``hazus_flood_v6``; the contract-registered ``fema_hazus_eq_2020`` slot
    # surfaces as a typed input error.
    if fragility_set == "fema_hazus_eq_2020":
        raise PelicunInputError(
            "fragility_set='fema_hazus_eq_2020' is registered but the "
            "seismic-engine integration is not implemented in v0.1. "
            "Use 'hazus_flood_v6' with a flood depth raster, or wait for "
            "the sprint-13+ seismic engine to land."
        )

    hazard_local: str | None = None
    assets_local: str | None = None
    # sprint-14-aws (job-0293b): s3:// staging also lands in a temp file the
    # finally-block must unlink — remote means either object-store scheme.
    hazard_was_remote = hazard_raster_uri.startswith(("gs://", "s3://"))
    assets_was_remote = assets_uri.startswith(("gs://", "s3://"))

    try:
        hazard_local = _download_uri_to_local(hazard_raster_uri, ".tif")
        assets_local = _download_uri_to_local(assets_uri, os.path.splitext(assets_uri)[1] or ".fgb")

        gdf = _assess_assets(
            hazard_raster_path=hazard_local,
            assets_path=assets_local,
            component_types_filter=component_types,
            realization_count=realization_count,
            hazard_uri_for_seed=hazard_raster_uri,
        )
        return _gdf_to_fgb_bytes(gdf)
    finally:
        # Only unlink files we created (not user-supplied local paths).
        if hazard_was_remote and hazard_local:
            try:
                os.unlink(hazard_local)
            except OSError:
                pass
        if assets_was_remote and assets_local:
            try:
                os.unlink(assets_local)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Registered atomic tool.
# ---------------------------------------------------------------------------


@register_tool(
    _METADATA,
    # Annotations: readOnlyHint=False (writes a FlatGeobuf artifact to GCS
    # and returns a LayerURI pointing at it — a new object is created per
    # call), openWorldHint=False (all computation is local / intra-GCP; HAZUS
    # curves are bundled in the package, no external API call),
    # destructiveHint=False (writes a fresh output file under a run-keyed
    # path; does not overwrite existing data), idempotentHint=False (Monte
    # Carlo sampling with a PRNG seed means repeated calls with the same args
    # may produce numerically different realizations unless the seed is fixed).
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
def run_pelicun_damage_assessment(
    hazard_raster_uri: str,
    assets_uri: str,
    fragility_set: FragilitySet = "hazus_flood_v6",
    component_types: list[str] | None = None,
    realization_count: int = 100,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """Fragility-curve-driven damage assessment via Pelicun.

    For each asset point or polygon in ``assets_uri``:
        1. Sample the hazard raster at the asset location.
        2. Look up the matching fragility function by ``component_type`` +
           hazard intensity (from ``fragility_set``).
        3. Monte-Carlo sample ``realization_count`` damage states.
        4. Aggregate to per-asset expected damage state + 95% CI + repair-cost
           statistics.

    Returns a ``LayerURI`` pointing at a FlatGeobuf of the asset features with
    per-feature damage properties — see "Returns" below.

    Use this when:
        - The user has a modeled or fetched hazard raster (flood depth COG,
          earthquake intensity raster) AND an asset layer (buildings, parcels,
          critical infrastructure) and wants quantitative damage / loss
          estimates over the asset set.
        - The user asks "how much damage", "expected losses", "which buildings
          are most exposed", or "monte-carlo damage assessment" on a modeled
          hazard.

    Do NOT use this for:
        - Plain hazard exposure counts (use ``compute_zonal_statistics`` with
          value=hazard raster, zone=asset polygons — cheaper and faster when
          you only need "how many assets are in the flood zone").
        - Building footprint counts or density (use ``compute_building_density``
          or ``fetch_buildings`` — they emit the asset layer this tool consumes).
        - Loss estimation without an asset layer (this tool requires per-asset
          features; if you only have aggregate population in a zone, use a
          zonal-statistics + WorldPop pipeline instead).
        - Hazards outside the available fragility sets (v0.1 ships flood;
          earthquake is registered but not wired — wildfire / wind /
          liquefaction fragility sets are gated on the seismic and wildfire
          engine work).

    Parameters:
        hazard_raster_uri: the hazard layer's ``layer_id`` HANDLE from a
            prior tool result (PREFERRED — job-0263 layer-handle
            indirection; e.g. ``"flood-depth-peak-<run_id>"`` from
            ``run_model_flood_scenario``). The server resolves the handle
            to the exact storage URI it recorded for the layer. A raw
            gs:// URI is accepted only when copied VERBATIM from a prior
            function_response; NEVER construct, guess, or pattern-match a
            gs:// path, and NEVER pass the WMS display URL
            (``https://...&LAYERS=...``) — invented or display URLs are
            rejected with ``URI_HANDLE_UNRESOLVED``. Raster CRS
            and the asset CRS are reconciled internally (assets are
            reprojected to the raster CRS for sampling). Raster ``units`` tag
            should be ``"meters"`` or ``"m"`` for HAZUS conversion; absent
            tag defaults to metres.
        assets_uri: the asset layer's ``layer_id`` HANDLE from a prior tool
            result (PREFERRED — e.g. the ``usace-nsi-...`` layer_id returned
            by ``fetch_usace_nsi``), or a verbatim gs:// URI to a FlatGeobuf
            of asset features. Points (buildings, infrastructure) and polygons
            (parcels, building footprints) are both supported. Each feature
            MAY carry a ``component_type`` property matching the
            fragility-set vocabulary (e.g. ``"RES1"`` / ``"COM1"`` for
            HAZUS); features without one default to ``"RES1"``. Each feature
            MAY carry a numeric ``replacement_value`` property in USD;
            absent values fall back to occupancy-class defaults
            (HAZUS-MH 4.2 reference, scaled to 2024 USD).
        fragility_set: which fragility curve family to use. v0.1 ships:
            - ``"hazus_flood_v6"`` — FEMA HAZUS-MH 6.1 flood depth-damage
              loss functions (the only sprint-12 wiring). Pair with flood
              depth COGs. Component vocabulary: HAZUS occupancy classes
              (RES1/RES2/COM1/.../IND1/...).
            - ``"fema_hazus_eq_2020"`` — registered for the contract but
              the seismic-engine integration is not implemented; passing
              this value raises ``PelicunInputError``.
        component_types: optional list of component-type codes to RESTRICT
            the assessment to (e.g. ``["RES1", "COM1"]`` for single-family
            residential + retail commercial). Pass ``None`` (default) to
            include every feature in ``assets_uri``. Empty list ``[]`` is
            rejected — pass ``None`` instead.
        realization_count: number of Monte-Carlo realizations per asset.
            Default 100; raise (e.g. 500-1000) for tighter 95 % CIs at the
            cost of compute. Each realization samples a loss ratio from a
            bounded lognormal distribution centred on the deterministic
            HAZUS curve value with ``σ_lnD = 0.4``.

    Returns:
        A ``LayerURI`` pointing at a FlatGeobuf in the cache bucket. Each
        output feature has the same geometry as the corresponding asset and
        carries these computed properties:

        - ``component_type_used`` (str): occupancy class used in the lookup.
        - ``fragility_curve_id`` (str): HAZUS curve ID for provenance.
        - ``hazard_depth_sampled`` (float): raster value at the asset
          centroid, in raster units (typically metres).
        - ``ds_mean`` (float): expected damage state in 0..4 (HAZUS DS0-DS4
          mapped: 0=None, 1=Slight, 2=Moderate, 3=Extensive, 4=Complete).
        - ``ds_p05`` (float): 5th-percentile damage state across realizations.
        - ``ds_p95`` (float): 95th-percentile damage state.
        - ``loss_ratio_mean`` (float): expected loss ratio (0..0.6).
        - ``loss_ratio_p95`` (float): 95th-percentile loss ratio.
        - ``repair_cost_mean`` (float): expected repair cost (USD).
        - ``repair_cost_p95`` (float): 95th-percentile repair cost (USD).
        - ``replacement_value`` (float): per-asset replacement value (USD)
          used to denominate repair cost.

        Layer metadata: ``layer_type="vector"``, ``role="primary"``,
        ``units="damage_state"``, ``style_preset="pelicun_damage_state"``.

    Assets convention (``assets_uri``):
        Preferred source (CONUS) — **USACE NSI structures** from
        ``fetch_usace_nsi``.  NSI is the authoritative U.S. National
        Structure Inventory (USACE-issued, used by FEMA / HAZUS).  Every
        feature already carries the HAZUS occupancy class
        (``component_type``) AND the per-structure replacement value
        (``replacement_value`` = ``val_struct``), so the Pelicun loop runs
        without the ``"RES1"`` default + class-default-USD fallback.  This
        is the preferred Pelicun substrate inside the United States.  When
        the bbox is outside CONUS / AK / HI, fall back to
        ``compute_building_density``.

        Fallback source — **building footprints / density grid** from
        ``compute_building_density`` (Microsoft Global ML Buildings) for
        international bboxes.  A density grid produces one point-asset per
        100 m cell (or whatever ``cell_size_m`` was requested), so the
        output damage choropleth shows spatially-varying damage aligned
        with where buildings actually exist — not with administrative
        boundaries.  Each cell defaults to ``component_type="RES1"``
        because Microsoft Buildings carries no occupancy data.

        v0.1 cache-first preference: if ``compute_building_density`` has
        already been called for the same bbox (a cache hit exists in GCS),
        pass its returned ``LayerURI.uri`` directly as ``assets_uri``.  The
        tool reads the COG, samples every non-zero cell as an asset point, and
        runs the Pelicun loop.

        Fallback only — ``fetch_administrative_boundaries(level='place')``:
        CDP polygons (Census Designated Places) cover large administrative
        areas and produce a low-resolution rectangular pattern in the damage
        output.  Use the admin-boundary fallback ONLY when building-footprint
        data is unavailable (international bbox with no Microsoft coverage,
        or explicit user request for an administrative aggregate).

        Convenience composer: ``run_pelicun_with_buildings`` (in
        ``grace2_agent.workflows.pelicun_damage_with_buildings``) encapsulates
        the "fetch building density → pass as assets" pattern in one call.

    LLM guidance:
        - Preferred CONUS pattern: ``fetch_usace_nsi(bbox)`` → use the
          returned URI as ``assets_uri`` here.  Every structure carries the
          real HAZUS ``component_type`` and ``replacement_value`` (USD), so
          the damage layer reflects per-structure occupancy + replacement
          cost rather than the RES1 + class-default fallback.
        - International fallback: ``compute_building_density(bbox)`` → use
          the returned URI as ``assets_uri`` here.  The resulting damage
          layer shows real spatial structure (building density grid)
          rather than administrative rectangles.
        - For quick composition: use the ``run_pelicun_with_buildings`` workflow
          wrapper — it handles the building-density fetch internally and
          returns the same ``LayerURI`` this tool returns.
        - Administrative-boundary fallback: ``fetch_administrative_boundaries(
          level='place')`` is acceptable when building data is unavailable.
          The output will be coarser (one point per CDP polygon) and may look
          rectangular — prefer the building-density path when precision matters.
        - Narrate ds_mean + repair_cost_mean from the returned feature
          properties — never from LLM-generated numbers (invariant 1).

    Cache: ``ttl_class="static-30d"``, ``source_class="pelicun_damage"``.
    The Monte-Carlo loop is seeded per-asset for byte-identical reproducibility
    across runs with the same inputs.

    Cross-tool dependencies:
        Upstream (consumes):
        - ``run_model_flood_scenario`` / ``postprocess_flood`` — flood-depth
          COG ``LayerURI.uri`` is the primary ``hazard_raster_uri`` input.
        - ``fetch_usace_nsi`` — preferred CONUS ``assets_uri`` source; NSI
          structures carry HAZUS ``component_type`` and ``replacement_value``
          so no fallback defaults fire.
        - ``compute_building_density`` — international ``assets_uri`` fallback;
          the returned raster COG is sampled to generate per-cell asset points.
        - ``fetch_administrative_boundaries`` — low-resolution fallback asset
          layer (CDP polygons) when building data is unavailable.
        Downstream (feeds):
        - ``publish_layer`` — pass the returned FlatGeobuf ``LayerURI`` to
          display the per-asset damage layer on the map.
        - ``run_pelicun_with_buildings`` — convenience composer that calls this
          after ``compute_building_density`` → ``density_cog_to_point_fgb``.
        - Agent narration — extract ``ds_mean`` / ``repair_cost_mean`` from
          the returned feature properties for headline numbers (Invariant 7).

    Raises:
        PelicunInputError: bad URI shape, ``fragility_set`` outside the
            allowed enum, empty ``component_types`` list, or non-positive
            ``realization_count``.
        PelicunFragilityDataError: Pelicun isn't installed or the bundled
            HAZUS CSV is missing/malformed.
        PelicunRuntimeError: I/O failure (GCS download, rasterio open,
            CRS reprojection).
        PelicunNoAssetsError: zero assets in input, zero after the
            component-types filter, or zero overlapping the hazard footprint.
    """
    # Input validation runs FIRST so the typed-error surface is preserved
    # even if downstream Pelicun runtime is unavailable.
    hazard_raster_uri = _validate_uri(hazard_raster_uri, "hazard_raster_uri")
    assets_uri = _validate_uri(assets_uri, "assets_uri")
    fragility_set_validated = _validate_fragility_set(fragility_set)
    component_types_validated = _validate_component_types(component_types)
    realization_count_validated = _validate_realization_count(realization_count)

    logger.info(
        "run_pelicun_damage_assessment: invoked "
        "hazard_raster_uri=%s assets_uri=%s fragility_set=%s "
        "component_types=%s realization_count=%d",
        hazard_raster_uri,
        assets_uri,
        fragility_set_validated,
        component_types_validated,
        realization_count_validated,
    )

    # Build the cache-key params dict. Sorted component_types so equivalent
    # call shapes hit the same cache entry.
    params = {
        "hazard_raster_uri": hazard_raster_uri,
        "assets_uri": assets_uri,
        "fragility_set": fragility_set_validated,
        "component_types": sorted(component_types_validated)
        if component_types_validated
        else None,
        "realization_count": realization_count_validated,
    }

    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="fgb",
        fetch_fn=lambda: _fetch_pelicun_damage_bytes(
            hazard_raster_uri=hazard_raster_uri,
            assets_uri=assets_uri,
            fragility_set=fragility_set_validated,
            component_types=component_types_validated,
            realization_count=realization_count_validated,
        ),
    )
    assert result.uri is not None, (
        "run_pelicun_damage_assessment is cacheable; uri must be set by read_through"
    )

    # Build a stable layer_id from the input hash.
    seed_str = f"{hazard_raster_uri}|{assets_uri}|{fragility_set_validated}"
    digest = hashlib.sha256(seed_str.encode("utf-8")).hexdigest()[:12]

    return LayerURI(
        layer_id=f"pelicun-damage-{digest}",
        name=f"Pelicun damage assessment ({fragility_set_validated})",
        layer_type="vector",
        uri=result.uri,
        style_preset="pelicun_damage_state",
        role="primary",
        units="damage_state",
        # DATA-DRIVEN LEGEND: the per-asset ``ds_mean`` choropleth key. Carries
        # ``value_field="ds_mean"`` so the generic web vector path drives the
        # fill from the real feature property -- fixing the prior
        # pelicun_damage_state(style) vs pelicun_damage(web sentinel) mismatch by
        # construction (the web no longer needs the layer name to match a string;
        # it renders ANY layer whose legend carries a value_field).
        legend=_build_damage_state_legend(),
    )
