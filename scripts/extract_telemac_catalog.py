#!/usr/bin/env python3
"""Extract every exposed TELEMAC module's keyword catalog out of the image.

The dictionaries are the engine's own publication of its keyword surface, and
they live in the worker image. This runs the in-container extractor over them
and lands one committed JSON per module under
``trid3nt_server/workflows/telemac/catalog/`` - generated data, never hand
edited, re-extracted and compared by the suite whenever the image is present.

  python scripts/extract_telemac_catalog.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trid3nt_server.workflows.mesh.meshers.drivers import drivers_dir  # noqa: E402

#: The modules the wrapper layer exposes. TOMAWAC rides along because its
#: dictionary is in the same image and the extraction costs nothing extra.
MODULES: tuple[str, ...] = ("telemac2d", "telemac3d", "artemis", "tomawac",
                            "waqtel", "gaia")

IMAGE = "trid3nt-local/telemac:latest"
_DRIVER = "telemac_dico_driver.py"
_TIMEOUT_S = 600


def catalog_dir() -> Path:
    """Where the committed catalogs live."""
    return (Path(__file__).resolve().parents[1] / "trid3nt_server" / "workflows"
            / "telemac" / "catalog")


def image_present() -> bool:
    """Whether the TELEMAC image this reads the dictionaries out of is here."""
    cp = subprocess.run(["docker", "image", "inspect", IMAGE],
                        capture_output=True, text=True)
    return cp.returncode == 0


def extract_catalogs(dest: Path, modules: tuple[str, ...] = MODULES) -> dict[str, int]:
    """Write ``<module>.json`` for every module into ``dest`` -> keyword counts."""
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "config.json").write_text(json.dumps({"modules": list(modules)}))
    argv = ["docker", "run", "--rm", "--network", "none",
            "-v", f"{drivers_dir()}:/drivers:ro", "-v", f"{dest}:/data",
            IMAGE, "python", f"/drivers/{_DRIVER}", "/data/config.json", "/data"]
    cp = subprocess.run(argv, capture_output=True, text=True, timeout=_TIMEOUT_S)
    if cp.returncode != 0:
        raise RuntimeError("the TELEMAC dictionaries could not be read "
                           f"(rc={cp.returncode}):\n{cp.stdout[-2000:]}\n"
                           f"{cp.stderr[-2000:]}")
    counts = json.loads((dest / "telemac_dico_stats.json").read_text())
    for scratch in ("config.json", "telemac_dico_stats.json"):
        (dest / scratch).unlink()
    return counts


def main() -> int:
    counts = extract_catalogs(catalog_dir())
    print(json.dumps(counts, indent=2))
    print("total keywords:", sum(counts.values()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
