#!/usr/bin/env python3
"""Harvest the public ESRI Living Atlas of the World into two curation catalogs.

Queries the keyless ArcGIS Online sharing/search API for the PUBLIC items in the
Living Atlas of the World curation group ("LAW Search", owner Esri_LivingAtlas),
filtered to the CONSUMABLE service types (Image / Feature / Map Service -- web
maps / apps / scenes are skipped), and normalizes each item to a fetchable catalog
entry. Output is TWO YAML files (DATA, not code):

    data/living_atlas/living_atlas_authoritative.yaml   (ESRI contentStatus badge)
    data/living_atlas/living_atlas_community.yaml        (everything else)

NATE's two-pool rule is the split key: an item is AUTHORITATIVE iff its ESRI
``contentStatus`` carries the authoritative badge (``public_authoritative`` /
``org_authoritative``); everything else is community. Premium/subscription items
(typeKeywords ``Requires Subscription`` / ``Requires Credits``) are flagged so the
fetch bridge raises the honest subscription error.

Re-runnable / idempotent: a re-run overwrites both files with a fresh snapshot.
Rate-limited politely. Offline-testable: ``--fixture <json>`` reads a canned search
response instead of the network (used by the offline test suite).

Usage:
    python scripts/harvest_living_atlas.py                    # full live harvest
    python scripts/harvest_living_atlas.py --max-per-type 50  # quick sample
    python scripts/harvest_living_atlas.py --fixture f.json --out-dir /tmp/la
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

SHARING_SEARCH = "https://www.arcgis.com/sharing/rest/search"
#: The canonical Living Atlas of the World curation group (owner Esri_LivingAtlas).
LAW_GROUP_ID = "47dd57c9a59d458c86d3d6b978560088"
CONSUMABLE_TYPES = ("Image Service", "Feature Service", "Map Service")
PREMIUM_KEYWORDS = ("Requires Subscription", "Requires Credits")
USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)


def _http_get_json(url: str, params: dict[str, str], *, retries: int = 4) -> dict[str, Any]:
    """GET a JSON body with polite backoff on transient failure."""
    full = f"{url}?{urllib.parse.urlencode(params)}"
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(full, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 -- ops tool: backoff + retry
            last_exc = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"ArcGIS search failed after {retries} tries: {full}: {last_exc}")


def _normalize_extent(raw: Any) -> list[float] | None:
    """ArcGIS ``[[xmin,ymin],[xmax,ymax]]`` -> ``[w, s, e, n]`` (None if unusable)."""
    try:
        (xmin, ymin), (xmax, ymax) = raw
        w, s, e, n = float(xmin), float(ymin), float(xmax), float(ymax)
    except (TypeError, ValueError):
        return None
    if not (-180.0 <= w <= 180.0 and -180.0 <= e <= 180.0 and -90.0 <= s <= 90.0 and -90.0 <= n <= 90.0):
        return None
    if e <= w or n <= s:
        return None
    return [round(w, 6), round(s, 6), round(e, 6), round(n, 6)]


def _normalize_item(item: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize one ArcGIS item to a LivingAtlasEntry dict (None if not fetchable)."""
    url = item.get("url")
    stype = item.get("type")
    if not url or stype not in CONSUMABLE_TYPES:
        return None
    content_status = str(item.get("contentStatus") or "")
    authoritative = content_status.endswith("authoritative")
    type_keywords = item.get("typeKeywords") or []
    premium = any(kw in type_keywords for kw in PREMIUM_KEYWORDS)
    snippet = (item.get("snippet") or item.get("description") or "").strip()
    # Strip crude HTML from descriptions used as snippet.
    if "<" in snippet and ">" in snippet:
        import re as _re
        snippet = _re.sub(r"<[^>]+>", " ", snippet)
        snippet = _re.sub(r"\s+", " ", snippet).strip()
    return {
        "id": item.get("id"),
        "title": (item.get("title") or "").strip() or item.get("id"),
        "snippet": snippet[:600],
        "service_url": url,
        "service_type": stype,
        "owner": item.get("owner") or "",
        "extent": _normalize_extent(item.get("extent")),
        "authoritative": authoritative,
        "curation": "authoritative" if authoritative else "community",
        "premium": premium,
        "tags": [str(t) for t in (item.get("tags") or [])][:20],
    }


