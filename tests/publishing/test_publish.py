"""The one publisher: a field becomes a layer, a series a chart, a field over
time an animation.

Offline: the store and the render chokepoint are stood in for; what is proved
is what each deliverable becomes and in which order they are handed on."""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

from trid3nt_server.workflows.publishing import (
    Deliverable,
    Field,
    Frames,
    Line,
    Profile,
    Series,
    Track,
    quantity_of,
)
from trid3nt_server.workflows.publishing import publish as publish_mod
from trid3nt_server.workflows.publishing.publish import publish


def test_the_quantity_token_is_the_caption_s_own_words():
    assert quantity_of("dye concentration") == "dye_concentration"
    assert quantity_of("  Suspended   sediment  ") == "suspended_sediment"


def _field(**over) -> Field:
    lon = np.array([-124.10, -124.099, -124.10, -124.099, -124.0995])
    lat = np.array([40.50, 40.50, 40.501, 40.501, 40.5005])
    ikle = np.array([[0, 1, 4], [1, 3, 4], [2, 3, 4], [0, 2, 4]])
    return Field(**{"name": "DYE", "units": "mg/L", "lon": lon, "lat": lat,
                    "ikle": ikle, "values": np.array([10.0, 80.0, 5.0, 50.0, 60.0]),
                    "floor": 4.0, "measures": {"max": 80.0}, **over})


def _series() -> Series:
    return Series(name="DYE", units="mg/L", times=np.array([0.0, 60.0, 120.0]),
                  values=np.array([0.0, 80.0, 50.0]), at="the domain maximum",
                  measures={"max": 80.0, "t_max": 60.0})


def test_a_field_becomes_one_styled_layer_named_by_its_caption(monkeypatch):
    """The COG is written, uploaded, published through the chokepoint; the
    legend spans the field's own range from its floor's zero; the name, the
    quantity and the units come off the caption and the read."""
    from trid3nt_server.emission import publish as emission_publish
    from trid3nt_server.workflows.publishing import cog

    seen = {}

    def _upload(local, run_id, bucket, *, dest_filename, **_kw):
        import rasterio

        with rasterio.open(local) as src:
            seen["shape"] = (src.height, src.width)
            seen["crs"] = str(src.crs)
        return f"s3://runs/{run_id}/{dest_filename}"

    monkeypatch.setattr(cog, "upload_cog", _upload)
    monkeypatch.setattr(emission_publish, "publish_layer",
                        lambda *, layer_uri, layer_id, style=None, **_kw:
                        seen.setdefault("published", (layer_uri, layer_id, style))
                        and layer_uri + "#published")
    layer = publish_mod._layer(_field(), run_id="RID", engine="telemac",
                               name="reach", caption="dye concentration",
                               style={"kind": "continuous", "ramp": "reds",
                                      "units": "mg/L", "label": "Dye concentration"})
    assert seen["crs"] == "EPSG:4326" and min(seen["shape"]) >= 128
    assert seen["published"] == ("s3://runs/RID/dye_concentration.tif",
                                 "telemac-dye_concentration-RID",
                                 {"kind": "continuous", "ramp": "reds",
                                  "units": "mg/L", "label": "Dye concentration"})
    assert layer.uri == "s3://runs/RID/dye_concentration.tif#published"
    assert layer.name == "Peak dye concentration (reach)"
    assert layer.quantity == "dye_concentration" and layer.units == "mg/L"
    assert layer.role == "primary" and layer.layer_type == "raster"
    assert (layer.legend.vmin, layer.legend.vmax) == (0.0, 80.0)
    assert layer.legend.label == "Dye concentration (mg/L)"
    # the bbox is the nodes' own, padded so a ribbon at the bank is not clipped
    assert layer.bbox[0] == pytest.approx(-124.10 - 0.0009)
    assert layer.bbox[3] == pytest.approx(40.501 + 0.0009)


def test_a_field_at_an_instant_is_named_for_it_and_ranged_on_itself(monkeypatch):
    from trid3nt_server.emission import publish as emission_publish
    from trid3nt_server.workflows.publishing import cog

    monkeypatch.setattr(cog, "upload_cog",
                        lambda local, run_id, bucket, *, dest_filename, **_kw:
                        f"s3://runs/{run_id}/{dest_filename}")
    monkeypatch.setattr(emission_publish, "publish_layer",
                        lambda *, layer_uri, **_kw: layer_uri)
    layer = publish_mod._layer(
        _field(name="WATER DEPTH", units="m", floor=None, t=120.0,
               values=np.array([1.5, 2.0, 0.5, 1.0, 1.25])),
        run_id="RID", engine="telemac", name="reach", caption="water depth",
        style=None)
    assert layer.layer_id == "telemac-water_depth-t120-RID"
    assert layer.name == "Water depth (m) at t = 120 s (reach)"
    assert (layer.legend.vmin, layer.legend.vmax) == (0.5, 2.0)


