"""``compose_case_report`` - a markdown situation report for a Case, written into
the case artifacts dir. Nothing here recomputes or invents: a layer whose artifact
cannot be read gets an explicit "statistics unavailable" row, a Case with zero
layers still produces a report saying so, and the exposure section appears only
when it was actually measured."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.processing.query_point_hazard.query_point_hazard import layers_from_case, resolve_case_id

__all__ = [
    "compose_case_report",
    "CaseReportError",
    "CaseReportInputError",
    "CaseReportNotFoundError",
]

logger = logging.getLogger("trid3nt_server.tools.meta.compose_case_report.compose_case_report")


# ---------------------------------------------------------------------------
# Typed errors.
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
    """One honest stats fragment for a layer; NEVER raises - an unreadable layer
    comes back as a "statistics unavailable" sentence. The staged artifact is read
    directly, so the report never routes through the cache bucket."""
    from trid3nt_server.emission.charts import (
        _layer_type,
        _materialize_uri,
        _summarize_raster,
        _summarize_vector,
    )
    from trid3nt_server.tools._uri_util import _strip_query

    uri = str(layer.get("uri") or "")
    if not uri:
        return "statistics unavailable: layer has no uri"
    try:
        resolved = uri if uri.startswith("s3://") else _strip_query(uri)
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
    """Best-effort simulation parameters off the case record. The Case contract
    carries no typed sim-params field, so a few plausible attribute names are
    probed defensively and an absence is stated rather than filled in."""
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
    """The case artifacts dir: ``$TRID3NT_EXPORT_DIR`` (or ``~/trid3nt-exports``)
    over ``<case>-<hash>/``, created if absent."""
    if output_dir:
        out = Path(output_dir).expanduser()
    else:
        base = Path(
            os.environ.get("TRID3NT_EXPORT_DIR") or (Path.home() / "trid3nt-exports")
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
    # Absorb model-invented kwargs.
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Write a markdown situation report for the current Case and return its path.

    ROUTING: "write up this case", "give me a situation report", "export a briefing
    of what we found" - the closing step after a solve and an exposure analysis. NOT
    for a single number, NOT a bundle of the layers; this writes ONE markdown file.

    The report carries the title and date, the AOI bbox, a row per loaded layer with
    its statistics, simulation parameters when the case record has any, and exposure
    numbers only when they were actually measured this session.

    `case_id` defaults to the turn's Case, `output_dir` to the case artifacts dir,
    and `include_layer_stats=False` gives a fast listing that stages nothing.
    Returns {status, report_path, output_dir, case_id, case_title, layer_count,
    stats_computed_count, stats_unavailable_count, has_exposure_summary,
    generated_at} - no map layer. A per-layer stat failure shows INSIDE the report as
    an honest "statistics unavailable" row and in the counts; nothing is invented.
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
        with tempfile.TemporaryDirectory(prefix="trid3nt_case_report_") as tmpdir:
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
    from trid3nt_server.tools.processing.compute_exposure_summary.compute_exposure_summary import get_session_exposure

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
