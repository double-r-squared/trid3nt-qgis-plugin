"""The flood composer's emit-on-solve SEAM FORK (ADR 0280 item 4, live close-out).

`model_flood_scenario`'s post-solve publication chooses ONE path:

  1. SEAM     -- `outputs.json` present under the run prefix: the seam
                 (`build_layers_from_outputs`) owns ALL publication (peak +
                 temporal frames + replay stamps). The `publish_manifest.json`
                 is still read, but ONLY for the top-level `FloodMetrics`
                 narration scalars (the flat outputs.json entries carry none) --
                 it is the metrics carrier, NOT a second publication.
  2. REGISTER -- no outputs.json, `publish_manifest.json` present: the legacy
                 register-only path, byte-unchanged.
  3. ON-BOX   -- neither present: the legacy on-box postprocess fallback.

These tests pin the FORK PRECEDENCE + the metrics-carrier coexistence at the
read boundary (the exact objects `flood.py` consults), mirroring
`_patch_solver` from the register-only phase-4 suite. The end-to-end composer
path is proven live through the rebuilt worker image (the quadtree canary).
"""

from __future__ import annotations

import json

from trid3nt_contracts.outputs_manifest import append_entries, build_entry
from trid3nt_server.emission.outputs_seam import (
    build_layers_from_outputs,
    read_outputs_manifest,
)
from trid3nt_server.workflows.shared.register_published_manifest import (
    read_publish_manifest,
)

RID = "01SEAMFORKRUN0000000000000A"


class _RR:
    run_id = RID


def _outputs_bytes():
    entries = [
        build_entry(kind="raster", quantity="flood_depth", name="Peak flood depth",
                    uri=f"s3://runs/{RID}/flood_depth_peak.tif", units="meters"),
        build_entry(kind="raster", quantity="flood_depth", name="Flood depth step 1",
                    uri=f"s3://runs/{RID}/flood_depth_frame_01.tif", t=0.0, units="meters"),
        build_entry(kind="raster", quantity="flood_depth", name="Flood depth step 2",
                    uri=f"s3://runs/{RID}/flood_depth_frame_02.tif", t=1800.0, units="meters"),
    ]
    return append_entries(None, engine="sfincs", run_id=RID, new=entries).encode("utf-8")


def _publish_manifest_dict():
    # The metrics carrier: the top-level aggregates the seam path threads into
    # FloodMetrics (outputs.json entries carry no aggregates).
    return {
        "schema_version": 1,
        "engine": "sfincs",
        "status": "ok",
        "metrics": {
            "max_depth_m": 2.4,
            "mean_depth_m": 0.6,
            "p95_depth_m": 1.9,
            "flooded_cell_count": 5123,
        },
        "layers": [
            {
                "layer_id_stem": "flood-depth-peak",
                "name": "Peak flood depth",
                "cog_uri": f"s3://runs/{RID}/flood_depth_peak.tif",
                "style_preset": "continuous_flood_depth",
                "role": "primary",
                "units": "meters",
                "band_stats": {"is_categorical": False, "is_rgba": False,
                               "p2": 0.0, "p98": 3.0},
            }
        ],
    }


def _patch_both_present(monkeypatch):
    """Serve BOTH outputs.json and publish_manifest.json for the run prefix."""
    from trid3nt_server.data.simulation.solver import solver as solver_mod

    outputs_uri = f"s3://runs/{RID}/outputs.json"
    manifest_uri = f"s3://runs/{RID}/publish_manifest.json"
    manifest_bytes = json.dumps(_publish_manifest_dict()).encode("utf-8")

    monkeypatch.setattr(solver_mod, "_get_runs_bucket", lambda: "runs")
    monkeypatch.setattr(
        solver_mod,
        "_try_get_completion_s3",
        lambda bucket, run_id: {"status": "ok", "publish_manifest_uri": manifest_uri},
    )

    def _read(uri):
        if uri == outputs_uri:
            return _outputs_bytes()
        if uri == manifest_uri:
            return manifest_bytes
        raise FileNotFoundError(uri)

    monkeypatch.setattr(solver_mod, "_read_object_bytes", _read)


