"""Refuse a source row that takes a key without declaring one.

For each named row (every row when none is named) it asks the provider for the description the provider publishes -
an ArcGIS REST service document, an OGC capabilities document, an OpenAPI document - and reads what that document states
about a key. A row whose provider states a key is needed and whose ``auth.credential`` is absent fails the check. A
provider that states nothing, or does not answer, is reported and never guessed about. Run it when a row is added.

    venvs/agent/bin/python scripts/check_row_keys.py [fetch_<name> ...]
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from trid3nt_server.tools.fetchers._router.key_statement import (  # noqa: E402
    arcgis_states_key, ogc_states_key, openapi_states_key)
from trid3nt_server.tools.fetchers._router.spec import compose_specs_from_tree  # noqa: E402

_TIMEOUT_S = 20.0


def _endpoint_urls(spec) -> list[str]:
    return [e.url for e in spec.endpoints.values()
            if e.url and e.url.startswith("https://") and "{" not in e.url]


def _descriptions(spec, url: str) -> list[tuple[str, str, dict, object]]:
    """``(kind, url, params, reader)`` for each description the provider publishes for ``url``."""
    wanted = []
    parts = urlsplit(url)
    if "/arcgis/rest/services" in parts.path:
        service = parts.path.split("/query")[0]
        wanted.append(("arcgis", urlunsplit(parts._replace(path=service, query="")), {"f": "json"}, arcgis_states_key))
    provider = ((spec.ingest or {}).get("qgis_provider") or {}).get("provider")
    if provider in ("wcs", "wms"):
        wanted.append(("ogc", url, {"service": provider.upper(), "request": "GetCapabilities"}, ogc_states_key))
    root = f"{parts.scheme}://{parts.netloc}"
    wanted.append(("openapi", f"{root}/openapi.json", {}, openapi_states_key))
    return wanted


def check(spec, client: httpx.Client) -> tuple[str, str]:
    """``(statement, note)``: ``key``, ``open``, ``silent`` or ``unreachable``."""
    stated: list[tuple[str, bool | None]] = []
    answered = False
    for url in _endpoint_urls(spec):
        for kind, target, params, reader in _descriptions(spec, url):
            try:
                resp = client.get(target, params=params, headers={"User-Agent": spec.auth.user_agent})
            except httpx.HTTPError:
                continue
            answered = True
            verdict = reader(resp.content)
            if verdict is not None:
                stated.append((kind, verdict))
    if not answered:
        return "unreachable", ""
    if any(v for _k, v in stated):
        return "key", ",".join(k for k, v in stated if v)
    if stated:
        return "open", ",".join(k for k, _v in stated)
    return "silent", ""


def main(argv: list[str]) -> int:
    specs = compose_specs_from_tree()
    names = argv or sorted(specs)
    refused = 0
    with httpx.Client(timeout=_TIMEOUT_S, follow_redirects=True) as client:
        for name in names:
            spec = specs[name]
            statement, note = check(spec, client)
            declared = spec.auth.credential is not None
            fail = statement == "key" and not declared
            refused += fail
            print(f"{'REFUSE' if fail else 'ok':7}{name:44}{statement:12}{'declared' if declared else 'undeclared':11}{note}")
    return 1 if refused else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
