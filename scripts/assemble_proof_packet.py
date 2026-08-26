#!/usr/bin/env python
"""THE DELIVERY CHECKLIST, AS A SCRIPT THAT CANNOT FORGET.

Handing NATE a template's proof used to be a remembered list - the panels, the
canvas view, the charts, the animation, the evidence JSON - and a remembered list
is one that ships without its GIF the week everybody is busy. This assembles the
whole packet for one ``<template>/<variant>`` and then REFUSES it with a named
missing-list on any gap, so the orchestrator's delivery step becomes "send exactly
what packet.json lists" and carries no judgement at all.

WHAT THE CHECKLIST IS

  1. every published layer as a full-size panel, in EMISSION ORDER
  2. the composite canvas view (the last panel) plus the contact sheet
  3. every chart the run persisted, drawn through the plugin dock's own renderer
  4. the GIF and its peak-or-final still WHEN the run is time-stepped; an explicit
     EXEMPTION line naming the physics when it is not
  5. the canary evidence JSON the whole packet was assembled from

WHAT IT VERIFIES, MECHANICALLY

  * TIME-STEPPED IS MEASURED, NEVER REMEMBERED. The frame count is read off the
    run's own SELAFIN - its header, then arithmetic over the file's length - and
    cross-checked against the worker's recorded ``ntimestep``. A run with more
    than one frame OWES a GIF; one frame is exempt with the reason written down.
  * THE GIF ACTUALLY EVOLVES. Every frame is extracted through PIL (with
    ``.copy()``, because Pillow reuses its decode buffer and a list of un-copied
    frames is N references to the last one), hashed, and required to be distinct.
  * THE GIF'S LEGEND DOES NOT. The colorbar strip must be BYTE-IDENTICAL across
    frames: a legend that shifts while the numbers behind it did not is a
    per-frame rescale, and the reader watching the ramp is watching the renderer.
  * THE PANELS ARE ALL THERE. Panel count == published (frame-collapsed) layer
    count + 1 for the canvas view, every file nonzero, and each one carries the
    RUN ID - burned into its caption and stamped into its PNG text chunk, so an
    audit months later can still tie the picture to the run.
  * NOTHING IS STALE. Any deliverable older than the evidence JSON it claims to
    show is reported STALE with both mtimes, because a proof pile that keeps the
    previous run's GIF beside this run's panels reads as complete and is not.

Env (MinIO): set -a; source .env.local; set +a
Usage:
  assemble_proof_packet.py --template coastal_tidal_surge --variant refined
  assemble_proof_packet.py --template artemis_harbor_agitation --variant refined
  assemble_proof_packet.py --template coastal_tidal_surge --variant coarse --check
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import importlib.util
import io
import json
import os
import re
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from trid3nt_server.testing.proof_paths import (  # noqa: E402
    VARIANTS,
    proof_dir,
)

__all__ = ["Animation", "ANIMATIONS", "assemble", "main", "selafin_frame_count"]

#: Where the colorbar strip starts, as a fraction of image width. The animation
#: renderer builds its figure at matplotlib's default subplot margins and steals
#: ``fraction=0.025, pad=0.01`` for the colorbar, so the map axes end at ~0.873
#: and the bar begins at ~0.881 - the cut sits between them and is a property of
#: THAT renderer, not of any one run. A layout change is meant to break the ramp
#: sanity check below rather than quietly stop covering the legend.
_LEGEND_X_FRAC = 0.875
#: A legend crop holding fewer distinct colours than this is not covering a
#: colour ramp, so asserting byte-identity over it would prove nothing.
_LEGEND_MIN_COLOURS = 16
#: PNG text keys the assembler stamps, so a delivered picture can be tied back to
#: its run without reading the pixels.
_STAMP_RUN = "trid3nt_run_id"
_STAMP_TEMPLATE = "trid3nt_template"
_STAMP_VARIANT = "trid3nt_variant"
_STAMP_CAPTION = "trid3nt_caption"
#: ``<base>_panel_NN_<slug>.png`` - the per-panel filename the layer renderer writes.
_PANEL_RE = re.compile(r"^(?P<base>.+)_panel_(?P<index>\d{2})_(?P<slug>.+)\.png$")


class PacketError(RuntimeError):
    """The packet cannot be assembled at all, and the message says why."""


# --------------------------------------------------------------------------- #
# The one DECLARATION in this file: what a template's animation shows.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Animation:
    """Which field of a run's result SELAFIN the delivered animation paints.

    The SELAFIN file itself comes from the run's ``completion.json``
    (``result_slf``) and the frame count from the file - only WHICH VARIABLE is a
    choice, and a choice is a declaration rather than something inferred from a
    filename. ``steady_reason`` is the other half: a solver with no simulation
    clock declares WHY it owes no animation, so the exemption is a stated physics
    fact in the packet instead of a hole nobody noticed.
    """

    var: str | None = None
    units: str = ""
    quantity: str | None = None
    still: str = "peak"
    mask_var: str | None = None
    mask_min: float = 0.0
    plane: str = "surface"
    steady_reason: str | None = None


#: Per TOOL, because a tool is what an evidence JSON names. Variable tokens are
#: matched against the SELAFIN's own padded names, so a token that stops matching
#: refuses loudly instead of animating a neighbouring variable.
ANIMATIONS: dict[str, Animation] = {
    "coastal_tidal_surge": Animation(
        var="WATER DEPTH", units="m", quantity="flood_depth", still="peak"),
    "telemac_rain_on_grid": Animation(
        var="WATER DEPTH", units="m", quantity="flood_depth", still="peak"),
    "telemac_river_dye": Animation(
        var="DYE", units="mg/L", quantity="dye_concentration", still="peak"),
    # A sag DECAYS toward its answer: the peak frame is the initial condition and
    # shows the reader nothing the run did, so the still is the final frame.
    "telemac_do_sag": Animation(
        var="DISSOLVED O2", units="mgO2/l", still="final"),
    # The column settles rather than builds, and the surface plane of the prism
    # stack is the one the thermocline question is asked about.
    "telemac3d_stratified_flow": Animation(
        var="TEMPERATURE", units="degC", still="final", plane="surface"),
    "tomawac_wave_field": Animation(
        var="WAVE HEIGHT", units="m", quantity="wave_height", still="peak"),
    # ARTEMIS is the phase-resolving elliptic mild-slope (Berkhoff) solver: it
    # solves a boundary-value problem for ONE monochromatic sea state and returns
    # ONE field, the steady agitation coefficient Kd. Its deck has no simulation
    # clock at all, which is why the run records no ntimestep.
    "artemis_harbor_agitation": Animation(
        var="WAVE HEIGHT", units="m", still="peak",
        steady_reason="ARTEMIS solves a steady boundary-value problem for a "
                      "single monochromatic sea state (elliptic mild-slope, "
                      "Berkhoff); the deck has no simulation clock, so the run "
                      "produces ONE field and there is no time evolution to "
                      "animate. The peak frame IS the whole answer."),
}


# --------------------------------------------------------------------------- #
# Object store
# --------------------------------------------------------------------------- #
def _s3():
    import boto3

    return boto3.client("s3", endpoint_url=os.environ["AWS_ENDPOINT_URL"],
                        region_name=os.environ.get("AWS_REGION", "us-east-1"))


def _read_json(s3, bucket: str, key: str) -> dict:
    try:
        return json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
    except Exception:  # noqa: BLE001 - absence is a fact the caller reports
        return {}


# --------------------------------------------------------------------------- #
# MEASURING the frame count - off the file, not off a remembered number
# --------------------------------------------------------------------------- #
def _record(fh) -> bytes:
    (n,) = struct.unpack(">i", fh.read(4))
    payload = fh.read(n)
    fh.read(4)
    return payload


def selafin_frame_count(head: bytes, total_bytes: int) -> tuple[int, dict]:
    """Frames in a SELAFIN, from its HEADER plus the file's length.

    A SELAFIN is Fortran sequential-unformatted with a fixed-shape header and
    then one identical block per time step, so the count is arithmetic over the
    remaining bytes - no need to pull a 58 MB result across the wire to learn that
    it has 41 frames. Returns ``(frames, how)``; ``frames`` is -1 when the
    arithmetic does not divide evenly, which means the assumption failed and the
    caller must fall back to a full read rather than trust a rounded answer.
    """
    fh = io.BytesIO(head)
    title = _record(fh)
    tag = title[72:80].decode("latin-1", "replace").upper()
    fsize = 8 if ("SERAFIND" in tag or "SELAFIND" in tag) else 4
    nbv1, nbv2 = struct.unpack(">2i", _record(fh))
    varnames = [_record(fh)[:32].decode("latin-1", "replace").strip()
                for _ in range(nbv1)]
    for _ in range(nbv2):
        _record(fh)
    iparam = struct.unpack(">10i", _record(fh))
    if iparam[9] == 1:
        _record(fh)
    nelem, npoin, ndp, _ = struct.unpack(">4i", _record(fh))

    header_bytes = (fh.tell()
                    + (nelem * ndp * 4 + 8)      # IKLE
                    + (npoin * 4 + 8)            # IPOBO
                    + 2 * (npoin * fsize + 8))   # X, Y
    frame_bytes = (fsize + 8) + nbv1 * (npoin * fsize + 8)
    frames, remainder = divmod(total_bytes - header_bytes, frame_bytes)
    how = {"method": "SELAFIN header + file length",
           "npoin": npoin, "nelem": nelem, "nbv1": nbv1,
           "varnames": varnames, "bytes": total_bytes,
           "header_bytes": header_bytes, "frame_bytes": frame_bytes}
    if remainder or frames < 0:
        return -1, {**how, "error": f"{remainder} trailing bytes - the block "
                                    "arithmetic does not describe this file"}
    return int(frames), how


def measure_frames(s3, bucket: str, run_id: str, slf: str) -> dict:
    """How many time steps the run's result SELAFIN actually carries."""
    key = f"{run_id}/{slf}"
    size = s3.head_object(Bucket=bucket, Key=key)["ContentLength"]
    head = s3.get_object(Bucket=bucket, Key=key,
                         Range="bytes=0-65535")["Body"].read()
    frames, how = selafin_frame_count(head, int(size))
    return {"frames": frames, "slf": slf, "source": f"s3://{bucket}/{key}", **how}


