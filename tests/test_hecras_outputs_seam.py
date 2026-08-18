"""ADR 0287 -- the HEC-RAS emit-on-solve leg (host-exec / agent-side producers).

HEC-RAS depth frames are ADDITIVE (no prior frame emission existed -- postprocess
was peak-only on BOTH lineages), so the bar is CORRECTNESS, not a byte-equivalence
baseline:

  * the frame quantity ``flood_depth`` resolves to the SAME physical preset the
    peak COG publishes through (``continuous_flood_depth``);
  * ``frames_only=True`` skips the peak entry (the composer keeps its typed
    ``HecrasDepthLayerURI`` peak -- no double registration);
  * NEVER-OMIT: every per-step entry is published (both producers write all steps;
    there is NO subsample cap);
  * ``t`` is the plan-HDF Time in DAYS -> seconds; monotonic in one group;
  * the per-cell depth masking matches the peak (dry cell -> NaN, never painted).

Pure/offline: entries are built by the SAME contracts writer both producers use
(``build_entry``); no HEC-RAS solve needed.
"""

from __future__ import annotations

import numpy as np

from trid3nt_contracts.outputs_manifest import (
    append_entries,
    build_entry,
    parse_outputs_manifest,
)
from trid3nt_server.emission.outputs_seam import build_layers_from_outputs
from trid3nt_server.emission.quantity_styles import resolve_style_preset
from trid3nt_server.workflows.hecras.postprocess_hecras import (
    HECRAS_DEPTH_STYLE_PRESET,
    HECRAS_FRAME_NAME_STEM,
    HECRAS_FRAME_QUANTITY,
    HECRAS_WET_DEPTH_FT,
    _depth_for_step,
    _peak_frame_entry,
)

RID = "01HECRASSEAM00000000000000"
_BBOX = (-85.41, 40.18, -85.36, 40.22)  # a Muncie-ish AOI
_SECONDS_PER_DAY = 86400.0


def _frame_entries(n_frames: int, days: list[float]) -> list[dict]:
    """The peak (t=None) + N per-step depth frame entries (both producers' shape)."""
    entries = [_peak_frame_entry(f"s3://trid3nt-runs/{RID}/hecras_depth_peak.tif", list(_BBOX))]
    for i in range(1, n_frames + 1):
        entries.append(
            build_entry(
                kind="raster",
                quantity=HECRAS_FRAME_QUANTITY,
                name=f"{HECRAS_FRAME_NAME_STEM} step {i}",
                uri=f"s3://trid3nt-runs/{RID}/hecras_depth_frame_{i:02d}.tif",
                t=float(days[i - 1]) * _SECONDS_PER_DAY,
                units="ft",
                bbox=list(_BBOX),
            )
        )
    return entries


# --------------------------------------------------------------------------- #
# Styling: the frame quantity resolves to the peak's physical preset.
# --------------------------------------------------------------------------- #
def test_flood_depth_resolves_to_the_peak_preset():
    preset, is_fallback = resolve_style_preset(HECRAS_FRAME_QUANTITY)
    assert preset == HECRAS_DEPTH_STYLE_PRESET and is_fallback is False
    assert HECRAS_DEPTH_STYLE_PRESET == "continuous_flood_depth"


# --------------------------------------------------------------------------- #
# frames_only skips the peak; never-omit; t in seconds monotonic.
# --------------------------------------------------------------------------- #
def test_frames_only_skips_peak_and_styles_as_flood_depth():
    days = [0.00347222, 0.00694444, 0.01041667, 0.01388889, 0.01736111]  # 5-min steps
    manifest = parse_outputs_manifest(
        append_entries(None, engine="hecras", run_id=RID, new=_frame_entries(5, days))
    )
    seam = build_layers_from_outputs(manifest, run_id=RID, bbox=_BBOX, frames_only=True)
    assert len(seam.layers) == 5  # 5 frames, NO peak
    assert all(l.role == "context" for l in seam.layers)
    assert all(l.style_preset == HECRAS_DEPTH_STYLE_PRESET for l in seam.layers)
    temporal = [f for f in seam.frames if f.t is not None]
    assert [f.t for f in temporal] == [d * _SECONDS_PER_DAY for d in days]
    # single flood_depth group.
    assert {f.group_id for f in temporal} == {f"flood-depth-{RID}"}


def test_never_omit_all_289_steps_published():
    n = 289  # the real Muncie cadence -- every step, NO cap
    days = [round(i * 0.00347222, 8) for i in range(n)]
    manifest = parse_outputs_manifest(
        append_entries(None, engine="hecras", run_id=RID, new=_frame_entries(n, days))
    )
    seam = build_layers_from_outputs(manifest, run_id=RID, bbox=_BBOX, frames_only=True)
    assert len(seam.layers) == n
    assert seam.layers[0].name == f"{HECRAS_FRAME_NAME_STEM} step 1"
    assert seam.layers[-1].name == f"{HECRAS_FRAME_NAME_STEM} step {n}"


def test_peak_entry_is_nontemporal_and_survives_full_publish():
    # Without frames_only the peak entry publishes as a standalone primary layer.
    days = [1.0, 2.0, 3.0]
    manifest = parse_outputs_manifest(
        append_entries(None, engine="hecras", run_id=RID, new=_frame_entries(3, days))
    )
    seam = build_layers_from_outputs(manifest, run_id=RID, bbox=_BBOX, frames_only=False)
    primaries = [l for l in seam.layers if l.role == "primary"]
    assert len(primaries) == 1
    assert primaries[0].name == f"Peak {HECRAS_FRAME_NAME_STEM.lower()}"


# --------------------------------------------------------------------------- #
# The 6.x per-cell masking matches the peak (dry cell -> NaN, never painted).
# --------------------------------------------------------------------------- #
def test_depth_for_step_masks_dry_cells_like_the_peak():
    # cell 0 wet (WSE 10 over bed 5 -> 4 ft), cell 1 dry (WSE 0), cell 2 HDF-fill,
    # cell 3 below-surface (WSE < bed).
    wse = np.array([10.0, 0.0, 1e30, 3.0], dtype=float)
    bed = np.array([5.0, 4.0, 2.0, 8.0], dtype=float)
    depth = _depth_for_step(wse, bed)
    assert depth[0] == 5.0  # 10 - 5
    assert np.isnan(depth[1])  # dry (WSE 0)
    assert np.isnan(depth[2])  # HDF fill
    assert np.isnan(depth[3])  # WSE < bed
    # the wet floor is applied by the rasterizer, not here -- a thin film survives
    # _depth_for_step but is dropped by the > HECRAS_WET_DEPTH_FT rasterize cut.
    thin = _depth_for_step(np.array([5.05]), np.array([5.0]))
    assert np.isfinite(thin[0]) and thin[0] < HECRAS_WET_DEPTH_FT
