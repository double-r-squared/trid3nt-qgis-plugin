"""In-container extractor for the TELEMAC keyword dictionaries.

Runs INSIDE ``trid3nt-local/telemac:latest``, the only place the engine's own
dictionaries and their DAMOCLES reader live. The host mounts this file and an
output directory and shells it; nothing here imports trid3nt code.

  python telemac_dico_driver.py /data/config.json /data

Config key: ``modules`` - the module names whose dictionary is read. Emits one
``<module>.json`` per module, keywords ORDERED AS THE DICTIONARY, plus
``telemac_dico_stats.json`` carrying the count each module contributed.

The table is TRIMMED: what a slot needs to be filled and refused, and nothing
the eficas GUI needs. Everything a consumer would otherwise have to work out for
itself is resolved HERE, where the engine's own answer is at hand - the stray
French type spellings (``ENTIER``, ``REEL``); the help text, which arrives
LaTeX-marked and with every apostrophe swapped to a double quote by the
dictionary reader; the identifier a class body writes each keyword under, from
the map eficas ships; and the keywords whose one value is a separator-joined
selection, from telapy's own list of them.
"""

from __future__ import annotations

import importlib
import json
import re
import sys

sys.path.insert(0, "/opt/conda/opentelemac/scripts/python3")
sys.path.insert(0, "/opt/conda/opentelemac/scripts/python3/eficas")

from execution.telemac_cas import SPECIAL, get_dico  # noqa: E402
from execution.telemac_dico import TelemacDico  # noqa: E402

#: French type spellings that survive in a handful of keywords.
_TYPES = {"ENTIER": "INTEGER", "REEL": "REAL"}

#: APPARENCE values that mark a keyword's list as OPEN-ENDED - TAILLE is then
#: the width the dictionary allocated, not the width a value must have. TAILLE
#: itself is the ARITY: at one, the value is a single value however open its
#: choices are.
_UNBOUNDED = frozenset(("LIST", "DYNLIST", "DYNLIST2"))

#: The SUBMIT field's read/write segment -> what the file is to the run.
_ROLES = {"LIT": "input", "ECR": "output", "ECRLIT": "input/output"}

#: The engine's own name macros, which carry the sentence's subject.
_NAMES = {"tel": "TELEMAC", "tomawac": "TOMAWAC", "waqtel": "WAQTEL",
          "gaia": "GAIA", "khione": "KHIONE", "nestor": "NESTOR",
          "artemis": "ARTEMIS", "stbtel": "STBTEL"}

#: The math tail the help text reaches for, each rendered as its own word.
_MATH = ("alpha", "delta", "epsilon", "gamma", "Gamma", "mu", "nu", "omega",
         "Omega", "rho", "sin", "sqrt", "tau", "theta")

_LATEX = (
    # a hard line break and a forced space are whitespace
    (re.compile(r"\\\\|\\ "), " "),
    # environment wrappers; the text they wrap IS the help
    (re.compile(r"\\(?:begin|end)\{[A-Za-z]+\}(?:\{[^{}]*\})?"), " "),
    (re.compile(r"\\item\b"), " "),
    # the cross-reference macros are their argument
    (re.compile(r"\\tel(?:key|file)\{([^{}]*)\}"), r"\1"),
    (re.compile(r"\\telemac\{([^{}]*)\}"),
     lambda m: "TELEMAC-" + m.group(1).upper()),
    (re.compile(r"\\(" + "|".join(_NAMES) + r")(?![A-Za-z])"),
     lambda m: _NAMES[m.group(1)]),
    # accents and font switches are their argument
    (re.compile(r"\\(?:tilde|vec|ddot|rm|mathrm)\{([^{}]*)\}"), r"\1"),
    # the math tail as words
    (re.compile(r"\^\{\\circ\}"), "deg"),
    (re.compile(r"\\ldots"), "..."),
    (re.compile(r"\\times"), "x"),
    (re.compile(r"\\(" + "|".join(_MATH) + r")(?![A-Za-z])"), r"\1"),
    # escaped punctuation is the punctuation
    (re.compile(r"\\([_%&])"), r"\1"),
    # sub/superscripts and the inline-math delimiters go, their content stays
    (re.compile(r"([\^_])\{([^{}]*)\}"), r"\1\2"),
    (re.compile(r"[${}]"), ""),
)


def de_latex(help_text: str) -> str:
    """The dictionary's help as prose - plain words, no markup left.

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


def identifiers(module: str) -> dict:
    """The module's own keyword -> identifier map, as eficas ships it.

    The identifiers a class body writes keywords under are the image's, not a
    spelling rule guessed at from the keywords: hyphens and parentheses become
    underscores too, and one TOMAWAC keyword carries a trailing space the map is
    keyed without.
    """
    eficas = importlib.import_module(module + "_dicoCasEnToCata")
    return {engine: cata
            for cata, engine in eficas.dicoCataToEngTelemac.items()}


def _slot(keyword: str, identifier: str, info: dict) -> dict:
    """One dictionary entry, trimmed to what a slot is filled and refused by."""
    size, unbounded = _size(info)
    row: dict = {"keyword": keyword,
                 "identifier": identifier,
                 "type": _TYPES.get(info["TYPE"], info["TYPE"]),
                 "size": size,
                 "unbounded": unbounded,
                 "help": de_latex(info.get("AIDE1") or info.get("AIDE") or ""),
                 "rubrique": info["RUBRIQUE1"],
                 "is_file": "SUBMIT" in info}
    # telapy's own list of the keywords whose ONE value is a separator-joined
    # SELECTION from the choices ('U,V,H'), read from telapy rather than
    # transcribed: its reader is what judges these, and it judges them by name.
    if keyword.strip() in SPECIAL:
        row["multi_select"] = True
    for key, field in (("DEFAUT1", "default"), ("NIVEAU", "level"),
                       ("CHOIX1", "choices"), ("MNEMO", "mnemo")):
        if key in info:
            row[field] = info[key]
    # A choice's LABEL and the Fortran mnemonic are prose out of the same
    # dictionary and carry the same markup; a choice's VALUE is what gets
    # written to the deck and is never touched.
    if isinstance(row.get("choices"), dict):
        row["choices"] = {value: de_latex(label)
                          for value, label in row["choices"].items()}
    if isinstance(row.get("mnemo"), str):
        row["mnemo"] = de_latex(row["mnemo"])
    if row["is_file"]:
        submit = str(info["SUBMIT"]).split(";")
        row["file_role"] = _ROLES.get(submit[4], submit[4])
        row["file_mandatory"] = submit[2] == "OBLIG"
    return row


def extract(module: str) -> dict:
    """A module's whole keyword surface, in the dictionary's own order."""
    dico = TelemacDico(get_dico(module))
    named = identifiers(module)
    return {"module": module,
            "keywords": [_slot(k, named[k.strip()], v)
                         for k, v in dico.data.items()]}


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
