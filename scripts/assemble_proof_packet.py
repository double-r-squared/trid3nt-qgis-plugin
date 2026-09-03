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
  4. EVERY animation the template declares, each with its peak-or-final still,
     when the run is time-stepped; an explicit EXEMPTION line naming the physics
     when it is not. A template may declare more than one - a coastal solve
     answers both "how did the water surface move" and "where did it go onto
     land", which are two variables, two masks and two scales off one SELAFIN -
     and the checklist requires ALL of them
  5. the canary evidence JSON the whole packet was assembled from

WHAT IT VERIFIES, MECHANICALLY

  * TIME-STEPPED IS MEASURED, NEVER REMEMBERED. The frame count is read off the
    run's own SELAFIN - its header, then arithmetic over the file's length - and
    cross-checked against the worker's recorded ``ntimestep`` - a second reader
    of the same file, and a time-stepped run that offers none is itself a gap. A
    run with more than one frame OWES a GIF; one frame is exempt with the reason
    written down.
  * THE GIF ACTUALLY EVOLVES. Every frame is extracted through PIL (with
    ``.copy()``, because Pillow reuses its decode buffer and a list of un-copied
    frames is N references to the last one), hashed, and required to be distinct.
  * THE GIF'S LEGEND DOES NOT. The colorbar strip must be BYTE-IDENTICAL across
    frames: a legend that shifts while the numbers behind it did not is a
    per-frame rescale, and the reader watching the ramp is watching the renderer.
  * ONE SCALE PER QUANTITY. The animation and its still are painted on the range
    the PUBLISHED raster of the same quantity carries, and the packet reports the
    pair. A GIF on its own percentile stretch beside a panel on another is two
    legends for one field, and the value the narrow one saturates is usually the
    run's own headline number.
  * THE PANELS ARE ALL THERE. Panel count == published (frame-collapsed) layer
    count + 1 for the canvas view, every file nonzero, and each one carries the
    RUN ID - burned into its caption and stamped into its PNG text chunk, so an
    audit months later can still tie the picture to the run.
  * NOTHING IS STALE. Any deliverable older than the evidence JSON it claims to
    show is reported STALE with both mtimes, because a proof pile that keeps the
    previous run's GIF beside this run's panels reads as complete and is not.
  * THE ANIMATED FIELD IS DECLARED, NEVER DEFAULTED.
    ``trid3nt_server.testing.proof_animations`` rules which variable each
    template's animation paints, which mask gates it, which still to keep and
    WHY; a time-stepped template with no declaration REFUSES rather than
    animating whatever the renderer would have picked. That refusal exists
    because the alternative already shipped once: a default painted coastal
    WATER DEPTH - bathymetry-dominated, barely moving - where the ruled field
    was FREE SURFACE masked to wet nodes, and every other check passed.

Env (MinIO): set -a; source .env.local; set +a
Usage:
  assemble_proof_packet.py --template artemis_harbor_agitation --variant refined
  assemble_proof_packet.py --template artemis_harbor_agitation --variant coarse --check
  assemble_proof_packet.py --template telemac_river_dye --variant coarse --out-dir DIR
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
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from trid3nt_server.testing.proof_animations import (  # noqa: E402
    ProofAnimation,
    animations_for,
    packet_notes,
    suffixed,
)
from trid3nt_server.testing.proof_paths import (  # noqa: E402
    VARIANTS,
    proof_dir,
)

__all__ = ["assemble", "main", "selafin_frame_count"]

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


def _declaration_row(animation: ProofAnimation, declared: int) -> dict:
    """One declaration, as the packet reports it - what was ruled, and why."""
    return {"name": animation.name, "variable": animation.variable,
            "units": animation.units, "quantity": animation.quantity,
            "mask_var": animation.mask_var,
            "mask_threshold": animation.mask_threshold,
            "dry_land_only": animation.dry_land_only,
            "derived": list(animation.derived), "transform": animation.transform,
            "vectors": animation.vectors, "still_vectors": animation.still_vectors,
            "vector_density": animation.vector_density,
            "vector_grid_n": animation.vector_grid_n,
            "arrow_size": animation.arrow_size,
            "vector_lw": list(animation.vector_lw),
            "still": animation.still, "plane": animation.plane,
            "suffix": suffixed(animation, declared),
            "reason": animation.reason,
            "exempt_reason": animation.exempt_reason,
            "declared_in": "trid3nt_server/testing/proof_animations.py"}


