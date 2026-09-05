"""In-container writer and parser for the TELEMAC steering files.

Runs INSIDE ``trid3nt-local/telemac:latest``, the only place the engine's own
dictionaries, its DAMOCLES reader and telapy live. The host mounts this file and
the authoring directory and shells it; nothing here imports trid3nt code.

  python telemac_cas_driver.py /data/config.json /data

Two config keys, either or both:

``write``    ``{basename: {"module": ..., "values": {KEYWORD: value}}}`` - telapy
             writes each file from the values given. Emits
             ``telemac_cas_written.json``.
``steering`` ``{basename: module}``, where the module names the dictionary the
             file is read against. Emits ``telemac_cas_stats.json``: one row per
             file carrying the keyword count it parsed, or the parse error and
             the keyword it names.

``TelemacCas`` is the only writer of the steering format, and two measured
caveats are handled around it. A file keyword is assigned through ``values``
because telapy's ``set()`` demands the file already exist. And a string is
handed over as :class:`_EngineString`, whose ``repr`` is the form DAMOCLES
reads: Python's own repr switches to a double-quote delimiter as soon as the
value holds an apostrophe, and a double-quoted string derails the parse on the
first space inside it.

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


class _EngineString(str):
    """A string spelled the way DAMOCLES reads one: quoted with ``'``, doubled
    inside. telapy formats every value with ``repr``, so the spelling is a repr."""

    def __repr__(self) -> str:
        return "'" + self.replace("'", "''") + "'"


class _EngineLogical(int):
    """A logical spelled the way DAMOCLES reads one. Python's ``repr(True)`` is
    ``True``, which the dictionary reader does not answer to."""

    def __repr__(self) -> str:
        return "YES" if self else "NO"


def _engine(value):
    """One value in the engine's own spelling; a list, item by item."""
    if isinstance(value, list):
        return [_engine(item) for item in value]
    if isinstance(value, bool):
        return _EngineLogical(value)
    if isinstance(value, str):
        return _EngineString(value)
    return value


def write_steering(cfg: dict) -> dict:
    """Write every ``{basename: {module, values}}`` the config names -> what was
    written. telapy holds the format; only the spelling of a value is ours."""
    rows = {}
    for basename, spec in dict(cfg.get("write") or {}).items():
        path = cfg["data_dir"] + "/" + basename
        cas = TelemacCas(path, get_dico(spec["module"]), access="w",
                         check_files=False)
        for keyword, value in spec["values"].items():
            cas.values[keyword] = _engine(value)
        cas.write(path)
        rows[basename] = {"module": spec["module"],
                          "keywords": len(spec["values"])}
    return rows


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
    if cfg.get("write"):
        written = write_steering(cfg)
        json.dump(written, open(out + "/telemac_cas_written.json", "w"), indent=2)
        print("TELEMAC_CAS_WRITTEN", json.dumps(written))
    if cfg.get("steering"):
        rows = parse_steering(cfg)
        json.dump(rows, open(out + "/telemac_cas_stats.json", "w"), indent=2)
        print("TELEMAC_CAS_OK", json.dumps(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
