"""ADR 0280 -- the emit-on-solve seam consumer + its byte-equivalence bar.

Two concerns:

1. Seam unit behaviour (synthetic outputs.json): temporal grouping, deterministic
   idempotent layer ids, registered-quantity pinned styling, unknown-quantity
   neutral-ramp fallback, and the missing-manifest no-op.

2. The MIGRATION BAR (Section 7.1): the emitted layer-event stream from the NEW
   ``outputs.json`` + seam path is byte-identical -- field for field -- to the
   OLD ``register_manifest_layers`` path for the SAME solved output. Proven on a
   self-contained synthetic ``sfincs_map.nc`` (no solve, no docker, no
   committed fixture) so it is a durable regression.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from trid3nt_contracts.outputs_manifest import (
    append_entries,
    build_entry,
    parse_outputs_manifest,
)
from trid3nt_server.emission import quantity_styles
from trid3nt_server.emission.outputs_seam import (
    build_layers_from_outputs,
    read_outputs_manifest,
)

RID = "01SEAMTEST00000000000000000"


def _manifest(entries):
    return parse_outputs_manifest(
        append_entries(None, engine="sfincs", run_id=RID, new=entries)
    )


# --------------------------------------------------------------------------- #
# Seam unit behaviour
# --------------------------------------------------------------------------- #
def test_temporal_grouping_and_deterministic_ids():
    entries = [
        build_entry(kind="raster", quantity="flood_depth", name="Peak flood depth",
                    uri="s3://b/%s/flood_depth_peak.tif" % RID, units="meters"),
        # deliberately out-of-order t to prove the seam sorts.
        build_entry(kind="raster", quantity="flood_depth", name="Flood depth step 2",
                    uri="s3://b/%s/flood_depth_frame_02.tif" % RID, t=1800.0, units="meters"),
        build_entry(kind="raster", quantity="flood_depth", name="Flood depth step 1",
                    uri="s3://b/%s/flood_depth_frame_01.tif" % RID, t=0.0, units="meters"),
    ]
    res = build_layers_from_outputs(_manifest(entries), run_id=RID)
    ids = [l.layer_id for l in res.layers]
    assert ids == [
        f"flood-depth-peak-{RID}",
        f"flood-depth-frame-01-{RID}",
        f"flood-depth-frame-02-{RID}",
    ]
    assert [l.role for l in res.layers] == ["primary", "context", "context"]
    # temporal group membership + t on the replay records (ADR item 7).
    frames = {f.layer_id: f for f in res.frames}
    assert frames[f"flood-depth-peak-{RID}"].t is None
    assert frames[f"flood-depth-frame-01-{RID}"].t == 0.0
    assert frames[f"flood-depth-frame-01-{RID}"].group_id == f"flood-depth-{RID}"
    # idempotence: a re-poll mints the SAME ids.
    res2 = build_layers_from_outputs(_manifest(entries), run_id=RID)
    assert [l.layer_id for l in res2.layers] == ids


def test_registered_quantity_pins_preset():
    entries = [build_entry(kind="raster", quantity="flood_depth",
                           name="Peak flood depth",
                           uri="s3://b/%s/flood_depth_peak.tif" % RID, units="meters")]
    res = build_layers_from_outputs(_manifest(entries), run_id=RID)
    assert res.layers[0].style_preset == "continuous_flood_depth"
    assert res.unknown_quantity_count == 0


def test_unknown_quantity_neutral_ramp():
    quantity_styles.reset_unknown_quantity_fallback_count()
    entries = [build_entry(kind="raster", quantity="mystery_field",
                           name="Mystery", uri="s3://b/%s/mystery.tif" % RID)]
    res = build_layers_from_outputs(_manifest(entries), run_id=RID)
    assert res.layers[0].style_preset == "neutral_ramp"
    assert res.unknown_quantity_count == 1


def test_scalar_is_log_only():
    entries = [
        build_entry(kind="scalar", quantity="mass_balance", name="Mass balance",
                    uri="s3://b/%s/mb.json" % RID),
    ]
    res = build_layers_from_outputs(_manifest(entries), run_id=RID)
    assert res.layers == []
    assert res.scalar_count == 1


def test_mesh_entry_publishes_native_mesh_layer(tmp_path):
    """ADR 0283: a kind=mesh entry -> a layer_type=mesh LayerURI (role context),
    crs_authid threaded from the entry, bbox None (MDAL derives the extent), and a
    deterministic {quantity-base}-mesh-{run_id} id. Byte-equivalent (name/style/
    role/crs/uri) to the bespoke rain_on_grid _publish_full_results_mesh it
    supersedes; only the layer_id STEM diverges (idempotence key, explained)."""
    reach = "Coweeta"
    mesh_uri = "s3://trid3nt-runs/%s/r2d_rog.slf" % RID
    entries = [
        # peak entry (whole-run record, skipped under frames_only).
        build_entry(kind="raster", quantity="flood_depth",
                    name="Peak depth (%s)" % reach,
                    uri="s3://trid3nt-runs/%s/telemac_wse_max.tif" % RID,
                    bbox=[-83.5, 35.0, -83.4, 35.1]),
        build_entry(kind="mesh", quantity="model_results",
                    name="Model results (time series): %s" % reach,
                    uri=mesh_uri, crs_authid="EPSG:32617"),
    ]
    manifest = _manifest(entries)

    # frames_only=True (the composer path): the seam builds ONLY the mesh.
    res = build_layers_from_outputs(
        manifest, run_id=RID, bbox=(-83.5, 35.0, -83.4, 35.1), frames_only=True
    )
    assert len(res.layers) == 1 and res.mesh_count == 1
    mesh = res.layers[0]

    # Field-for-field vs the bespoke _publish_full_results_mesh (byte-equivalence).
    assert mesh.name == "Model results (time series): %s" % reach
    assert mesh.layer_type == "mesh"
    assert mesh.uri == mesh_uri
    assert mesh.style_preset == "mesh_grid"
    assert mesh.role == "context"
    assert mesh.bbox is None  # NOT the composer AOI -- MDAL derives it.
    assert mesh.crs_authid == "EPSG:32617"
    # The ONE explained divergence: layer_id stem (idempotence key).
    assert mesh.layer_id == "model-results-mesh-%s" % RID
    assert mesh.layer_id != "rog-results-%s" % RID
    # idempotence: a re-poll mints the SAME id.
    res2 = build_layers_from_outputs(manifest, run_id=RID, frames_only=True)
    assert res2.layers[0].layer_id == mesh.layer_id

    # frames_only=False: the mesh is STILL built (it is the temporal artifact),
    # alongside the standalone peak.
    res3 = build_layers_from_outputs(manifest, run_id=RID, frames_only=False)
    assert res3.mesh_count == 1
    assert sorted(l.layer_type for l in res3.layers) == ["mesh", "raster"]
    mesh3 = [l for l in res3.layers if l.layer_type == "mesh"][0]
    assert mesh3.role == "context" and mesh3.crs_authid == "EPSG:32617"


def test_missing_manifest_is_a_noop():
    class _Result:
        run_id = "01NOSUCHRUN0000000000000000"

    # No outputs.json object exists for this run -> None (byte-identical no-op).
    assert read_outputs_manifest(_Result()) is None


# --------------------------------------------------------------------------- #
# The byte-equivalence bar (Section 7.1)
# --------------------------------------------------------------------------- #
rasterio = pytest.importorskip("rasterio")
xr = pytest.importorskip("xarray")
pytest.importorskip("scipy")
import numpy as np  # noqa: E402


def _write_synthetic_map(path: Path, *, nx=20, ny=16, n_time=5) -> None:
    x = np.linspace(500000.0, 500000.0 + nx * 50.0, nx).astype("float64")
    y = np.linspace(3300000.0, 3300000.0 + ny * 50.0, ny).astype("float64")
    hmax = np.full((ny, nx), 2.0, dtype="float32")
    zb = np.full((ny, nx), -1.0, dtype="float32")
    zs = np.stack(
        [np.full((ny, nx), 0.5 + 1.0 * t / (n_time - 1), dtype="float32")
         for t in range(n_time)]
    )
    ds = xr.Dataset(
        {
            "hmax": (("n", "m"), hmax),
            "zb": (("n", "m"), zb),
            "zs": (("time", "n", "m"), zs),
            "crs": ((), np.int32(32616)),
        },
        coords={
            "x": ("m", x), "y": ("n", y),
            "time": ("time", np.arange(n_time, dtype="float64")),
        },
    )
    ds.to_netcdf(path, engine="scipy")


def _resolved_style(preset, band_stats, uri):
    from trid3nt_server.emission.publish import (
        style_params_from_band_stats,
    )
    bs = band_stats or {}
    return style_params_from_band_stats(
        preset, is_categorical=bool(bs.get("is_categorical")),
        is_rgba=bool(bs.get("is_rgba")), p2=bs.get("p2"), p98=bs.get("p98"),
        layer_uri=uri,
    )


def _stashed_legend(uri):
    from trid3nt_server.emission import publish as pl
    lg = pl._LAST_LEGEND_BY_URI.get(uri)
    if lg is None:
        return None
    return (lg.kind, getattr(lg, "colormap", None), getattr(lg, "vmin", None),
            getattr(lg, "vmax", None), getattr(lg, "units", None))


def _row(layer, resolved):
    lid = layer.layer_id
    suffix = "-" + RID
    if lid.endswith(suffix):
        lid = lid[: -len(suffix)]
    return {
        "layer_id_modulo_runid": lid, "name": layer.name,
        "layer_type": layer.layer_type, "style_preset": layer.style_preset,
        "role": layer.role, "units": layer.units,
        "bbox": tuple(round(v, 6) for v in layer.bbox) if layer.bbox else None,
        "rescale": resolved, "stashed_legend": _stashed_legend(layer.uri),
    }


def test_byte_equivalence_seam_vs_register(tmp_path):
    """The seam's PEAK row == the register path's, field-for-field, and every seam
    FRAME renders with that same resolved style.

    publish_manifest.json carries the non-frame entries alone now (the metrics
    carrier + legacy register-only fallback), so the register path has exactly the
    peak to offer; the frames are the seam's alone."""
    from workers._raster_postprocess import outputs_manifest as om
    from workers._raster_postprocess import postprocess as pp
    from trid3nt_contracts.publish_manifest import parse_publish_manifest
    from trid3nt_server.workflows.shared.register_published_manifest import (
        register_manifest_layers,
    )
    import json

    deck = Path(tempfile.mkdtemp(prefix="seam-equiv-"))
    _write_synthetic_map(deck / "sfincs_map.nc")
    runs_uri_for = lambda rel: f"s3://trid3nt-runs/{RID}/{rel}"  # noqa: E731

    res = pp.run_postprocess(
        deck / "sfincs_map.nc", run_id=RID, deck_dir=deck,
        runs_uri_for=runs_uri_for, kind="depth", engine="sfincs",
    )
    assert res.status == "ok"
    assert len(res.manifest["layers"]) == 1
    assert len(res.outputs_entries) >= 3

    # OLD path.
    pm = parse_publish_manifest(json.dumps(res.manifest))
    old = register_manifest_layers(pm, run_id=RID, bbox=None)
    old_stream = []
    for e, lyr in zip(pm.layers, old.layers):
        bs = {"is_categorical": e.band_stats.is_categorical,
              "is_rgba": e.band_stats.is_rgba, "p2": e.band_stats.p2,
              "p98": e.band_stats.p98}
        old_stream.append(_row(lyr, _resolved_style(e.style_preset, bs, e.cog_uri)))

    # NEW path.
    manifest = parse_outputs_manifest(
        om.append_entries(None, engine="sfincs", run_id=RID, new=res.outputs_entries)
    )
    new = build_layers_from_outputs(manifest, run_id=RID, bbox=None)
    new_stream = []
    for lyr in new.layers:
        new_stream.append(_row(lyr, _resolved_style(lyr.style_preset, None, lyr.uri)))

    new_peak = [r for r in new_stream if r["role"] == "primary"]
    assert new_peak == old_stream, (
        "peak layer-event row diverged:\nOLD=%s\nNEW=%s" % (old_stream, new_peak)
    )
    # Every frame renders with the peak's resolved style/rescale/legend -- only
    # the name, role and layer_id ordinal differ.
    frame_rows = [r for r in new_stream if r["role"] != "primary"]
    assert frame_rows
    for row in frame_rows:
        for field in ("style_preset", "units", "bbox", "rescale", "stashed_legend"):
            assert row[field] == new_peak[0][field], field
    # The NEW path additionally carries per-frame t + group_id (additive item 7).
    temporal = [f for f in new.frames if f.t is not None]
    assert temporal and all(f.group_id == f"flood-depth-{RID}" for f in temporal)
