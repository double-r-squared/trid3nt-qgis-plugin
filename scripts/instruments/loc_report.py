"""Pure-code LOC by tree and server subtree: no blank, comment or docstring lines.

The standing measure for every LOC report; YAML, JSON, docs/ and images never count.
"""
from __future__ import annotations

import argparse
import collections
import io
import os
import subprocess
import tokenize

STATEMENT_ENDS = {tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.ENCODING}


def classify(path: str) -> tuple[int, int, int, int]:
    text = open(path, "rb").read().decode("utf-8", errors="replace")
    lines = text.splitlines()
    total = len(lines)
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, SyntaxError):
        return total, sum(1 for ln in lines if not ln.strip()), 0, 0
    comment: set[int] = set()
    doc: set[int] = set()
    for i, t in enumerate(toks):
        if t.type == tokenize.COMMENT and lines[t.start[0] - 1].strip().startswith("#"):
            comment.add(t.start[0])
        elif t.type == tokenize.STRING:
            j = i - 1
            while j >= 0 and toks[j].type in (tokenize.NL, tokenize.COMMENT):
                j -= 1
            k = i + 1
            while k < len(toks) and toks[k].type in (tokenize.NL, tokenize.COMMENT):
                k += 1
            if (j < 0 or toks[j].type in STATEMENT_ENDS) and (k >= len(toks) or toks[k].type == tokenize.NEWLINE):
                doc.update(range(t.start[0], t.end[0] + 1))
    # The four counts partition the file, so a blank line inside a docstring is
    # docstring and nothing else; counting it as blank as well would subtract it
    # twice out of pure code.
    blank = sum(1 for n, ln in enumerate(lines, 1) if not ln.strip() and n not in doc)
    return total, blank, len(comment), len(doc)


def is_test(f: str) -> bool:
    return f.startswith("tests/") or "/tests/" in f or os.path.basename(f).startswith("test_")


def main() -> int:
    ap = argparse.ArgumentParser(description="Pure-code LOC by tree and server subtree.")
    ap.add_argument("subtrees", nargs="?", type=int, default=14,
                    help="how many server subtrees to list (default 14)")
    args = ap.parse_args()
    root = subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip()
    os.chdir(root)
    files = [f for f in subprocess.check_output(["git", "ls-files"], text=True).split("\n")
             if f.endswith(".py") and os.path.isfile(f) and not f.startswith("docs/")]
    trees: dict[str, list[int]] = collections.defaultdict(lambda: [0, 0, 0, 0])
    subs: dict[str, list[int]] = collections.defaultdict(lambda: [0, 0, 0, 0])
    for f in files:
        counts = classify(f)
        key = "TESTS" if is_test(f) else f.split("/")[0]
        for i, v in enumerate(counts):
            trees[key][i] += v
        if key == "trid3nt_server":
            parts = f.split("/")
            sub = "/".join(parts[1:3]) if len(parts) > 3 else parts[1]
            for i, v in enumerate(counts):
                subs[sub][i] += v

    def pure(v: list[int]) -> int:
        return v[0] - v[1] - v[2] - v[3]

    print(f"{'tree':<24}{'pure':>9}{'total':>9}{'docstr':>9}{'comment':>9}")
    product = [0, 0, 0, 0]
    for k in ("trid3nt_server", "plugin", "contracts", "scripts", "workers"):
        v = trees[k]
        print(f"{k:<24}{pure(v):>9}{v[0]:>9}{v[3]:>9}{v[2]:>9}")
        product = [a + b for a, b in zip(product, v)]
    print(f"{'PRODUCT':<24}{pure(product):>9}{product[0]:>9}{product[3]:>9}{product[2]:>9}")
    v = trees["TESTS"]
    print(f"{'TESTS':<24}{pure(v):>9}{v[0]:>9}{v[3]:>9}{v[2]:>9}")
    print("\nserver subtrees (pure)")
    for k, v in sorted(subs.items(), key=lambda kv: -pure(kv[1]))[: args.subtrees]:
        print(f"{k:<24}{pure(v):>9}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
