"""job-0305: the s3 in-memory raster read must keep its MemoryFile ALIVE.

Live 2026-06-16 the NLCD validation gate read a categorical landcover raster as
real classes (11-95) PLUS a continuous garbage spread (96-254) and failed the
flood NON-deterministically. Root cause: ``MemoryFile(read_object_bytes_s3(uri))
.open()`` orphaned the MemoryFile, so GC could free its /vsimem/ buffer mid-read.
These tests build a known categorical raster, force GC around the read, and
assert the class set is EXACTLY the categorical legend — never garbage.
"""
from __future__ import annotations

import gc
import inspect

import numpy as np
import rasterio
from rasterio.io import MemoryFile
from rasterio.transform import from_bounds as transform_from_bounds


KNOWN = {11, 21, 22, 23, 24, 31, 41, 42, 43, 52, 71, 81, 82, 90, 95}


def _categorical_geotiff_bytes(w=256, h=256) -> bytes:
    """A uint8 single-band GeoTIFF whose ONLY values are the KNOWN classes."""
    vals = sorted(KNOWN)
    arr = np.array(vals * ((w * h) // len(vals) + 1), dtype="uint8")[: w * h].reshape(h, w)
    transform = transform_from_bounds(-82.0, 26.5, -81.7, 26.7, w, h)
    with MemoryFile() as mf:
        with mf.open(driver="GTiff", height=h, width=w, count=1, dtype="uint8",
                     crs="EPSG:4326", transform=transform, nodata=255) as dst:
            dst.write(arr, 1)
        return mf.read()


def test_extract_unique_nlcd_classes_s3_no_garbage_under_gc(monkeypatch):
    from trid3nt_server.workflows import sfincs_builder
    data = _categorical_geotiff_bytes()
    monkeypatch.setattr(sfincs_builder, "read_object_bytes_s3", lambda uri: data, raising=False)
    # also patch the cache module symbol the function imports locally
    import trid3nt_server.tools.cache as cache_mod
    monkeypatch.setattr(cache_mod, "read_object_bytes_s3", lambda uri: data)

    for _ in range(8):
        gc.collect()
        classes = sfincs_builder._extract_unique_nlcd_classes("s3://bkt/nlcd.tif")
        gc.collect()
        # EXACTLY the categorical legend — no 96-254 garbage, ever.
        assert classes == KNOWN, f"got spurious classes: {sorted(classes - KNOWN)}"


def test_open_source_is_context_manager():
    from trid3nt_server.tools.extract_landcover_class import _open_source
    # @contextmanager wraps the generator function -> not a plain generator fn,
    # but calling it returns a context manager with __enter__/__exit__.
    cm = _open_source.__wrapped__ if hasattr(_open_source, "__wrapped__") else _open_source
    assert inspect.isgeneratorfunction(cm), "_open_source must yield (context manager)"


def test_extract_landcover_open_source_s3_reads_clean_under_gc(monkeypatch):
    import trid3nt_server.tools.extract_landcover_class as elc
    data = _categorical_geotiff_bytes()
    monkeypatch.setattr(elc, "read_object_bytes_s3", lambda uri: data, raising=False)
    import trid3nt_server.tools.cache as cache_mod
    monkeypatch.setattr(cache_mod, "read_object_bytes_s3", lambda uri: data)
    for _ in range(8):
        gc.collect()
        with elc._open_source("s3://bkt/nlcd.tif") as src:
            arr = src.read(1)
        gc.collect()
        u = set(int(v) for v in np.unique(arr).tolist()) - {255}
        assert u == KNOWN, f"got spurious classes: {sorted(u - KNOWN)}"
