"""The engine patches this image bakes, applied at BUILD time against the
installed opentelemac source.

Each patch states the text it expects VERBATIM and stops the build when the
installed source no longer carries it: an unpatched engine would take a deck
this product writes and refuse it, or solve something other than what the deck
says, and either is worse than a build that fails."""

from __future__ import annotations

import sys
from pathlib import Path

#: The launcher's own dictionary gate. It reads a graphic-printouts value token
#: by token against the keyword's CHOIX and refuses anything that is not one of
#: them - including the engine's OWN wildcards, which ``bief/sortie.f`` matches:
#: a ``~`` stands for the rest of a mnemonic wherever the character under it is
#: a letter. A deck whose variable table does not fit the 72 columns DAMOCLES
#: reads a line to has no other spelling, so the gate is taught the wildcard its
#: own solver answers to.
GATE = Path("/opt/conda/opentelemac/scripts/python3/execution/telemac_cas.py")

EXPECTED = """                    for val in list_val:
                        tmp_val = str(val.strip(' 0123456789*'))
"""

REPLACEMENT = """                    for val in list_val:
                        stem = str(val).strip(' ')
                        if stem.endswith('~') and any(
                                choice.startswith(stem[:-1])
                                and len(choice) > len(stem) - 1
                                and choice[len(stem) - 1].isalpha()
                                for choice in list_choix):
                            continue
                        tmp_val = str(val.strip(' 0123456789*'))
"""


#: The table the patched gate must take, and one the engine matches nothing
#: with, which it must still refuse: a gate that takes every ``~`` would let a
#: misspelled mnemonic through as a variable nobody asked for.
_TAKEN = "P~,CO~,ICETYPE"
_REFUSED = "ZZ~"


def _takes(value: str) -> bool:
    """Does the installed gate take this graphic-printouts value?"""
    sys.path.insert(0, str(GATE.parents[1]))
    from execution.telemac_cas import TelemacCas, get_dico
    from utils.exceptions import TelemacException

    cas = TelemacCas("/tmp/gate_probe.cas", get_dico("khione"),
                     access="w", check_files=False)
    cas.values["VARIABLES FOR GRAPHIC PRINTOUTS"] = value
    try:
        cas._check_choix()
    except TelemacException:
        return False
    return True


def main() -> int:
    source = GATE.read_text(encoding="utf-8")
    if REPLACEMENT not in source:
        if source.count(EXPECTED) != 1:
            print(f"ENGINE PATCH REFUSED: {GATE} carries {source.count(EXPECTED)} "
                  "copies of the choices loop this patch is written against "
                  "(expected exactly 1). The installed engine source has drifted.",
                  file=sys.stderr)
            return 1
        GATE.write_text(source.replace(EXPECTED, REPLACEMENT), encoding="utf-8")
    if not _takes(_TAKEN) or _takes(_REFUSED):
        print(f"ENGINE PATCH REFUSED: the gate in {GATE} does not take "
              f"{_TAKEN!r} or does not refuse {_REFUSED!r} after the patch.",
              file=sys.stderr)
        return 1
    print(f"engine patch applied: the dictionary gate in {GATE} takes the "
          "wildcard sortie.f matches and still refuses one it does not")
    return 0


if __name__ == "__main__":
    sys.exit(main())
