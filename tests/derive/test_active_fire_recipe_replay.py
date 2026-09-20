"""The western US replay: the active-fire recipe's sequence over a recorded scene.

The capability left the tree as `docs/authoring/active-fire-recipe.md`, so what
guards it is the recipe's own numbers run over the scene it was measured on -
GOES-18 bands 2, 3, 7, 14 and 15 over (-124, 33, -110, 49) at 2026-09-14T22:06Z,
recorded beside the recipe. Four cells burn in that window and they are one
northern California fire; a change to any coefficient in the recipe moves that
count, which is what makes the number a test rather than a claim.
"""
from __future__ import annotations

import os

import numpy as np
import pytest

SCENE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "docs", "authoring",
    "active-fire-replay-west-conus.npz")

#: Step 2 of the recipe, the daytime cloud test.
CLOUD_REFLECTANCE_SUM = 0.9
CLOUD_LONGWAVE_K = 265.0
CLOUD_WARM_REFLECTANCE_SUM = 0.7
CLOUD_WARM_LONGWAVE_K = 285.0
#: Step 3, the candidate screen.
BT_SHORTWAVE_MIN_K = 310.0
BT_SPLIT_MIN_K = 10.0
#: Steps 4 and 5, the background and the detection.
BACKGROUND_WINDOW_CELLS = 15
BACKGROUND_MIN_CELLS = 6
BACKGROUND_MIN_FRACTION = 0.25
SPLIT_DEVIATIONS = 3.5
SPLIT_MARGIN_K = 6.0
BACKGROUND_SIGMA = 3.0

#: What the recipe's sequence finds in this window.
DETECTIONS = 4
#: Where they are: one fire, every cell of it inside this box.
FIRE_BOX = (-121.9, 41.7, -121.6, 41.9)


def _window_sums(values, radius):
    """Sums over a square window ``2 * radius + 1`` across, clipped at the edge."""
    array = np.asarray(values, dtype=np.float64)
    height, width = array.shape
    integral = np.pad(array, ((1, 0), (1, 0)))
    np.cumsum(integral, axis=0, out=integral)
    np.cumsum(integral, axis=1, out=integral)
    top = np.clip(np.arange(height) - radius, 0, height)[:, None]
    bottom = np.clip(np.arange(height) + radius + 1, 0, height)[:, None]
    left = np.clip(np.arange(width) - radius, 0, width)[None, :]
    right = np.clip(np.arange(width) + radius + 1, 0, width)[None, :]
    return (integral[bottom, right] - integral[top, right]
            - integral[bottom, left] + integral[top, left])


def _background_stats(values, background, counts, radius):
    """Per cell: the background mean in its window and the MEAN ABSOLUTE deviation
    about it - the published statistic, which over ground as skewed as warm bare
    rock is a different and looser number than the standard deviation."""
    safe = np.where(counts > 0, counts, 1.0)
    mean = _window_sums(np.where(background, values, 0.0), radius) / safe
    height, width = mean.shape
    padded = np.full((height + 2 * radius, width + 2 * radius), np.nan)
    padded[radius:radius + height, radius:radius + width] = np.where(
        background, values, np.nan)
    total = np.zeros_like(mean)
    span = 2 * radius + 1
    for row in range(span):
        for col in range(span):
            total += np.nan_to_num(
                np.abs(padded[row:row + height, col:col + width] - mean), nan=0.0)
    return mean, total / safe


def _stands_above(values, background, counts, radius, deviations, margin_k):
    mean, deviation = _background_stats(values, background, counts, radius)
    return values > mean + deviations * deviation + margin_k


def _replay(scene, *, cloud_masked: bool = True):
    """The recipe's steps 2-6 over one recorded scene -> the detected cells."""
    red, nir = scene["red"], scene["nir"]
    shortwave, longwave, dirty = scene["bt39"], scene["bt11"], scene["bt12"]
    valid = np.isfinite(shortwave) & np.isfinite(longwave)
    reflectance = np.where(np.isfinite(red) & np.isfinite(nir),
                           np.nan_to_num(red + nir), 0.0)
    cloud = valid & np.isfinite(dirty) & (
        (reflectance > CLOUD_REFLECTANCE_SUM)
        | (dirty < CLOUD_LONGWAVE_K)
        | ((reflectance > CLOUD_WARM_REFLECTANCE_SUM)
           & (dirty < CLOUD_WARM_LONGWAVE_K)))
    if not cloud_masked:
        cloud = np.zeros_like(cloud)
    split = np.where(valid, shortwave - longwave, np.float32(np.nan))
    candidate = valid & ~cloud & (shortwave >= np.float32(BT_SHORTWAVE_MIN_K)) \
        & (split >= np.float32(BT_SPLIT_MIN_K))
    radius = BACKGROUND_WINDOW_CELLS // 2
    background = valid & ~candidate & ~cloud
    counts = _window_sums(background, radius)
    covered = _window_sums(np.ones_like(background, dtype=np.float64), radius)
    enough = (counts >= BACKGROUND_MIN_CELLS) & (
        counts >= BACKGROUND_MIN_FRACTION * covered)
    return (candidate & enough
            & _stands_above(split, background, counts, radius,
                            SPLIT_DEVIATIONS, 0.0)
            & _stands_above(split, background, counts, radius, 0.0,
                            SPLIT_MARGIN_K)
            & _stands_above(shortwave, background, counts, radius,
                            BACKGROUND_SIGMA, 0.0)), cloud, valid


@pytest.fixture(scope="module")
def scene():
    if not os.path.exists(SCENE):
        pytest.skip(f"the recorded western US scene is not beside the recipe: {SCENE}")
    with np.load(SCENE) as data:
        yield {key: data[key] for key in data.files}


def test_western_us_replay_detects_four_cells(scene):
    flagged, _cloud, _valid = _replay(scene)
    assert int(flagged.sum()) == DETECTIONS


def test_the_four_are_one_fire(scene):
    flagged, _cloud, _valid = _replay(scene)
    transform = scene["transform"]
    rows, cols = np.nonzero(flagged)
    west, south, east, north = FIRE_BOX
    for row, col in zip(rows.tolist(), cols.tolist()):
        lon = transform[0] * (col + 0.5) + transform[1] * (row + 0.5) + transform[2]
        lat = transform[3] * (col + 0.5) + transform[4] * (row + 0.5) + transform[5]
        assert west <= lon <= east and south <= lat <= north


def test_the_cloud_test_sets_ground_aside(scene):
    """Step 2 earns its place: it masks a sixth of the window, and the candidate
    screen behind it raises hundreds fewer cells for the contextual stage to judge."""
    masked, cloud, valid = _replay(scene)
    unmasked, _cloud, _valid = _replay(scene, cloud_masked=False)
    assert 0.1 < float(cloud.sum()) / float(valid.sum()) < 0.3
    assert int(masked.sum()) == int(unmasked.sum()) == DETECTIONS
