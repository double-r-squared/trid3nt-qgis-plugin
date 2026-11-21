"""The cores a run has: this box's for the daemon's own work, the docker host's for each solver container.

``docker_host`` reads the docker host once per process; a host docker cannot answer for is read as this box.
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess

logger = logging.getLogger(__name__)

_HOST: tuple[int, bool] | None = None


def local_cores() -> int:
    """The cores of the box the daemon runs on."""
    return os.cpu_count() or 1


def docker_host() -> tuple[int, bool]:
    """``(cores, is this box)`` of the docker host the solver containers run on; compared by the host's own name."""
    global _HOST
    if _HOST is None:
        try:
            said = subprocess.run(
                ["docker", "info", "--format", "{{.NCPU}}\t{{.Name}}"],
                capture_output=True, text=True, timeout=30, check=True).stdout
            count, name = said.strip().split("\t", 1)
            _HOST = (int(count), name == socket.gethostname())
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            logger.warning("docker host not read (%s); its cores are read as this box's", exc)
            return local_cores(), True
    return _HOST


def docker_cores() -> int:
    """The cores of the docker host: the most a solver container can seat."""
    return docker_host()[0]


def container_default() -> int:
    """All but one of the docker host's cores when the host is this box (the daemon keeps one), else all of them."""
    count, here = docker_host()
    return max(1, count - 1) if here else count


def host_rows() -> dict[str, tuple[int, int]]:
    """``{setting: (default, most)}`` for the two core settings: what the host states and the dictionary cannot."""
    return {"LOCAL_CORES": (local_cores(), local_cores()),
            "CONTAINER_CORES": (container_default(), docker_cores())}
