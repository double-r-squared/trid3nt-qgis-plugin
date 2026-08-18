"""ADR 0284 -- the MODFLOW transport-family emit-on-solve leg (host-exec writer).

MODFLOW transport frames are ADDITIVE (no prior frame emission ever existed --
``publish_modflow_quantities`` was dead code), so the bar is CORRECTNESS, not a
byte-equivalence baseline:

  * the concentration / temperature quantities resolve to the SAME physical
    presets the peak uses (``continuous_plume_concentration`` /
    ``continuous_temperature_c``), incl. the per-species FAMILY fallback
    (``plume_concentration__<slug>`` -> the plume family preset, ADR 0284);
  * ``frames_only=True`` skips the peak entries (the composer keeps its typed
    peak -- no double registration);
  * NEVER-OMIT: every saved step is published (OC saves ALL steps by
    construction; there is no cap to thin);
  * ``t`` is the MF6 totim in DAYS -> seconds; monotonic per group;
  * N species with IDENTICAL save-times do NOT collide -- the per-species
    quantity keeps each stack its OWN temporal group (the collision the bare
    quantity would cause).

Pure/offline: entries are built by the SAME contracts writer the host-exec
producer uses (``build_entry``); no mf6 solve needed.
"""

from __future__ import annotations

from trid3nt_contracts.outputs_manifest import (
    append_entries,
    build_entry,
    parse_outputs_manifest,
)
from trid3nt_server.emission.outputs_seam import build_layers_from_outputs
from trid3nt_server.emission.quantity_styles import resolve_style_preset
from trid3nt_server.workflows.modflow.postprocess_modflow import (
    PLUME_FRAME_QUANTITY_BASE,
    PLUME_STYLE_PRESET,
    TEMPERATURE_FRAME_QUANTITY,
    TEMPERATURE_STYLE_PRESET,
    _SECONDS_PER_DAY,
)

RID = "01MODFLOWSEAM0000000000000"
_BBOX = (-87.10, 30.30, -87.02, 30.36)


def _species_quantity(slug: str) -> str:
    return f"{PLUME_FRAME_QUANTITY_BASE}__{slug}"


def _plume_entries(slug: str, label: str, n_frames: int, days: list[float]) -> list[dict]:
    """One species' peak (t=None) + N concentration frame entries."""
    q = _species_quantity(slug)
    entries = [
        build_entry(
            kind="raster",
            quantity=q,
            name=f"Contaminant Plume - {label} (peak concentration)",
            uri=f"s3://trid3nt-runs/{RID}/plume_{slug}_concentration_4326.tif",
            t=None,
            units="mg/L",
            bbox=list(_BBOX),
        )
    ]
    for i in range(1, n_frames + 1):
        entries.append(
            build_entry(
                kind="raster",
                quantity=q,
                name=f"Contaminant Plume - {label} step {i}",
                uri=f"s3://trid3nt-runs/{RID}/plume_{slug}_frame_{i:02d}.tif",
                t=float(days[i - 1]) * _SECONDS_PER_DAY,
                units="mg/L",
                bbox=list(_BBOX),
            )
        )
    return entries


def _thermal_entries(n_frames: int, days: list[float]) -> list[dict]:
    entries = [
        build_entry(
            kind="raster",
            quantity=TEMPERATURE_FRAME_QUANTITY,
            name="Thermal Plume (peak temperature excess)",
            uri=f"s3://trid3nt-runs/{RID}/thermal_plume_temperature_4326.tif",
            t=None,
            units="degC",
            bbox=list(_BBOX),
        )
    ]
    for i in range(1, n_frames + 1):
        entries.append(
            build_entry(
                kind="raster",
                quantity=TEMPERATURE_FRAME_QUANTITY,
                name=f"Thermal plume temperature step {i}",
                uri=f"s3://trid3nt-runs/{RID}/thermal_plume_temperature_frame_{i:02d}.tif",
                t=float(days[i - 1]) * _SECONDS_PER_DAY,
                units="degC",
                bbox=list(_BBOX),
            )
        )
    return entries


# --------------------------------------------------------------------------- #
# Styling: the frame quantities resolve to the peak's physical preset.
# --------------------------------------------------------------------------- #
def test_plume_family_resolves_to_plume_preset():
    # The bare family + a per-species instance both style as the plume preset.
    base, base_fb = resolve_style_preset(PLUME_FRAME_QUANTITY_BASE)
    assert base == PLUME_STYLE_PRESET and base_fb is False
    inst, inst_fb = resolve_style_preset(_species_quantity("cis_dce"))
    assert inst == PLUME_STYLE_PRESET and inst_fb is False  # family fallback, NOT neutral


