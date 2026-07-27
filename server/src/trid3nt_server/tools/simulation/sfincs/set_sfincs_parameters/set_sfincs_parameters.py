"""``set_sfincs_parameters`` -- named-parameter SFINCS deck calibration setter.

Wraps ``hydromt_sfincs.SfincsModel.setup_manning_roughness`` /
``.setup_constant_infiltration`` (API verified live against the installed
``hydromt-sfincs==1.2.2`` -- there is no ``setup_infiltration`` method in this
version; the constant-value path is ``setup_constant_infiltration``, fed an
in-memory uniform ``xarray.DataArray`` rather than a raster file). See
``docs/validation/build-contract.md`` section 3.4 for the SetterEnvelope shape
and ``_setter_envelope.py`` for the shared copy-on-write/bounds/publish
machinery this module composes.

ASCII hyphens only.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.simulation._setter_envelope import (
    PhysicalBound,
    SetterInputError,
    SetterUpstreamError,
    build_setter_envelope,
    check_bounds,
    make_work_dir,
    new_child_id,
    publish_child,
    stage_parent,
    utc_now_iso,
)

__all__ = ["set_sfincs_parameters", "SFINCS_PARAM_BOUNDS"]

logger = logging.getLogger("trid3nt_server.tools.simulation.sfincs.set_sfincs_parameters.set_sfincs_parameters")


#: Physical bounds, named table, per-parameter literature source (lane-D brief).
#: manning_land/manning_sea: overland Manning's n plausible band 0.011-0.8
#: (build-contract 3.4; Chow 1959 -- smooth concrete/asphalt ~0.011 up through
#: dense brush/heavy-timber floodplain ~0.8). Outside that band is a WARNING; a
#: NEGATIVE Manning's n (hard_min=0) is physically meaningless -> hard error.
#: qinf: uniform SFINCS constant-infiltration rate (mm/hr); a negative rate
#: (hard_min=0) is meaningless (a source, not a loss) -> hard error; the
#: plausible ceiling is the top of measured sandy-soil infiltration capacity
#: (Rawls et al. 1983), above which is only a WARNING.
SFINCS_PARAM_BOUNDS: dict[str, PhysicalBound] = {
    "manning_land": PhysicalBound(0.011, 0.8, "s.m^-1/3", "Chow (1959) overland Manning's n compilation (build-contract 3.4 band 0.011-0.8)", hard_min=0.0),
    "manning_sea": PhysicalBound(0.011, 0.8, "s.m^-1/3", "Chow (1959) overland Manning's n compilation (build-contract 3.4 band 0.011-0.8)", hard_min=0.0),
    "qinf": PhysicalBound(0.0, 1000.0, "mm/hr", "uniform infiltration rate; ceiling = extreme sandy-soil capacity (Rawls et al. 1983)", hard_min=0.0),
}

_MANNING_DEFAULT_LAND = 0.04  # hydromt_sfincs.setup_manning_roughness default
_MANNING_DEFAULT_SEA = 0.02  # hydromt_sfincs.setup_manning_roughness default
_RGH_LEV_LAND = 0.0  # land/sea elevation split (mean sea level; hydromt default)


_METADATA = AtomicToolMetadata(
    name="set_sfincs_parameters",
    ttl_class="live-no-cache",
    source_class="param_setter",
    cacheable=False,
)


#: sfincs.inp config keys whose value/presence hydromt_sfincs.write_config()
#: silently corrupts and which the copy-on-write setter therefore restores
#: from the parent deck verbatim: the native ``epsg`` grid-CRS code (rewritten
#: from the bare integer SFINCS' Fortran reader requires into a CRS string
#: "EPSG:3857") and the separate ``crs`` passthrough line (dropped entirely).
_SFINCS_CRS_CONFIG_KEYS = ("epsg", "crs")


def _sfincs_inp_config_key(line: str) -> str:
    """The config key of a ``key = value`` sfincs.inp line ("" for a blank or
    no-``=`` line). SFINCS' key column is whitespace-padded, so strip it."""
    return line.split("=", 1)[0].strip() if "=" in line else ""