# --------------------------------------------------------------------------- #
# Verifying a GIF: it must EVOLVE, and its legend must not
# --------------------------------------------------------------------------- #
def verify_gif(path: Path) -> dict:
    """Frame-distinctness and legend byte-identity, straight off the encoded GIF.

    Pillow decodes a GIF frame into a buffer it REUSES on the next ``seek``, so a
    list built without ``.copy()`` is N references to the final frame and every
    distinctness check it feeds passes vacuously. The copy is the whole reason
    this check means anything.
    """
    import numpy as np
    from PIL import Image

    image = Image.open(path)
    frames = []
    for index in range(image.n_frames):
        image.seek(index)
        frames.append(np.asarray(image.convert("RGB")).copy())

    digests = [hashlib.sha256(frame.tobytes()).hexdigest() for frame in frames]
    stack = np.stack(frames)
    cut = int(stack.shape[2] * _LEGEND_X_FRAC)
    legend, field = stack[:, :, cut:, :], stack[:, :, :cut, :]
    ramp = int(len(np.unique(legend[0].reshape(-1, 3), axis=0)))
    drifted = [i for i in range(1, len(frames))
               if legend[i].tobytes() != legend[0].tobytes()]
    moved = [i for i in range(1, len(frames))
             if not np.array_equal(field[i], field[0])]
    return {
        "frames": len(frames),
        "size": [int(stack.shape[2]), int(stack.shape[1])],
        "distinct_frames": len(set(digests)),
        "legend_x_frac": _LEGEND_X_FRAC,
        "legend_ramp_colours": ramp,
        "legend_covers_a_ramp": ramp >= _LEGEND_MIN_COLOURS,
        "legend_drift_frames": drifted,
        "field_moves": bool(moved),
    }


