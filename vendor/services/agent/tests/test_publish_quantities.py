"""Unit tests for the generic output-quantity executor (STEP 2; DEFAULT-OFF).

Drives the executor against a FAKE registry + FAKE registrar + FAKE upload, with
``cog_io.write_cog_4326_from_grid`` / ``cog_bbox_4326`` patched so the routing
logic is tested without real rasterio. Pins:

  - DEFAULT-OFF: the empty scaffold registry publishes nothing (byte-identical).
  - RasterField -> one manifest raster layer (style/name/role/units from spec).
  - TimeseriesField -> peak (primary "Peak <q>") + frame layers ("<q> step N").
  - ScalarField -> merged into the manifest metrics, no layer.
  - corrupt-frame degrades to peak-only (never raises); peak failure raises.
  - the empty-registry executor hands an empty manifest to the registrar.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from grace2_contracts.output_quantities import (
    OutputQuantitySpec,
    RasterField,
    ScalarField,
    TimeseriesField,
)
from grace2_agent.workflows import publish_quantities as pq
from grace2_agent.workflows.cog_io import CogIoError
from grace2_agent.workflows.publish_quantities import (
    QuantityExecError,
    build_quantities_manifest,
    publish_quantities,
)


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
def _fake_upload(cog, run_id, runs_bucket=None, *, dest_filename):  # noqa: ANN001
    return f"s3://runs/{run_id}/{dest_filename}"


def _patch_cog_io():
    """Patch cog_io write+bbox so no real rasterio is needed."""
    return (
        patch(
            "grace2_agent.workflows.publish_quantities.cog_io.write_cog_4326_from_grid",
            return_value=Path("/tmp/fake.tif"),
        ),
        patch(
            "grace2_agent.workflows.publish_quantities.cog_io.cog_bbox_4326",
            return_value=(-1.0, 2.0, 3.0, 4.0),
        ),
        patch(
            "grace2_agent.workflows.publish_quantities.cog_io.safe_unlink",
            return_value=None,
        ),
    )


def _raster_spec(enabled: bool = True) -> OutputQuantitySpec:
    def _reader(_ctx):
        return RasterField(
            grid=[[1.0]], src_crs="EPSG:4326", src_transform=None,
            metrics={"max_depth_m": 2.5},
        )

    return OutputQuantitySpec(
        quantity_id="flood-depth",
        kind="raster",
        name="Peak flood depth",
        style_preset="continuous_flood_depth",
        units="meters",
        role="primary",
        reader=_reader,
        default_on=enabled,
    )


# --------------------------------------------------------------------------- #
# DEFAULT-OFF / empty scaffold
# --------------------------------------------------------------------------- #
def test_empty_registry_produces_empty_manifest() -> None:
    m = build_quantities_manifest("sfincs", run_id="r1", upload=_fake_upload)
    assert m.layers == [] and m.metrics == {} and m.frame_count == 0
    assert m.engine == "sfincs" and m.schema_version == 1


def test_default_off_spec_is_skipped() -> None:
    spec = _raster_spec(enabled=False)  # default_on=False
    m = build_quantities_manifest(
        "x", run_id="r1", upload=_fake_upload, specs=[spec]
    )
    assert m.layers == []  # DEFAULT-OFF -> not published


def test_reader_none_scaffold_spec_skipped() -> None:
    spec = OutputQuantitySpec(
        quantity_id="q", kind="raster", name="Q", style_preset="p",
        reader=None, default_on=True,
    )
    m = build_quantities_manifest("x", run_id="r", upload=_fake_upload, specs=[spec])
    assert m.layers == []


# --------------------------------------------------------------------------- #
# RasterField routing
# --------------------------------------------------------------------------- #
def test_raster_field_routes_to_one_layer() -> None:
    w, b, u = _patch_cog_io()
    with w, b, u:
        m = build_quantities_manifest(
            "x", run_id="r1", upload=_fake_upload, specs=[_raster_spec()]
        )
    assert len(m.layers) == 1
    layer = m.layers[0]
    assert layer.name == "Peak flood depth"
    assert layer.style_preset == "continuous_flood_depth"
    assert layer.role == "primary" and layer.units == "meters"
    assert layer.layer_id_stem == "flood-depth-peak"  # -peak- token preserved
    assert layer.cog_uri == "s3://runs/r1/flood-depth_peak.tif"
    assert layer.metrics == {"max_depth_m": 2.5}
    # peak metrics also bubble up to the run aggregates.
    assert m.metrics.get("max_depth_m") == 2.5


def test_raster_write_failure_raises_quantity_exec_error() -> None:
    def _reader(_ctx):
        return RasterField(grid=[[1.0]], src_crs="EPSG:4326", src_transform=None)

    spec = OutputQuantitySpec(
        quantity_id="q", kind="raster", name="Q", style_preset="p",
        reader=_reader, default_on=True,
    )
    with patch(
        "grace2_agent.workflows.publish_quantities.cog_io.write_cog_4326_from_grid",
        side_effect=CogIoError("WRITE", message="disk full"),
    ):
        with pytest.raises(QuantityExecError) as ei:
            build_quantities_manifest("x", run_id="r", upload=_fake_upload, specs=[spec])
    assert ei.value.quantity_id == "q" and ei.value.stage == "WRITE"


# --------------------------------------------------------------------------- #
# ScalarField routing
# --------------------------------------------------------------------------- #
def test_scalar_field_merges_into_metrics_no_layer() -> None:
    def _reader(_ctx):
        return ScalarField(values={"basin_total_m3": 12345.0, "converged": True})

    spec = OutputQuantitySpec(
        quantity_id="s", kind="scalar", name="S", style_preset="p",
        reader=_reader, default_on=True,
    )
    m = build_quantities_manifest("x", run_id="r", upload=_fake_upload, specs=[spec])
    assert m.layers == []
    assert m.metrics == {"basin_total_m3": 12345.0, "converged": True}


# --------------------------------------------------------------------------- #
# TimeseriesField routing
# --------------------------------------------------------------------------- #
def _timeseries_spec(n_steps: int = 4) -> OutputQuantitySpec:
    peak = RasterField(
        grid=[[3.0]], src_crs="EPSG:4326", src_transform=None,
        metrics={"max_depth_m": 3.0},
    )

    def _read_step(raw_idx):
        return RasterField(
            grid=[[float(raw_idx)]], src_crs="EPSG:4326", src_transform=None,
            metrics={"max_depth_m": float(raw_idx)},
        )

    def _reader(_ctx):
        return TimeseriesField(
            n_steps=n_steps, read_step=_read_step, peak=peak,
            quantity_label="Flood depth",
        )

    return OutputQuantitySpec(
        quantity_id="flood-depth", kind="timeseries", name="Peak flood depth",
        style_preset="continuous_flood_depth", units="meters",
        reader=_reader, default_on=True,
    )


def test_timeseries_emits_peak_plus_frames() -> None:
    w, b, u = _patch_cog_io()
    with w, b, u:
        m = build_quantities_manifest(
            "x", run_id="r1", upload=_fake_upload, specs=[_timeseries_spec(4)]
        )
    names = [layer.name for layer in m.layers]
    assert names[0] == "Peak flood depth"  # peak first, role primary
    assert m.layers[0].role == "primary"
    frame_names = names[1:]
    assert frame_names == ["Flood depth step 1", "Flood depth step 2",
                           "Flood depth step 3", "Flood depth step 4"]
    assert all(layer.role == "context" for layer in m.layers[1:])
    # frame_no set on frames, None on peak.
    assert m.layers[0].frame_no is None
    assert [layer.frame_no for layer in m.layers[1:]] == [1, 2, 3, 4]
    assert m.frame_count == 4
    # distinct cog URIs (distinct keys -> no dedup collapse).
    uris = [layer.cog_uri for layer in m.layers]
    assert len(set(uris)) == len(uris)


def test_timeseries_corrupt_frame_degrades_to_peak_only() -> None:
    peak = RasterField(grid=[[3.0]], src_crs="EPSG:4326", src_transform=None)

    def _read_step(raw_idx):
        return RasterField(grid=[[float(raw_idx)]], src_crs="EPSG:4326",
                           src_transform=None)

    def _reader(_ctx):
        return TimeseriesField(n_steps=5, read_step=_read_step, peak=peak,
                               quantity_label="Flood depth")

    spec = OutputQuantitySpec(
        quantity_id="flood-depth", kind="timeseries", name="Peak flood depth",
        style_preset="p", reader=_reader, default_on=True,
    )

    call = {"n": 0}

    def _write(grid, **kw):  # noqa: ANN001
        # peak write (n==0) ok; frame 3 (the 4th write) blows up.
        call["n"] += 1
        if call["n"] == 4:
            raise CogIoError("WRITE", message="corrupt frame")
        return Path("/tmp/f.tif")

    with (
        patch("grace2_agent.workflows.publish_quantities.cog_io.write_cog_4326_from_grid",
              side_effect=_write),
        patch("grace2_agent.workflows.publish_quantities.cog_io.cog_bbox_4326",
              return_value=None),
        patch("grace2_agent.workflows.publish_quantities.cog_io.safe_unlink",
              return_value=None),
    ):
        m = build_quantities_manifest("x", run_id="r", upload=_fake_upload, specs=[spec])
    # Peak survives; frames abandoned (degrade-to-peak-only). NEVER raised.
    assert [layer.name for layer in m.layers] == ["Peak flood depth"]
    assert m.frame_count == 0


def test_timeseries_peak_failure_raises() -> None:
    peak = RasterField(grid=[[3.0]], src_crs="EPSG:4326", src_transform=None)
    spec = OutputQuantitySpec(
        quantity_id="q", kind="timeseries", name="Peak",
        style_preset="p",
        reader=lambda _c: TimeseriesField(
            n_steps=3, read_step=lambda i: peak, peak=peak, quantity_label="Q"
        ),
        default_on=True,
    )
    with patch(
        "grace2_agent.workflows.publish_quantities.cog_io.write_cog_4326_from_grid",
        side_effect=CogIoError("REPROJECT", message="warp failed"),
    ):
        with pytest.raises(QuantityExecError) as ei:
            build_quantities_manifest("x", run_id="r", upload=_fake_upload, specs=[spec])
    assert ei.value.stage == "REPROJECT"


# --------------------------------------------------------------------------- #
# publish_quantities full executor against a FAKE registrar
# --------------------------------------------------------------------------- #
def test_publish_quantities_hands_manifest_to_registrar() -> None:
    captured = {}

    def _fake_registrar(manifest, *, run_id, bbox=None):  # noqa: ANN001
        captured["manifest"] = manifest
        captured["run_id"] = run_id
        captured["bbox"] = bbox
        return "REGISTERED"

    w, b, u = _patch_cog_io()
    with w, b, u:
        out = publish_quantities(
            "x", run_id="r9", upload=_fake_upload,
            register_manifest_layers=_fake_registrar,
            specs=[_raster_spec()], bbox=(1, 2, 3, 4),
        )
    assert out == "REGISTERED"
    assert captured["run_id"] == "r9" and captured["bbox"] == (1, 2, 3, 4)
    assert len(captured["manifest"].layers) == 1


def test_publish_quantities_empty_registry_registers_empty_manifest() -> None:
    seen = {}

    def _fake_registrar(manifest, *, run_id, bbox=None):  # noqa: ANN001
        seen["layers"] = manifest.layers
        return "OK"

    out = publish_quantities(
        "sfincs", run_id="r", upload=_fake_upload,
        register_manifest_layers=_fake_registrar,
    )
    assert out == "OK" and seen["layers"] == []


def test_enabled_override_can_force_default_off_spec_on() -> None:
    spec = _raster_spec(enabled=False)  # default_on False
    w, b, u = _patch_cog_io()
    with w, b, u:
        m = build_quantities_manifest(
            "x", run_id="r", upload=_fake_upload, specs=[spec],
            enabled=lambda s: True,  # explicit enable overrides DEFAULT-OFF
        )
    assert len(m.layers) == 1
