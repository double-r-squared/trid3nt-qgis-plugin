"""One-time migration: legacy tile-template uris -> the s3:// object they wrap.

Persisted cases written before QGIS-native rendering carry a display URL where
a layer reference belongs::

    http://127.0.0.1:8080/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png?url=s3%3A%2F%2F...

One store, one scheme leaves nothing that can read that shape, so it is
rewritten HERE, once, rather than unwrapped forever at every reader. The
embedded ``url=`` param names the same object the template always pointed at,
so the rewrite renames a reference; it does not change which bytes a layer is.

Scope: the persistence store's STATE documents -- ``projects.json`` (each
case's ``loaded_layer_summaries[].uri``) and ``sessions.json`` (chart
``source_layer_uri`` fields). ``case_chat_messages.json`` is deliberately NOT
touched: it is the record of what was said in a turn, not state a reader
resolves, and editing an assistant's own words to make them tidy is a lie about
the transcript. A legacy template surviving there resolves to nothing and fails
honestly.

Idempotent: a document with no template is rewritten to itself. Every file it
changes is backed up alongside first.

    ./venvs/agent/bin/python scripts/migrate_legacy_tile_templates.py [--apply]

Without ``--apply`` it reports what it would rewrite and writes nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

#: The state documents a reader resolves layer uris out of.
MIGRATED_DOCUMENTS = ("projects.json", "sessions.json")


def unwrap(uri: str) -> str | None:
    """The ``s3://`` object a tile template wraps, or None when it is not one."""
    if "/cog/tiles/" not in uri:
        return None
    cog = (parse_qs(urlparse(uri).query).get("url") or [None])[0]
    if not cog:
        return None
    cog = unquote(cog)
    return cog if cog.startswith("s3://") else None


def rewrite(node: Any, counter: list[int]) -> Any:
    """Return ``node`` with every wrapped template replaced by its object."""
    if isinstance(node, str):
        cog = unwrap(node)
        if cog is None:
            return node
        counter[0] += 1
        return cog
    if isinstance(node, dict):
        return {k: rewrite(v, counter) for k, v in node.items()}
    if isinstance(node, list):
        return [rewrite(v, counter) for v in node]
    return node


def migrate_file(path: str, apply: bool) -> int:
    with open(path, encoding="utf-8") as f:
        document = json.load(f)
    counter = [0]
    rewritten = rewrite(document, counter)
    if counter[0] and apply:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        shutil.copy2(path, f"{path}.pre-migration-{stamp}")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rewritten, f)
    return counter[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--persistence-dir",
        default=os.environ.get("TRID3NT_DEV_PERSISTENCE_DIR", "data/persistence"),
    )
    parser.add_argument("--database", default="trid3nt_dev")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    root = os.path.join(args.persistence_dir, args.database)
    if not os.path.isdir(root):
        print(f"no persistence store at {root}", file=sys.stderr)
        return 1

    total = 0
    for name in MIGRATED_DOCUMENTS:
        path = os.path.join(root, name)
        if not os.path.isfile(path):
            print(f"{name}: absent")
            continue
        count = migrate_file(path, args.apply)
        total += count
        verb = "rewrote" if (count and args.apply) else "would rewrite"
        print(f"{name}: {verb} {count} legacy tile-template uri(s)")
    if total and not args.apply:
        print("\nnothing written -- re-run with --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