def _capture_parent_crs_lines(inp_path: Path) -> dict[str, str]:
    """Snapshot the parent deck's ``epsg`` (normalized to the bare-integer form)
    and verbatim ``crs`` lines from its sfincs.inp, keyed by config key, so they
    can be restored after hydromt's ``write_config()`` mangles them (see
    ``_restore_parent_crs_lines``). Normalizing ``epsg`` -- extracting the digit
    run from either a bare integer ("32616") or a stray CRS string
    ("EPSG:3857") -- makes the fix robust even if the parent was itself produced
    by a pre-fix (buggy) setter run. Returns ``{}`` when the file is unreadable
    (the setter's own read path will surface a real error later)."""
    out: dict[str, str] = {}
    try:
        lines = inp_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        key = _sfincs_inp_config_key(line)
        if key not in _SFINCS_CRS_CONFIG_KEYS:
            continue
        if key == "epsg":
            m = re.search(r"\d+", line.split("=", 1)[1])
            if m is not None:
                left = line.split("=", 1)[0]
                out["epsg"] = f"{left}= {m.group(0)}"
        else:  # crs -- preserve the parent's line verbatim
            out[key] = line
    return out


def _restore_parent_crs_lines(inp_path: Path, parent_crs_lines: dict[str, str]) -> None:
    """Undo ``hydromt_sfincs.SfincsModel.write_config()``'s two CRS regressions
    on the freshly written child sfincs.inp: it rewrites the native ``epsg``
    line from the bare integer SFINCS v2.3.3's Fortran reader requires
    (``sfincs_input.f90`` line 837 list-directed integer read) into a CRS
    *string* ("EPSG:3857"), and DROPS the separate ``crs = ...`` passthrough
    line -- either leaves the child deck UNSOLVABLE ("Bad integer for item 1 in
    list input", exit code 2). Restore the parent deck's exact lines so the
    child's sfincs.inp matches the ``build_sfincs_model`` deck-build format.
    Copy-on-write invariant: the CRS is never an intended change of this setter,
    so every OTHER line hydromt wrote is preserved verbatim -- only the ``epsg``
    value is corrected in place (its column formatting kept) and the dropped
    ``crs`` line re-inserted adjacent to ``epsg`` (SFINCS parses ``key = value``
    lines order-independently)."""
    if not parent_crs_lines:
        return
    try:
        lines = inp_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        key = _sfincs_inp_config_key(line)
        if key in parent_crs_lines:
            out.append(parent_crs_lines[key])
            seen.add(key)
        else:
            out.append(line)
        # A ``crs`` line write_config() dropped is re-inserted right after the
        # restored ``epsg`` line (deterministic position).
        if key == "epsg" and "crs" in parent_crs_lines and "crs" not in seen:
            out.append(parent_crs_lines["crs"])
            seen.add("crs")
    # Any captured CRS key write_config() emitted nowhere gets appended.
    for key, text in parent_crs_lines.items():
        if key not in seen:
            out.append(text)
    inp_path.write_text("\n".join(out) + "\n", encoding="utf-8")


def _land_sea_means(model: Any) -> tuple[float, float, float]:
    """Return (land_mean, sea_mean, qinf_mean) over ACTIVE cells of the
    current deck grid, falling back to hydromt's own defaults when a mask
    selects no cells (e.g. a deck with no active sea cells) or the "man"/
    "qinf" grid has not been built yet."""
    import numpy as np

    grid = model.grid
    dep = grid.get("dep")
    msk = grid.get("msk")
    if dep is None or msk is None:
        raise SetterUpstreamError(
            "SFINCS deck at parent_model_uri has no dep/msk grid -- not a "
            "built SFINCS model (run build_sfincs_model first)"
        )
    active = msk >= 1
    land_mask = active & (dep >= _RGH_LEV_LAND)
    sea_mask = active & (dep < _RGH_LEV_LAND)

    man = grid.get("manning")
    land_mean = _MANNING_DEFAULT_LAND
    sea_mean = _MANNING_DEFAULT_SEA
    if man is not None:
        if bool(land_mask.any()):
            v = float(man.where(land_mask).mean())
            if not np.isnan(v):
                land_mean = v
        if bool(sea_mask.any()):
            v = float(man.where(sea_mask).mean())
            if not np.isnan(v):
                sea_mean = v

    qinf = grid.get("qinf")
    qinf_mean = 0.0  # SFINCS scalar baseline (qinf=0.0, losses off) absent a grid
    if qinf is not None and bool(active.any()):
        v = float(qinf.where(active).mean())
        if not np.isnan(v):
            qinf_mean = v
    return land_mean, sea_mean, qinf_mean