def published_scale(evidence: dict, animation: ProofAnimation) -> dict:
    """ONE range per quantity: the PUBLISHED raster's, for this animation to adopt.

    A percentile read over the frames and a percentile read over a peak envelope
    are two reads of two different distributions, so a packet that lets each
    renderer pick its own ships two scales for one quantity and the reader is left
    to notice. The published layer is the product a reader also meets on the map,
    so its range is the one the GIF and its still are held to. A quantity with no
    published raster of its own has nothing to agree with, and the row says that
    rather than inventing an agreement.
    """
    from trid3nt_server.emission import styles

    quantity = animation.quantity
    preset = styles.resolve_style_preset(quantity)[0] if quantity else None
    ranges: list[tuple[float, float]] = []
    names: list[str] = []
    for layer in evidence.get("layers") or []:
        legend = layer.get("legend")
        if layer.get("style_preset") != preset or not isinstance(legend, dict):
            continue
        if legend.get("kind") != "continuous":
            continue
        lo, hi = legend.get("vmin"), legend.get("vmax")
        if lo is None or hi is None:
            continue
        ranges.append((float(lo), float(hi)))
        names.append(str(layer.get("name")))
    found = styles.shared_range(ranges)
    return {"quantity": quantity, "preset": preset,
            "published_range": list(found) if found else None,
            "published_by": names}


def _field_label(animation: ProofAnimation) -> str:
    """The one-line description of what an animation paints."""
    bits = [str(animation.variable)]
    if animation.derived:
        bits.append("derived from " + " / ".join(animation.derived))
    if animation.mask_var:
        bits.append(f"masked to {animation.mask_var} > {animation.mask_threshold}")
    if animation.dry_land_only:
        bits.append("over INITIALLY-DRY land only")
    if animation.vectors:
        bits.append(f"with {animation.vectors} (density "
                    f"{animation.vector_density:g}, arrow "
                    f"{animation.arrow_size:g})")
        if animation.still_vectors and animation.still_vectors != animation.vectors:
            bits.append(f"still as {animation.still_vectors}")
    if animation.transform and animation.transform != "linear":
        bits.append(f"on a {animation.transform.upper()} ramp")
    return " ".join(bits) + f" ({animation.units})"


# --------------------------------------------------------------------------- #
# Rendering the packet
# --------------------------------------------------------------------------- #
def _render(directory: Path, stem: str, evidence_path: Path, evidence: dict,
            *, template: str, variant: str, run_id: str, bucket: str,
            declared: tuple, frames: int, s3) -> dict:
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

    # NO DEFAULT VARIABLE, EVER. An undeclared template REFUSES rather than
    # animating whatever the renderer would have picked: the one regression this
    # script has shipped was a fallback painting WATER DEPTH (bathymetry-
    # dominated, barely moves) where the ruled field was a masked FREE SURFACE.
    if not declared or declared[0].variable is None:
        report["animation_error"] = (
            f"tool {tool!r} has no declared field in "
            "trid3nt_server/testing/proof_animations.PROOF_ANIMATIONS, so this "
            "script cannot say which variable its animation paints. Declare one "
            "- with its mask and its physics reason - rather than let a default "
            "choose the picture.")
        return report
    if frames < 1:
        return report

    completion = _read_json(s3, bucket, f"{run_id}/completion.json")
    metrics = _read_json(s3, bucket, f"{run_id}/telemac_metrics.json")
    origin = aoi_bbox(evidence, metrics)
    report["origin_bbox_basis"] = (
        "telemac_metrics.json / the canary's declared bbox" if origin
        else "NONE - neither the worker metrics nor the evidence args carry a "
             "bbox, so a LOCAL mesh cannot be put back on the map")

    # EVERY declared animation, not the first one. A template that says its run
    # answers two questions owes two pictures, and the checklist below requires
    # both - one rendered and one forgotten is the same delivery gap the whole
    # script exists to close, just at a finer grain.
    report["animations"] = {}
    report["scales"] = {}
    for animation in declared:
        scale = published_scale(evidence, animation)
        report["scales"][animation.name] = scale
        initial_wl = None
        if animation.dry_land_only:
            initial_wl = completion.get("init_wl_m", metrics.get("init_wl_m"))
            if initial_wl is None:
                report.setdefault("animation_errors", []).append(
                    f"{animation.name}: the declaration asks for initially-dry "
                    f"land only, but run {run_id} records no init_wl_m to gate "
                    "on - the mask cannot be the scalar's mask if the number is "
                    "not the run's own")
                continue
        report["animations"][animation.name] = _sibling(
            "render_selafin_animation").render_run(
            run_id=run_id, slf=str(completion.get("result_slf")),
            var=animation.variable, stem=stem, out_dir=directory,
            units=animation.units, quantity=animation.quantity, bucket=bucket,
            plane=animation.plane, mask_var=animation.mask_var,
            mask_min=animation.mask_threshold, still=animation.still,
            origin_bbox=origin, initial_water_level=initial_wl,
            derived=animation.derived, transform=animation.transform,
            vectors=animation.vectors, vector_density=animation.vector_density,
            vector_grid_n=animation.vector_grid_n,
            arrow_size=animation.arrow_size, vector_lw=animation.vector_lw,
            still_vectors=animation.still_vectors,
            shared_range=(tuple(scale["published_range"])
                          if scale["published_range"] else None),
            name_infix=suffixed(animation, len(declared)),
            title=f"{stem} - {_field_label(animation)} - run {run_id}")
        rendered = report["animations"][animation.name]
        scale["rendered_range"] = [rendered.get("vmin"), rendered.get("vmax")]
        scale["transform"] = rendered.get("transform")
        scale["agrees"] = _scales_agree(scale)
    return report


