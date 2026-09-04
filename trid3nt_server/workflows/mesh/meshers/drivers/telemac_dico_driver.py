"""In-container extractor for the TELEMAC keyword dictionaries.

Runs INSIDE ``trid3nt-local/telemac:latest``, the only place the engine's own
dictionaries and their DAMOCLES reader live. The host mounts this file and an
output directory and shells it; nothing here imports trid3nt code.

  python telemac_dico_driver.py /data/config.json /data

Config key: ``modules`` - the module names whose dictionary is read. Emits one
``<module>.json`` per module, keywords ORDERED AS THE DICTIONARY, plus
``telemac_dico_stats.json`` carrying the count each module contributed.

The table is TRIMMED: what a slot needs to be filled and refused, and nothing
the eficas GUI needs. The two normalizations the dictionaries themselves force
are done here so no consumer repeats them - the stray French type spellings
(``ENTIER``, ``REEL``), and the help text, which arrives LaTeX-marked and with
every apostrophe swapped to a double quote by the dictionary reader.
"""

from __future__ import annotations

import json
import re
import sys

sys.path.insert(0, "/opt/conda/opentelemac/scripts/python3")

from execution.telemac_cas import get_dico  # noqa: E402
from execution.telemac_dico import TelemacDico  # noqa: E402

#: French type spellings that survive in a handful of keywords.
_TYPES = {"ENTIER": "INTEGER", "REEL": "REAL"}

#: APPARENCE values that mark a keyword's list as OPEN-ENDED. Every other
#: TAILLE above one is a fixed tuple of that width.
_UNBOUNDED = frozenset(("LIST", "DYNLIST", "DYNLIST2"))

#: The SUBMIT field's read/write segment -> what the file is to the run.
_ROLES = {"LIT": "input", "ECR": "output", "ECRLIT": "input/output"}

_LATEX = (
    (re.compile(r"\\(?:begin|end)\{itemize\}"), " "),
    (re.compile(r"\\item\b"), " "),
    (re.compile(r"\\tel(?:key|file)\{([^{}]*)\}"), r"\1"),
)


def de_latex(help_text: str) -> str:
    """The dictionary's help as prose.

    The dictionary reader replaces every apostrophe inside a string with a
    double quote (a DAMOCLES string is single-quoted, so an apostrophe arrives
    doubled and is un-doubled into ``"``), which is why the swap is undone here
    and not guessed at by a reader.
    """
    for pattern, repl in _LATEX:
        help_text = pattern.sub(repl, help_text)
    return " ".join(help_text.replace('"', "'").split())


def _size(info: dict) -> tuple[int | None, bool]:
    """``TAILLE`` and whether APPARENCE declares the list open-ended."""
    apparence = info.get("APPARENCE")
    shown = apparence if isinstance(apparence, list) else [apparence]
    return info.get("TAILLE"), bool(_UNBOUNDED.intersection(shown))


def _slot(keyword: str, info: dict) -> dict:
    """One dictionary entry, trimmed to what a slot is filled and refused by."""
    size, unbounded = _size(info)
    row: dict = {"keyword": keyword,
                 "type": _TYPES.get(info["TYPE"], info["TYPE"]),
                 "size": size,
                 "unbounded": unbounded,
                 "help": de_latex(info.get("AIDE1") or info.get("AIDE") or ""),
                 "rubrique": info["RUBRIQUE1"],
                 "is_file": "SUBMIT" in info}
    for key, field in (("DEFAUT1", "default"), ("NIVEAU", "level"),
                       ("CHOIX1", "choices"), ("MNEMO", "mnemo")):
        if key in info:
            row[field] = info[key]
    if row["is_file"]:
        submit = str(info["SUBMIT"]).split(";")
        row["file_role"] = _ROLES.get(submit[4], submit[4])
        row["file_mandatory"] = submit[2] == "OBLIG"
    return row


def extract(module: str) -> dict:
    """A module's whole keyword surface, in the dictionary's own order."""
    dico = TelemacDico(get_dico(module))
    return {"module": module,
            "keywords": [_slot(k, v) for k, v in dico.data.items()]}


def main() -> int:
    cfg = json.load(open(sys.argv[1]))
    out = sys.argv[2].rstrip("/")
    counts = {}
    for module in cfg["modules"]:
        catalog = extract(module)
        with open(f"{out}/{module}.json", "w") as handle:
            json.dump(catalog, handle, indent=2, sort_keys=False)
            handle.write("\n")
        counts[module] = len(catalog["keywords"])
    json.dump(counts, open(out + "/telemac_dico_stats.json", "w"), indent=2)
    print("TELEMAC_DICO_OK", json.dumps(counts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
