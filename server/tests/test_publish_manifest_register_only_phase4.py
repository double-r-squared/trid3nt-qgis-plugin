"""SFINCS postprocess-offload Phase 4 - AGENT-side thin-out tests.

Covers the worker -> agent ``publish_manifest.json`` register-only handoff:

1. Typed contract parse + schema-version REJECT (the agent's typed reader mirror
   of the worker's plain-dict writer, gated on schema_version==1).
2. ``register_manifest_layers`` builds the correct TiTiler tile URLs from the
   bare ``cog_uri`` + the agent style registry, mints ``layer_id`` =
   ``<stem>-<run_id>``, registers BOTH faces, honors the publish-or-honest-drop
   ``GRACE2_TILE_SERVER_BASE`` gate, and surfaces the top-level metrics.
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

from grace2_agent.tools import publish_layer as pl
from grace2_agent.uri_registry import (
    SessionUriRegistry,
    activate_registry,
    deactivate_registry,
)
from grace2_agent.workflows import register_published_manifest as rpm
from grace2_contracts.publish_manifest import (
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
# 2. register_manifest_layers - TiTiler URL + registration + gate + metrics.
# --------------------------------------------------------------------------- #


@pytest.fixture()
def active_registry():
    reg = SessionUriRegistry(session_id="sess-test")
    token = activate_registry(reg)
    try:
        yield reg
    finally:
        deactivate_registry(token)


def test_register_manifest_layers_builds_tile_urls_and_registers(
    monkeypatch, active_registry
):
    monkeypatch.setenv("GRACE2_TILE_SERVER_BASE", _TILE_BASE)
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
    # The tile URL is built from the BARE cog_uri + the flood-depth style preset.
    assert peak.uri.startswith(
        f"{_TILE_BASE}/cog/tiles/WebMercatorQuad/{{z}}/{{x}}/{{y}}.png?url="
    )
    # continuous_flood_depth resolves to the pinned ylgnbu 0,3 ramp.
    assert "rescale=0,3" in peak.uri
    assert "colormap_name=ylgnbu" in peak.uri
    # The bare COG key is URL-encoded into url=.
    assert "flood_depth_peak.tif" in peak.uri
    # roles preserved.
    assert peak.role == "primary"
    assert frame.role == "context"

    # BOTH faces registered: the s3:// COG (data) + the tile template (display).
    rec = active_registry._records.get("flood-depth-peak-RUNRUNRUN")
    assert rec is not None
    assert rec.uri == "s3://runs/RUNRUNRUN/flood_depth_peak.tif"
    assert rec.wms_url == peak.uri  # tile template routed to the display face


def test_register_manifest_layers_honest_drop_without_tile_server(
    monkeypatch, active_registry
):
    monkeypatch.delenv("GRACE2_TILE_SERVER_BASE", raising=False)
    m = parse_publish_manifest(json.dumps(_depth_manifest_dict()))
    res = rpm.register_manifest_layers(m, run_id="RUNRUNRUN")
    # Publish-or-honest-drop: no tile server -> every layer dropped, metrics stand.
    assert res.layers == []
    assert res.dropped_count == 2
    assert res.tile_publish_available is False
    assert res.metrics["max_depth_m"] == 2.41


def test_register_swan_wave_layers_carries_narration_scalars(
    monkeypatch, active_registry
):
    monkeypatch.setenv("GRACE2_TILE_SERVER_BASE", _TILE_BASE)
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
    assert "colormap_name=gnbu" in peak.uri  # continuous_wave_height ramp


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
    from grace2_agent.tools import solver as solver_mod

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
    import grace2_agent.workflows.model_flood_scenario as mfs

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
    import grace2_agent.workflows.model_wave_scenario as mws

    body = inspect.getsource(mws.model_wave_scenario)
    assert "manifest = await asyncio.to_thread(read_publish_manifest, run_result)" in body
    assert "if manifest is not None:" in body
    assert "register_swan_wave_layers" in body
    # The on-box fallback path is preserved (download + postprocess_swan).
    assert "_download_batch_swan_outputs" in body
    assert "postprocess_swan," in body
