"""Render the finite-fault Okada seafloor-deformation proofs.

Canonical proof (``geoclaw_okada_deformation.png``): the REAL 2021 M8.2 Chignik
seafloor deformation from the published USGS finite-fault inversion
(``ak0219neiszm_1``, 396 subfaults) -- a concentrated, asymmetric uplift/subsidence
field following the inverted slip, NOT a single idealized rectangle. The synthetic
single-subfault sibling (``geoclaw_okada_deformation_synthetic.png``) is kept for
contrast: one Mw-scaled rectangle reads as a straight bar.

Both fields are the final-time vertical dZ the worker's ``maketopo.py`` wrote
(``deformation_*.asc``, EPSG:4326), reprojected to EPSG:3857 and drawn on the
diverging rdbu ramp centred on 0 (blue=subsidence / white=0 / red=uplift) over Esri
World Imagery. Symmetric colour scale per panel; caption states max uplift /
subsidence + subfault count + the product citation.

Run (from repo root):
  python scripts/proof_geoclaw_okada_finite_fault.py
"""
from __future__ import annotations

import io
import math

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import rasterio
import requests
from PIL import Image
from pyproj import Transformer
from rasterio.warp import Resampling, reproject

TILE_URL = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
OUT_DIR = "/home/nate/Documents/trid3nt-local/docs/proof/templates"
TO_3857 = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)

FINITE_ASC = "/tmp/ff-proof/deformation_finite.asc"
SINGLE_ASC = "/tmp/ff-proof/deformation_single.asc"
PRODUCT_URL = (
    "https://earthquake.usgs.gov/product/finite-fault/ak0219neiszm_1/us/"
    "1635188938271/complete_inversion.fsp"
)


def tile_xy(lon, lat, z):
    n = 2 ** z
    return ((lon + 180.0) / 360.0 * n,
            (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)


def tile_bounds_3857(x, y, z):
    n = 2 ** z

    def merc(tx, ty):
        lon = tx / n * 360.0 - 180.0
        lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * ty / n))))
        return TO_3857.transform(lon, lat)

    x0, y0 = merc(x, y + 1)
    x1, y1 = merc(x + 1, y)
    return x0, y0, x1, y1


def fetch_basemap(w, s, e, n, zoom):
    x0f, y1f = tile_xy(w, s, zoom)
    x1f, y0f = tile_xy(e, n, zoom)
    xs = list(range(int(math.floor(x0f)), int(math.floor(x1f)) + 1))
    ys = list(range(int(math.floor(y0f)), int(math.floor(y1f)) + 1))
    mosaic = Image.new("RGB", (256 * len(xs), 256 * len(ys)))
    sess = requests.Session()
    for j, ty in enumerate(ys):
        for i, tx in enumerate(xs):
            r = sess.get(TILE_URL.format(z=zoom, y=ty, x=tx), timeout=30)
            r.raise_for_status()
            mosaic.paste(Image.open(io.BytesIO(r.content)).convert("RGB"), (i * 256, j * 256))
    wm0, _, _, _ = tile_bounds_3857(min(xs), max(ys), zoom)
    _, sm0, _, _ = tile_bounds_3857(min(xs), max(ys), zoom)
    _, _, em1, nm1 = tile_bounds_3857(max(xs), min(ys), zoom)
    return np.asarray(mosaic), (wm0, em1, sm0, nm1)


def read_asc(path):
    """Read a bare ESRI-ASCII grid -> (north-up array, (w, s, e, n) EPSG:4326)."""
    hdr = {}
    rows = []
    with open(path) as fh:
        for line in fh:
            t = line.split()
            if not t:
                continue
            k = t[0].lower()
            if k in ("ncols", "nrows", "xllcorner", "yllcorner", "cellsize", "nodata_value"):
                hdr[k] = float(t[1])
            else:
                rows.append([float(v) for v in t])
    ncols, nrows = int(hdr["ncols"]), int(hdr["nrows"])
    cell = hdr["cellsize"]
    xll, yll = hdr["xllcorner"], hdr["yllcorner"]
    nod = hdr.get("nodata_value", -9999.0)
    arr = np.asarray(rows, dtype="float64").reshape(nrows, ncols)
    arr = np.where(arr == nod, np.nan, arr)
    return arr, (xll, yll, xll + ncols * cell, yll + nrows * cell)


