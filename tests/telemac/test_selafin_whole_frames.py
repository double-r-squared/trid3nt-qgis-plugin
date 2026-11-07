"""A result file a killed run left mid-frame reads as the whole frames it holds (VERIFICATION: a real result file cut by bytes)."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from trid3nt_server.workflows.telemac.authoring.selafin_io import (
    SelafinReadError, keep_whole_frames, read_selafin)

_FULL = Path(__file__).parents[1] / "fixtures" / "progress" / "r2d_domain.slf"


def test_a_file_cut_mid_frame_keeps_its_whole_frames(tmp_path):
    whole = len(read_selafin(_FULL)["times"])
    cut = tmp_path / "cut.slf"
    cut.write_bytes(_FULL.read_bytes()[: _FULL.stat().st_size - 100])
    with pytest.raises(SelafinReadError):
        read_selafin(cut)
    assert keep_whole_frames(cut) == whole - 1
    assert len(read_selafin(cut)["times"]) == whole - 1


def test_a_file_cut_inside_the_header_or_before_its_first_frame_refuses(tmp_path):
    cut = tmp_path / "cut.slf"
    cut.write_bytes(_FULL.read_bytes()[:11000])
    with pytest.raises(SelafinReadError):
        keep_whole_frames(cut)
    shutil.copy(_FULL, cut)
    assert keep_whole_frames(cut) == len(read_selafin(_FULL)["times"])