def _scales_agree(scale: dict) -> bool | None:
    """Did the animation land on the published range? ``None`` = nothing to compare.

    A LOG ramp cannot start at the published floor - the norm needs a strictly
    positive one - so agreement there is on the TOP of the range, which is the end
    a reader reads a peak off.
    """
    published, rendered = scale.get("published_range"), scale.get("rendered_range")
    if not published or rendered is None or rendered[1] is None:
        return None
    if scale.get("transform") == "log":
        return abs(float(rendered[1]) - float(published[1])) <= 1e-6 * max(
            1.0, abs(float(published[1])))
    return all(abs(float(a) - float(b)) <= 1e-6 * max(1.0, abs(float(b)))
               for a, b in zip(rendered, published))


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
    for animation in (report.get("animations") or {}).values():
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


def _code_staleness(completion: dict, tool: str) -> dict | None:
    """The run's code stamp, read against THIS tree. ``None`` = nothing to say.

    The engine is resolved from the completion's own ``solver`` when it has one,
    falling back to the tool name, which carries the engine as its prefix for
    every template in the fleet.
    """
    sys.path.insert(0, str(REPO))
    from trid3nt_server.workflows.solver.code_provenance import staleness

    engine = str(completion.get("solver") or tool or "")
    return staleness(code_sha=completion.get("code_sha"), engine=engine,
                     code_dirty=completion.get("code_dirty"))


