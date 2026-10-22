"""A raster read delivered: the framework writes the COGs under the run prefix and the Raster carries their windows."""

from __future__ import annotations

import io
from types import SimpleNamespace

import numpy as np
import pytest
import rasterio

from trid3nt_server.store import objects as storage
from trid3nt_server.workflows.runtime import primitives as P

_BOUNDS = (-77.0, -45.0, -72.0, -41.0)


class _Store:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, ContentType: str | None = None):
        self.objects[Key] = Body

    def get_object(self, *, Bucket: str, Key: str):
        return {"Body": io.BytesIO(self.objects[Key])}


@pytest.fixture()
def held(monkeypatch):
    store = _Store()
    monkeypatch.setattr(storage, "_CLIENT", store)
    return store


def _read(frames: int, **over) -> P.Rasters:
    values = np.stack([np.full((4, 5), float(n + 1)) for n in range(frames)])
    values[:, 0, 0] = np.nan
    return P.Rasters(name="water depth", units="m", values=values, bounds=_BOUNDS, **over)


def _deliver(read, reference="2010-02-27T06:34:00Z"):
    solved = SimpleNamespace(run_id="RID", run={"reference_time": reference})
    return P.deliver(P.field("DEPTH", "every"), read, solved, caption="water depth", name="x", where="x")


def test_three_frames_become_three_cogs_in_consecutive_windows(held) -> None:
    deliverable = _deliver(_read(3, times=(0.0, 600.0, 1200.0), until=3600.0))
    frames = deliverable.product.frames
    assert [f.t for f in frames] == [0.0, 600.0, 1200.0]
    assert [f.valid_from for f in frames] == ["2010-02-27T06:34:00Z", "2010-02-27T06:44:00Z", "2010-02-27T06:54:00Z"]
    assert [f.valid_to for f in frames] == ["2010-02-27T06:44:00Z", "2010-02-27T06:54:00Z", "2010-02-27T07:34:00Z"]
    assert deliverable.product.value_range[1] >= 3.0
    assert sorted(held.objects) == ["RID/water_depth-t0.tif", "RID/water_depth-t1200.tif", "RID/water_depth-t600.tif"]
    with rasterio.MemoryFile(held.objects["RID/water_depth-t600.tif"]) as memory, memory.open() as src:
        assert src.crs.to_string() == "EPSG:4326" and tuple(src.bounds) == _BOUNDS
        data = src.read(1)
    assert np.isnan(data[0, 0]) and float(data[1, 1]) == 2.0


def test_an_envelope_is_one_untimed_cog(held) -> None:
    product = _deliver(_read(1), reference=None).product
    assert product.uri == "s3://" + storage.runs_bucket() + "/RID/water_depth.tif" and not product.frames
    assert list(held.objects) == ["RID/water_depth.tif"]


def test_a_timed_read_on_a_run_with_no_reference_time_refuses_by_name(held) -> None:
    with pytest.raises(P.RasterUnanchored) as caught:
        _deliver(_read(2, times=(0.0, 600.0)), reference=None)
    assert "reference time" in str(caught.value) and not held.objects
    assert caught.value.error_code == "RASTER_UNANCHORED" and P.OutputEmpty.error_code == "TELEMAC_OUTPUT_EMPTY"


def test_a_last_frame_at_the_run_s_end_holds_its_own_instant(held) -> None:
    frames = _deliver(_read(2, times=(0.0, 600.0), until=600.0)).product.frames
    assert frames[-1].valid_from == frames[-1].valid_to
