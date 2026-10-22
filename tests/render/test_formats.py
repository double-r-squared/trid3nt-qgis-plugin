"""The FORMAT SET: what render accepts, and the layers one outputs list leaves.

A mesh paints ONE dataset group on the range its producer measured, a vector is
written as the GeoJSON it arrived as, a chart is a payload already built, and a
raster is a COG already in the store. Every product of one quantity is held to
one scale."""

from __future__ import annotations

import asyncio
import json

import pytest

from trid3nt_server.render.formats import (
    Chart,
    Deliverable,
    Mesh,
    Raster,
    RasterFrame,
    Vector,
    publish,
    quantity_of,
)


@pytest.fixture(autouse=True)
def _store(fake_s3):
    return fake_s3


def _publish(items, *, run_id="RID", name="reach"):
    return asyncio.run(publish(run_id=run_id, engine="telemac", name=name,
                               items=items))


def test_a_mesh_product_paints_the_group_it_declares_on_the_shared_range() -> None:
    """The still and the animation of one quantity are two layers over one file,
    and both are painted on the range that spans them."""
    # The animation declares no row of its own: a template states the style
    # beside the read it styles, and the quantity carries it to the rest.
    animation = Deliverable(
        product=Mesh(file="r2d.slf", group="DYE", epsg=32611, frames=12,
                     reference_time="2026-01-01T00:00:00+00:00", units="mg/L",
                     value_range=(0.0, 40.0)),
        caption="dye concentration")
    envelope = Deliverable(
        product=Mesh(file="r2d.slf", group="Dye concentration", epsg=32611,
                     datasets=("dye_concentration.dat",), units="mg/L",
                     value_range=(0.0, 97.3), bbox=(-114.4, 42.5, -114.2, 42.6)),
        caption="dye concentration", style={"kind": "continuous", "ramp": "reds"})
    published = _publish([animation, envelope])

    assert [layer.layer_type for layer in published.layers] == ["mesh", "mesh"]
    assert published.layers[0].name == "Dye concentration over time (reach)"
    assert published.layers[1].name == "Peak dye concentration (reach)"
    assert published.layers[0].uri == "s3://trid3nt-runs/RID/r2d.slf"
    assert published.layers[0].style["dataset_group"] == "DYE"
    assert published.layers[1].style["dataset_group"] == "Dye concentration"
    assert published.layers[1].dataset_uris == [
        "s3://trid3nt-runs/RID/dye_concentration.dat"]
    # ONE scale AND one ramp: both products of the quantity are read the same.
    for layer in published.layers:
        assert layer.style["scale"] == {"policy": "fixed", "range": [0.0, 97.3]}
        assert layer.style["ramp"] == "reds"
    # Two products of one file and one quantity are two layers, never one.
    assert [layer.layer_id for layer in published.layers] == [
        "telemac-dye_concentration-over-time-RID",
        "telemac-dye_concentration-RID"]
    assert published.layers[0].crs_authid == "EPSG:32611"
    assert published.layers[0].reference_time == "2026-01-01T00:00:00+00:00"


def test_an_instant_names_the_time_it_was_read_at() -> None:
    published = _publish([Deliverable(
        product=Mesh(file="r2d.slf", group="Bed evolution at t = 3600 s",
                     epsg=32611, datasets=("bed_evolution-t3600.dat",),
                     t=3600.0, units="m", value_range=(-0.5, 0.5)),
        caption="bed evolution", style={"kind": "continuous", "ramp": "rdbu"})])
    assert published.layers[0].name == "Bed evolution (m) at t = 3600 s (reach)"
    assert published.layers[0].layer_id == "telemac-bed_evolution-t3600-RID"


def test_a_plane_of_a_3d_result_is_named_and_keyed_by_its_plane() -> None:
    published = _publish([
        Deliverable(product=Mesh(file="res2d.slf", group="Water temperature, "
                                 "bottom plane", epsg=32611,
                                 datasets=("water_temperature_bottom_plane.dat",),
                                 plane="bottom plane", units="degC",
                                 value_range=(15.0, 25.0)),
                    caption="water temperature", style={"kind": "continuous"})])
    assert published.layers[0].layer_id == (
        "telemac-water_temperature_bottom_plane-RID")
    assert "bottom plane" in published.layers[0].name


def test_a_vector_is_written_as_the_geojson_it_arrived_as(_store) -> None:
    features = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "MultiPoint",
                                         "coordinates": [[-114.3, 42.5],
                                                         [-114.2, 42.6]]},
         "properties": {}}]}
    published = _publish([Deliverable(product=Vector(features=features),
                                      caption="oil slick track")])
    layer = published.layers[0]
    assert layer.layer_type == "vector"
    assert layer.name == "Oil slick track (reach)"
    assert layer.uri == "s3://trid3nt-runs/RID/oil_slick_track.geojson"
    assert json.loads(_store.store["RID/oil_slick_track.geojson"]) == features
    assert layer.bbox == (-114.3, 42.5, -114.2, 42.6)
    assert layer.style["kind"] == "reference"


def test_a_single_point_vector_gets_an_honest_box() -> None:
    published = _publish([Deliverable(
        product=Vector(features={"type": "FeatureCollection", "features": [
            {"type": "Feature",
             "geometry": {"type": "Point", "coordinates": [-114.3, 42.5]},
             "properties": {}}]}),
        caption="outlet hydrograph",
        style={"kind": "reference", "geometry": "point"})])
    west, south, east, north = published.layers[0].bbox
    assert east - west == pytest.approx(0.004) and north - south == pytest.approx(0.004)


