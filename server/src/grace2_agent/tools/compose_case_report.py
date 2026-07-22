"""``compose_case_report`` atomic tool -- markdown situation report for a Case.

Generates a plain-markdown situation report for the current Case and writes it
into the case artifacts dir (the ``export_case_to_qgis`` output convention:
``${GRACE2_EXPORT_DIR or ~/trid3nt-exports}/<case>-<hash>/``). Sections:

    1. Title + generation date + Case id.
    2. AOI bbox (EPSG:4326) when the Case carries one.
    3. Layers: one row per loaded layer with key stats -- raster min/max/mean
       + valid-cell count (via the ``summarize_layer_statistics`` machinery:
       the SAME ``_summarize_raster`` / ``_summarize_vector`` helpers, called
       directly on the staged artifact so no cache write happens), vector
       feature count + leading numeric attributes.
    4. Simulation parameters, when the case record carries any (the Case
       contract has no typed sim-params field today; this section probes the
       record defensively and states the honest absence otherwise).
    5. Exposure summary, when ``compute_exposure_summary`` ran THIS SESSION
       for this Case (read from its in-memory session store) -- never
       recomputed or invented here.

Honesty
=======

- A layer whose artifact cannot be read gets an explicit
  "statistics unavailable: <reason>" row -- the report never fabricates
  stats and never silently drops a layer.
- A Case with zero layers still produces a report, with an explicit
  "no layers loaded" line (the report IS the artifact; it states emptiness
  honestly rather than refusing).
- The returned dict is deliberately LayerURI-free: this tool writes a local
  file, not a map layer, so nothing must trip the ``add_loaded_layer``
  isinstance gate.

``cacheable=False`` (``ttl_class="live-no-cache"``): writes a local artifact
and reads live session/case state.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from grace2_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from .query_point_hazard import layers_from_case, resolve_case_id

__all__ = [
    "compose_case_report",
    "CaseReportError",
    "CaseReportInputError",
    "CaseReportNotFoundError",
]

logger = logging.getLogger("grace2_agent.tools.compose_case_report")


# ---------------------------------------------------------------------------
# Typed errors (FR-AS-11).
# ---------------------------------------------------------------------------


class CaseReportError(RuntimeError):
    """Base class for compose_case_report failures."""

    error_code: str = "CASE_REPORT_ERROR"
    retryable: bool = True


class CaseReportInputError(CaseReportError):
    """No Case is identifiable (no case_id and no turn-bound Case)."""

    error_code = "CASE_REPORT_INPUT_INVALID"
    retryable = False


class CaseReportNotFoundError(CaseReportError):
    """The Case does not exist or persistence is unreachable."""

    error_code = "CASE_REPORT_CASE_NOT_FOUND"
    retryable = True


# ---------------------------------------------------------------------------
# Metadata.
# ---------------------------------------------------------------------------

_METADATA = AtomicToolMetadata(
    name="compose_case_report",
    ttl_class="live-no-cache",
    source_class=None,
    cacheable=False,
)

#: Stats are computed for at most this many layers (a runaway case with
#: hundreds of frames should not stage hundreds of COGs for one report).
_MAX_STAT_LAYERS = 24

#: At most this many numeric vector attributes are tabulated per layer.
_MAX_VECTOR_ATTRS = 4


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _sanitize_name(name: str) -> str:
    token = re.sub(r"[^A-Za-z0-9_-]+", "_", (name or "").strip()).strip("_")
    return token or "case"


def _short_hash(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:8]


def _fmt(v: Any) -> str:
    """Compact numeric formatting for report tables."""
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.4g}"
    return str(v)


def _layer_stats_line(layer: dict[str, Any], tmpdir: str) -> str:
    """One honest stats fragment for a layer (never raises).

    Reuses the ``summarize_layer_statistics`` machinery directly
    (``analytical_qa._materialize_uri`` + ``_summarize_raster`` /
    ``_summarize_vector``) on the staged artifact -- same numbers as the
    registered tool, without routing the report through the cache bucket.
    """
    from .analytical_qa import (
        _layer_type,
        _materialize_uri,
        _summarize_raster,
        _summarize_vector,
    )
    from .export_case_to_qgis import _strip_query, _unwrap_tile_template

    uri = str(layer.get("uri") or "")
    if not uri:
        return "statistics unavailable: layer has no uri"
    try:
        resolved = _unwrap_tile_template(uri)
        if not resolved.startswith("s3://"):
            resolved = _strip_query(resolved)
        local = _materialize_uri(resolved, tmpdir, _sanitize_name(layer.get("name") or "layer"))
        declared = layer.get("layer_type")
        ltype = declared if declared in ("raster", "vector") else _layer_type(local)
        if ltype == "raster":
            s = _summarize_raster(local)
            units = s.get("units") or layer.get("units")
            unit_sfx = f" {units}" if units else ""
            if not s.get("count"):
                return "raster: no valid pixels"
            return (
                f"raster: min {_fmt(s['min'])}{unit_sfx}, "
                f"max {_fmt(s['max'])}{unit_sfx}, "
                f"mean {_fmt(s['mean'])}{unit_sfx}, "
                f"valid cells {s['count']}"
            )
        s = _summarize_vector(local)
        frags = [f"vector: {s['feature_count']} feature(s)"]
        for attr, st in list(s.get("attribute_summary", {}).items())[:_MAX_VECTOR_ATTRS]:
            if st.get("count"):
                frags.append(
                    f"{attr} min {_fmt(st['min'])} / max {_fmt(st['max'])} / "
                    f"mean {_fmt(st['mean'])}"
                )
        return "; ".join(frags)
    except Exception as exc:  # noqa: BLE001 -- honest per-layer degrade
        logger.warning(
            "compose_case_report: stats unavailable for layer %r: %s",
            layer.get("name"),
            exc,
        )
        return f"statistics unavailable: {type(exc).__name__}: {exc}"


def _simulation_parameters(case: Any) -> dict[str, Any]:
    """Best-effort simulation parameters off the case record.

    The Case contract (``CaseSummary``) carries no typed sim-params field
    today; ``primary_hazard`` is the one denormalized run hint. Probe a few
    plausible attribute names defensively so a future/extended record is
    picked up without a contract change here.
    """
    params: dict[str, Any] = {}
    hazard = getattr(case, "primary_hazard", None)
    if hazard:
        params["primary_hazard"] = hazard
    for attr in ("simulation_parameters", "sim_params", "run_settings"):
        candidate = getattr(case, attr, None)
        if isinstance(candidate, dict) and candidate:
            params[attr] = candidate
    return params


def _resolve_output_dir(case_id: str, title: str, output_dir: str | None) -> Path:
    """The case artifacts dir -- the export_case_to_qgis convention."""
    if output_dir:
        out = Path(output_dir).expanduser()
    else:
        base = Path(
            os.environ.get("GRACE2_EXPORT_DIR") or (Path.home() / "trid3nt-exports")
        )
        out = base / f"{_sanitize_name(case_id)}-{_short_hash(case_id, title)}"
    out.mkdir(parents=True, exist_ok=True)
    return out


# ---------------------------------------------------------------------------
# Registered tool.
# ---------------------------------------------------------------------------


@register_tool(
    _METADATA,
    # Writes the .md artifact (side effect); reads object storage for stats.
    read_only_hint=False,
    open_world_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
)
async def compose_case_report(
    case_id: str | None = None,
    output_dir: str | None = None,
    include_layer_stats: bool = True,
    # job-0164: absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Generate a markdown situation report for the current Case.

    **What it does:** Writes a plain-markdown situation report --
    title, generation date, AOI bbox, a table of every loaded layer with key
    statistics (raster min/max/mean via the ``summarize_layer_statistics``
    machinery; vector feature counts), simulation parameters when the case
    record carries any, and the exposure numbers when
    ``compute_exposure_summary`` ran this session for this Case -- into the
    case artifacts dir and returns the file path.

    **When to use:**
    - "Write up / summarize this case", "give me a situation report",
      "export a briefing of what we found".
    - As the closing step after a hazard solve + exposure analysis.

    **When NOT to use:**
    - A desktop-GIS bundle of the layers themselves -- use
      ``export_case_to_qgis``.
    - A single number ("how many people are flooded") -- use
      ``compute_exposure_summary`` / ``compute_zonal_statistics`` directly.

    **Parameters:**
    - ``case_id``: the Case to report on. Default: the turn's bound Case.
    - ``output_dir``: destination folder. Default:
      ``${GRACE2_EXPORT_DIR or ~/trid3nt-exports}/<case>-<hash>/``.
    - ``include_layer_stats``: compute per-layer statistics (default True;
      pass False for a fast layer listing without staging artifacts).

    **Returns:** a LayerURI-free dict:
    ``{status: "ok", report_path, output_dir, case_id, case_title,
    layer_count, stats_computed_count, stats_unavailable_count,
    has_exposure_summary, generated_at}``. Per-layer stat failures appear
    INSIDE the report as honest "statistics unavailable" rows (and in the
    counts); they never fabricate numbers.

    **Errors (FR-AS-11):** ``CaseReportInputError`` (no case identifiable),
    ``CaseReportNotFoundError`` (case missing / persistence unreachable).
    """
    resolved_case = resolve_case_id(case_id, CaseReportInputError)
    layers, case_bbox, case_title, case = await layers_from_case(
        resolved_case, CaseReportNotFoundError
    )

    generated_at = datetime.now(timezone.utc)
    lines: list[str] = [
        f"# Situation report: {case_title}",
        "",
        f"- Generated: {generated_at.strftime('%Y-%m-%d %H:%M UTC')}",
        f"- Case id: `{resolved_case}`",
    ]
    created = getattr(case, "created_at", None)
    if created is not None:
        lines.append(f"- Case created: {created}")

    # ---- AOI ---------------------------------------------------------------
    lines += ["", "## Area of interest", ""]
    if case_bbox and len(case_bbox) == 4:
        lines.append(
            "Bounding box (EPSG:4326, min_lon, min_lat, max_lon, max_lat): "
            f"`{case_bbox[0]:.5f}, {case_bbox[1]:.5f}, "
            f"{case_bbox[2]:.5f}, {case_bbox[3]:.5f}`"
        )
    else:
        lines.append("No AOI bbox is recorded on this case.")

    # ---- Layers ------------------------------------------------------------
    lines += ["", "## Layers", ""]
    stats_ok = 0
    stats_failed = 0
    if not layers:
        lines.append(
            "No layers are loaded on this case (nothing has been fetched or "
            "computed yet)."
        )
    else:
        lines.append(f"{len(layers)} layer(s) loaded:")
        lines.append("")
        capped = layers[:_MAX_STAT_LAYERS]
        with tempfile.TemporaryDirectory(prefix="grace2_case_report_") as tmpdir:
            for layer in capped:
                name = str(layer.get("name") or layer.get("layer_id") or "unnamed")
                ltype = str(layer.get("layer_type") or "unknown")
                if include_layer_stats:
                    stats = _layer_stats_line(layer, tmpdir)
                    if stats.startswith("statistics unavailable"):
                        stats_failed += 1
                    else:
                        stats_ok += 1
                    lines.append(f"- **{name}** ({ltype}): {stats}")
                else:
                    lines.append(f"- **{name}** ({ltype})")
        if len(layers) > _MAX_STAT_LAYERS:
            lines.append(
                f"- ... plus {len(layers) - _MAX_STAT_LAYERS} more layer(s) "
                f"(statistics capped at {_MAX_STAT_LAYERS} layers per report)."
            )

    # ---- Simulation parameters ----------------------------------------------
    lines += ["", "## Simulation parameters", ""]
    sim = _simulation_parameters(case)
    if sim:
        for key, value in sim.items():
            if isinstance(value, dict):
                lines.append(f"- {key}:")
                for k, v in value.items():
                    lines.append(f"  - {k}: {_fmt(v)}")
            else:
                lines.append(f"- {key}: {_fmt(value)}")
    else:
        lines.append("No simulation parameters are recorded on this case.")

    # ---- Exposure summary (session store; never recomputed here) ------------
    lines += ["", "## Exposure summary", ""]
    from .compute_exposure_summary import get_session_exposure

    exposure = get_session_exposure(resolved_case)
    has_exposure = exposure is not None
    if exposure is not None:
        thr = exposure.get("threshold")
        lines.append(
            "From compute_exposure_summary (this session), footprint = "
            + ("any wet cell" if thr is None else f"value > {_fmt(thr)}")
            + ":"
        )
        lines.append("")
        pop = exposure.get("population")
        bld = exposure.get("buildings")
        err = exposure.get("errors") or {}
        lines.append(
            f"- Population exposed: {_fmt(pop)}"
            + (f" (unavailable: {err['population']})" if pop is None and "population" in err else "")
        )
        lines.append(
            f"- Buildings exposed: {_fmt(bld)}"
            + (f" (unavailable: {err['buildings']})" if bld is None and "buildings" in err else "")
        )
        lines.append(f"- Footprint area: {_fmt(exposure.get('area_km2'))} km^2")
        src_uri = exposure.get("hazard_layer_uri")
        if src_uri:
            lines.append(f"- Hazard layer: `{src_uri}`")
    else:
        lines.append(
            "No exposure summary was computed this session. Run "
            "compute_exposure_summary on a hazard layer to add population / "
            "building / area numbers here."
        )

    lines += [
        "",
        "---",
        "Generated by TRID3NT compose_case_report. Per-layer statistics come "
        "from the loaded artifacts; unavailable statistics are stated, never "
        "estimated.",
        "",
    ]

    out = _resolve_output_dir(resolved_case, case_title, output_dir)
    filename = (
        f"situation_report_{generated_at.strftime('%Y%m%d')}_"
        f"{_short_hash(resolved_case, generated_at.isoformat())}.md"
    )
    report_path = out / filename
    report_path.write_text("\n".join(lines), encoding="utf-8")

    logger.info(
        "compose_case_report: case=%s layers=%d stats_ok=%d stats_failed=%d "
        "exposure=%s -> %s",
        resolved_case,
        len(layers),
        stats_ok,
        stats_failed,
        has_exposure,
        report_path,
    )
    return {
        "status": "ok",
        "report_path": str(report_path),
        "output_dir": str(out),
        "case_id": resolved_case,
        "case_title": case_title,
        "layer_count": len(layers),
        "stats_computed_count": stats_ok,
        "stats_unavailable_count": stats_failed,
        "has_exposure_summary": has_exposure,
        "generated_at": generated_at.isoformat(),
    }