def set_sfincs_parameters(
    parent_model_uri: str,
    changes: list[dict[str, Any]],
    _work_dir: str | None = None,
    _force_local: bool = False,
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Write Manning's roughness / infiltration into a NEW child SFINCS deck.

    **What it does:** Copy-on-write parameter setter for an existing, already
    built SFINCS deck (``build_sfincs_model`` output). Copies the parent deck
    into a fresh child deck dir, applies named parameter changes via
    ``hydromt_sfincs.SfincsModel.setup_manning_roughness`` /
    ``.setup_constant_infiltration``, writes the child, and reports before/after
    values actually read back from the written deck. The parent deck is never
    touched -- still runnable afterward.

    **When to use:** calibrating/adjusting a staged SFINCS model before
    ``run_solver`` -- "increase overland roughness by 50% before rerunning",
    "set the land Manning's n to 0.08", "turn on 5 mm/hr infiltration".

    **When NOT to use:** building a NEW deck from DEM/landcover/forcing (use
    the ``build_sfincs_model`` workflow, not this setter); spatially-distributed
    pilot-point calibration fields (out of v1 scope, frozen with the group-E
    optimizer loops); dispatching a run (use ``run_solver`` on the returned
    ``child_setup_uri``'s ``model_root_uri``).

    **Parameters:**
    - ``parent_model_uri`` (str): the parent SFINCS deck directory --
      ``s3://.../deck/``, ``file:///...``, or a bare local path. Required.
    - ``changes`` (list[dict]): one or more
      ``{"parameter": "manning_land"|"manning_sea"|"qinf", "op": "set"|"scale",
      "value": <float, required for set>, "factor": <float, required for
      scale>}``. ``manning_land``/``manning_sea`` split at mean-sea-level
      elevation (hydromt default). ``qinf`` sets a spatially-uniform constant
      infiltration rate across the whole active domain (v1 = global scope
      only; zone-masked infiltration is a follow-up). Duplicate parameter
      entries collapse to the last one.

    **Returns:** SetterEnvelope dict ``{engine="sfincs", child_setup_uri,
    parent_model, changes_applied[], plausibility[], notes[]}`` (see
    build-contract.md section 3.4). ``changes_applied[].before``/``.after``
    are means READ BACK from the written child deck, not an echo of the
    request.

    **Raises:** ``SetterInputError`` (bad/unknown parameter, missing
    value/factor, unreadable parent); ``BoundsViolation`` (a physically
    MEANINGLESS value only -- a negative Manning's n or negative qinf; an
    atypical-but-physical value outside the plausible band, Manning's n
    0.011-0.8 / qinf 0-1000 mm/hr, is carried as a WARNING with
    ``plausibility[].in_range=false`` and proceeds);
    ``SetterUpstreamError`` (hydromt_sfincs not importable, deck read/write
    failed).

    **Cross-tool dependencies:** consumes a ``build_sfincs_model`` /
    ``run_solver`` deck directory; the child's ``model_root_uri`` is a deck
    directory shaped the same way, ready for a follow-up deck-to-dispatch
    manifest build (that staging step is out of scope for this atomic setter).
    """
    if not isinstance(changes, list) or not changes:
        raise SetterInputError("changes must be a non-empty list of change dicts")

    try:
        from hydromt_sfincs import SfincsModel
    except Exception as exc:  # noqa: BLE001
        raise SetterUpstreamError(f"hydromt_sfincs not importable: {exc}") from exc
    import xarray as xr

    work_dir = make_work_dir(_work_dir)
    child_id = new_child_id()
    child_root = work_dir / child_id
    child_model_dir = child_root / "model"
    stage_parent(parent_model_uri, child_model_dir, is_dir=True)
    # Snapshot the parent deck's CRS config lines from the just-staged copy
    # BEFORE hydromt overwrites sfincs.inp: write_config() mangles the native
    # bare-integer ``epsg`` into a CRS string and drops the ``crs`` passthrough
    # line, leaving the child unsolvable (restored after write; see
    # _restore_parent_crs_lines).
    _parent_crs_lines = _capture_parent_crs_lines(child_model_dir / "sfincs.inp")

    try:
        model = SfincsModel(root=str(child_model_dir), mode="r+")
        model.read()
    except Exception as exc:  # noqa: BLE001
        raise SetterUpstreamError(f"SFINCS deck read failed at {child_model_dir}: {exc}") from exc

    land_before, sea_before, qinf_before = _land_sea_means(model)

    pending_land: float | None = None
    pending_sea: float | None = None
    pending_qinf: float | None = None
    plausibility: list[dict[str, Any]] = []
    touched_params: list[str] = []

    for change in changes:
        if not isinstance(change, dict):
            raise SetterInputError(f"each change must be a dict; got {change!r}")
        param = change.get("parameter")
        op = change.get("op")
        if param not in SFINCS_PARAM_BOUNDS:
            raise SetterInputError(
                f"unknown sfincs parameter {param!r}; supported: "
                f"{sorted(SFINCS_PARAM_BOUNDS)}"
            )
        if op not in ("set", "scale"):
            raise SetterInputError(f"op must be 'set' or 'scale'; got {op!r}")

        current = {"manning_land": land_before, "manning_sea": sea_before, "qinf": qinf_before}[param]
        if op == "set":
            if "value" not in change:
                raise SetterInputError(f"op='set' requires 'value' for parameter {param!r}")
            requested = float(change["value"])
        else:
            if "factor" not in change:
                raise SetterInputError(f"op='scale' requires 'factor' for parameter {param!r}")
            requested = current * float(change["factor"])

        entry = check_bounds(engine="sfincs", param=param, value=requested, table=SFINCS_PARAM_BOUNDS)
        # de-dup: keep only the latest plausibility entry per param
        plausibility = [p for p in plausibility if p["param"] != param] + [entry]
        touched_params.append(param)
        if param == "manning_land":
            pending_land = requested
        elif param == "manning_sea":
            pending_sea = requested
        else:
            pending_qinf = requested

    notes: list[str] = []
    if len(touched_params) != len(set(touched_params)):
        notes.append("duplicate parameter entries in changes[] collapsed to the last requested value")

    try:
        if pending_land is not None or pending_sea is not None:
            model.setup_manning_roughness(
                manning_land=pending_land if pending_land is not None else land_before,
                manning_sea=pending_sea if pending_sea is not None else sea_before,
                rgh_lev_land=_RGH_LEV_LAND,
            )
        if pending_qinf is not None:
            const = xr.full_like(model.grid["msk"], float(pending_qinf), dtype="float32")
            model.setup_constant_infiltration(qinf=const)
        model.write_grid()
        model.write_config()
        # Undo write_config()'s CRS regressions so the child sfincs.inp matches
        # the build_sfincs_model deck-build format (bare-int epsg + crs line) and
        # the child deck actually SOLVES (not merely parses).
        _restore_parent_crs_lines(child_model_dir / "sfincs.inp", _parent_crs_lines)
    except Exception as exc:  # noqa: BLE001
        raise SetterUpstreamError(f"SFINCS deck write failed at {child_model_dir}: {exc}") from exc

    try:
        reread = SfincsModel(root=str(child_model_dir), mode="r")
        reread.read()
    except Exception as exc:  # noqa: BLE001
        raise SetterUpstreamError(f"SFINCS deck re-read (write verification) failed: {exc}") from exc
    land_after, sea_after, qinf_after = _land_sea_means(reread)

    changes_applied: list[dict[str, Any]] = []
    if pending_land is not None:
        changes_applied.append({
            "param": "manning_land", "scope": "global",
            "before": round(land_before, 6), "after": round(land_after, 6), "unit": "s.m^-1/3",
        })
    if pending_sea is not None:
        changes_applied.append({
            "param": "manning_sea", "scope": "global",
            "before": round(sea_before, 6), "after": round(sea_after, 6), "unit": "s.m^-1/3",
        })
    if pending_qinf is not None:
        changes_applied.append({
            "param": "qinf", "scope": "global",
            "before": round(qinf_before, 6), "after": round(qinf_after, 6), "unit": "mm/hr",
        })

    manifest = {
        "schema_version": "v1",
        "engine": "sfincs",
        "child_id": child_id,
        "parent_model": parent_model_uri,
        "changes_applied": changes_applied,
        "created_at": utc_now_iso(),
    }
    publish = publish_child(
        child_root,
        engine="sfincs",
        child_id=child_id,
        manifest=manifest,
        prefer_s3=parent_model_uri.startswith("s3://") and not _force_local,
    )
    notes.append(f"child deck staged {publish['storage']}; parent left immutable (A.7 replace-not-reconcile)")
    notes.append(f"model_root_uri={publish['model_root_uri']}")

    return build_setter_envelope(
        engine="sfincs",
        child_setup_uri=publish["child_setup_uri"],
        parent_model=parent_model_uri,
        changes_applied=changes_applied,
        plausibility=plausibility,
        notes=notes,
    )


set_sfincs_parameters = register_tool(
    _METADATA,
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)(set_sfincs_parameters)