def test_seam_wins_when_outputs_json_present(monkeypatch):
    # The fork's first read: outputs.json present -> seam path is taken.
    _patch_both_present(monkeypatch)
    manifest = read_outputs_manifest(_RR())
    assert manifest is not None
    assert manifest.engine == "sfincs"
    assert len(manifest.entries) == 3


def test_publish_manifest_is_the_metrics_carrier_alongside_the_seam(monkeypatch):
    # Reading publish_manifest for metrics is NOT a second publication: the seam
    # owns the layers; publish_manifest supplies ONLY the narration scalars.
    _patch_both_present(monkeypatch)
    pm = read_publish_manifest(_RR())
    assert pm is not None
    assert pm.metrics["max_depth_m"] == 2.4
    assert pm.metrics["flooded_cell_count"] == 5123


def test_seam_owns_the_full_stream_with_replay_stamps(monkeypatch):
    # The seam builds the peak (primary) + the two ordered frames (context) and
    # carries the item-7 replay meta (t / group_id) alongside each.
    _patch_both_present(monkeypatch)
    manifest = read_outputs_manifest(_RR())
    seam = build_layers_from_outputs(manifest, run_id=RID)
    assert [l.layer_id for l in seam.layers] == [
        f"flood-depth-peak-{RID}",
        f"flood-depth-frame-01-{RID}",
        f"flood-depth-frame-02-{RID}",
    ]
    assert [l.role for l in seam.layers] == ["primary", "context", "context"]
    frames = {f.layer_id: f for f in seam.frames}
    assert frames[f"flood-depth-peak-{RID}"].t is None
    assert frames[f"flood-depth-frame-01-{RID}"].t == 0.0
    assert frames[f"flood-depth-frame-02-{RID}"].t == 1800.0
    assert frames[f"flood-depth-frame-01-{RID}"].group_id == f"flood-depth-{RID}"


# --------------------------------------------------------------------------- #
# Honesty floor: no metrics carrier -> refuse, never narrate confident zeros
# --------------------------------------------------------------------------- #


def test_absent_metrics_carrier_is_a_refusal_not_zeros():
    """When the run's completion.json carries no publish_manifest pointer,
    ``read_publish_manifest`` returns None -> ``depth_metrics`` is {} -> the
    composer must REFUSE (law 9). The four narrated depth scalars have no
    default: 0.0 over a peak COG holding metres of water is the worst answer."""
    from trid3nt_server.workflows.sfincs.run_sfincs import (
        NARRATED_DEPTH_METRIC_KEYS,
        missing_depth_metric_keys,
    )

    assert missing_depth_metric_keys({}) == list(NARRATED_DEPTH_METRIC_KEYS)
    # A partial carrier is just as unnarratable as an empty one.
    assert missing_depth_metric_keys({"max_depth_m": 19.9}) == [
        "mean_depth_m", "p95_depth_m", "flooded_cell_count"
    ]


def test_genuinely_dry_solve_narrates_its_zeros():
    """Zero VALUES are real data; only a missing KEY means "no data". A dry run
    must still narrate, so the guard keys on presence, never on truthiness."""
    from trid3nt_server.workflows.sfincs.run_sfincs import missing_depth_metric_keys

    dry = {
        "max_depth_m": 0.0,
        "mean_depth_m": 0.0,
        "p95_depth_m": 0.0,
        "flooded_cell_count": 0,
    }
    assert missing_depth_metric_keys(dry) == []


def test_composer_refuses_with_a_typed_code_when_metrics_are_missing():
    """The refusal rides the documented failed-envelope seam (error_code threaded
    into solver_version + the ``:FAILED:`` workflow_name infix), so the agent
    surface narrates the failure instead of the zeros."""
    import inspect

    from trid3nt_server.workflows.sfincs.flood import flood as m

    body = inspect.getsource(m.model_flood_scenario)
    assert "missing_depth_metric_keys(depth_metrics)" in body
    assert "SFINCS_METRICS_UNAVAILABLE" in body
    # The refusal must come BEFORE the FloodMetrics build it guards.
    assert body.index("SFINCS_METRICS_UNAVAILABLE") < body.index("metrics = FloodMetrics(")
