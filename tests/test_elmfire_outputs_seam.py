"""ADR 0288 -- the ELMFIRE emit-on-solve leg (agent-side writer).

The ELMFIRE burned-extent frames are ADDITIVE (no prior frame emission ever rode
the ``outputs.json`` seam -- they used to ride the returned ``layers`` list), so the
bar is CORRECTNESS, not a byte-equivalence baseline:

  * the ``fire_arrival`` frame quantity resolves to the SAME physical preset the
    ToA peak uses (``continuous_fire_arrival_hr``) -- one colormap tells both the
    burned WHERE and the arrival WHEN;
  * ``frames_only=True`` skips the peak entry (``t=None``) -- the composer keeps its
    own typed ToA peak, so the primary COG uri is never registered twice;
  * NEVER-OMIT: every hourly burn bucket is published (postprocess writes one entry
    per burn hour -- there is no post-hoc frame thinning);
  * ``t`` is the burn hour -> seconds; monotonic within the single group;
  * the web ``"Burned area step N"`` name token forms exactly ONE
    ``fire_arrival`` sequential group.

Pure/offline: entries are built by the SAME contracts writer the agent-side
producer uses (``build_entry``); no ELMFIRE solve needed. The DERIVED-FROM-ToA
reconstruction (a per-hour ``toa <= hour`` threshold of the single solved arrival
raster) is a lossless query of the run's complete spatiotemporal solution -- see
``test_toa_frame_grids_threshold_per_hour`` in test_model_fire_spread_chain.py.
"""

from __future__ import annotations

from trid3nt_contracts.outputs_manifest import (
    append_entries,
    build_entry,
    parse_outputs_manifest,
)
from trid3nt_server.emission.outputs_seam import build_layers_from_outputs
from trid3nt_server.emission.styles import resolve_style_preset
from trid3nt_server.workflows.elmfire.postprocess_elmfire import (
    _FIRE_FRAME_QUANTITY,
    _FIRE_QUANTITY_LABEL,
)

RID = "01ELMFIRESEAM00000000000000"
_BBOX = (-120.60, 39.10, -120.55, 39.14)
_FIRE_STYLE_PRESET = "continuous_fire_arrival_hr"


def _fire_entries(n_hours: int) -> list[dict]:
    """A peak (t=None) + N hourly burned-extent frame entries (ADR 0288 shape)."""
    entries = [
        build_entry(
            kind="raster",
            quantity=_FIRE_FRAME_QUANTITY,
            name="Fire arrival time",
            uri=f"s3://trid3nt-runs/{RID}/elmfire_toa.tif",
            t=None,
            units="hours",
            bbox=list(_BBOX),
        )
    ]
    for h in range(1, n_hours + 1):
        entries.append(
            build_entry(
                kind="raster",
                quantity=_FIRE_FRAME_QUANTITY,
                name=f"{_FIRE_QUANTITY_LABEL} step {h}",
                uri=f"s3://trid3nt-runs/{RID}/elmfire_burned_{h:04d}.tif",
                t=float(h) * 3600.0,
                units="hours",
                bbox=list(_BBOX),
            )
        )
    return entries


# --------------------------------------------------------------------------- #
# Styling: the frame quantity resolves to the peak's physical preset.
# --------------------------------------------------------------------------- #
def test_fire_arrival_resolves_to_arrival_preset():
    preset, is_fallback = resolve_style_preset(_FIRE_FRAME_QUANTITY)
    assert preset == _FIRE_STYLE_PRESET and is_fallback is False


# --------------------------------------------------------------------------- #
# frames_only skips the peak; one group; t in seconds; monotonic.
# --------------------------------------------------------------------------- #
def test_fire_frames_only_skips_peak():
    manifest = parse_outputs_manifest(
        append_entries(
            None, engine="elmfire", run_id=RID, new=_fire_entries(6)
        )
    )
    seam = build_layers_from_outputs(
        manifest, run_id=RID, bbox=_BBOX, frames_only=True
    )
    assert len(seam.layers) == 6  # 6 frames, NO peak
    assert all(l.role == "context" for l in seam.layers)
    assert all(l.style_preset == _FIRE_STYLE_PRESET for l in seam.layers)
    # t is the burn hour -> seconds; monotonic.
    temporal = [f for f in seam.frames if f.t is not None]
    assert [f.t for f in temporal] == [float(h) * 3600.0 for h in range(1, 7)]


def test_fire_single_arrival_group():
    manifest = parse_outputs_manifest(
        append_entries(
            None, engine="elmfire", run_id=RID, new=_fire_entries(6)
        )
    )
    seam = build_layers_from_outputs(
        manifest, run_id=RID, bbox=_BBOX, frames_only=True
    )
    groups = {f.group_id for f in seam.frames if f.t is not None}
    assert groups == {f"fire-arrival-{RID}"}


def test_never_omit_all_burn_hours_published():
    # A long burn: every hourly bucket rides -- no cap thins the sweep.
    n = 30
    manifest = parse_outputs_manifest(
        append_entries(
            None, engine="elmfire", run_id=RID, new=_fire_entries(n)
        )
    )
    seam = build_layers_from_outputs(
        manifest, run_id=RID, bbox=_BBOX, frames_only=True
    )
    assert len(seam.layers) == n
    assert [l.name for l in seam.layers] == [
        f"{_FIRE_QUANTITY_LABEL} step {i}" for i in range(1, n + 1)
    ]
