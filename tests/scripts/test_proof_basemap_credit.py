"""A proof render names the ground it was drawn on.

The mosaic is measured, the ocean reference basemap takes over when the imagery
carries no legible signal, and the label travels with the image so a caption
cannot credit imagery that is not there. Offline: a measurement and a read."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("PIL")

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

DEV = Path(__file__).resolve().parents[2] / "dev"
MODULE = DEV / "packet" / "merc_render.py"

if not DEV.is_dir():
    pytest.skip("dev/ is absent: the dev tools are not on the remote",
                allow_module_level=True)


def _merc_render():
    spec = importlib.util.spec_from_file_location("merc_render_under_test", MODULE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


#: Measured over open Lake Superior at z11 (mean luminance 12, std 3) against
#: land extents at z14-16 (mean 59-84, std 22-49).
_OPEN_WATER_RGB = (1, 14, 22)


def test_open_water_imagery_measures_as_unlit():
    module = _merc_render()
    dark = Image.fromarray(
        np.full((64, 64, 3), _OPEN_WATER_RGB, dtype=np.uint8))
    assert module._is_unlit(dark) is True


def test_a_land_mosaic_is_not_mistaken_for_unlit_water():
    module = _merc_render()
    rng = np.random.default_rng(0)
    land = Image.fromarray(
        np.clip(rng.normal(60.0, 22.0, (64, 64, 3)), 0, 255).astype(np.uint8))
    assert module._is_unlit(land) is False


def test_a_mosaic_with_no_stamp_is_credited_as_imagery():
    """The default source, for a caller that built its own image."""
    module = _merc_render()
    plain = Image.new("RGB", (8, 8))
    assert module.basemap_label(plain) == module.IMAGERY_LABEL


def test_the_credit_travels_with_the_image_it_describes():
    module = _merc_render()
    stamped = Image.new("RGB", (8, 8))
    stamped.info["basemap"] = module.OCEAN_LABEL
    assert module.basemap_label(stamped) == module.OCEAN_LABEL
    assert "imagery is unlit open water" in module.OCEAN_LABEL
