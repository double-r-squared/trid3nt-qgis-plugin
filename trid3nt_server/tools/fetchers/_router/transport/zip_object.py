"""Whole-object ZIP fetch for the multi-file / DEFLATE-member family.

A DEFLATE-compressed member cannot be byte-range windowed (decoding forces a
near-whole transfer) and a shapefile needs every sibling co-located, so the
honest shape is a whole-object GET then in-memory or tmp-dir extraction."""

from __future__ import annotations

import io
import zipfile

import httpx

from .client import get_bytes

__all__ = ["get_zip"]


def get_zip(
    client: httpx.Client, url: str, *, headers: dict[str, str] | None = None
) -> zipfile.ZipFile:
    """GET a whole ZIP object and open it in memory, so a caller may read members or
    ``extractall`` with no further network. A 404/403 has already classified to a
    typed transport error; a non-ZIP body raises ``zipfile.BadZipFile``."""
    body, _ct, _final_url = get_bytes(client, url, headers=headers)
    return zipfile.ZipFile(io.BytesIO(body))