def test_a_publish_failure_never_retracts_the_layer(monkeypatch):
    from trid3nt_server.emission import publish as emission_publish
    from trid3nt_server.workflows.publishing import cog

    monkeypatch.setattr(cog, "upload_cog",
                        lambda local, run_id, bucket, *, dest_filename, **_kw:
                        f"s3://runs/{run_id}/{dest_filename}")

    def _refuse(**_kw):
        raise emission_publish.PublishLayerError("LAYER_URI_NOT_FOUND", "no")

    monkeypatch.setattr(emission_publish, "publish_layer", _refuse)
    layer = publish_mod._layer(_field(), run_id="RID", engine="telemac",
                               name="reach", caption="dye concentration", style=None)
    assert layer.uri == "s3://runs/RID/dye_concentration.tif"


def test_a_series_becomes_a_chart_with_one_point_per_instant():
    payload = publish_mod._chart(_series(), caption="dye concentration",
                                 where="the Wabash")
    values = payload["vega_lite_spec"]["data"]["values"]
    assert [v["t_s"] for v in values] == [0.0, 60.0, 120.0]
    assert [v["value"] for v in values] == [0.0, 80.0, 50.0]
    assert payload["title"] == "Dye concentration, the domain maximum - the Wabash"
    assert "peaks at 80 mg/L at t = 60 s" in payload["caption"]
    assert payload["vega_lite_spec"]["encoding"]["y"]["title"] == "Dye concentration (mg/L)"
    assert payload["envelope_type"] == "chart-emission"


def test_layers_lead_and_an_animation_adopts_its_sibling_s_scale(monkeypatch):
    """The first layer is the primary; the animation of the same quantity is
    handed that layer so the frames and the still share one range; the charts
    are persisted under the run."""
    from trid3nt_contracts.execution import LayerURI

    from trid3nt_server.workflows.runtime import run_products

    seen: dict = {}
    primary = LayerURI(layer_id="L", name="Peak dye concentration (reach)",
                       layer_type="raster", uri="s3://runs/RID/dye_concentration.tif",
                       quantity="dye_concentration")
    monkeypatch.setattr(publish_mod, "_layer", lambda *a, **k: primary)

    async def _seam(_emitter, **kwargs):
        seen["seam"] = kwargs
        return 1

    async def _persist(run_id, *, charts, metrics):
        seen["persisted"] = (run_id, dict(charts), metrics)
        return []

    monkeypatch.setattr(publish_mod, "publish_results_mesh_via_seam", _seam)
    monkeypatch.setattr(run_products, "persist_run_products", _persist)
    frames = Frames(name="DYE", units="mg/L", file="r2d.slf", group="DYE",
                    epsg=32610, reference_time="2026-01-01T00:00:00+00:00", frames=3)
    published = asyncio.run(publish(
        run_id="RID", engine="telemac", name="reach", where="the Wabash",
        reference_time="2026-01-01T00:00:00+00:00",
        items=[Deliverable(read=frames, mode="animate", caption="dye concentration"),
               Deliverable(read=_field(), mode="layer", caption="dye concentration"),
               Deliverable(read=_series(), mode="chart", caption="dye concentration")]))
    assert published.primary is primary and published.animations == 1
    assert seen["seam"]["peak_layer"] is primary
    assert (seen["seam"]["peak_quantity"], seen["seam"]["mesh_group"],
            seen["seam"]["mesh_basename"], seen["seam"]["mesh_epsg"],
            seen["seam"]["reach_name"]) == (
        "dye_concentration", "DYE", "r2d.slf", 32610, "reach")
    assert list(published.charts) == ["dye_concentration"]
    assert seen["persisted"][0] == "RID" and list(seen["persisted"][1]) == [
        "dye_concentration"]


