"""Georef regression for postprocess_artemis (latent #7).

The worker meshes an ARTEMIS agitation field in a LOCAL UTM frame: it subtracts
the AOI SW corner (min easting/northing) from every node so the SELAFIN float32
coordinates keep sub-metre precision. The postprocess MUST add that origin offset
back before the UTM->4326 inverse, or the published COG georeferences to the
UTM-zone origin (near lon -91, lat 0 -- the Gulf of Guinea) instead of the real
harbour. This is the test that would have caught latent #7: the published COG
bounds must fall inside the request bbox.

No docker / no TELEMAC / no S3: a synthetic local-frame SELAFIN + a monkeypatched
COG write/upload seam. The georef math (offset reconstruction + reproject) runs
for real; only the raster bytes + upload are stubbed.
"""

from __future__ import annotations

import struct

import numpy as np
import pytest

from trid3nt_server.agent.workflows.telemac import postprocess_telemac as P


def _rec(payload: bytes) -> bytes:
    n = struct.pack(">i", len(payload))
    return n + payload + n


def _write_local_frame_selafin(path, x_local, y_local, hs, *, ikle):
    """A single-frame WAVE HEIGHT SELAFIN in LOCAL (origin-shifted) metres."""
    varnames = ["WAVE HEIGHT     M"]
    npoin = len(x_local)
    nelem = len(ikle)
    ndp = len(ikle[0])
    title = b"ARTEMIS GEOREF TEST".ljust(72) + b"SERAFIN "
    with open(path, "wb") as fh:
        fh.write(_rec(title))
        fh.write(_rec(struct.pack(">2i", len(varnames), 0)))
        for v in varnames:
            fh.write(_rec(v.encode("latin-1").ljust(32)))
        fh.write(_rec(struct.pack(">10i", *([0] * 10))))
        fh.write(_rec(struct.pack(">4i", nelem, npoin, ndp, 1)))
        fh.write(_rec(np.asarray(ikle, dtype=">i4").tobytes()))
        fh.write(_rec(np.arange(1, npoin + 1, dtype=">i4").tobytes()))
        fh.write(_rec(np.asarray(x_local, dtype=">f4").tobytes()))
        fh.write(_rec(np.asarray(y_local, dtype=">f4").tobytes()))
        fh.write(_rec(struct.pack(">f", 0.0)))
        fh.write(_rec(np.asarray(hs, dtype=">f4").tobytes()))


def _marquette_local_mesh():
    """A grid of LOCAL-frame nodes spanning the interior of the Marquette AOI.

    Returns (x_local, y_local, hs, ikle, bbox, utm_epsg). x/y are the metres the
    worker actually writes (origin at the AOI SW corner), NOT true UTM eastings.
    """
    from pyproj import Transformer

    bbox = (-87.34, 46.57, -87.27, 46.61)   # Lake Superior off Marquette, MI
    utm_epsg = 32616                         # UTM 16N
    fwd = Transformer.from_crs(4326, utm_epsg, always_xy=True)
    x0, y0 = fwd.transform(bbox[0], bbox[1])
    x1, y1 = fwd.transform(bbox[2], bbox[3])
    Lx, Ly = abs(x1 - x0), abs(y1 - y0)
    # nodes span the interior 10%-90% (the wet domain is a subset of the AOI);
    # LOCAL frame => x in [0.1 Lx, 0.9 Lx], never the true UTM ~4.3e5 easting.
    n = 6
    xs = np.linspace(0.10 * Lx, 0.90 * Lx, n)
    ys = np.linspace(0.10 * Ly, 0.90 * Ly, n)
    X, Y = np.meshgrid(xs, ys)
    x_local = X.ravel()
    y_local = Y.ravel()
    hs = np.full(x_local.size, 1.0)          # Kd ~ 1 everywhere (wet)
    # a trivial triangulation just so the reader has real elements
    ikle = [[1, 2, 3]]
    return x_local, y_local, hs, ikle, bbox, utm_epsg