def test_temperature_resolves_to_temperature_preset():
    preset, is_fallback = resolve_style_preset(TEMPERATURE_FRAME_QUANTITY)
    assert preset == TEMPERATURE_STYLE_PRESET and is_fallback is False


# --------------------------------------------------------------------------- #
# frames_only skips the peaks; never-omit; t in seconds; monotonic.
# --------------------------------------------------------------------------- #
def test_single_species_frames_only_skips_peak():
    days = [1.0, 2.0, 3.0, 4.0, 5.0]
    manifest = parse_outputs_manifest(
        append_entries(
            None, engine="modflow", run_id=RID,
            new=_plume_entries("benzene", "benzene", 5, days),
        )
    )
    seam = build_layers_from_outputs(manifest, run_id=RID, bbox=_BBOX, frames_only=True)
    assert len(seam.layers) == 5  # 5 frames, NO peak
    assert all(l.role == "context" for l in seam.layers)
    assert all(l.style_preset == PLUME_STYLE_PRESET for l in seam.layers)
    # t is the totim DAYS -> seconds; monotonic.
    temporal = [f for f in seam.frames if f.t is not None]
    assert [f.t for f in temporal] == [d * _SECONDS_PER_DAY for d in days]


def test_never_omit_all_frames_published():
    n = 40  # far past any retired cap; OC saves ALL steps
    days = [float(i) for i in range(1, n + 1)]
    manifest = parse_outputs_manifest(
        append_entries(
            None, engine="modflow", run_id=RID,
            new=_plume_entries("tce", "TCE", n, days),
        )
    )
    seam = build_layers_from_outputs(manifest, run_id=RID, bbox=_BBOX, frames_only=True)
    assert len(seam.layers) == n
    assert [l.name for l in seam.layers] == [
        f"Contaminant Plume - TCE step {i}" for i in range(1, n + 1)
    ]


# --------------------------------------------------------------------------- #
# N species with IDENTICAL save-times -> N distinct groups (no (q,t) collision).
# --------------------------------------------------------------------------- #
def test_multi_species_identical_times_do_not_collide():
    days = [1.0, 2.0, 3.0]  # SAME for both species (one shared time discretization)
    entries = (
        _plume_entries("tce", "TCE", 3, days)
        + _plume_entries("cis_dce", "cis-DCE", 3, days)
    )
    manifest = parse_outputs_manifest(
        append_entries(None, engine="modflow", run_id=RID, new=entries)
    )
    seam = build_layers_from_outputs(manifest, run_id=RID, bbox=_BBOX, frames_only=True)
    # 3 + 3 = 6 frames survive (a bare shared quantity would collapse to 3 via the
    # (quantity, t) dedup -- the per-species quantity is what prevents that).
    assert len(seam.layers) == 6
    groups = {f.group_id for f in seam.frames if f.t is not None}
    assert groups == {
        f"plume-concentration--tce-{RID}",
        f"plume-concentration--cis-dce-{RID}",
    }
    # Each species keeps 3 monotonic frames + all style as the plume preset.
    assert all(l.style_preset == PLUME_STYLE_PRESET for l in seam.layers)
    names = sorted(l.name for l in seam.layers)
    assert names == sorted(
        [f"Contaminant Plume - TCE step {i}" for i in range(1, 4)]
        + [f"Contaminant Plume - cis-DCE step {i}" for i in range(1, 4)]
    )


# --------------------------------------------------------------------------- #
# Thermal: one temperature group.
# --------------------------------------------------------------------------- #
def test_thermal_single_temperature_group():
    days = [10.0, 20.0, 30.0, 40.0]
    manifest = parse_outputs_manifest(
        append_entries(
            None, engine="modflow", run_id=RID, new=_thermal_entries(4, days)
        )
    )
    seam = build_layers_from_outputs(manifest, run_id=RID, bbox=_BBOX, frames_only=True)
    assert len(seam.layers) == 4  # frames only, peak skipped
    assert all(l.style_preset == TEMPERATURE_STYLE_PRESET for l in seam.layers)
    groups = {f.group_id for f in seam.frames if f.t is not None}
    assert groups == {f"temperature-{RID}"}
    assert [f.t for f in seam.frames if f.t is not None] == [
        d * _SECONDS_PER_DAY for d in days
    ]
