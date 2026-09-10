"""Every module, script or path a comment, docstring or reader-facing doc names,
and every relative link those docs carry, must resolve against the tracked tree.

A reference that stopped resolving is a lie the reader cannot check. Prose scope
is the product trees, the manual and the two law documents; a dated record states
what was true then and is read as evidence, so it is out.
"""

from __future__ import annotations

import re
import subprocess

from tests.hygiene import _source as source

TOP_LEVEL = ("trid3nt_server", "trid3nt_contracts", "plugin", "contracts", "scripts", "tests", "docs")

PATH_REF = re.compile(rf"(?<![\w./>-])(?:{'|'.join(TOP_LEVEL)})/[\w./-]*[\w](?![\w<*])")
SCRIPT_REF = re.compile(r"(?<![\w./*>-])[a-z_][a-z0-9_]{2,}\.(?:py|sh)\b")
MODULE_REF = re.compile(r"\b(?:trid3nt_server|trid3nt_contracts)(?:\.[a-z_][a-z0-9_]*)+\b")

PACKAGE_ROOTS = ("", "contracts/", "plugin/")

#: Markdown whose sentences are claims about the tree as it is now: the reader's
#: manual, the generated template pages, and every directory map. A dated record
#: under `docs/design/`, `docs/validation/`, `docs/reports/` or `docs/decisions/`
#: names what it named when it was written and is not scanned.
LIVE_DOC_ROOTS = ("docs/site/", "docs/authoring/", "docs/playbooks/", "docs/templates/")

#: The two law documents are scanned with the maps: an agent is told to obey them,
#: so a path either of them names must resolve or the instruction cannot be carried
#: out. They are undated standing law, not a record of what was once true.
LAW_DOCS = ("AGENTS.md", "docs/CONVENTIONS.md")

#: Relative links are resolved in every tracked markdown outside frozen evidence:
#: a link is a promise the reader can follow, whatever the page's vintage.
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")


def _tracked():
    listing = subprocess.run(
        ["git", "ls-files", "-z"], cwd=source.REPO_ROOT,
        capture_output=True, text=True, check=True,
    ).stdout
    return tuple(p for p in listing.split("\0") if p)


def _resolvable():
    paths = _tracked()
    files = set(paths)
    dirs = {d for p in paths for d in _parents(p)}
    basenames = {p.rsplit("/", 1)[-1] for p in paths}
    return files, dirs, basenames


def _parents(path: str):
    parts = path.split("/")
    for i in range(1, len(parts)):
        yield "/".join(parts[:i])


def _module_paths(dotted: str):
    """Every file a dotted name could be, and every prefix of it - a trailing
    segment is as often a symbol as a submodule."""
    parts = dotted.split(".")
    for depth in range(len(parts), 0, -1):
        stem = "/".join(parts[:depth])
        for root in PACKAGE_ROOTS:
            yield f"{root}{stem}.py"
            yield f"{root}{stem}/__init__.py"
            yield f"{root}{stem}"


def _prose():
    """(file, line, text, base) - base is the directory a relative path resolves against."""
    for path in source.python_files():
        for comment in source.all_comments(path):
            yield comment.rel, comment.lineno, comment.text, ""
        for doc in source.docstrings(path):
            yield doc.rel, doc.lineno, doc.text, ""
    for rel in _live_docs():
        base = rel.rsplit("/", 1)[0] + "/" if "/" in rel else ""
        text = (source.REPO_ROOT / rel).read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            yield rel, number, line, base


def _live_docs() -> list[str]:
    """The markdown whose sentences are claims about the tree as it is now."""
    live = ["Makefile", *LAW_DOCS]
    for path in source.markdown_files():
        rel = str(path.relative_to(source.REPO_ROOT))
        if path.name == "README.md" or rel.startswith(LIVE_DOC_ROOTS):
            live.append(rel)
    return live


def test_named_modules_and_scripts_exist() -> None:
    files, dirs, basenames = _resolvable()
    dead = []
    for rel, lineno, text, base in _prose():
        for offset, match in _matches(text):
            if not _resolves(match, files, dirs, basenames, base):
                dead.append(f"{rel}:{lineno + offset} names {match!r}, which does not exist")
    assert not dead, "dead references:\n" + "\n".join(sorted(set(dead)))


def _matches(text: str):
    for pattern in (PATH_REF, SCRIPT_REF, MODULE_REF):
        for match in pattern.finditer(text):
            yield text.count("\n", 0, match.start()), match.group(0)


def _resolves(ref: str, files, dirs, basenames, base: str = "") -> bool:
    for candidate in (ref, base + ref):
        if candidate in files or candidate in dirs or (source.REPO_ROOT / candidate).exists():
            return True
    if "/" not in ref and "." in ref and not ref.startswith(("trid3nt_server.", "trid3nt_contracts.")):
        return ref in basenames
    if ref.startswith(("trid3nt_server.", "trid3nt_contracts.")):
        return any(candidate in files or candidate in dirs for candidate in _module_paths(ref))
    return (source.REPO_ROOT / ref).exists()


def test_relative_links_resolve() -> None:
    """A link the reader cannot follow is the same defect as a dead path."""
    broken = []
    for path in source.markdown_files():
        rel = str(path.relative_to(source.REPO_ROOT))
        if rel.startswith("docs/proof/"):
            continue
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            for match in LINK.finditer(line):
                target = match.group(1)
                if target.startswith(("http://", "https://", "mailto:", "#")):
                    continue
                target = target.split("#")[0]
                if target and not (path.parent / target).resolve().exists():
                    broken.append(f"{rel}:{number} links {match.group(1)!r}, which does not exist")
    assert not broken, "broken relative links:\n" + "\n".join(sorted(set(broken)))
