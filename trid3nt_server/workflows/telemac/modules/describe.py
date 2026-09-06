"""``describe_keywords``: the READ over a module's keyword catalog.

A module's dictionary is between ninety and four hundred keywords, and the whole
set across the exposed modules is more than a thousand. No tool docstring carries
that, so the surface is REACHED rather than carried: a question in words - "what
governs friction", "how do I write the results more often" - is answered out of
the catalog itself, with each match's own help, its labeled choices, its engine
default, its level and whether it names a file.

Nothing here decides anything and nothing here runs: the answer is the
dictionary, and what a caller does with a keyword it learns is state it on a
fill, through the ``keywords={NAME: value}`` floor every template's wire carries.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool

from .module import catalog_dir, load_catalog

__all__ = ["DescribeKeywordsError", "describe_keywords"]

#: Words that name no keyword. A query is a person's sentence, and matching on
#: its glue would rank every keyword whose help says "the" first.
_GLUE = frozenset((
    "a", "an", "and", "are", "at", "be", "by", "can", "do", "does", "for", "from",
    "how", "i", "in", "is", "it", "its", "many", "much", "my", "of", "on", "or",
    "set", "that", "the", "then", "there", "this", "to", "want", "what", "when",
    "where", "which", "with", "you",
))
#: How much a hit in each field is worth. The keyword's own NAME is what a person
#: is usually reaching for; the rubrique is the section it lives in; the help is
#: the widest net and the weakest signal.
_WEIGHTS: Mapping[str, int] = {"keyword": 6, "mnemo": 4, "rubrique": 3, "help": 1}


class DescribeKeywordsError(RuntimeError):
    """The catalog cannot answer: no such module.

    Codes: ``UNKNOWN_MODULE`` - the name is not one of the exposed modules.
    """

    error_code: str
    retryable: bool = False

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


def _exposed() -> list[str]:
    return sorted(path.stem for path in catalog_dir().glob("*.json"))


def _words(text: str) -> list[str]:
    return [w for w in re.split(r"[^a-z0-9]+", str(text).lower()) if w]


def _score(slot: Any, wanted: set[str], phrase: str) -> int:
    """How well one slot answers the query. Deterministic, and model-free.

    A whole-phrase hit in the keyword's own name outranks any accumulation of
    single words, because "law of bottom friction" typed in full is the caller
    naming the keyword rather than describing it.
    """
    fields = {"keyword": slot.keyword, "mnemo": slot.mnemo,
              "rubrique": " ".join(slot.rubrique), "help": slot.desc}
    score = 0
    for field, text in fields.items():
        hits = wanted & set(_words(text))
        score += _WEIGHTS[field] * len(hits)
    if phrase and phrase in slot.keyword.lower():
        score += 24
    return score


def _row(slot: Any) -> dict[str, Any]:
    """One matched keyword, as the dictionary describes it."""
    row: dict[str, Any] = {
        "keyword": slot.keyword, "type": slot.type, "help": slot.desc,
        "level": slot.level, "is_file": slot.is_file,
        "rubrique": list(slot.rubrique),
    }
    if slot.choices:
        row["choices"] = slot.choices
    if slot.is_list:
        row["values"] = "a list of any length" if slot.unbounded else slot.size
    if slot.is_open:
        # An emptiness a reader has to be able to see: the dictionary answers
        # this keyword with nothing at all, so what the engine does with it
        # unset is the engine's own business, not a value this states.
        row["engine_default"] = None
        row["open"] = True
        row["required"] = slot.is_required
    else:
        row["engine_default"] = slot.engine_default
    return row


@register_tool(
    AtomicToolMetadata(
        name="describe_keywords",
        ttl_class="live-no-cache",
        cacheable=False,
    ),
    read_only_hint=True,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
)
def describe_keywords(module: str = "telemac2d", query: str = "",
                      limit: int = 12) -> dict[str, Any]:
    """Look up TELEMAC steering keywords: what governs friction, turbulence, output.

    Use this when a run has to state something the template's own params do not
    name - a friction law, a turbulence model, a printout period, an advection
    scheme - and you need the engine's own keyword for it. What comes back is the
    module dictionary's own entry: the keyword, its help, its allowed values, the
    engine default it has when nobody states it, and whether it names a file. Set
    what you learn on the call: `keywords={"LAW OF BOTTOM FRICTION": 4}` on any
    telemac template, which fills that keyword on the sheet and shows it in the
    review as user-set.

    Do NOT use this to run anything - it reads the dictionary and nothing else -
    and do NOT use it to pick a template: the template answers the QUESTION, the
    keywords tune the deck it writes.

    Args:
        module: Which engine module's dictionary to read - ``telemac2d`` (2D
            shallow water), ``telemac3d``, ``artemis`` (waves), ``waqtel`` (water
            quality), ``gaia`` (sediment), ``tomawac``. Defaults to telemac2d.
        query: What you are looking for, in words ("bottom friction", "how often
            are results written", "turbulence model"). Matched against every
            keyword's name, its dictionary section and its help text. EMPTY
            returns the module's section index instead of keywords.
        limit: How many matches to return, at most 50.

    Returns:
        ``{module, query, keyword_count, match_count, matches: [{rubrique,
        keywords: [{keyword, help, type, choices, engine_default, level,
        is_file}, ...]}, ...]}`` - matches grouped under the dictionary's own
        section, best first. With an empty ``query``: ``{module, keyword_count,
        sections: [{rubrique, keywords: n}, ...]}``. An unknown module raises
        naming the modules that exist.
    """
    name = str(module or "").strip().lower()
    if name not in _exposed():
        raise DescribeKeywordsError(
            "UNKNOWN_MODULE",
            f"there is no keyword catalog for {module!r}; the exposed modules are "
            f"{', '.join(_exposed())}.")
    catalog = load_catalog(name)
    if not str(query or "").strip():
        sections: dict[str, int] = {}
        for slot in catalog.values():
            head = slot.rubrique[0] if slot.rubrique else ""
            sections[head] = sections.get(head, 0) + 1
        return {"module": name, "keyword_count": len(catalog),
                "sections": [{"rubrique": head, "keywords": count}
                             for head, count in sorted(sections.items())],
                "modules": _exposed()}

    phrase = " ".join(_words(query))
    wanted = {w for w in _words(query) if w not in _GLUE}
    scored = [(_score(slot, wanted, phrase), index, slot)
              for index, slot in enumerate(catalog.values())]
    # The dictionary's own order breaks a tie, so the same question asked twice
    # is answered the same way.
    best = sorted((row for row in scored if row[0] > 0),
                  key=lambda row: (-row[0], row[1]))
    kept = best[:max(1, min(int(limit or 12), 50))]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for _score_value, _index, slot in kept:
        head = slot.rubrique[0] if slot.rubrique else ""
        grouped.setdefault(head, []).append(_row(slot))
    return {"module": name, "query": str(query), "keyword_count": len(catalog),
            "match_count": len(best),
            "matches": [{"rubrique": head, "keywords": rows}
                        for head, rows in grouped.items()]}
