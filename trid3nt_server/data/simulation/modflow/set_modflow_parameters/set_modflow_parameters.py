"""``set_modflow_parameters`` -- named-parameter MODFLOW 6 (flopy) calibration setter.

Wraps ``flopy.mf6`` (API verified live against the installed ``flopy==3.10.0``):
``MFSimulation.load`` -> read/reassign the NPF (``k``/``k33``), STO
(``ss``/``sy``), and RCH/RCHA (``recharge``) package arrays in place ->
``write_simulation``. See ``docs/validation/build-contract.md`` section 3.4
for the SetterEnvelope shape and ``_setter_envelope.py`` for the shared
copy-on-write/bounds/publish machinery this module composes.

v1 scope: ONE GWF model per simulation, a structured DIS grid (DISV/DISU
unstructured grids are out of scope), ``layer:<k>`` (1-based) OR global scope
for k/k33/ss/sy; recharge is global-only (RCHA has no explicit geologic-layer
axis -- it targets each column's highest active layer).

ASCII hyphens only.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.workflows.lib._setter_envelope import (
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

__all__ = ["set_modflow_parameters", "MODFLOW_PARAM_BOUNDS"]

logger = logging.getLogger("trid3nt_server.data.simulation.modflow.set_modflow_parameters.set_modflow_parameters")


#: Physical bounds, named table, per-parameter literature source (lane-D brief).
#: Soft plausible bands (lo/hi) warn when exceeded; hard floors/ceilings raise.
#: k/k33: Freeze & Cherry (1979) Table 2.2 conductivity compilation (clay
#: ~1e-9 m/day up to karst/clean gravel ~1e4 m/day); K <= 0 (hard_min=0
#: exclusive) is meaningless -> hard error, an implausibly large positive K is
#: only a WARNING.
#: recharge: natural-recharge sanity band; MODFLOW recharge MAY legitimately be
#: negative (ET/discharge), so it has NO hard floor -- out-of-band is a WARNING.
#: ss: specific storage typical range (Freeze & Cherry 1979); negative ss
#: (hard_min=0) is meaningless.
#: sy: specific yield is a drainable-porosity FRACTION -- outside 0-1
#: (hard_min=0, hard_max=1) is meaningless; the typical band is 0.01-0.5
#: (Johnson 1967 / Freeze & Cherry 1979).
MODFLOW_PARAM_BOUNDS: dict[str, PhysicalBound] = {
    "k": PhysicalBound(1e-9, 1e4, "m/day", "Freeze & Cherry (1979) Table 2.2 hydraulic conductivity range", hard_min=0.0, hard_min_exclusive=True),
    "k33": PhysicalBound(1e-9, 1e4, "m/day", "Freeze & Cherry (1979) Table 2.2 (vertical K, same envelope)", hard_min=0.0, hard_min_exclusive=True),
    "recharge": PhysicalBound(0.0, 0.05, "m/day", "natural recharge sanity ceiling (~18 m/yr, extreme karst/irrigation)"),
    "ss": PhysicalBound(1e-7, 1e-2, "1/m", "specific storage typical range (Freeze & Cherry 1979)", hard_min=0.0),
    "sy": PhysicalBound(0.01, 0.5, "dimensionless", "specific yield typical range (Johnson 1967 / Freeze & Cherry 1979)", hard_min=0.0, hard_max=1.0),
}

#: parameter -> (package_type set, attribute name).
_PARAM_PKG: dict[str, tuple[frozenset[str], str]] = {
    "k": (frozenset({"npf"}), "k"),
    "k33": (frozenset({"npf"}), "k33"),
    "ss": (frozenset({"sto"}), "ss"),
    "sy": (frozenset({"sto"}), "sy"),
    "recharge": (frozenset({"rch", "rcha"}), "recharge"),
}
_LAYERED_PARAMS = frozenset({"k", "k33", "ss", "sy"})


def _get_package(gwf: Any, types: frozenset[str]) -> Any:
    for pkg in gwf.packagelist:
        if getattr(pkg, "package_type", None) in types:
            return pkg
    return None


def set_modflow_parameters(
    parent_model_uri: str,
    changes: list[dict[str, Any]],
    _work_dir: str | None = None,
    _force_local: bool = False,
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Write hydraulic conductivity / storage / recharge into a NEW child MODFLOW 6 sim.

    **What it does:** Copy-on-write parameter setter for an existing MODFLOW 6
    simulation directory (one GWF model, structured DIS grid). Copies the
    parent ``sim_ws`` into a fresh child directory, reassigns the NPF
    (``k``/``k33``) / STO (``ss``/``sy``) / RCH(A) (``recharge``) package
    arrays via ``flopy.mf6``, writes the child, and reports before/after
    values read back from the WRITTEN child simulation. The parent sim_ws is
    never touched -- still runnable afterward.

    **When to use:** calibrating/adjusting a staged MODFLOW model before
    ``run_solver`` -- "double the hydraulic conductivity in layer 2",
    "set specific yield to 0.15 site-wide", "reduce recharge by 30%".

    **When NOT to use:** building a new MODFLOW deck from scratch; a
    DISV/DISU unstructured-grid deck or a multi-GWF-model simulation (v1
    supports one structured-DIS GWF model only -- raises a typed error
    rather than guessing which model/grid to target); pilot-point/zone-array
    K fields (out of v1 scope, frozen with the group-E calibration loops);
    dispatching a run (use ``run_solver`` on the returned ``model_root_uri``).

    **Parameters:**
    - ``parent_model_uri`` (str): the parent MODFLOW 6 ``sim_ws`` directory --
      ``s3://.../``, ``file:///...``, or a bare local path. Required.
    - ``changes`` (list[dict]): one or more
      ``{"parameter": "k"|"k33"|"ss"|"sy"|"recharge", "op": "set"|"scale",
      "value": <float, required for set>, "factor": <float, required for
      scale>, "layer": <int, 1-based, optional -- omitted = ALL layers
      (global scope); not accepted for "recharge", which has no explicit
      geologic-layer axis>}``. "set" broadcasts one value uniformly across
      the targeted layer(s); "scale" multiplies each cell in the targeted
      layer(s) by ``factor``, preserving spatial heterogeneity. Both the
      min AND max of the resulting array slice are bounds-checked (not just
      the mean), so an outlier cell cannot silently blow past the physical
      range.

    **Returns:** SetterEnvelope dict ``{engine="modflow", child_setup_uri,
    parent_model, changes_applied[], plausibility[], notes[]}`` (see
    build-contract.md section 3.4). ``changes_applied[].before``/``.after``
    are the mean over the targeted layer(s), read back from the written
    child sim.

    **Raises:** ``SetterInputError`` (unknown parameter, bad op, missing
    value/factor, out-of-range layer, layer given for recharge, missing
    package, non-DIS grid, multi-model sim); ``BoundsViolation`` (a physically
    MEANINGLESS value only -- or any resulting cell after a scale -- e.g. K <= 0,
    a negative specific storage, a specific yield outside 0-1; an atypical
    but physical value outside a plausible band is carried as a WARNING with
    ``plausibility[].in_range=false`` and proceeds); ``SetterUpstreamError``
    (flopy not importable, sim load/write failed).

    **Cross-tool dependencies:** consumes a staged MODFLOW 6 ``sim_ws``; the
    child's ``model_root_uri`` is a sim_ws directory shaped the same way,
    ready for a follow-up dispatch-manifest build (out of scope here).
    """
    if not isinstance(changes, list) or not changes:
        raise SetterInputError("changes must be a non-empty list of change dicts")

    try:
        import flopy
    except Exception as exc:  # noqa: BLE001
        raise SetterUpstreamError(f"flopy not importable: {exc}") from exc

    work_dir = make_work_dir(_work_dir)
    child_id = new_child_id()
    child_root = work_dir / child_id
    child_model_dir = child_root / "model"
    stage_parent(parent_model_uri, child_model_dir, is_dir=True)

    try:
        sim = flopy.mf6.MFSimulation.load(sim_ws=str(child_model_dir))
    except Exception as exc:  # noqa: BLE001
        raise SetterUpstreamError(f"MODFLOW 6 sim load failed at {child_model_dir}: {exc}") from exc
    if len(sim.model_names) != 1:
        raise SetterInputError(
            f"set_modflow_parameters v1 supports exactly one GWF model per "
            f"simulation; parent has {sim.model_names!r}"
        )
    gwf = sim.get_model()
    dis = _get_package(gwf, frozenset({"dis"}))
    if dis is None:
        raise SetterInputError(
            "set_modflow_parameters v1 requires a structured DIS grid; "
            "parent sim has no dis package (DISV/DISU are out of v1 scope)"
        )
    nlay = int(dis.nlay.get_data())

    baseline: dict[str, float] = {}
    scope_labels: dict[str, list[str]] = {}
    last_layer_by_param: dict[str, int | None] = {}
    plausibility: list[dict[str, Any]] = []
    pkg_cache: dict[str, Any] = {}

    for change in changes:
        if not isinstance(change, dict):
            raise SetterInputError(f"each change must be a dict; got {change!r}")
        param = change.get("parameter")
        op = change.get("op")
        if param not in _PARAM_PKG:
            raise SetterInputError(
                f"unknown modflow parameter {param!r}; supported: {sorted(_PARAM_PKG)}"
            )
        if op not in ("set", "scale"):
            raise SetterInputError(f"op must be 'set' or 'scale'; got {op!r}")
        layer = change.get("layer")
        if layer is not None and param not in _LAYERED_PARAMS:
            raise SetterInputError(f"parameter {param!r} does not accept a layer scope")
        if layer is not None:
            layer = int(layer)
            if not (1 <= layer <= nlay):
                raise SetterInputError(f"layer must be in [1, {nlay}]; got {layer}")
        if op == "set" and "value" not in change:
            raise SetterInputError(f"op='set' requires 'value' for parameter {param!r}")
        if op == "scale" and "factor" not in change:
            raise SetterInputError(f"op='scale' requires 'factor' for parameter {param!r}")

        pkg_types, attr = _PARAM_PKG[param]
        pkg = pkg_cache.get(param) or _get_package(gwf, pkg_types)
        if pkg is None:
            raise SetterInputError(
                f"parent MODFLOW sim has no package for parameter {param!r} "
                f"(need one of {sorted(pkg_types)})"
            )
        pkg_cache[param] = pkg

        is_transient = param == "recharge"  # RCH(A) is a per-stress-period
        # MFTransientArray -- flopy stores/accepts it as {period: array}, NOT
        # the dense (nper, ...) ndarray .array returns (verified live:
        # assigning the dense array raises "unhashable type: numpy.ndarray"
        # deep in MFTransientArray.set_data). Layer scoping is disallowed for
        # recharge above, so this branch is always the whole-array case.
        if is_transient:
            period_data = {k: np.array(v, dtype=float) for k, v in getattr(pkg, attr).get_data().items()}
            sel = np.stack(list(period_data.values())) if period_data else np.zeros((0,))
        else:
            arr = np.array(getattr(pkg, attr).array, dtype=float, copy=True)
            sel = arr[layer - 1] if layer is not None else arr

        old_mean = float(sel.mean())
        if op == "set":
            new_sel = np.full_like(sel, float(change["value"]))
        else:
            new_sel = sel * float(change["factor"])

        # Bounds-check the FULL resulting range, not just the mean, so a
        # heterogeneous scale cannot silently push an outlier cell past the
        # physical HARD bound (that raises); an out-of-PLAUSIBLE-band cell is a
        # warning, and the reported plausibility is in_range only when BOTH the
        # min and max stay inside the plausible band.
        entry_min = check_bounds(engine="modflow", param=param, value=float(new_sel.min()), table=MODFLOW_PARAM_BOUNDS)
        entry_max = check_bounds(engine="modflow", param=param, value=float(new_sel.max()), table=MODFLOW_PARAM_BOUNDS)
        plaus_entry = dict(
            entry_max,
            value=round(float(new_sel.mean()), 8),
            in_range=bool(entry_min["in_range"] and entry_max["in_range"]),
        )

        if is_transient:
            new_period_data = {}
            for i, key in enumerate(period_data):
                new_period_data[key] = new_sel[i]
            setattr(pkg, attr, new_period_data)
        elif layer is not None:
            arr[layer - 1] = new_sel
            setattr(pkg, attr, arr)
        else:
            setattr(pkg, attr, new_sel)

        if param not in baseline:
            baseline[param] = old_mean
            scope_labels[param] = []
        label = f"layer:{layer}" if layer is not None else "global"
        if label not in scope_labels[param]:
            scope_labels[param].append(label)
        last_layer_by_param[param] = layer
        plausibility = [p for p in plausibility if p["param"] != param] + [plaus_entry]

    try:
        sim.write_simulation()
    except Exception as exc:  # noqa: BLE001
        raise SetterUpstreamError(f"MODFLOW 6 sim write failed at {child_model_dir}: {exc}") from exc

    try:
        reread_sim = flopy.mf6.MFSimulation.load(sim_ws=str(child_model_dir))
        reread_gwf = reread_sim.get_model()
    except Exception as exc:  # noqa: BLE001
        raise SetterUpstreamError(f"MODFLOW 6 sim re-load (write verification) failed: {exc}") from exc

    changes_applied: list[dict[str, Any]] = []
    for param, labels in scope_labels.items():
        pkg_types, attr = _PARAM_PKG[param]
        re_pkg = _get_package(reread_gwf, pkg_types)
        re_arr = np.array(getattr(re_pkg, attr).array, dtype=float)
        # multiple distinct layer scopes touched in one call -> report the
        # mean over the LAST-touched layer (matches "after" being the final
        # written state); a single scope reports that scope's own mean.
        final_layer = last_layer_by_param[param]
        if final_layer is not None:
            after_mean = float(re_arr[final_layer - 1].mean())
        else:
            after_mean = float(re_arr.mean())
        scope = labels[0] if len(labels) == 1 else "+".join(labels)
        changes_applied.append({
            "param": param,
            "scope": scope,
            "before": round(baseline[param], 8),
            "after": round(after_mean, 8),
            "unit": MODFLOW_PARAM_BOUNDS[param].unit,
        })

    manifest = {
        "schema_version": "v1",
        "engine": "modflow",
        "child_id": child_id,
        "parent_model": parent_model_uri,
        "changes_applied": changes_applied,
        "created_at": utc_now_iso(),
    }
    publish = publish_child(
        child_root,
        engine="modflow",
        child_id=child_id,
        manifest=manifest,
        prefer_s3=parent_model_uri.startswith("s3://") and not _force_local,
    )
    notes = [
        f"child sim_ws staged {publish['storage']}; parent left immutable (A.7 replace-not-reconcile)",
        f"model_root_uri={publish['model_root_uri']} gwf_model={reread_gwf.name}",
    ]

    return build_setter_envelope(
        engine="modflow",
        child_setup_uri=publish["child_setup_uri"],
        parent_model=parent_model_uri,
        changes_applied=changes_applied,
        plausibility=plausibility,
        notes=notes,
    )


set_modflow_parameters = register_tool(
    AtomicToolMetadata(
        name="set_modflow_parameters",
        ttl_class="live-no-cache",
        source_class="param_setter",
        cacheable=False,
    ),
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)(set_modflow_parameters)
