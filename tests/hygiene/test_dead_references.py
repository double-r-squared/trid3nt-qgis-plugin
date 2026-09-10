"""Every module, script or path a comment, docstring or README names must exist.

A reference that stopped resolving is a lie the reader cannot check, so the
guard resolves paths, bare script names and dotted module paths against the
tracked tree.
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
    for path in source.markdown_files():
        if path.name != "README.md":
            continue
        rel = str(path.relative_to(source.REPO_ROOT))
        base = rel.rsplit("/", 1)[0] + "/" if "/" in rel else ""
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            yield rel, number, line, base


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
