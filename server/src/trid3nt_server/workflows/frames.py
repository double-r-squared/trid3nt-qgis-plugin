"""Engine-agnostic flood/wave time-stepped animation frame machinery.

STEP 1 of the engine-coverage-levers refactor (pure extract-in-place, NO behavior
change). This module is the single source of truth for the per-frame animation
contract every engine's postprocess shares:

  - ``MAX_FLOOD_FRAMES`` (env ``TRID3NT_MAX_FLOOD_FRAMES``) - the upper bound on
    emitted animation frames. Lifted VERBATIM from ``postprocess_flood`` (the
    cap, the rationale, the env override).
  - ``_select_frame_time_indices(n_steps)`` - the even-subsample selector
    (endpoints always kept, ``np.linspace`` + ``np.unique``, logged when the cap
    fires). Lifted VERBATIM.
  - The web frame-token NAMING contract (``peak_layer_name`` / ``frame_name`` /
    ``peak_layer_id`` / ``frame_layer_id``): the ``-peak-`` token in the layer id
    is load-bearing for the web ``isPeakLayer`` detection, and the ``"<quantity>
    step N"`` NAME is the EXACT token the web ``parseFrameToken`` /
    ``detectSequentialGroups`` scrubber requires. These were inline string
    f-strings in every engine; centralizing them keeps the web contract pinned
    in ONE place (a name-format drift now fails one test, not five).
  - ``emit_timeseries_layers`` - the corrupt-frame-degrades-to-peak-only guard +
    the "< 2 frames never groups" guard, distilled from the identical
    try/except/cleanup loops the engines hand-roll. The STEP-2 quantity executor
    routes ``TimeseriesField`` through here; the existing engine postprocess
    modules keep their hand-rolled loops UNCHANGED in STEP 1 (this is additive
    plumbing for the executor, the byte-identical guarantee is on the lifted
    constant + selector that postprocess_flood now imports back from here).

``postprocess_flood.py`` (SFINCS) keeps working by importing ``MAX_FLOOD_FRAMES``
and ``_select_frame_time_indices`` from this module and RE-EXPORTING them (so the
existing ``from .postprocess_flood import MAX_FLOOD_FRAMES, _select_frame_time_indices``
in postprocess_swmm / _swan / _waves / _geoclaw, and the agent test imports,
resolve to the SAME objects). Byte-identical output is the regression-safety
guarantee for STEP 1.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable

logger = logging.getLogger("trid3nt_server.workflows.frames")

#: The subsample-cap log line is pinned by tests + ops scrapers to the
#: ``postprocess_flood`` logger (it carries the ``postprocess_flood:`` prefix and
#: predates this extraction). Emit it under that exact logger name so STEP 1 is
#: byte-identical in OBSERVABLE behavior, not just in return values.
_cap_logger = logging.getLogger("trid3nt_server.workflows.postprocess_flood")

__all__ = [
    "MAX_FLOOD_FRAMES",
    "select_frame_time_indices",
    "_select_frame_time_indices",
    "peak_layer_name",
    "frame_name",
    "peak_layer_id",
    "frame_layer_id",
    "frame_dest_filename",
    "EmittedFrame",
    "emit_timeseries_layers",
]


#: Upper bound on the number of per-frame depth COGs the time-stepped flood
#: animation emits (flood North Star Phase 1, engine-agnostic). When a map output
#: carries MORE than this many time snapshots we subsample EVENLY across the full
#: time span (first + last steps always kept) so the scrubber stays bounded and
#: the per-Case session-state snapshot never balloons.
#:
#: COASTAL/WAVE "looks like rain" fix: the cap was a HARD 24 (~one frame/hour
#: over a 1-day sim) which is too coarse for a coastal surge+SnapWave animation  -
#: waves move in seconds-to-minutes, so an hourly stride reads as a filling
#: bathtub. The deck now emits minute-scale frames for coastal runs (sfincs_builder
#: ``output_interval_min``); raising the cap to 144 lets a fine-cadence run
#: (e.g. 5-min over ~12 h, or ~10-min over a full day) emit ALL its frames
#: rather than silently subsampling back down to 24. 144 ~= 5-min frames over
#: 12 h, a bounded-but-watchable wave animation. When a run still exceeds the
#: cap, ``select_frame_time_indices`` LOGS the subsample (never silent).
#: Overridable via env for ops tuning.
MAX_FLOOD_FRAMES: int = int(os.environ.get("TRID3NT_MAX_FLOOD_FRAMES", "144"))


def select_frame_time_indices(n_steps: int) -> list[int]:
    """Pick up to ``MAX_FLOOD_FRAMES`` evenly-spaced time indices over ``n_steps``.

    Endpoints (first + last step) are ALWAYS included so the animation spans the
    full event. When ``n_steps <= MAX_FLOOD_FRAMES`` every step is returned. When
    more, ``np.linspace(0, n_steps-1, MAX_FLOOD_FRAMES)`` rounded to ints +
    ``np.unique`` gives an even, collision-free subsample (<= MAX_FLOOD_FRAMES,
    strictly increasing). Returned indices are the RAW time-dim positions; the
    frame NUMBER (1..k) used for the web token is the position in this list.

    Lifted VERBATIM from ``postprocess_flood._select_frame_time_indices`` (STEP 1
    extract-in-place). The log message keeps the ``postprocess_flood`` prefix so
    existing log-scrapers / ops dashboards do not regress.
    """
    import numpy as np  # type: ignore[import-not-found]

    if n_steps <= 0:
        return []
    if n_steps <= MAX_FLOOD_FRAMES:
        return list(range(n_steps))
    idx = np.linspace(0, n_steps - 1, MAX_FLOOD_FRAMES).round().astype(int)
    kept = [int(i) for i in np.unique(idx)]
    # Never silently truncate (kickoff requirement): a coastal/wave run with a
    # fine cadence can emit MANY raw snapshots; if we still exceed the cap we
    # subsample EVENLY (endpoints kept) and LOG it so the cap is visible.
    _cap_logger.info(
        "postprocess_flood: %d raw map snapshots exceed MAX_FLOOD_FRAMES=%d; "
        "subsampling evenly to %d frames (first+last kept). Raise "
        "TRID3NT_MAX_FLOOD_FRAMES to emit more.",
        n_steps,
        MAX_FLOOD_FRAMES,
        len(kept),
    )
    return kept


#: Backward-compat alias: the engines + tests import the PRIVATE name
#: ``_select_frame_time_indices``. Keep it bound to the SAME function object so
#: ``from .frames import _select_frame_time_indices`` (and the postprocess_flood
#: re-export) resolve identically (STEP 1 byte-identical guarantee).
_select_frame_time_indices = select_frame_time_indices


# --------------------------------------------------------------------------- #
# Web frame-token naming contract (the SINGLE source of truth).
# --------------------------------------------------------------------------- #
def peak_layer_name(quantity_label: str = "Flood depth") -> str:
    """The PEAK layer NAME (e.g. ``"Peak flood depth"``).

    Mirrors the inline ``"Peak flood depth"`` every depth engine assigns. The
    quantity label is lower-cased after "Peak " to match the existing token
    ("Peak flood depth", not "Peak Flood depth"). For non-depth quantities pass
    the desired label (the wave path uses its own name).
    """
    return f"Peak {quantity_label[:1].lower()}{quantity_label[1:]}"


def frame_name(frame_no: int, quantity_label: str = "Flood depth") -> str:
    """The per-frame NAME carrying the EXACT web ``step N`` token.

    ``"<quantity_label> step N"`` (N = 1..k, contiguous, 1-based). The
    ``parseFrameToken`` regex on the web side keys on the ``step N`` token, and
    ``detectSequentialGroups`` collapses the contiguous run into ONE scrubber
    group. NEVER change the ``step N`` substring without the matching web change.
    """
    return f"{quantity_label} step {frame_no}"


def peak_layer_id(stem: str, run_id: str) -> str:
    """The PEAK layer id ``"<stem>-peak-<run_id>"``.

    The ``-peak-`` token is LOAD-BEARING: the web ``isPeakLayer`` detection keys
    on it to treat the layer as the representative still (not a scrubber frame).
    ``stem`` is the engine prefix (e.g. ``"flood-depth"``, ``"swmm-depth"``).
    """
    return f"{stem}-peak-{run_id}"


def frame_layer_id(stem: str, frame_no: int, run_id: str) -> str:
    """The per-frame layer id ``"<stem>-frame-NN-<run_id>"`` (NN zero-padded).

    A DISTINCT object key per frame -> distinct TiTiler ``url=`` -> distinct
    ``_layer_identity_key`` -> no dedup collapse (the scrubber group keeps every
    member). ``frame_no`` is 1-based; the id is zero-padded to two digits exactly
    as the engines do (``frame_{frame_no:02d}``).
    """
    return f"{stem}-frame-{frame_no:02d}-{run_id}"


def frame_dest_filename(stem: str, frame_no: int, suffix: str = ".tif") -> str:
    """The per-frame runs-bucket object filename ``"<stem>_frame_NN.tif"``.

    Mirrors the engines' ``f"..._frame_{frame_no:02d}.tif"`` so each frame lands
    at its own key (distinct url -> distinct identity key -> no dedup).
    """
    return f"{stem}_frame_{frame_no:02d}{suffix}"


# --------------------------------------------------------------------------- #
# Corrupt-frame-degrades-to-peak-only guard (the executor's frame emitter).
# --------------------------------------------------------------------------- #
class EmittedFrame:
    """One successfully written+uploaded animation frame.

    ``frame_no`` is the 1-based contiguous frame number (the web token N);
    ``uri`` is the uploaded COG URI; ``bbox`` is the per-frame zoom-to bbox (or
    None); ``metrics`` carries the per-frame aggregate the engine row needs.
    """

    __slots__ = ("frame_no", "uri", "bbox", "metrics")

    def __init__(
        self,
        *,
        frame_no: int,
        uri: str,
        bbox: tuple[float, float, float, float] | None,
        metrics: dict[str, Any],
    ) -> None:
        self.frame_no = frame_no
        self.uri = uri
        self.bbox = bbox
        self.metrics = metrics


def emit_timeseries_layers(
    n_steps: int,
    *,
    write_frame: Callable[[int, int], EmittedFrame],
    on_degrade: Callable[[Exception], None] | None = None,
    cleanup: Callable[[], None] | None = None,
) -> list[EmittedFrame]:
    """Drive the per-frame emit loop with the shared honesty guards.

    Distills the IDENTICAL try/except/cleanup loop the engines hand-roll (and
    that ``postprocess_flood._extract_depth_frames`` runs) into ONE executor the
    STEP-2 quantity executor uses for a ``TimeseriesField``:

      1. Subsample to <= ``MAX_FLOOD_FRAMES`` evenly-spaced raw step indices
         (endpoints kept) via ``select_frame_time_indices``.
      2. For each kept index, call ``write_frame(frame_no, raw_step_index)`` which
         writes + uploads the frame COG and returns an ``EmittedFrame``. The
         callback owns the engine-specific rasterize/write/upload; this function
         owns only the SEQUENCING + the guards.
      3. CORRUPT-FRAME GUARD: if ``write_frame`` raises, the partial frames are
         abandoned (``cleanup`` invoked, ``on_degrade`` notified) and ``[]`` is
         returned -> the caller degrades to peak-only (better one good layer than
         a broken group). NEVER re-raises (the peak layer must survive a bad
         frame).
      4. "< 2 NEVER GROUPS" GUARD: a single-frame result can never form a web
         scrubber group (needs >= 2 distinct members), so a result with < 2
         frames is dropped to ``[]``.

    Returns the list of ``EmittedFrame`` (length 0 or >= 2). ``cleanup`` is the
    caller's "unlink any temp COGs still on disk" hook; ``on_degrade`` is the
    caller's logging hook.
    """
    indices = select_frame_time_indices(n_steps)
    frames: list[EmittedFrame] = []
    try:
        for frame_no, raw_idx in enumerate(indices, start=1):
            frames.append(write_frame(frame_no, raw_idx))
    except Exception as exc:  # noqa: BLE001 - degrade to peak-only, never re-raise
        if on_degrade is not None:
            on_degrade(exc)
        if cleanup is not None:
            cleanup()
        return []

    # A lone styled frame can never group on the web (needs >= 2 distinct
    # members); drop a < 2 frame set so we never publish a single orphan frame.
    if len(frames) < 2:
        if cleanup is not None:
            cleanup()
        return []
    return frames