def grid_to_3857(arr, bbox):
    w, s, e, n = bbox
    nrows, ncols = arr.shape
    src_tf = rasterio.transform.from_bounds(w, s, e, n, ncols, nrows)
    x0, y0 = TO_3857.transform(w, s)
    x1, y1 = TO_3857.transform(e, n)
    dst = np.full((nrows, ncols), np.nan, dtype="float64")
    dst_tf = rasterio.transform.from_bounds(x0, y0, x1, y1, ncols, nrows)
    reproject(source=arr, destination=dst, src_transform=src_tf, src_crs="EPSG:4326",
              dst_transform=dst_tf, dst_crs="EPSG:3857", resampling=Resampling.bilinear)
    return dst, (x0, x1, y0, y1)


def pick_zoom(bbox):
    span = max(bbox[2] - bbox[0], bbox[3] - bbox[1])
    for z in range(9, 3, -1):
        if 360.0 / (2 ** z) * 3 >= span:  # ~<= 4 tiles across
            return z
    return 5


def render(asc_path, out_name, title, subtitle, caption):
    arr, bbox = read_asc(asc_path)
    # Mask near-zero deformation so the basemap shows through the quiet field.
    vmax = float(np.nanmax(np.abs(arr)))
    masked = np.where(np.abs(arr) < 0.02 * vmax, np.nan, arr)
    arr3857, (x0, x1, y0, y1) = grid_to_3857(masked, bbox)
    pad = 0.15 * max(bbox[2] - bbox[0], bbox[3] - bbox[1])
    zoom = pick_zoom(bbox)
    bm, bm_ext = fetch_basemap(bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad, zoom)

    up = float(np.nanmax(arr))
    down = float(np.nanmin(arr))

    fig, ax = plt.subplots(figsize=(9, 9.6))
    ax.imshow(bm, extent=bm_ext, origin="upper")
    im = ax.imshow(arr3857, extent=(x0, x1, y0, y1), origin="upper", cmap="RdBu_r",
                   vmin=-vmax, vmax=vmax, alpha=0.85)
    ax.set_xlim(bm_ext[0], bm_ext[1])
    ax.set_ylim(bm_ext[2], bm_ext[3])
    ax.set_xticks([])
    ax.set_yticks([])
    fig.suptitle(title, fontsize=12.5, fontweight="bold", y=0.975)
    ax.set_title(subtitle, fontsize=9.5, pad=6)
    cb = fig.colorbar(im, ax=ax, fraction=0.040, pad=0.02)
    cb.set_label("vertical seafloor deformation dZ (m): blue = subsidence, red = uplift",
                 fontsize=8.5)
    fig.text(0.5, 0.05, caption, ha="center", va="top", fontsize=8.3, wrap=True)
    fig.subplots_adjust(bottom=0.13, top=0.9)
    out = f"{OUT_DIR}/{out_name}"
    fig.savefig(out, dpi=120, bbox_inches="tight")
    plt.close(fig)
    print(f"wrote {out}  uplift={up:+.3f} m subsidence={down:+.3f} m")
    return up, down


def main():
    _fa, _ = read_asc(FINITE_ASC)
    up, down = float(np.nanmax(_fa)), float(np.nanmin(_fa))
    render(
        FINITE_ASC, "geoclaw_okada_deformation.png",
        "GeoClaw Okada seafloor deformation -- REAL finite-fault slip (2021 M8.2 Chignik)",
        "USGS finite-fault inversion ak0219neiszm_1: 396 subfaults, Okada superposition",
        (f"MEASURED finite-fault inversion (396 subfaults): the deformation is the "
         f"Okada superposition of the published inverted slip -- a concentrated, "
         f"ASYMMETRIC uplift/subsidence field following the rupture, NOT a single "
         f"idealized rectangle.\nMax uplift +{up:.3f} m, max subsidence {down:.3f} m. "
         f"basis=measured_inversion. Product: {PRODUCT_URL}"),
    )
    render(
        SINGLE_ASC, "geoclaw_okada_deformation_synthetic.png",
        "GeoClaw Okada seafloor deformation -- SINGLE-subfault synthesis (degrade rung)",
        "Wells & Coppersmith Mw-8.2 scaling: one rectangular subfault (illustrative)",
        ("DERIVED single-subfault scaling synthesis (1 rectangle): one idealized "
         "Okada patch reads as a STRAIGHT BAR -- the degrade rung used ONLY when no "
         "USGS finite-fault product exists for the event. basis=derived (LOUDLY "
         "labeled non-site-specific)."),
    )
    print(f"CANONICAL real-slip proof: uplift +{up:.3f} m / subsidence {down:.3f} m over 396 subfaults")


if __name__ == "__main__":
    main()
