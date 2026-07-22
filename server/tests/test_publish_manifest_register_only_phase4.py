"""SFINCS postprocess-offload Phase 4 - AGENT-side thin-out tests.

Covers the worker -> agent ``publish_manifest.json`` register-only handoff:

1. Typed contract parse + schema-version REJECT (the agent's typed reader mirror
   of the worker's plain-dict writer, gated on schema_version==1).
2. ``register_manifest_layers`` (TiTiler exit / QGIS-native swap) emits the raw
   ``s3://`` ``cog_uri`` AS the layer uri (the plugin reads it via /vsicurl/),
   stashes the data-driven legend keyed by that ``cog_uri`` (resolved from the
   agent style registry + ``band_stats``, NO COG read), mints ``layer_id`` =
   ``<stem>-<run_id>``, registers the COG, and surfaces the top-level metrics.
   No tile server is needed - the old ``TRID3NT_TILE_SERVER_BASE``
   publish-or-honest-drop gate is GONE (nothing drops).
3. ``read_publish_manifest`` returns the typed manifest when present + parses to
   schema 1, and ``None`` (the on-box fallback trigger) when absent / unknown.
4. ``model_flood_scenario``: the on-box ``postprocess_flood`` convert sits under
   ``if not register_only:`` so a present manifest short-circuits it and an
   absent manifest runs the legacy convert unchanged.

All mocked - no network / GDAL / solver / S3.
"""

from __future__ import annotations

import inspect
import json

import pytest

from trid3nt_server.tools import publish_layer as pl
from trid3nt_server.uri_registry import (
    SessionUriRegistry,
    activate_registry,
    deactivate_registry,
)
from trid3nt_server.workflows import register_published_manifest as rpm
from trid3nt_contracts.publish_manifest import (
    MANIFEST_SCHEMA_VERSION,
    PublishManifest,
    parse_publish_manifest,
)

_TILE_BASE = "https://tiles.example.test"


# --------------------------------------------------------------------------- #
# Manifest fixtures (mirror the worker's build_manifest output exactly).
# --------------------------------------------------------------------------- #


def _depth_manifest_dict() -> dict:
    return {
        "schema_version": 1,
        "engine": "sfincs_quadtree",
        "run_id": "RUNRUNRUN",
        "status": "ok",
        "frame_count": 2,
        "metrics": {
            "max_depth_m": 2.41,
            "mean_depth_m": 0.63,
            "p95_depth_m": 1.88,
            "flooded_cell_count": 184213,
            "crs": "EPSG:32616",
            "units": "meters",
        },
        "layers": [
            {
                "layer_id_stem": "flood-depth-peak",
                "name": "Peak flood depth",
                "layer_type": "raster",
                "role": "primary",
                "style_preset": "continuous_flood_depth",
                "units": "meters",
                "cog_uri": "s3://runs/RUNRUNRUN/flood_depth_peak.tif",
                "frame_no": None,
                "bbox": [-85.45, 29.93, -85.38, 29.98],
                "has_overviews": True,
                "band_stats": {
                    "is_categorical": False,
                    "is_rgba": False,
                    "p2": 0.05,
                    "p98": 2.30,
                    "min": 0.0,
                    "max": 2.41,
                },
                "metrics": {
                    "max_depth_m": 2.41,
                    "mean_depth_m": 0.63,
                    "p95_depth_m": 1.88,
                    "flooded_cell_count": 184213,
                },
            },
            {
                "layer_id_stem": "flood-depth-frame-01",
                "name": "Flood depth step 1",
                "layer_type": "raster",
                "role": "context",
                "style_preset": "continuous_flood_depth",
                "units": "meters",
                "cog_uri": "s3://runs/RUNRUNRUN/flood_depth_frame_01.tif",
                "frame_no": 1,
                "bbox": [-85.45, 29.93, -85.38, 29.98],
                "has_overviews": True,
                "band_stats": {"is_categorical": False, "is_rgba": False},
            },
        ],
    }


