"""ADR 0280 -- the emit-on-solve seam consumer.

Seam unit behaviour over a synthetic ``outputs.json``: temporal grouping,
deterministic idempotent layer ids, registered-quantity pinned styling,
unknown-quantity neutral-ramp fallback, and the missing-manifest no-op.
"""

from __future__ import annotations

import pytest

from trid3nt_contracts.outputs_manifest import (
    append_entries,
    build_entry,
    parse_outputs_manifest,
)
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


def test_a_solved_raster_derives_its_row_from_its_own_kind_and_quantity():
    entries = [build_entry(kind="raster", quantity="flood_depth",
                           name="Peak flood depth",
                           uri="s3://b/%s/flood_depth_peak.tif" % RID, units="meters")]
    res = build_layers_from_outputs(_manifest(entries), run_id=RID)
    assert res.layers[0].style == {"kind": "continuous", "label": "Flood depth",
                                   "units": "meters"}


def test_no_quantity_can_be_unregistered_because_nothing_registers_one():
    # The quantity nobody put in a table still gets its own title and its own
    # range - there is no table to be missing from, so there is no fallback.
    entries = [build_entry(kind="raster", quantity="mystery_field",
                           name="Mystery", uri="s3://b/%s/mystery.tif" % RID)]
    res = build_layers_from_outputs(_manifest(entries), run_id=RID)
    assert res.layers[0].style == {"kind": "continuous", "label": "Mystery field"}


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
    assert mesh.style == {"kind": "mesh", "label": "Model results",
                          "dataset_group": "model_results"}
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