def assemble(template: str, variant: str, *, run_id: str | None = None,
             check: bool = False, bucket: str | None = None,
             out_dir: str | Path | None = None) -> dict:
    """Assemble and VERIFY one template+variant proof packet. Writes packet.json.

    The DECLARATION is always read from the template's own proof folder, because
    that is where the evidence a packet reports on lives. ``out_dir`` names where
    the renders and ``packet.json`` land: unset it is that same folder, and a lane
    that must not write into the frozen proof tree - an acceptance drive, a verify
    pass - names a scratch directory instead and still owes the whole checklist.
    """
    if variant not in VARIANTS:
        raise PacketError(f"{variant!r} is not a proof variant; the four are "
                          f"{list(VARIANTS)}")
    directory = Path(proof_dir(template, variant, create=not check and not out_dir))
    if not directory.is_dir():
        raise PacketError(f"no proof directory at {directory}")
    if out_dir:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
    else:
        out = directory
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
    declared = animations_for(tool)

    # ------------------------------------------------------------------ #
    # 1. Is this run time-stepped? MEASURED off its own SELAFIN.
    # ------------------------------------------------------------------ #
    s3 = _s3()
    completion = _read_json(s3, bucket, f"{run_id}/completion.json")
    # Both keys are the RUN'S OWN: the server names the result file in the
    # manifest's server facts and the worker copies it verbatim, then measures
    # the frame count off the file it just wrote. This reads them; it does not
    # reconstruct either, because a packet that guesses which file to open cannot
    # report that the run and its metrics disagree.
    result_slf = str(completion.get("result_slf") or "")
    recorded = completion.get("ntimestep")
    measured: dict[str, Any] = {"recorded_ntimestep": recorded}
    if not result_slf:
        measured |= {"frames": -1, "error": f"run {run_id} publishes no "
                                            "completion.json/result_slf to measure"}
    else:
        try:
            measured |= measure_frames(s3, bucket, run_id, result_slf)
        except Exception as exc:  # noqa: BLE001 - unmeasurable IS the finding
            measured |= {"frames": -1, "slf": result_slf,
                         "error": f"{type(exc).__name__}: {exc}"}
    frames = int(measured.get("frames", -1))
    if frames < 0:
        missing.append(
            "frames: the time-stepped decision is UNMEASURABLE - "
            f"{measured.get('error')}. The GIF requirement cannot be settled "
            "without reading the run's own result file.")
    elif recorded is None and frames > 1:
        # A single-field run has no time series to truncate, so there is nothing
        # for a second reader to disagree with; a time-stepped one that reports no
        # frame count of its own leaves this script as the only reader of the file.
        missing.append(
            f"frames: the SELAFIN carries {frames} frames and the run records no "
            "ntimestep to cross-check them against - one reader is not a "
            "cross-check")
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
            out, stem, evidence_path, evidence, template=template,
            variant=variant, run_id=run_id, bucket=bucket, declared=declared,
            frames=frames, s3=s3)
        if render_report.get("animation_error"):
            missing.append(f"animation: {render_report['animation_error']}")
        if render_report.get("chart_error"):
            missing.append(f"chart: {render_report['chart_error']}")
        for note in render_report.get("animation_errors") or []:
            missing.append(f"animation: {note}")
        # ONE SCALE PER QUANTITY. A packet whose GIF and whose panel of the same
        # field carry different ranges asks the reader to reconcile two legends,
        # and the peak one of them saturates is the run's own headline number.
        for name, scale in (render_report.get("scales") or {}).items():
            if scale.get("agrees") is False:
                missing.append(
                    f"animation {name}: the frames were painted on "
                    f"{scale['rendered_range']} while the published "
                    f"{scale['quantity']} raster carries "
                    f"{scale['published_range']} - one quantity, two scales in "
                    "one packet")
        # WHERE the frames landed, against where the run was asked about. A LOCAL
        # mesh rendered without its origin lands at the UTM false origin and every
        # other number in the render report stays healthy while it does.
        aoi = aoi_bbox(evidence, _read_json(s3, bucket,
                                            f"{run_id}/telemac_metrics.json"))
        for name, rendered in (render_report.get("animations") or {}).items():
            drawn = rendered.get("bbox_ll")
            if drawn and aoi and not _intersects(drawn, aoi):
                missing.append(
                    f"animation {name}: the frames were drawn over {drawn} but "
                    f"the run's AOI is {aoi} - the two do not overlap, so the "
                    "animation is at the UTM false origin rather than on the "
                    "water")
            elif drawn and not aoi:
                missing.append(
                    f"animation {name}: the run records no bbox and the evidence "
                    "declares none, so there is nothing to check the frames' "
                    f"extent against - they were drawn over {drawn}")
    fresh = _rendered_paths(render_report)

    # ------------------------------------------------------------------ #
    # 3. Walk the checklist over what is on disk now.
    # ------------------------------------------------------------------ #
    layers = _collapsed_layers(evidence)
    groups = _panel_groups(out)
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
    candidates = [out / f"{base}_canvas_layers.png",
                  out / f"{base}.png"] if base else []
    candidates.append(out / f"{stem}.png")
    sheet = next((p for p in candidates if p.exists()), out / f"{stem}.png")
    caption = (f"Contact sheet - every emitted layer, {expected_panels} panels in "
               f"emission order, run {run_id}")
    if sheet in fresh:
        stamp_png(sheet, caption=caption, **stamp)
    deliverables.append(_item(order, "contact_sheet", caption, path=sheet,
                              evidence_mtime=evidence_mtime, missing=missing,
                              **stamp))

    for name in _chart_names(evidence):
        order += 1
        path = out / (f"{stem}_chart.png" if name is None
                      else f"{stem}_chart_{name}.png")
        caption = (f"Chart {name or '(unnamed)'} - the run's persisted spec through "
                   f"the plugin chart dock's own renderer, run {run_id}")
        if path in fresh:
            stamp_png(path, caption=caption, **stamp)
        deliverables.append(_item(order, "chart", caption, path=path,
                                  evidence_mtime=evidence_mtime, missing=missing,
                                  extra={"chart": name}, **stamp))

    # ------------------------------------------------------------------ #
    # 4. EVERY declared animation, or the exemption that names why there is none.
    # ------------------------------------------------------------------ #
    fallback = declared or (ProofAnimation(),)
    for animation in fallback:
        infix = suffixed(animation, len(fallback))
        field = (_field_label(animation) if animation.variable
                 else "(undeclared)")
        still = animation.still
        gif = out / f"{stem}_animation{infix}.gif"
        order += 1
        if frames > 1:
            # The GIF's run id is BURNED into every frame by the renderer and
            # cannot be read back as text, so its tie to this run is the pair of
            # checks a picture cannot fake: the frame count must equal the
            # SELAFIN's, and it must not predate the evidence beside it.
            row = _item(order, "animation",
                        f"Animation [{animation.name}] - {field}, {frames} frames "
                        f"on one run-scoped colour scale with a static legend, "
                        f"run {run_id}",
                        path=gif, evidence_mtime=evidence_mtime, missing=missing,
                        require_stamp=False,
                        extra={"animation": animation.name,
                               "declared_field": field,
                               "declared_reason": animation.reason or None},
                        **stamp)
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
                        reasons.append(f"{checks['frames']} GIF frames against "
                                       f"the SELAFIN's {frames}")
                    if checks["distinct_frames"] != checks["frames"]:
                        reasons.append(
                            f"only {checks['distinct_frames']} of "
                            f"{checks['frames']} frames are distinct - repeats "
                            "carry no new field data")
                    if not checks["field_moves"]:
                        reasons.append("the field never changes - this is a "
                                       "still wearing an animation's extension")
                    if not checks["legend_covers_a_ramp"]:
                        reasons.append(
                            f"the legend crop at x >= {_LEGEND_X_FRAC} holds only "
                            f"{checks['legend_ramp_colours']} colours, so it is "
                            "not covering the colorbar and proves nothing")
                    elif checks["legend_drift_frames"]:
                        reasons.append(
                            f"the legend is not byte-identical across frames "
                            f"({len(checks['legend_drift_frames'])} of "
                            f"{checks['frames'] - 1} pairs drift) - the colour "
                            "scale is being recomputed per frame")
                if reasons:
                    row["verdict"] = "FAILED"
                    missing.append(f"animation {animation.name}: {gif.name} - "
                                   + "; ".join(reasons))
            deliverables.append(row)
            caption = (f"{still.upper()} frame [{animation.name}] - {field}, the "
                       f"animation's own figure at its {still} step, same colours "
                       f"and extent, run {run_id}")
        else:
            reason = (animation.exempt_reason or
                      f"the run's result SELAFIN carries {frames} frame(s): a "
                      "single-frame (steady) result has nothing to animate")
            deliverables.append({
                "order": order, "kind": "animation", "path": None,
                "animation": animation.name, "verdict": "exempt",
                "reason": reason,
                "caption": f"Animation [{animation.name}] - EXEMPT. {reason}"})
            caption = (f"{still.upper()} frame [{animation.name}] - {field}, which "
                       f"IS the whole answer rather than one sample of it, "
                       f"run {run_id}")

        order += 1
        path = out / f"{stem}{infix}_{still}_frame.png"
        if path in fresh:
            stamp_png(path, caption=caption, **stamp)
        deliverables.append(_item(order, "still", caption, path=path,
                                  evidence_mtime=evidence_mtime, missing=missing,
                                  extra={"animation": animation.name}, **stamp))

    order += 1
    deliverables.append(_item(
        order, "evidence",
        f"Canary evidence JSON - the declaration, layers, metrics and products "
        f"this whole packet was assembled from, run {run_id}",
        path=evidence_path, missing=missing, **stamp))

    # STALE-vs-CODE: the run's own stamp against the tree reading it. A packet
    # re-read weeks later must SAY that the engine moved rather than let a reader
    # assume today's code produced these numbers.
    staleness_warning = _code_staleness(completion, tool)

    packet = {
        "what": "the delivery checklist for one template+variant, assembled and "
                "verified mechanically; send exactly what `deliverables` lists, "
                "in order",
        "code_staleness": staleness_warning,
        "template": template, "variant": variant, "tool": tool, "run_id": run_id,
        "stem": stem, "directory": str(out), "panel_base": base,
        "mode": "check" if check else "render",
        "assembled_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "assembler": "scripts/assemble_proof_packet.py",
        "time_stepped": measured,
        "animation_declarations": [_declaration_row(a, len(declared))
                                   for a in declared],
        "quantity_scales": render_report.get("scales") or {},
        "notes": list(packet_notes(tool, variant)),
        "published_layers": [layer.get("name") for layer in layers],
        "verdict": "REFUSED" if missing else "PASS",
        "missing": missing,
        "deliverables": deliverables,
    }
    if render_report:
        packet["render"] = render_report
    (out / "packet.json").write_text(
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
    ap.add_argument("--out-dir", dest="out_dir", default=None,
                    help="write the renders and packet.json HERE instead of into "
                         "the template's proof folder (the declaration is still "
                         "read from that folder)")
    ns = ap.parse_args(argv)

    try:
        packet = assemble(ns.template, ns.variant, run_id=ns.run, check=ns.check,
                          bucket=ns.bucket, out_dir=ns.out_dir)
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