# --------------------------------------------------------------------------- #
# PNG stamping - the run id, machine-readable, beside the one in the caption
# --------------------------------------------------------------------------- #
def stamp_png(path: Path, *, run_id: str, template: str, variant: str,
              caption: str) -> None:
    from PIL import Image, PngImagePlugin

    with Image.open(path) as image:
        payload = image.copy()
        existing = dict(image.info)
    meta = PngImagePlugin.PngInfo()
    for key, value in existing.items():
        if isinstance(value, str) and key.startswith("trid3nt_"):
            continue
        if isinstance(value, str):
            meta.add_text(key, value)
    meta.add_text(_STAMP_RUN, run_id)
    meta.add_text(_STAMP_TEMPLATE, template)
    meta.add_text(_STAMP_VARIANT, variant)
    meta.add_text(_STAMP_CAPTION, caption)
    payload.save(path, pnginfo=meta)


def read_stamp(path: Path) -> str | None:
    try:
        from PIL import Image

        with Image.open(path) as image:
            return image.info.get(_STAMP_RUN)
    except Exception:  # noqa: BLE001 - an unreadable stamp IS an absent stamp
        return None


# --------------------------------------------------------------------------- #
# The sibling render scripts, imported by path (``scripts/`` is not a package)
# --------------------------------------------------------------------------- #
def _sibling(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, module)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------- #
# Reading the variant directory
# --------------------------------------------------------------------------- #
def stem_for(template: str, variant: str) -> str:
    """The filename stem every deliverable in this variant carries."""
    return template if variant == "coarse" else f"{template}_{variant}"


