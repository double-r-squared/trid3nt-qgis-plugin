"""THE ANIMATION'S COLOUR SCALE IS FIXED FOR THE LENGTH OF THE GIF.

A time-stepped picture whose scale moves with the frame is a picture of the
renderer, not of the run: the same colour means one value at t=0 and another at
t=5, so a reader watching the plume "arrive" may only be watching the autoscale
chase it. The preset family states the rule - the scope of a ``policy: data`` rescale is
THE RUN, never the frame - and this pins it in pixels.

Frames 0-2 live in 0..1 and frames 3-5 in 0..100, which is as loud a per-frame
rescale as a field can offer. The legend region of the produced GIF must still be
BYTE-IDENTICAL in every frame, while the field region must not be (otherwise the
identity assertion is measuring a still).
"""

from __future__ import annotations

import functools
import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("matplotlib")
pytest.importorskip("PIL")

import numpy as np  # noqa: E402

from trid3nt_server.emission import presets  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "render_selafin_animation.py"

#: The style row the synthetic field is drawn by - the same row the published
#: raster of that quantity carries.
STYLE = {"kind": "continuous", "ramp": "reds", "units": "mg/L",
         "label": "Plume concentration"}

#: Where the colorbar and its labels sit in the produced figure, as a fraction of
#: image width. Everything the frames animate is left of this; a change to the
#: figure layout is meant to break this test rather than quietly stop covering
#: the legend, which is why the crop is checked for a real colour ramp below.
_LEGEND_X_FRAC = 0.86


