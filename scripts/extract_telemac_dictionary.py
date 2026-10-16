#!/usr/bin/env python3
"""Extract every exposed TELEMAC module's keyword dictionary out of the image.

Keyword rows are generated data the image owns and are never hand-edited; the
authored ``inputs`` section a template's values read their schema off is written
back beside the fresh rows, so regeneration never clobbers it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trid3nt_server.workflows.solver.image_script import run_image_script  # noqa: E402

#: The modules the wrapper layer exposes. TOMAWAC rides along because its
#: dictionary is in the same image and the extraction costs nothing extra.
MODULES: tuple[str, ...] = ("telemac2d", "telemac3d", "artemis", "tomawac",
                            "waqtel", "gaia", "khione")

IMAGE = "trid3nt-local/telemac:latest"
_SCRIPT = "dico.py"
_TIMEOUT_S = 600


def module_input_dir() -> Path:
    """Where the committed module inputs live."""
    return (Path(__file__).resolve().parents[1] / "trid3nt_server" / "workflows"
            / "telemac" / "modules" / "module_input")


def image_present() -> bool:
    """Whether the TELEMAC image this reads the dictionaries out of is here."""
    cp = subprocess.run(["docker", "image", "inspect", IMAGE],
                        capture_output=True, text=True)
    return cp.returncode == 0


def extract_dictionaries(dest: Path, modules: tuple[str, ...] = MODULES) -> dict[str, int]:
    """Write ``<module>.json`` for every module into `dest` -> keyword counts.

    The image's fresh rows take the ``module`` and ``keywords`` keys of the
    committed file it replaces; every other key the committed file carries stays,
    in its own order, so an authored section survives regeneration."""
    committed: dict[str, dict] = {}
    for module in modules:
        path = module_input_dir() / f"{module}.json"
        if path.is_file():
            committed[module] = json.loads(path.read_text())
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "config.json").write_text(json.dumps({"modules": list(modules)}))
    cp = run_image_script(
        image=IMAGE, engine="telemac", script=_SCRIPT, rundir=dest,
        argv=["/data/config.json", "/data"], timeout_s=_TIMEOUT_S)
    if cp.returncode != 0:
        raise RuntimeError("the TELEMAC dictionaries could not be read "
                           f"(rc={cp.returncode}):\n{cp.stdout[-2000:]}\n"
                           f"{cp.stderr[-2000:]}")
    counts = json.loads((dest / "telemac_dico_stats.json").read_text())
    for scratch in ("config.json", "telemac_dico_stats.json"):
        (dest / scratch).unlink()
    for module in modules:
        doc = dict(committed.get(module, {}))
        doc.update(json.loads((dest / f"{module}.json").read_text()))
        (dest / f"{module}.json").write_text(json.dumps(doc, indent=2) + "\n")
    return counts


def main() -> int:
    counts = extract_dictionaries(module_input_dir())
    print(json.dumps(counts, indent=2))
    print("total keywords:", sum(counts.values()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