def _paginate_type(group_id: str, service_type: str, *, delay: float,
                   max_items: int | None) -> tuple[list[dict[str, Any]], int]:
    """Page through one service type in the group. Returns (normalized, reported_total)."""
    q = f'group:{group_id} AND type:"{service_type}"'
    out: list[dict[str, Any]] = []
    start = 1
    reported_total = 0
    while True:
        data = _http_get_json(SHARING_SEARCH, {
            "q": q, "f": "json", "num": "100", "start": str(start),
            "sortField": "title", "sortOrder": "asc",
        })
        reported_total = int(data.get("total", 0))
        for item in data.get("results", []):
            norm = _normalize_item(item)
            if norm is not None:
                out.append(norm)
        next_start = int(data.get("nextStart", -1))
        if max_items is not None and len(out) >= max_items:
            out = out[:max_items]
            break
        # ArcGIS caps paging at 10000; nextStart == -1 signals the end.
        if next_start <= 0 or next_start > 10000:
            break
        start = next_start
        time.sleep(delay)
    return out, reported_total


def _write_catalog(path: Path, curation: str, entries: list[dict[str, Any]],
                   meta: dict[str, Any]) -> None:
    import yaml

    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "schema_version": "v1",
        "curation": curation,
        "harvested_at": meta["harvested_at"],
        "group_id": meta["group_id"],
        "source": "ESRI Living Atlas of the World (ArcGIS Online sharing/search API)",
        "count": len(entries),
        "entries": entries,
    }
    with path.open("w") as fh:
        yaml.safe_dump(doc, fh, sort_keys=False, allow_unicode=True, width=100)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--group-id", default=LAW_GROUP_ID)
    ap.add_argument("--out-dir", default=None, help="default: <repo>/data/living_atlas")
    ap.add_argument("--max-per-type", type=int, default=None, help="cap items per service type")
    ap.add_argument("--delay", type=float, default=0.3, help="seconds between pages")
    ap.add_argument("--fixture", default=None, help="JSON file of a canned search response (offline)")
    args = ap.parse_args(argv)

    out_dir = Path(args.out_dir) if args.out_dir else Path(__file__).resolve().parents[1] / "data" / "living_atlas"
    from datetime import datetime, timezone
    meta = {
        "harvested_at": datetime.now(timezone.utc).isoformat(),
        "group_id": args.group_id,
    }

    all_norm: list[dict[str, Any]] = []
    per_type_counts: dict[str, int] = {}
    reported_totals: dict[str, int] = {}

    if args.fixture:
        data = json.loads(Path(args.fixture).read_text())
        for item in data.get("results", []):
            norm = _normalize_item(item)
            if norm is not None:
                all_norm.append(norm)
        per_type_counts = {"fixture": len(all_norm)}
    else:
        for stype in CONSUMABLE_TYPES:
            norm, total = _paginate_type(
                args.group_id, stype, delay=args.delay, max_items=args.max_per_type
            )
            all_norm.extend(norm)
            per_type_counts[stype] = len(norm)
            reported_totals[stype] = total
            print(f"  {stype:16s} reported_total={total:6d} harvested={len(norm)}", file=sys.stderr)

    # De-dup by id (an item can appear under multiple sublayer entries).
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for e in all_norm:
        if e["id"] and e["id"] not in seen:
            seen.add(e["id"])
            deduped.append(e)

    authoritative = [e for e in deduped if e["authoritative"]]
    community = [e for e in deduped if not e["authoritative"]]
    premium_n = sum(1 for e in deduped if e["premium"])

    _write_catalog(out_dir / "living_atlas_authoritative.yaml", "authoritative", authoritative, meta)
    _write_catalog(out_dir / "living_atlas_community.yaml", "community", community, meta)

    print("Living Atlas harvest complete:")
    print(f"  group_id        : {args.group_id}")
    print(f"  reported totals : {reported_totals}")
    print(f"  harvested/type  : {per_type_counts}")
    print(f"  unique items    : {len(deduped)}")
    print(f"  authoritative   : {len(authoritative)}")
    print(f"  community       : {len(community)}")
    print(f"  premium flagged : {premium_n}")
    print(f"  wrote           : {out_dir}/living_atlas_authoritative.yaml")
    print(f"  wrote           : {out_dir}/living_atlas_community.yaml")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