def _wave_manifest_dict() -> dict:
    return {
        "schema_version": 1,
        "engine": "swan",
        "run_id": "WAVEWAVE",
        "status": "ok",
        "frame_count": 1,
        "metrics": {"max_hs_m": 3.2},
        "layers": [
            {
                "layer_id_stem": "swan-wave-height-peak",
                "name": "Peak wave height",
                "layer_type": "raster",
                "role": "primary",
                "style_preset": "continuous_wave_height",
                "units": "meters",
                "cog_uri": "s3://runs/WAVEWAVE/swan_wave_height_peak.tif",
                "frame_no": None,
                "bbox": [-85.45, 29.93, -85.38, 29.98],
                "has_overviews": True,
                "band_stats": {"is_categorical": False, "is_rgba": False},
                "metrics": {
                    "max_hs_m": 3.2,
                    "mean_tp_s": 8.1,
                    "mean_dir_deg": 145.0,
                    "wave_area_km2": 12.5,
                },
            }
        ],
    }


# --------------------------------------------------------------------------- #
# 1. Typed contract parse + schema-version reject.
# --------------------------------------------------------------------------- #


def test_parse_manifest_known_schema_version_validates():
    m = parse_publish_manifest(json.dumps(_depth_manifest_dict()))
    assert isinstance(m, PublishManifest)
    assert m.schema_version == MANIFEST_SCHEMA_VERSION == 1
    assert m.engine == "sfincs_quadtree"
    assert len(m.layers) == 2
    peak = m.layers[0]
    assert peak.layer_id_stem == "flood-depth-peak"
    assert peak.name == "Peak flood depth"
    assert peak.cog_uri == "s3://runs/RUNRUNRUN/flood_depth_peak.tif"
    assert peak.band_stats.p2 == 0.05 and peak.band_stats.p98 == 2.30
    assert m.metrics["max_depth_m"] == 2.41


def test_parse_manifest_unknown_schema_version_rejects():
    bad = _depth_manifest_dict()
    bad["schema_version"] = 999
    with pytest.raises(ValueError, match="unknown publish_manifest schema_version"):
        parse_publish_manifest(json.dumps(bad))


def test_parse_manifest_missing_schema_version_rejects():
    bad = _depth_manifest_dict()
    del bad["schema_version"]
    with pytest.raises(ValueError, match="missing schema_version"):
        parse_publish_manifest(json.dumps(bad))


def test_parse_manifest_non_object_rejects():
    with pytest.raises(ValueError, match="must be a JSON object"):
        parse_publish_manifest("[1, 2, 3]")


def test_parse_manifest_ignores_forward_compatible_extra_keys():
    """A future additive top-level/layer key must NOT crash the reader."""
    fwd = _depth_manifest_dict()
    fwd["future_top_key"] = {"anything": 1}
    fwd["layers"][0]["future_layer_key"] = "ok"
    m = parse_publish_manifest(json.dumps(fwd))  # must not raise
    assert m.layers[0].layer_id_stem == "flood-depth-peak"


def test_parse_manifest_accepts_bytes_body():
    m = parse_publish_manifest(json.dumps(_depth_manifest_dict()).encode("utf-8"))
    assert m.schema_version == 1


# --------------------------------------------------------------------------- #
# 2. register_manifest_layers - raw cog_uri emission + legend stash + registration.
# --------------------------------------------------------------------------- #


@pytest.fixture()
def active_registry():
    reg = SessionUriRegistry(session_id="sess-test")
    token = activate_registry(reg)
    try:
        yield reg
    finally:
        deactivate_registry(token)


