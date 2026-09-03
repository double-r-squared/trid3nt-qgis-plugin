"""In-container parser for the authored TELEMAC steering files.

Runs INSIDE ``trid3nt-local/telemac:latest``, the only place the engine's own
dictionaries and its DAMOCLES reader live. The host mounts this file and the
authoring directory and shells it; nothing here imports trid3nt code.

  python telemac_cas_driver.py /data/config.json /data

Config key: ``steering`` - ``{basename: module}``, where the module names the
dictionary the file is read against (``telemac2d``, ``gaia``, ``waqtel``, ...).
Emits ``/data/telemac_cas_stats.json``: one row per file carrying the keyword
count it parsed, or the parse error and the keyword it names.

The parse runs with the file existence check OFF. A steering file is validated at
AUTHORING time, before anything is staged, so the geometry and boundary files it
names are legitimately not beside it yet; what is being checked is the grammar
and the vocabulary, which is the half that a run cannot recover from.
"""

from __future__ import annotations

import json
import sys

sys.path.insert(0, "/opt/conda/opentelemac/scripts/python3")

from execution.telemac_cas import TelemacCas, get_dico  # noqa: E402


def parse_steering(cfg: dict) -> dict:
    """Every steering file the config names, read against its own dictionary."""
    rows = {}
    for basename, module in dict(cfg.get("steering") or {}).items():
        try:
            cas = TelemacCas(cfg["data_dir"] + "/" + basename, get_dico(module),
                             access="r", check_files=False)
            rows[basename] = {"module": module, "ok": True,
                              "keywords": len(cas.values)}
        except Exception as exc:  # noqa: BLE001 -- any refusal is a typed row
            rows[basename] = {"module": module, "ok": False,
                              "error": f"{type(exc).__name__}: {exc}"}
    return rows


def main() -> int:
    cfg = json.load(open(sys.argv[1]))
    out = sys.argv[2].rstrip("/")
    cfg["data_dir"] = out
    rows = parse_steering(cfg)
    json.dump(rows, open(out + "/telemac_cas_stats.json", "w"), indent=2)
    print("TELEMAC_CAS_OK", json.dumps(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