def find_evidence(directory: Path, stem: str) -> Path:
    canonical = directory / f"{stem}_canary_evidence.json"
    if canonical.exists():
        return canonical
    found = sorted(directory.glob("*_evidence.json"))
    if len(found) == 1:
        return found[0]
    raise PacketError(
        f"no single evidence JSON in {directory}: expected {canonical.name}, "
        f"found {[p.name for p in found]}")


def _collapsed_layers(evidence: dict) -> list[dict]:
    """The layers as the PANEL renderer counts them - a frame series is one panel."""
    layers = [dict(x) for x in (evidence.get("layers") or [])]
    return _sibling("render_all_layers_proof")._collapse_frames(layers)


def _chart_names(evidence: dict) -> list[str | None]:
    """The DECLARED chart names the run persisted; ``[None]`` for the unnamed shape."""
    document = evidence.get("chart_spec")
    if not isinstance(document, dict) or not document:
        payloads = evidence.get("chart_payloads") or []
        return [None] * len(payloads)
    if any(key in document for key in ("vega_lite_spec", "spec", "layer", "mark")):
        return [None]
    return sorted(k for k, v in document.items() if isinstance(v, dict))


def aoi_bbox(evidence: dict, worker_metrics: dict) -> list[float] | None:
    """The 4326 corner a LOCAL mesh was built from, worker FIRST then the args.

    The open-water builds lay node 0 at the AOI's SW corner, so a result SELAFIN's
    metres are local and the bbox is what puts them back on the map. The worker's
    own ``telemac_metrics.json`` is the authority, but older parser versions never
    recorded it - and the canary's DECLARED bbox is the same corner, sitting right
    there in the evidence. Reaching for it is recovery, not invention: a run whose
    frames land at the UTM false origin is a picture of nothing.

    The run's OWN persisted answer sits between the two, for the legs whose mesh
    is projected agent-side: their worker never sees a bbox to echo and their
    question may name no bbox at all (a catchment is delineated from a pour
    point), but the ``domain_bbox`` they publish records exactly where they
    modelled. It is the run's product, which is what a packet check should cite.
    """
    for candidate in (worker_metrics.get("bbox"),
                      (evidence.get("metrics") or {}).get("domain_bbox"),
                      (evidence.get("args") or {}).get("bbox")):
        if isinstance(candidate, (list, tuple)) and len(candidate) == 4:
            return [float(v) for v in candidate]
    return None


def _intersects(a, b) -> bool:
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def _panel_groups(directory: Path) -> dict[str, list[tuple[int, Path]]]:
    groups: dict[str, list[tuple[int, Path]]] = {}
    for path in directory.glob("*_panel_*.png"):
        match = _PANEL_RE.match(path.name)
        if match:
            groups.setdefault(match["base"], []).append((int(match["index"]), path))
    return {base: sorted(items) for base, items in groups.items()}