@functools.lru_cache(maxsize=1)
def _animation_module():
    """The script, imported by path - ``scripts/`` is not a package."""
    spec = importlib.util.spec_from_file_location("render_selafin_animation", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("render_selafin_animation", module)
    spec.loader.exec_module(module)
    return module


def _frames() -> np.ndarray:
    """Six frames whose ranges differ by two orders of magnitude."""
    rng = np.random.default_rng(20260819)
    amplitudes = [1.0, 1.0, 1.0, 100.0, 100.0, 100.0]
    return np.asarray([amp * (0.2 + 0.8 * rng.random(81)) for amp in amplitudes])


def _triangulation():
    from matplotlib.tri import Triangulation

    gx, gy = np.meshgrid(np.linspace(0.0, 1.0, 9), np.linspace(0.0, 1.0, 9))
    return Triangulation(gx.ravel(), gy.ravel())


def _rgb_frames(gif_path: Path) -> list[np.ndarray]:
    from PIL import Image

    image = Image.open(gif_path)
    out = []
    for index in range(image.n_frames):
        image.seek(index)
        out.append(np.asarray(image.convert("RGB")))
    return out


@pytest.fixture(scope="module")
def rendered(tmp_path_factory) -> dict:
    module = _animation_module()
    values = _frames()
    out = tmp_path_factory.mktemp("animation")
    gif = out / "legend_stability.gif"
    result = module.render_frames(
        _triangulation(), values, list(range(values.shape[0])),
        bbox_ll=(-85.5, 29.9, -85.3, 30.1), units="mg/L",
        title="legend stability", run_id="TEST", source_name="synthetic.slf",
        variable="TRACER", gif_path=gif, peak_path=out / "legend_stability.png",
        style=STYLE, axes_factory=module.plain_axes)
    return {"module": module, "values": values, "gif": gif, "result": result,
            "preset": presets.from_row(STYLE)}


def test_the_scale_is_resolved_over_the_whole_run_never_one_frame(rendered):
    module, values = rendered["module"], rendered["values"]
    preset = rendered["preset"]
    scale = module.animation_scale(values, style=STYLE)

    whole_run = (float(np.percentile(values, 2.0)), float(np.percentile(values, 98.0)))
    assert scale == pytest.approx(whole_run), (
        "the range must be read over EVERY frame at once - the scope of a "
        "data-policy rescale is the run, never the frame")

    quiet = module.animation_scale(values[:1], style=STYLE)
    loud = module.animation_scale(values[-1:], style=STYLE)
    assert scale != pytest.approx(quiet) and scale != pytest.approx(loud), (
        "the fixture's frames must differ enough that a per-frame scale would be "
        "a visibly different picture")
    assert scale[1] > 10.0 * quiet[1], "frame 0 alone would compress the loud frames"


def test_the_animation_paints_what_the_published_raster_of_that_quantity_paints(
        rendered):
    module, values = rendered["module"], rendered["values"]
    preset = rendered["preset"]
    published = presets.resolve(
        preset,
        read_range=lambda _s: (float(np.percentile(values, 2.0)),
                               float(np.percentile(values, 98.0))))

    assert (rendered["result"]["vmin"], rendered["result"]["vmax"]) == \
        pytest.approx(published.range)
    assert rendered["result"]["legend_note"] == published.legend_note()
    assert rendered["result"]["colormap"].lower() == preset.ramp.lower(), (
        "one declared ramp, spelled for matplotlib rather than chosen again")


def test_the_legend_region_is_byte_identical_in_every_frame(rendered):
    frames = _rgb_frames(rendered["gif"])
    assert len(frames) == rendered["values"].shape[0] > 1

    width = frames[0].shape[1]
    cut = int(width * _LEGEND_X_FRAC)
    legend = [frame[:, cut:, :] for frame in frames]
    field = [frame[:, :cut, :] for frame in frames]

    ramp = np.unique(legend[0].reshape(-1, 3), axis=0)
    assert len(ramp) >= 16, (
        f"the crop at x >= {_LEGEND_X_FRAC} holds only {len(ramp)} colours, so it "
        "is not covering the colorbar and this test proves nothing")
    assert any(not np.array_equal(field[i], field[0]) for i in range(1, len(field))), (
        "the frames must actually differ, or byte-identity is measuring a still")

    for index, region in enumerate(legend[1:], start=1):
        assert region.tobytes() == legend[0].tobytes(), (
            f"the legend moved between frame 0 and frame {index}: the colour scale "
            "is being recomputed per frame")


def test_the_still_reports_the_run_peak_and_the_run_scale(rendered):
    result, values = rendered["result"], rendered["values"]
    assert result["peak_frame"] == int(np.argmax(values.max(axis=1)))
    assert result["peak_value"] == pytest.approx(float(values.max()))
    assert result["vmax"] < result["peak_value"], (
        "a p98 clip sits below the run maximum by construction")


def test_the_scale_helper_runs_without_the_preset_family(monkeypatch):
    """The script stays runnable where the server package is not importable."""
    module = _animation_module()
    monkeypatch.setattr(module, "_PRESETS", None)
    values = _frames()

    scale = module.resolve_animation_style(values)
    assert scale.colormap == "viridis"
    assert (scale.vmin, scale.vmax) == pytest.approx(
        (float(np.percentile(values, 2.0)), float(np.percentile(values, 98.0))))
    assert "scaled to this run (p2-p98)" in scale.note


def test_an_empty_field_still_yields_a_usable_scale():
    module = _animation_module()
    scale = module.resolve_animation_style(np.full((3, 4), np.nan), style=STYLE)
    assert scale.vmax > scale.vmin, "a zero-width scale is not a scale"
    assert "unreadable" in scale.note, "the legend admits it never saw the data"


# --------------------------------------------------------------------------- #
# WHICH published layer an animation is held to
# --------------------------------------------------------------------------- #


def _packet_module():
    spec = importlib.util.spec_from_file_location(
        "assemble_proof_packet", REPO / "scripts" / "assemble_proof_packet.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_published_range_is_found_by_quantity_not_by_the_title_it_was_painted_under():
    """A title is prose a product may rewrite; the quantity is the layer's
    identity. Matching on prose is how a still and its animation drift onto two
    scales for one field with no range comparison able to catch it.
    """
    from trid3nt_server.testing.proof_animations import ProofAnimation

    packet = _packet_module()
    evidence = {"layers": [
        {"name": "Max water depth (watershed mesh)", "layer_type": "raster",
         "quantity": "water_depth",
         "legend": {"kind": "continuous", "label": "Max water depth",
                    "vmin": 0.0, "vmax": 9.9493}},
        {"name": "Input: mesh bed", "layer_type": "raster",
         "quantity": None,
         "legend": {"kind": "continuous", "label": "Elevation",
                    "vmin": 621.0, "vmax": 1382.0}},
    ]}
    scale = packet.published_scale(
        evidence, ProofAnimation(variable="WATER DEPTH", units="m",
                                 quantity="water_depth"))
    assert scale["published_range"] == [0.0, 9.9493]
    assert scale["published_by"] == [
        {"layer": "Max water depth (watershed mesh)", "range": [0.0, 9.9493]}]


def test_a_quantity_the_run_never_published_has_nothing_to_agree_with():
    """Honest silence, not an invented agreement: a field with no published
    raster of its own is rendered on its own range and the row says so."""
    from trid3nt_server.testing.proof_animations import ProofAnimation

    packet = _packet_module()
    evidence = {"layers": [
        {"name": "Max water depth", "layer_type": "raster",
         "quantity": "water_depth",
         "legend": {"kind": "continuous", "label": "Max water depth",
                    "vmin": 0.0, "vmax": 9.9}},
    ]}
    scale = packet.published_scale(
        evidence, ProofAnimation(variable="VELOCITY MAGNITUDE", units="m/s",
                                 quantity="flow_velocity"))
    assert scale["published_range"] is None
    assert scale["run_raster_presets"] == ["Max water depth"]


def test_a_log_ramp_takes_the_published_top_and_its_own_declared_floor():
    """A published envelope's floor is routinely ZERO, and a log ramp has no
    zero. Reaching for the smallest positive value the run wrote spans every
    decade down to a float32 denormal and paints the whole domain one colour -
    a picture of the norm rather than of the water. The floor is read at the
    clip the row declares; the TOP is the published one, which is the end a
    peak is read off.
    """
    module = _animation_module()
    values = np.zeros((3, 400), dtype="float64")
    values[1, :200] = np.linspace(1e-40, 1e-3, 200)   # a denormal-adjacent tail
    values[2, :] = np.linspace(1e-3, 9.9, 400)

    scale = module.resolve_animation_style(
        values, style={"kind": "continuous"}, transform="log",
        shared=(0.0, 9.9493))
    assert scale.range == (0.0, 9.9493)

    positive = values[values > 0]
    expected = float(np.percentile(positive, scale.clip[0]))
    norm = module.log_norm(values, scale)
    assert norm.vmin == pytest.approx(expected)
    assert norm.vmax == pytest.approx(9.9493)
    assert norm.vmin > 1e-12, "a denormal floor is not a scale a reader can read"