def test_register_manifest_layers_emits_raw_cog_uri_and_registers(
    monkeypatch, active_registry
):
    # No tile server anywhere: the TiTiler exit needs none.
    monkeypatch.delenv("TRID3NT_TILE_SERVER_BASE", raising=False)
    m = parse_publish_manifest(json.dumps(_depth_manifest_dict()))
    res = rpm.register_manifest_layers(m, run_id="RUNRUNRUN")

    # Two layers, none dropped, top-level metrics surfaced for FloodMetrics.
    assert res.dropped_count == 0
    assert res.tile_publish_available is True
    assert res.metrics["max_depth_m"] == 2.41
    assert len(res.layers) == 2

    peak = res.layers[0]
    frame = res.layers[1]
    # layer_id = <stem>-<run_id>.
    assert peak.layer_id == "flood-depth-peak-RUNRUNRUN"
    assert frame.layer_id == "flood-depth-frame-01-RUNRUNRUN"
    # The web grouping token (name) is carried verbatim - never renamed.
    assert peak.name == "Peak flood depth"
    assert frame.name == "Flood depth step 1"
    # NEW CONTRACT (TiTiler exit): the layer uri IS the raw s3:// COG - the
    # plugin reads it via /vsicurl/; no tile template is minted.
    assert peak.uri == "s3://runs/RUNRUNRUN/flood_depth_peak.tif"
    assert frame.uri == "s3://runs/RUNRUNRUN/flood_depth_frame_01.tif"
    # roles preserved.
    assert peak.role == "primary"
    assert frame.role == "context"

    # The COG is registered as the consumable DATA uri (no separate display
    # face anymore - the raw COG IS the envelope uri).
    rec = active_registry._records.get("flood-depth-peak-RUNRUNRUN")
    assert rec is not None
    assert rec.uri == "s3://runs/RUNRUNRUN/flood_depth_peak.tif"

    # DATA-DRIVEN LEGEND: stashed keyed by the cog_uri, carrying the SAME
    # pinned continuous_flood_depth ramp the style registry resolves
    # (ylgnbu over 0-3 m) - mirrors publish_layer's raw-cog exit.
    legend = pl.pop_legend_for_uri(peak.uri)
    assert legend is not None
    assert legend.kind == "continuous"
    assert legend.colormap == "ylgnbu"
    assert legend.vmin == 0.0
    assert legend.vmax == 3.0


def test_register_manifest_layers_needs_no_tile_server(
    monkeypatch, active_registry
):
    """The old TRID3NT_TILE_SERVER_BASE publish-or-honest-drop gate is GONE:
    with AND without the env var the registration is identical (raw cog_uri),
    and nothing is ever dropped for lack of a tile server."""
    m = parse_publish_manifest(json.dumps(_depth_manifest_dict()))

    monkeypatch.delenv("TRID3NT_TILE_SERVER_BASE", raising=False)
    res_without = rpm.register_manifest_layers(m, run_id="RUNRUNRUN")
    monkeypatch.setenv("TRID3NT_TILE_SERVER_BASE", _TILE_BASE)
    res_with = rpm.register_manifest_layers(m, run_id="RUNRUNRUN")

    for res in (res_without, res_with):
        assert res.dropped_count == 0
        assert res.tile_publish_available is True
        assert [lyr.uri for lyr in res.layers] == [
            "s3://runs/RUNRUNRUN/flood_depth_peak.tif",
            "s3://runs/RUNRUNRUN/flood_depth_frame_01.tif",
        ]
        assert res.metrics["max_depth_m"] == 2.41


def test_register_swan_wave_layers_carries_narration_scalars(
    monkeypatch, active_registry
):
    monkeypatch.delenv("TRID3NT_TILE_SERVER_BASE", raising=False)
    m = parse_publish_manifest(json.dumps(_wave_manifest_dict()))
    layers, top_metrics, dropped = rpm.register_swan_wave_layers(
        m, run_id="WAVEWAVE", mode="stationary"
    )
    assert dropped == 0
    assert top_metrics["max_hs_m"] == 3.2
    assert len(layers) == 1
    peak = layers[0]
    # The four typed narration scalars come from the per-layer manifest metrics.
    assert peak.max_hs_m == 3.2
    assert peak.mean_tp_s == 8.1
    assert peak.mean_dir_deg == 145.0
    assert peak.wave_area_km2 == 12.5
    assert peak.mode == "stationary"
    assert peak.layer_id == "swan-wave-height-peak-WAVEWAVE"
    # Raw cog uri emission + the continuous_wave_height (gnbu) legend stash.
    assert peak.uri == "s3://runs/WAVEWAVE/swan_wave_height_peak.tif"
    legend = pl.pop_legend_for_uri(peak.uri)
    assert legend is not None and legend.colormap == "gnbu"


def test_style_params_from_band_stats_honors_rgba_and_generic_fallback():
    # RGBA -> empty (TiTiler renders baked colors), no COG read.
    assert pl.style_params_from_band_stats("anything", is_rgba=True) == ""
    # Categorical -> empty (embedded palette wins).
    assert pl.style_params_from_band_stats("anything", is_categorical=True) == ""
    # Unknown single-band preset -> generic p2/p98 viridis rescale (no COG read).
    sp = pl.style_params_from_band_stats("some_unregistered_scalar", p2=1.0, p98=9.0)
    assert "rescale=1,9" in sp and "colormap_name=viridis" in sp
    # Flood/wave presets resolve to their pinned ramps (byte-for-byte parity).
    assert pl.style_params_from_band_stats("continuous_flood_depth") == (
        "&rescale=0,3&colormap_name=ylgnbu"
    )
    sp_wave = pl.style_params_from_band_stats("continuous_wave_height")
    assert "rescale=0,6" in sp_wave and "colormap_name=gnbu" in sp_wave


