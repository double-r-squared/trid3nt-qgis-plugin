"""Offline tests for the TELEMAC rain-on-grid CN path + the shared node primitives.

The pure helpers only: the automatic native-vs-preprocessing runoff-path
selection, the per-node CN/Manning builders, and the UTM projection every mesh
node array goes through. Anything container-driven is proven live.
"""

from __future__ import annotations

import numpy as np
import pytest

from trid3nt_server.workflows.mesh.shared.nodes import reproject_nodes_to_utm
from trid3nt_server.workflows.telemac.rain_on_grid.cn_infiltration import (
    CNInfiltrationError,
    RunoffPathDecision,
    landcover_cn_manning,
    node_curve_numbers,
    select_runoff_path,
)


# --------------------------------------------------------------------------- #
# CN-path selection (native vs preprocessing).
# --------------------------------------------------------------------------- #
def test_constant_intensity_selects_native():
    d = select_runoff_path(constant_intensity_mm_per_hr=12.5)
    assert isinstance(d, RunoffPathDecision)
    assert d.path == "native"
    assert d.time_varying is False
    assert "RAINFALL-RUNOFF MODEL=1" in d.reason


def test_time_varying_hyetograph_selects_native_hyetograph():
    d = select_runoff_path(hyetograph_mm=[2.0, 8.0, 15.0, 6.0, 1.0])
    assert d.path == "native_hyetograph"
    assert d.time_varying is True
    assert "RAINDEF=3" in d.reason


def test_flat_hyetograph_selects_native():
    # a hyetograph that is one flat non-zero rate is NOT time-varying.
    d = select_runoff_path(hyetograph_mm=[5.0, 5.0, 5.0, 0.0])
    assert d.path == "native"
    assert d.time_varying is False


def test_no_forcing_raises():
    with pytest.raises(CNInfiltrationError):
        select_runoff_path()


# --------------------------------------------------------------------------- #
# The node primitives every mesher output goes through.
# --------------------------------------------------------------------------- #
def test_reproject_to_utm_coweeta():
    # Coweeta NC ~ (-83.4, 35.05) -> UTM 17N = EPSG 32617.
    pts = np.array([[-83.40, 35.05], [-83.41, 35.06], [-83.39, 35.04]])
    xy, epsg = reproject_nodes_to_utm(pts)
    assert epsg == 32617
    assert xy.shape == (3, 2)
    # eastings ~ a few hundred km, northings ~ 3.88 M m in zone 17N.
    assert 2e5 < xy[:, 0].mean() < 8e5
    assert 3.8e6 < xy[:, 1].mean() < 3.95e6


# --------------------------------------------------------------------------- #
# Per-node CN2 + Manning, as ``node_infiltration_fields`` composes them from
# ``node_curve_numbers`` + ``landcover_cn_manning``.
# --------------------------------------------------------------------------- #
def test_node_fields_distributed():
    # 41 = deciduous forest -> CN 80 / n 0.20; 22 = developed low -> CN 89 / n 0.10
    codes = [41, 22, 41]
    cn2 = node_curve_numbers(codes)
    manning = [landcover_cn_manning(c)[1] for c in codes]
    assert cn2 == [80.0, 89.0, 80.0]
    assert manning == [0.20, 0.10, 0.20]


def test_node_fields_uniform_cn_keeps_landcover_manning():
    codes = [41, 22]
    cn2 = node_curve_numbers(codes, uniform_cn=75.0)
    manning = [landcover_cn_manning(c)[1] for c in codes]
    assert cn2 == [75.0, 75.0]           # uniform override
    assert manning == [0.20, 0.10]       # Manning still from land cover


def test_node_fields_needs_a_landcover_list():
    # node_curve_numbers has no code list to look CN up against; a missing list
    # is a bug at the call site, not a value it can silently proceed on.
    with pytest.raises(TypeError):
        node_curve_numbers(None, uniform_cn=75.0)