# --------------------------------------------------------------------------- #
# Rendering the packet
# --------------------------------------------------------------------------- #
def _render(directory: Path, stem: str, evidence_path: Path, evidence: dict,
            *, template: str, variant: str, run_id: str, bucket: str,
            animation: Animation | None, frames: int, s3) -> dict:
    """Every deliverable, rendered fresh, in checklist order. Returns a report."""
    tool = str(evidence.get("tool") or template)
    title = f"{tool} - run {run_id}"
    report: dict[str, Any] = {}

    layers = _sibling("render_all_layers_proof")
    report["sheet"] = layers.render_from_evidence(
        evidence_path, out_path=directory / f"{stem}.png", title=title)

    charts = _sibling("render_run_chart_proof")
    try:
        report["charts"] = charts.render_charts(
            run_id=run_id, stem=stem, out_dir=directory, bucket=bucket,
            caption=f"{stem} - run {run_id} - the chart the run persisted")
    except SystemExit as exc:  # noqa: PERF203 - the reason IS the report
        report["charts"] = []
        report["chart_error"] = str(exc)

    if animation is None:
        report["animation_error"] = (
            f"tool {tool!r} has no entry in ANIMATIONS, so this script cannot say "
            "which field its animation paints - declare one rather than guess")
    elif frames >= 1:
        # ONE call for both cases. A steady result takes the same path and comes
        # back with ``animation: None`` and its still rendered, so the exemption
        # is the renderer's own finding rather than a branch that skipped it.
        completion = _read_json(s3, bucket, f"{run_id}/completion.json")
        origin = aoi_bbox(evidence, _read_json(s3, bucket,
                                               f"{run_id}/telemac_metrics.json"))
        report["origin_bbox_basis"] = (
            "telemac_metrics.json / the canary's declared bbox" if origin
            else "NONE - neither the worker metrics nor the evidence args carry a "
                 "bbox, so a LOCAL mesh cannot be put back on the map")
        report["animation"] = _sibling("render_selafin_animation").render_run(
            run_id=run_id, slf=str(completion.get("result_slf")), var=animation.var,
            stem=stem, out_dir=directory, units=animation.units,
            quantity=animation.quantity, bucket=bucket, plane=animation.plane,
            mask_var=animation.mask_var, mask_min=animation.mask_min,
            still=animation.still, origin_bbox=origin,
            title=f"{stem} - {animation.var} - run {run_id}")
    return report


def _rendered_paths(report: dict) -> set[Path]:
    """Exactly the files THIS invocation wrote.

    Only these get stamped. Stamping a file the assembler merely found would
    launder a stale picture into a verified one - the stamp has to mean "rendered
    from this run", not "seen next to this run's evidence".
    """
    out: set[Path] = set()
    sheet = report.get("sheet") or {}
    if sheet.get("sheet"):
        out.add(Path(sheet["sheet"]))
    for panel in sheet.get("panel_pngs") or []:
        out.add(Path(panel["path"]))
    for chart in report.get("charts") or []:
        out.add(Path(chart["chart"]))
    animation = report.get("animation") or {}
    for key in ("animation", "peak"):
        if animation.get(key):
            out.add(Path(animation[key]))
    return out


# --------------------------------------------------------------------------- #
# The checklist
# --------------------------------------------------------------------------- #
def _item(order: int, kind: str, caption: str, *, path: Path | None = None,
          evidence_mtime: float | None = None, template: str = "",
          variant: str = "", run_id: str = "", require_stamp: bool = True,
          missing: list[str], extra: dict | None = None) -> dict:
    """One checklist row, verified: present / STALE / EMPTY / MISSING."""
    row: dict[str, Any] = {"order": order, "kind": kind, "caption": caption,
                           "path": str(path) if path else None}
    if extra:
        row.update(extra)
    if path is None or not path.exists():
        row["verdict"] = "MISSING"
        missing.append(f"{kind}: {caption} - no file at "
                       f"{path if path else '(no path resolved)'}")
        return row
    size = path.stat().st_size
    row["bytes"] = size
    row["mtime"] = _dt.datetime.fromtimestamp(path.stat().st_mtime).isoformat(
        timespec="seconds")
    if size == 0:
        row["verdict"] = "EMPTY"
        missing.append(f"{kind}: {path.name} is zero bytes")
        return row
    if evidence_mtime is not None and path.stat().st_mtime < evidence_mtime - 1.0:
        row["verdict"] = "STALE"
        row["evidence_mtime"] = _dt.datetime.fromtimestamp(
            evidence_mtime).isoformat(timespec="seconds")
        missing.append(
            f"{kind}: {path.name} is STALE - written {row['mtime']}, the evidence "
            f"JSON it claims to show was written {row['evidence_mtime']}; it is a "
            "previous run's artifact sitting in this run's packet")
        return row
    if require_stamp and path.suffix.lower() == ".png":
        stamped = read_stamp(path)
        row["run_id_stamp"] = stamped
        if stamped != run_id:
            row["verdict"] = "UNSTAMPED"
            missing.append(
                f"{kind}: {path.name} carries run-id stamp {stamped!r}, not "
                f"{run_id!r} - the caption cannot be tied to this run")
            return row
    row["verdict"] = "present"
    return row