def _stub_cog_seam(monkeypatch, tmp_path):
    """Stub the S3/rasterio COG write+upload so only the georef math runs."""
    fake = tmp_path / "agit.tif"
    fake.write_bytes(b"stub")
    monkeypatch.setattr(P.cog_io, "write_cog_4326_from_grid",
                        lambda *a, **k: fake)
    monkeypatch.setattr(P.cog_io, "upload_cog",
                        lambda *a, **k: "s3://trid3nt-runs/runs/test/artemis_agitation.tif")
    monkeypatch.setattr(P.cog_io, "safe_unlink", lambda p: None)


def _inside(inner, outer, tol=0.0) -> bool:
    return (inner[0] >= outer[0] - tol and inner[1] >= outer[1] - tol
            and inner[2] <= outer[2] + tol and inner[3] <= outer[3] + tol)


def test_published_cog_bounds_fall_inside_request_bbox(tmp_path, monkeypatch):
    x_local, y_local, hs, ikle, bbox, utm_epsg = _marquette_local_mesh()
    slf = tmp_path / "agit_field.slf"
    _write_local_frame_selafin(slf, x_local, y_local, hs, ikle=ikle)
    _stub_cog_seam(monkeypatch, tmp_path)

    layers, metrics = P.postprocess_artemis(
        slf, run_id="georef-test", utm_epsg=utm_epsg, request_bbox=bbox,
        incident_hs_m=1.0, reach_name="marquette", wave_mode="diffraction")

    assert layers, "postprocess must return an agitation layer"
    cog_bbox = tuple(layers[0].bbox)
    # THE regression assertion: the georeferenced COG lands inside the AOI, not at
    # the UTM-zone origin. Interior nodes + a 0.0009 deg render pad stay within the
    # request bbox.
    assert _inside(cog_bbox, bbox), (
        f"COG bounds {cog_bbox} escaped the request bbox {bbox} -- "
        "local-frame mesh coordinates were not offset back to true UTM")
    assert _inside(tuple(metrics["bbox"]), bbox)

    # and it is genuinely at Marquette (~ -87.3, 46.6), NOT the pre-fix Gulf of
    # Guinea (~ -91.5, 0.0) the raw local coords would have produced.
    cx = 0.5 * (cog_bbox[0] + cog_bbox[2])
    cy = 0.5 * (cog_bbox[1] + cog_bbox[3])
    assert -87.34 < cx < -87.27 and 46.57 < cy < 46.61


def test_georef_needs_request_bbox_when_utm_set(tmp_path, monkeypatch):
    x_local, y_local, hs, ikle, bbox, utm_epsg = _marquette_local_mesh()
    slf = tmp_path / "agit_field.slf"
    _write_local_frame_selafin(slf, x_local, y_local, hs, ikle=ikle)
    _stub_cog_seam(monkeypatch, tmp_path)

    # utm_epsg set but no bbox = the offset is unknowable; refuse rather than
    # silently georeference to the zone origin.
    with pytest.raises(P.PostprocessTelemacError) as ei:
        P.postprocess_artemis(
            slf, run_id="georef-test", utm_epsg=utm_epsg, request_bbox=None,
            incident_hs_m=1.0, wave_mode="diffraction")
    assert ei.value.error_code == "TELEMAC_PARAMS_INVALID"


def test_idealized_local_frame_unaffected(tmp_path, monkeypatch):
    # utm_epsg None (idealized analytic path): coords stay in the local placeholder
    # frame, request_bbox is irrelevant, and no offset reconstruction runs.
    x_local, y_local, hs, ikle, _bbox, _epsg = _marquette_local_mesh()
    slf = tmp_path / "agit_field.slf"
    _write_local_frame_selafin(slf, x_local, y_local, hs, ikle=ikle)
    _stub_cog_seam(monkeypatch, tmp_path)

    layers, metrics = P.postprocess_artemis(
        slf, run_id="ideal-test", utm_epsg=None, request_bbox=None,
        incident_hs_m=1.0, wave_mode="diffraction")
    assert layers
    assert metrics["crs"] == f"EPSG:{P._LOCAL_FRAME_EPSG}"