def test_an_animation_with_no_layer_of_its_quantity_still_plays(monkeypatch):
    seen: dict = {}

    async def _seam(_emitter, **kwargs):
        seen.update(kwargs)
        return 1

    monkeypatch.setattr(publish_mod, "publish_results_mesh_via_seam", _seam)
    frames = Frames(name="WATER DEPTH", units="m", file="r2d.slf",
                    group="WATER DEPTH", epsg=32610, reference_time=None, frames=3)
    published = asyncio.run(publish(
        run_id="RID", engine="telemac", name="reach", where="x", reference_time=None,
        items=[Deliverable(read=frames, mode="animate", caption="water depth")]))
    assert published.primary is None and seen["peak_layer"] is None


def _profile(**over) -> Profile:
    return Profile(**{"name": "DISSOLVED O2", "units": "mg/L",
                      "distance_m": np.array([50.0, 150.0, 250.0]),
                      "values": np.array([9.0, 6.0, 7.5]),
                      "along": "downstream distance",
                      "measures": {"min": 6.0, "x_min_m": 150.0}, **over})


def test_a_profile_becomes_a_chart_down_the_line_with_its_lines_beside_it():
    """The x axis is distance, the read is one named series, every reference line
    another, and the caption names the lowest station."""
    payload = publish_mod._chart(
        _profile(lines=(Line(label="5 mg/L standard", x=[50.0, 250.0],
                             values=[5.0, 5.0]),)),
        caption="dissolved oxygen", where="the Eel")
    spec = payload["vega_lite_spec"]
    assert spec["encoding"]["x"]["title"] == "Downstream distance (m)"
    assert spec["encoding"]["color"]["field"] == "series"
    rows = spec["data"]["values"]
    assert [r["series"] for r in rows] == ["Dissolved oxygen"] * 3 + ["5 mg/L standard"] * 2
    assert rows[0] == {"x_m": 50.0, "value": 9.0, "series": "Dissolved oxygen"}
    assert payload["title"] == "Dissolved oxygen, downstream distance - the Eel"
    assert "lowest 6 mg/L at 150 m" in payload["caption"]
    assert "5 mg/L standard is drawn beside it" in payload["caption"]


def test_a_series_chart_keeps_its_time_axis_and_its_points():
    payload = publish_mod._chart(_series(), caption="dye concentration", where="X")
    spec = payload["vega_lite_spec"]
    assert spec["encoding"]["x"]["field"] == "t_s" and spec["mark"]["point"] is True
    assert "color" not in spec["encoding"]


def test_a_track_becomes_a_vector_layer_in_the_run_s_own_store(monkeypatch):
    from trid3nt_server.workflows.solver import solver as solver_mod

    put = {}

    class _S3:
        def put_object(self, **kw):
            put.update(kw)

    monkeypatch.setattr(solver_mod, "_get_runs_bucket", lambda: "runs")
    monkeypatch.setattr(solver_mod, "_get_s3_client", lambda: _S3())
    track = Track(features={"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "MultiPoint",
                                         "coordinates": [[-124.1, 40.5], [-124.0, 40.6]]},
         "properties": {"t_s": 0.0, "n": 2}}]}, measures={"released": 2})
    layer = publish_mod._vector_layer(track, run_id="RID", engine="telemac",
                                      name="reach", caption="oil slick track")
    assert put["Key"] == "RID/oil_slick_track.geojson"
    assert layer.layer_type == "vector" and layer.uri == "s3://runs/RID/oil_slick_track.geojson"
    assert layer.name == "Oil slick track (reach)" and layer.quantity == "oil_slick_track"
    assert layer.bbox == (-124.1, 40.5, -124.0, 40.6)


def test_every_layer_past_the_first_is_surfaced_beside_it(monkeypatch):
    """The first layer is the step's own return; the rest reach the canvas
    through the extra-layer seam, as results of the solve."""
    from trid3nt_contracts.execution import LayerURI

    from trid3nt_server.emission import layer_uri_emit
    from trid3nt_server.emission import pipeline_emitter

    surfaced = []
    layers = iter([LayerURI(layer_id="A", name="a", layer_type="raster", uri="s3://a"),
                   LayerURI(layer_id="B", name="b", layer_type="vector", uri="s3://b")])
    monkeypatch.setattr(publish_mod, "_layer", lambda *a, **k: next(layers))
    monkeypatch.setattr(publish_mod, "_vector_layer", lambda *a, **k: next(layers))
    monkeypatch.setattr(pipeline_emitter, "current_emitter", lambda: object())

    async def _surface(emitter, layer, *, role):
        surfaced.append((layer.layer_id, role))
        return True

    monkeypatch.setattr(layer_uri_emit, "publish_input_layer", _surface)
    published = asyncio.run(publish(
        run_id="RID", engine="telemac", name="reach", where="X", reference_time=None,
        items=[Deliverable(read=_field(), mode="layer", caption="dye concentration"),
               Deliverable(read=Track(features={"type": "FeatureCollection",
                                                "features": []}),
                           mode="layer", caption="oil slick track")]))
    assert published.primary.layer_id == "A"
    assert surfaced == [("B", "primary")]