def test_a_raster_is_the_cog_already_in_the_store() -> None:
    published = _publish([Deliverable(
        product=Raster(uri="s3://cache/dem.tif", bbox=(-1.0, 2.0, -0.5, 2.5),
                       units="m", value_range=(0.0, 100.0)),
        caption="ground elevation", style={"kind": "continuous"})])
    assert published.layers[0].layer_type == "raster"
    assert published.layers[0].uri == "s3://cache/dem.tif"
    assert published.layers[0].style["scale"]["range"] == [0.0, 100.0]


def test_a_chart_is_a_payload_and_never_a_layer() -> None:
    payload = {"chart_id": "C", "title": "Dye at the bridge"}
    published = _publish([Deliverable(product=Chart(payload=payload),
                                      caption="dye concentration")])
    assert published.layers == ()
    assert published.charts == {"dye_concentration": payload}


def test_the_producer_s_range_semantics_do_not_reach_the_preset_row() -> None:
    """``center``, ``floor`` and a percentile cap are how a producer MEASURED
    the range; what the layer carries is the range itself."""
    published = _publish([Deliverable(
        product=Mesh(file="g.slf", group="Bed evolution", epsg=32611,
                     datasets=("bed_evolution.dat",), units="m",
                     value_range=(-0.4, 0.4)),
        caption="bed evolution",
        style={"kind": "continuous", "ramp": "rdbu", "center": 0.0,
               "floor": 0, "range": "p99.5"})])
    row = published.layers[0].style
    assert set(row) == {"kind", "ramp", "dataset_group", "scale"}
    assert row["kind"] == "mesh" and row["scale"]["range"] == [-0.4, 0.4]


def test_quantity_of_spells_the_caption() -> None:
    assert quantity_of("  Suspended Sediment Concentration ") == (
        "suspended_sediment_concentration")


def test_two_groups_of_one_mesh_file_are_two_rows_on_the_canvas() -> None:
    """The emitter dedups by WHAT a row paints. One mesh file carries many
    dataset groups, so its uri alone is not what identifies a row."""
    import asyncio

    from trid3nt_contracts.execution import LayerURI
    from trid3nt_server.render.pipeline_emitter import PipelineEmitter

    from trid3nt_contracts import new_ulid

    emitter = PipelineEmitter(session_id=new_ulid(), sink=_swallow)
    for group in ("DYE", "Peak dye concentration"):
        asyncio.run(emitter.add_loaded_layer(LayerURI(
            layer_id=f"telemac-{group}-RID", name=group, layer_type="mesh",
            uri="s3://runs/RID/r2d.slf", quantity="dye_concentration",
            style={"kind": "mesh", "dataset_group": group})))
    rows = emitter.loaded_layers
    assert [row.dataset_group for row in rows] == ["DYE", "Peak dye concentration"]
    # A re-publish of the SAME group still replaces in place.
    asyncio.run(emitter.add_loaded_layer(LayerURI(
        layer_id="telemac-DYE-RID", name="DYE restyled", layer_type="mesh",
        uri="s3://runs/RID/r2d.slf", quantity="dye_concentration",
        style={"kind": "mesh", "dataset_group": "DYE"})))
    assert len(emitter.loaded_layers) == 2
    assert emitter.loaded_layers[0].name == "DYE restyled"


async def _swallow(_text: str) -> None:
    return None


def _frames(*instants: float) -> tuple[RasterFrame, ...]:
    edges = [*instants[1:], instants[-1] + 600.0]
    return tuple(RasterFrame(uri=f"s3://runs/RID/depth-t{int(t)}.tif", t=t,
                             valid_from=f"2010-02-27T00:{int(t) // 60:02d}:00Z",
                             valid_to=f"2010-02-27T00:{int(e) // 60:02d}:00Z")
                 for t, e in zip(instants, edges))


def test_a_timed_raster_group_is_one_layer_per_frame_on_one_range() -> None:
    published = _publish([Deliverable(
        product=Raster(frames=_frames(0.0, 600.0, 1200.0), bbox=(-1.0, 2.0, -0.5, 2.5), units="m",
                       value_range=(0.0, 7.0)),
        caption="water depth", style={"kind": "continuous"})])
    layers = published.layers
    assert len({layer.layer_id for layer in layers}) == 3
    assert [layer.layer_id for layer in layers][1] == "telemac-water_depth-t600-RID"
    assert [layer.valid_from for layer in layers] == ["2010-02-27T00:00:00Z", "2010-02-27T00:10:00Z",
                                                      "2010-02-27T00:20:00Z"]
    assert [layer.valid_to for layer in layers[:2]] == [layers[1].valid_from, layers[2].valid_from]
    assert {tuple(layer.style["scale"]["range"]) for layer in layers} == {(0.0, 7.0)}
    assert all(layer.layer_type == "raster" and layer.quantity == "water_depth" for layer in layers)


def test_an_untimed_raster_carries_no_window() -> None:
    layer = _publish([Deliverable(product=Raster(uri="s3://cache/dem.tif"), caption="ground elevation",
                                  style={"kind": "continuous"})]).layers[0]
    assert layer.valid_from is None and layer.valid_to is None and layer.layer_id == "telemac-ground_elevation-RID"


def test_a_raster_is_one_uri_or_its_frames() -> None:
    with pytest.raises(ValueError):
        Raster()
    with pytest.raises(ValueError):
        Raster(uri="s3://cache/a.tif", frames=_frames(0.0))