# --------------------------------------------------------------------------- #
# 3. read_publish_manifest - present (parse) vs absent (fallback trigger).
# --------------------------------------------------------------------------- #


class _RR:
    def __init__(self, run_id="RUNRUNRUN"):
        self.run_id = run_id


def _patch_solver(monkeypatch, *, completion, manifest_bytes=None):
    from trid3nt_server.tools.simulation import solver as solver_mod

    monkeypatch.setattr(solver_mod, "_get_runs_bucket", lambda: "runs")
    monkeypatch.setattr(
        solver_mod, "_try_get_completion_s3", lambda bucket, run_id: completion
    )
    if manifest_bytes is not None:
        monkeypatch.setattr(
            solver_mod, "_read_object_bytes", lambda uri: manifest_bytes
        )


def test_read_publish_manifest_present(monkeypatch):
    _patch_solver(
        monkeypatch,
        completion={
            "status": "ok",
            "publish_manifest_uri": "s3://runs/RUNRUNRUN/publish_manifest.json",
        },
        manifest_bytes=json.dumps(_depth_manifest_dict()).encode("utf-8"),
    )
    m = rpm.read_publish_manifest(_RR())
    assert m is not None
    assert m.schema_version == 1
    assert len(m.layers) == 2


def test_read_publish_manifest_absent_returns_none_for_fallback(monkeypatch):
    # completion.json present but NO publish_manifest_uri (pre-rebuild worker).
    _patch_solver(
        monkeypatch,
        completion={"status": "ok", "output_uris": ["s3://runs/x/sfincs_map.nc"]},
    )
    assert rpm.read_publish_manifest(_RR()) is None


def test_read_publish_manifest_no_completion_returns_none(monkeypatch):
    _patch_solver(monkeypatch, completion=None)
    assert rpm.read_publish_manifest(_RR()) is None


def test_read_publish_manifest_unknown_schema_returns_none(monkeypatch):
    bad = _depth_manifest_dict()
    bad["schema_version"] = 999
    _patch_solver(
        monkeypatch,
        completion={
            "status": "ok",
            "publish_manifest_uri": "s3://runs/RUNRUNRUN/publish_manifest.json",
        },
        manifest_bytes=json.dumps(bad).encode("utf-8"),
    )
    # Unknown schema_version -> None -> caller runs the on-box fallback.
    assert rpm.read_publish_manifest(_RR()) is None


# --------------------------------------------------------------------------- #
# 4. model_flood_scenario branch structure: register-only short-circuits the
#    on-box postprocess; absent manifest runs the legacy convert.
# --------------------------------------------------------------------------- #


def test_flood_scenario_branch_is_clean_if_else():
    """The register-only vs on-box fallback split is a clean if/else gated on a
    present manifest, and the heavy on-box steps sit under ``if not register_only``."""
    import trid3nt_server.workflows.model_flood_scenario as mfs

    body = inspect.getsource(mfs.model_flood_scenario)
    # The branch trigger.
    assert "manifest = await asyncio.to_thread(read_publish_manifest, run_result)" in body
    assert "register_only = manifest is not None" in body
    assert "register_manifest_layers(" in body
    # The on-box postprocess + publish blocks are guarded so a present manifest
    # short-circuits them (Step 8 + Steps 9/9b/9c).
    assert body.count("if not register_only:") >= 2
    assert "postprocess_flood," in body  # legacy convert still present (fallback)


def test_wave_scenario_branch_is_clean_if_else():
    """model_wave_scenario gains the same present-manifest register-only branch in
    front of the on-box download + postprocess_swan fallback."""
    import trid3nt_server.workflows.model_wave_scenario as mws

    body = inspect.getsource(mws.model_wave_scenario)
    assert "manifest = await asyncio.to_thread(read_publish_manifest, run_result)" in body
    assert "if manifest is not None:" in body
    assert "register_swan_wave_layers" in body
    # The on-box fallback path is preserved (download + postprocess_swan).
    assert "_download_batch_swan_outputs" in body
    assert "postprocess_swan," in body