def test_a_row_with_a_centre_ranges_the_legend_symmetrically_about_it(monkeypatch):
    """A diverging ramp's middle colour has to mean the centre value, so a signed
    field is ranged by its larger limb on both sides of the declared centre."""
    from trid3nt_server.emission import publish as emission_publish
    from trid3nt_server.workflows.publishing import cog

    monkeypatch.setattr(cog, "upload_cog", lambda *a, **k: "s3://runs/RID/x.tif")
    monkeypatch.setattr(emission_publish, "publish_layer", lambda **k: "https://t")
    signed = _field(values=np.array([-0.005, 0.0, 0.0016, 0.0, -0.001]), floor=None,
                    measures={"max": 0.0016, "min": -0.005})
    layer = publish_mod._layer(signed, run_id="RID", engine="telemac", name="reach",
                               caption="bed evolution",
                               style={"kind": "continuous", "ramp": "rdbu",
                                      "center": 0.0})
    assert (layer.legend.vmin, layer.legend.vmax) == (-0.005, 0.005)


def test_a_row_with_a_floor_and_a_percentile_cap_ranges_the_legend_by_them(
        monkeypatch):
    """A declared ``floor`` pins the legend's bottom where a standard has to stay
    on the ramp; a declared ``range`` of ``p<q>`` caps its top at that percentile
    so one pit cannot paint the rest of the field one colour."""
    from trid3nt_server.emission import publish as emission_publish
    from trid3nt_server.workflows.publishing import cog

    monkeypatch.setattr(cog, "upload_cog", lambda *a, **k: "s3://runs/RID/x.tif")
    monkeypatch.setattr(emission_publish, "publish_layer", lambda **k: "https://t")
    oxygen = _field(values=np.array([6.0, 7.0, 8.0, 9.0, 8.5]), floor=None,
                    measures={"max": 9.0, "min": 6.0})
    floored = publish_mod._layer(oxygen, run_id="RID", engine="telemac", name="reach",
                                 caption="dissolved oxygen",
                                 style={"kind": "continuous", "floor": 0})
    assert (floored.legend.vmin, floored.legend.vmax) == (0.0, 9.0)
    spiked = _field(values=np.array([0.5, 0.6, 0.7, 0.8, 40.0]), floor=None,
                    measures={"max": 40.0, "min": 0.5})
    capped = publish_mod._layer(spiked, run_id="RID", engine="harbour", name="basin",
                                caption="agitation coefficient",
                                style={"kind": "continuous", "range": "p50"})
    assert capped.legend.vmin == 0.5 and capped.legend.vmax == pytest.approx(0.7)


def test_a_series_at_a_station_becomes_the_point_layer_that_carries_it(monkeypatch):
    """The station sits where the series was read; the rows are ISO instants
    counted from the run's own reference time, so the pairing that reads a
    gauge's ``time_series_csv`` reads this one the same way."""
    import json

    from trid3nt_server.workflows.solver import solver as solver_mod

    put = {}

    class _S3:
        def put_object(self, **kw):
            put.update(kw)

    monkeypatch.setattr(solver_mod, "_get_runs_bucket", lambda: "runs")
    monkeypatch.setattr(solver_mod, "_get_s3_client", lambda: _S3())
    read = Series(name="FLUX BOUNDARY", units="m3/s",
                  times=np.array([0.0, 1800.0]), values=np.array([0.0, 4.5]),
                  at="at the outlet", lon=-83.4, lat=35.05,
                  measures={"max": 4.5, "t_max": 1800.0})
    layer = publish_mod._station_layer(
        read, run_id="RID", engine="telemac", name="watershed",
        caption="outlet hydrograph", reference_time="2026-01-01T00:00:00+00:00")
    assert put["Key"] == "RID/outlet_hydrograph.geojson"
    feature = json.loads(put["Body"])["features"][0]
    assert feature["geometry"] == {"type": "Point", "coordinates": [-83.4, 35.05]}
    assert feature["properties"]["time_series_csv"] == (
        "2026-01-01T00:00:00+00:00,0.000000\n2026-01-01T00:30:00+00:00,4.500000\n")
    assert feature["properties"]["quantity"] == "outlet_hydrograph"
    assert feature["properties"]["n_timesteps"] == 2
    assert layer.layer_type == "vector" and layer.quantity == "outlet_hydrograph"
    assert layer.name == "Outlet hydrograph (watershed)" and layer.units == "m3/s"
    assert layer.bbox == pytest.approx((-83.402, 35.048, -83.398, 35.052))


