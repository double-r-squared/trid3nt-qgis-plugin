"""A rendered packet lives seven days, and every write is what sweeps.

PROOF IS TRANSIENT: `run/proof/` is a delivery lane git does not carry, so the
renderer prunes it rather than leaving a cleanup step somebody has to remember.
The newest packet always survives its own write.
"""

from __future__ import annotations

import functools
import importlib.util
import os
import sys
import time
from pathlib import Path

import pytest

DEV = Path(__file__).resolve().parents[2] / "dev"
SCRIPT = DEV / "packet" / "assemble_proof_packet.py"

if not DEV.is_dir():
    pytest.skip("dev/ is absent: the dev tools are not on the remote",
                allow_module_level=True)

DAY = 86400.0


@functools.lru_cache(maxsize=1)
def _packet_module():
    """The renderer, imported by path - ``dev/packet/`` is not a package."""
    spec = importlib.util.spec_from_file_location("assemble_proof_packet", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("assemble_proof_packet", module)
    spec.loader.exec_module(module)
    return module


def _packet(root: Path, template: str, run_id: str, *, age_days: float) -> Path:
    """One packet folder whose contents are ``age_days`` old."""
    # The AGE of a packet is the newest file in it, so the fake mtimes go on the
    # files and the directory is aged with them: a re-render replaces files
    # without touching the folder's own stat, which is the case that would let a
    # live packet look ancient.
    directory = root / template / run_id
    directory.mkdir(parents=True)
    stamp = time.time() - age_days * DAY
    for name in ("packet.json", f"{template}.png"):
        path = directory / name
        path.write_text(name, encoding="utf-8")
        os.utime(path, (stamp, stamp))
    os.utime(directory, (stamp, stamp))
    return directory


def test_the_sweep_keeps_seven_days_and_drops_what_is_older(tmp_path, monkeypatch):
    module = _packet_module()
    monkeypatch.setattr(module, "PACKET_ROOT", str(tmp_path))
    fresh = _packet(tmp_path, "telemac_river_dye", "RUN_TODAY", age_days=0.0)
    inside = _packet(tmp_path, "telemac_river_dye", "RUN_SIX_DAYS", age_days=6.0)
    stale = _packet(tmp_path, "telemac_river_dye", "RUN_EIGHT_DAYS", age_days=8.0)
    ancient = _packet(tmp_path, "telemac_do_sag", "RUN_LAST_MONTH", age_days=30.0)

    removed = module.prune_packets()

    assert fresh.is_dir(), "the packet just written must survive its own sweep"
    assert inside.is_dir(), "a six-day packet is inside the seven-day window"
    assert not stale.exists()
    assert not ancient.exists()
    assert sorted(removed) == ["telemac_do_sag/RUN_LAST_MONTH",
                               "telemac_river_dye/RUN_EIGHT_DAYS"]


def test_keep_days_is_the_lever(tmp_path, monkeypatch):
    module = _packet_module()
    monkeypatch.setattr(module, "PACKET_ROOT", str(tmp_path))
    month_old = _packet(tmp_path, "telemac_do_sag", "RUN_LAST_MONTH", age_days=30.0)

    assert module.prune_packets(keep_days=60) == []
    assert month_old.is_dir()

    assert module.prune_packets(keep_days=7) == ["telemac_do_sag/RUN_LAST_MONTH"]
    assert not month_old.exists()


def test_a_replaced_file_keeps_its_packet_alive(tmp_path, monkeypatch):
    """A re-render writes over the files without touching the folder's stat."""
    module = _packet_module()
    monkeypatch.setattr(module, "PACKET_ROOT", str(tmp_path))
    directory = _packet(tmp_path, "telemac_river_dye", "RUN_REPLACED", age_days=30.0)
    (directory / "packet.json").write_text("re-rendered", encoding="utf-8")

    assert module.prune_packets() == []
    assert directory.is_dir()


def test_an_absent_root_is_not_an_error(tmp_path, monkeypatch):
    module = _packet_module()
    monkeypatch.setattr(module, "PACKET_ROOT", str(tmp_path / "never-rendered"))
    assert module.prune_packets() == []