def assemble(template: str, variant: str, *, run_id: str | None = None,
             check: bool = False, bucket: str | None = None) -> dict:
    """Assemble and VERIFY one template+variant proof packet. Writes packet.json."""
    if variant not in VARIANTS:
        raise PacketError(f"{variant!r} is not a proof variant; the four are "
                          f"{list(VARIANTS)}")
    directory = Path(proof_dir(template, variant, create=not check))
    if not directory.is_dir():
        raise PacketError(f"no proof directory at {directory}")
    stem = stem_for(template, variant)
    evidence_path = find_evidence(directory, stem)
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence = (evidence.get("evidence")
                if isinstance(evidence.get("evidence"), dict) else evidence)
    evidence_mtime = evidence_path.stat().st_mtime
    tool = str(evidence.get("tool") or template)
    run_id = run_id or str(evidence.get("run_id") or "")
    if not run_id:
        raise PacketError(f"{evidence_path.name} records no run_id")
    bucket = bucket or os.environ.get("TRID3NT_RUNS_BUCKET", "trid3nt-runs")

    missing: list[str] = []
    animation = ANIMATIONS.get(tool)

    # ------------------------------------------------------------------ #
    # 1. Is this run time-stepped? MEASURED off its own SELAFIN.
    # ------------------------------------------------------------------ #
    s3 = _s3()
    completion = _read_json(s3, bucket, f"{run_id}/completion.json")
    result_slf = completion.get("result_slf")
    measured: dict[str, Any]
    if not result_slf:
        measured = {"frames": -1, "error": f"run {run_id} publishes no "
                                           "completion.json/result_slf to measure"}
    else:
        try:
            measured = measure_frames(s3, bucket, run_id, str(result_slf))
        except Exception as exc:  # noqa: BLE001 - unmeasurable IS the finding
            measured = {"frames": -1, "slf": result_slf,
                        "error": f"{type(exc).__name__}: {exc}"}
    frames = int(measured.get("frames", -1))
    recorded = completion.get("ntimestep")
    measured["recorded_ntimestep"] = recorded
    if frames < 0:
        missing.append(
            "frames: the time-stepped decision is UNMEASURABLE - "
            f"{measured.get('error')}. The GIF requirement cannot be settled "
            "without reading the run's own result file.")
    elif recorded is not None and int(recorded) != frames:
        missing.append(
            f"frames: the SELAFIN carries {frames} frames but the worker recorded "
            f"ntimestep={recorded} - the file and the metrics disagree")

    # ------------------------------------------------------------------ #
    # 2. Render, unless this is an audit of what is already on disk.
    # ------------------------------------------------------------------ #
    render_report: dict[str, Any] = {}
    if not check:
        render_report = _render(
            directory, stem, evidence_path, evidence, template=template,
            variant=variant, run_id=run_id, bucket=bucket, animation=animation,
            frames=frames, s3=s3)
        if render_report.get("animation_error"):
            missing.append(f"animation: {render_report['animation_error']}")
        if render_report.get("chart_error"):
            missing.append(f"chart: {render_report['chart_error']}")
        # WHERE the frames landed, against where the run was asked about. A LOCAL
        # mesh rendered without its origin lands at the UTM false origin and every
        # other number in the render report stays healthy while it does.
        drawn = (render_report.get("animation") or {}).get("bbox_ll")
        aoi = aoi_bbox(evidence, _read_json(s3, bucket,
                                            f"{run_id}/telemac_metrics.json"))
        if drawn and aoi and not _intersects(drawn, aoi):
            missing.append(
                f"animation: the frames were drawn over {drawn} but the run's AOI "
                f"is {aoi} - the two do not overlap, so the animation is at the "
                "UTM false origin rather than on the water")
        elif drawn and not aoi:
            missing.append(
                "animation: the run records no bbox and the evidence declares "
                "none, so there is nothing to check the frames' extent against - "
                f"they were drawn over {drawn}")
    fresh = _rendered_paths(render_report)

    # ------------------------------------------------------------------ #
    # 3. Walk the checklist over what is on disk now.
    # ------------------------------------------------------------------ #
    layers = _collapsed_layers(evidence)
    groups = _panel_groups(directory)
    # The NEWEST generation wins, not the one whose base matches the stem: a
    # folder that accumulated two panel sets under two naming conventions is
    # exactly the situation this script exists for, and picking by name would
    # audit last week's pictures against this week's evidence and call it fine.
    base = max(groups,
               key=lambda k: (max(p.stat().st_mtime for _, p in groups[k]),
                              k == stem),
               default=None)
    panels = groups.get(base or "", [])
    if len(groups) > 1:
        others = sorted(set(groups) - {base})
        missing.append(
            f"panels: {len(groups)} panel GENERATIONS live in this directory "
            f"(newest base {base!r}; also {others}). A reader handed this folder "
            "cannot tell which set belongs to the run - delete the superseded "
            "generation or move it to an addendum.")

    deliverables: list[dict] = []
    order = 0
    stamp = dict(template=template, variant=variant, run_id=run_id)

    expected_panels = len(layers) + 1
    if len(panels) != expected_panels:
        missing.append(
            f"panels: found {len(panels)} panel file(s) under base {base!r}, "
            f"expected {expected_panels} ({len(layers)} published layers + the "
            "canvas view)")
    by_index = {index: path for index, path in panels}
    for position, layer in enumerate(layers, start=1):
        order += 1
        caption = (f"{position}. {layer.get('name')} - "
                   f"{layer.get('role') or 'layer'}/"
                   f"{layer.get('layer_type') or 'unknown'}, run {run_id}")
        path = by_index.get(position)
        if path is not None and path in fresh:
            stamp_png(path, caption=caption, **stamp)
        deliverables.append(_item(order, "panel", caption, path=path,
                                  evidence_mtime=evidence_mtime, missing=missing,
                                  extra={"layer": layer.get("name")}, **stamp))

    order += 1
    caption = (f"{expected_panels}. CANVAS VIEW - all {len(layers)} layers stacked "
               f"in emission order, run {run_id}")
    path = by_index.get(expected_panels)
    if path is not None and path in fresh:
        stamp_png(path, caption=caption, **stamp)
    deliverables.append(_item(order, "canvas_view", caption, path=path,
                              evidence_mtime=evidence_mtime, missing=missing,
                              **stamp))

    order += 1
    # The sheet that goes with the panels is the one the panel base was cut from
    # (``<base>_canvas_layers.png``), which is why the base is resolved first.
    candidates = [directory / f"{base}_canvas_layers.png",
                  directory / f"{base}.png"] if base else []
    candidates.append(directory / f"{stem}.png")
    sheet = next((p for p in candidates if p.exists()), directory / f"{stem}.png")
    caption = (f"Contact sheet - every emitted layer, {expected_panels} panels in "
               f"emission order, run {run_id}")
    if sheet in fresh:
        stamp_png(sheet, caption=caption, **stamp)
    deliverables.append(_item(order, "contact_sheet", caption, path=sheet,
                              evidence_mtime=evidence_mtime, missing=missing,
                              **stamp))

    for name in _chart_names(evidence):
        order += 1
        path = directory / (f"{stem}_chart.png" if name is None
                            else f"{stem}_chart_{name}.png")
        caption = (f"Chart {name or '(unnamed)'} - the run's persisted spec through "
                   f"the plugin chart dock's own renderer, run {run_id}")
        if path in fresh:
            stamp_png(path, caption=caption, **stamp)
        deliverables.append(_item(order, "chart", caption, path=path,
                                  evidence_mtime=evidence_mtime, missing=missing,
                                  extra={"chart": name}, **stamp))

    # ------------------------------------------------------------------ #
    # 4. The animation, or the exemption that names why there is none.
    # ------------------------------------------------------------------ #
    order += 1
    gif = directory / f"{stem}_animation.gif"
    if frames > 1:
        # The GIF's run id is BURNED into every frame by the renderer and cannot
        # be read back as text, so its tie to this run is the pair of checks a
        # picture cannot fake: the frame count must equal the SELAFIN's, and it
        # must not predate the evidence beside it.
        row = _item(order, "animation",
                    f"Animation - {frames} frames on one run-scoped colour scale "
                    f"with a static legend, run {run_id}",
                    path=gif, evidence_mtime=evidence_mtime, missing=missing,
                    require_stamp=False, **stamp)
        if row["verdict"] == "present":
            try:
                checks = verify_gif(gif)
            except Exception as exc:  # noqa: BLE001 - unreadable IS the finding
                checks = {"error": f"{type(exc).__name__}: {exc}"}
            row["gif_checks"] = checks
            reasons = []
            if checks.get("error"):
                reasons.append(f"unreadable: {checks['error']}")
            else:
                if checks["frames"] != frames:
                    reasons.append(f"{checks['frames']} GIF frames against the "
                                   f"SELAFIN's {frames}")
                if checks["distinct_frames"] != checks["frames"]:
                    reasons.append(
                        f"only {checks['distinct_frames']} of {checks['frames']} "
                        "frames are distinct - repeats carry no new field data")
                if not checks["field_moves"]:
                    reasons.append("the field never changes - this is a still "
                                   "wearing an animation's extension")
                if not checks["legend_covers_a_ramp"]:
                    reasons.append(
                        f"the legend crop at x >= {_LEGEND_X_FRAC} holds only "
                        f"{checks['legend_ramp_colours']} colours, so it is not "
                        "covering the colorbar and proves nothing")
                elif checks["legend_drift_frames"]:
                    reasons.append(
                        f"the legend is not byte-identical across frames "
                        f"({len(checks['legend_drift_frames'])} of "
                        f"{checks['frames'] - 1} pairs drift) - the colour scale "
                        "is being recomputed per frame")
            if reasons:
                row["verdict"] = "FAILED"
                missing.append(f"animation: {gif.name} - " + "; ".join(reasons))
        deliverables.append(row)

        order += 1
        still = animation.still if animation else "peak"
        path = directory / f"{stem}_{still}_frame.png"
        caption = (f"{still.upper()} frame - the animation's own figure at its "
                   f"{still} step, same colours and extent, run {run_id}")
        if path in fresh:
            stamp_png(path, caption=caption, **stamp)
        deliverables.append(_item(order, "still", caption, path=path,
                                  evidence_mtime=evidence_mtime, missing=missing,
                                  **stamp))
    else:
        reason = (animation.steady_reason if animation and animation.steady_reason
                  else f"the run's result SELAFIN carries {frames} frame(s): a "
                       "single-frame (steady) result has nothing to animate")
        deliverables.append({
            "order": order, "kind": "animation", "path": None,
            "verdict": "exempt", "reason": reason,
            "caption": f"Animation - EXEMPT. {reason}"})
        order += 1
        still = animation.still if animation else "peak"
        path = directory / f"{stem}_{still}_frame.png"
        caption = (f"{still.upper()} frame - the steady field, which IS the whole "
                   f"answer rather than one sample of it, run {run_id}")
        if path in fresh:
            stamp_png(path, caption=caption, **stamp)
        deliverables.append(_item(order, "still", caption, path=path,
                                  evidence_mtime=evidence_mtime, missing=missing,
                                  **stamp))

    order += 1
    deliverables.append(_item(
        order, "evidence",
        f"Canary evidence JSON - the declaration, layers, metrics and products "
        f"this whole packet was assembled from, run {run_id}",
        path=evidence_path, missing=missing, **stamp))

    packet = {
        "what": "the delivery checklist for one template+variant, assembled and "
                "verified mechanically; send exactly what `deliverables` lists, "
                "in order",
        "template": template, "variant": variant, "tool": tool, "run_id": run_id,
        "stem": stem, "directory": str(directory), "panel_base": base,
        "mode": "check" if check else "render",
        "assembled_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "assembler": "scripts/assemble_proof_packet.py",
        "time_stepped": measured,
        "published_layers": [layer.get("name") for layer in layers],
        "verdict": "REFUSED" if missing else "PASS",
        "missing": missing,
        "deliverables": deliverables,
    }
    if render_report:
        packet["render"] = render_report
    (directory / "packet.json").write_text(
        json.dumps(packet, indent=2, default=str) + "\n", encoding="utf-8")
    return packet


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--template", required=True)
    ap.add_argument("--variant", required=True, choices=VARIANTS)
    ap.add_argument("--run", default=None,
                    help="render from THIS run prefix (default: the run id the "
                         "evidence JSON records)")
    ap.add_argument("--check", action="store_true", default=False,
                    help="audit an existing variant directory against the "
                         "checklist WITHOUT rendering anything")
    ap.add_argument("--bucket", default=None)
    ns = ap.parse_args(argv)

    try:
        packet = assemble(ns.template, ns.variant, run_id=ns.run, check=ns.check,
                          bucket=ns.bucket)
    except PacketError as exc:
        print(f"NO PACKET: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(packet, indent=2, default=str))
    if packet["verdict"] != "PASS":
        print(f"\nPACKET REFUSED - {len(packet['missing'])} gap(s):",
              file=sys.stderr)
        for gap in packet["missing"]:
            print(f"  - {gap}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