def test_a_series_read_nowhere_is_no_station():
    with pytest.raises(ValueError, match="no station"):
        publish_mod._station_layer(
            _series(), run_id="RID", engine="telemac", name="reach",
            caption="dye concentration", reference_time=None)


def test_two_planes_of_one_quantity_share_a_scale_and_are_named_apart(monkeypatch):
    """One quantity, one scale: the surface and the bottom of a 3D field are
    ranged over both, and each layer carries its plane in its id, its file and
    its name."""
    from trid3nt_server.emission import publish as emission_publish
    from trid3nt_server.workflows.publishing import cog
    from trid3nt_server.workflows.runtime import run_products

    uploaded = []
    monkeypatch.setattr(cog, "upload_cog",
                        lambda local, run_id, bucket, *, dest_filename, **_kw:
                        uploaded.append(dest_filename) or f"s3://runs/{run_id}/{dest_filename}")
    monkeypatch.setattr(emission_publish, "publish_layer",
                        lambda *, layer_uri, **_kw: layer_uri)

    async def _surface(_emitter, layer, **_kw):
        return True

    async def _persist(run_id, *, charts, metrics):
        return []

    monkeypatch.setattr(publish_mod, "publish_input_layer", _surface, raising=False)
    monkeypatch.setattr("trid3nt_server.emission.layer_uri_emit.publish_input_layer",
                        _surface)
    monkeypatch.setattr(run_products, "persist_run_products", _persist)
    surface = _field(name="TEMPERATURE", units="degC", floor=None, t=3600.0,
                     plane="surface plane",
                     values=np.array([23.0, 24.0, 21.0, 22.0, 23.5]))
    bottom = _field(name="TEMPERATURE", units="degC", floor=None, t=3600.0,
                    plane="bottom plane",
                    values=np.array([17.0, 16.0, 19.0, 18.0, 17.5]))
    published = asyncio.run(publish(
        run_id="RID", engine="telemac", name="basin", where="the lake",
        reference_time=None,
        items=[Deliverable(read=surface, mode="layer", caption="water temperature",
                           style={"kind": "continuous", "ramp": "viridis"}),
               Deliverable(read=bottom, mode="layer", caption="water temperature",
                           style={"kind": "continuous", "ramp": "viridis"})]))
    top, bed = published.layers
    assert (top.layer_id, bed.layer_id) == (
        "telemac-water_temperature_surface_plane-t3600-RID",
        "telemac-water_temperature_bottom_plane-t3600-RID")
    assert top.name == "Water temperature (degC) at t = 3600 s, surface plane (basin)"
    assert bed.name == "Water temperature (degC) at t = 3600 s, bottom plane (basin)"
    assert uploaded == ["water_temperature_surface_plane.tif",
                        "water_temperature_bottom_plane.tif"]
    assert top.quantity == bed.quantity == "water_temperature"
    assert (top.legend.vmin, top.legend.vmax) == (16.0, 24.0)
    assert (bed.legend.vmin, bed.legend.vmax) == (16.0, 24.0)


def test_a_profile_chart_titles_its_axis_by_what_the_profile_runs_along():
    payload = publish_mod._chart(
        Profile(name="TEMPERATURE", units="degC",
                distance_m=np.array([0.0, 10.0, 20.0]),
                values=np.array([24.0, 20.0, 16.0]), along="depth below the surface",
                measures={}),
        caption="water temperature", where="the lake")
    spec = payload["vega_lite_spec"]
    assert spec["encoding"]["x"]["title"] == "Depth below the surface (m)"
    assert payload["title"] == "Water temperature, depth below the surface - the lake"
    assert "lowest 16 degC at 20 m" in payload["caption"]
