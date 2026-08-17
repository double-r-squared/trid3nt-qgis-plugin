"""VERBATIM worker mirror of the ``outputs.json`` WRITER half (ADR 0280).

The emit-on-solve manifest a docker solver leg writes under its run prefix so the
agent's emission seam can publish entries as they land. This module is a
byte-for-byte behavioural mirror of the WRITER half of
``trid3nt_contracts.outputs_manifest`` -- the two are kept in lockstep by the
SHARED ``OUTPUTS_MANIFEST_SCHEMA_VERSION`` gate (the ``publish_manifest`` /
``output_quantities`` deploy-boundary precedent: the WORKER images ship
``workers/**`` but NOT ``contracts``; the AGENT ships ``contracts`` but NOT
``workers``, so the writer cannot live in one place).

PURE STDLIB -- no pydantic, no numpy, no agent imports -- so it is importable
from a minimal worker image. Only the WRITER surface lives here (a worker never
reads back its own manifest through the tolerant reader); the READER half is
agent-side only.

Entry shape (flat, role-free core -- NATE ruling): ``{kind, quantity, name, uri,
t?, units?}`` plus the OPTIONAL render-hint fields ``bbox?`` / ``band_stats?``
(ADR 0280 EXECUTED amendment -- present when the producer already computed them,
so the seam resolves the SAME bbox + rescale the register-only path did without
a COG re-read). The wrapper carries the version marker:
``{schema_version, engine, run_id, entries: [...]}``.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = [
    "OUTPUTS_MANIFEST_SCHEMA_VERSION",
    "OUTPUT_KINDS",
    "OUTPUTS_MANIFEST_BASENAME",
    "build_entry",
    "new_manifest",
    "append_entries",
    "serialize",
]

#: The ONE schema_version both this worker mirror and the agent reader
#: understand. MUST equal ``trid3nt_contracts.outputs_manifest``'s constant --
#: bumping it is a coordinated worker-image + agent redeploy.
OUTPUTS_MANIFEST_SCHEMA_VERSION: int = 1

#: The seam's routing keys (Section 1). Temporality rides ``t``, NOT a distinct
#: kind: a ``raster`` with a ``t`` that shares a ``quantity`` with its siblings
#: forms a temporal group; a ``raster`` with no ``t`` is a single layer.
OUTPUT_KINDS: frozenset[str] = frozenset({"raster", "mesh", "vector", "scalar"})

#: The object basename a leg writes under its run prefix.
OUTPUTS_MANIFEST_BASENAME: str = "outputs.json"


def build_entry(
    *,
    kind: str,
    quantity: str,
    name: str,
    uri: str,
    t: float | None = None,
    units: str | None = None,
    bbox: list[float] | None = None,
    band_stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build ONE flat manifest entry dict.

    Raises ``ValueError`` on an unrecognized ``kind`` (a typed reject at write
    time, never a silent drop -- Section 6) or a missing required field. ``t`` /
    ``units`` / ``bbox`` / ``band_stats`` are omitted from the dict (absent, not
    null) when ``None`` so the object stays as small as the schema promises.
    """
    if kind not in OUTPUT_KINDS:
        raise ValueError(
            f"outputs.json entry kind {kind!r} not in {sorted(OUTPUT_KINDS)}"
        )
    if not quantity:
        raise ValueError("outputs.json entry requires a non-empty quantity")
    if not name:
        raise ValueError("outputs.json entry requires a non-empty name")
    if not uri:
        raise ValueError("outputs.json entry requires a non-empty uri")
    entry: dict[str, Any] = {
        "kind": kind,
        "quantity": quantity,
        "name": name,
        "uri": uri,
    }
    if t is not None:
        entry["t"] = float(t)
    if units:
        entry["units"] = units
    if bbox is not None:
        entry["bbox"] = [float(v) for v in bbox]
    if band_stats is not None:
        entry["band_stats"] = dict(band_stats)
    return entry


def new_manifest(*, engine: str, run_id: str) -> dict[str, Any]:
    """A fresh, empty manifest dict carrying the version marker."""
    return {
        "schema_version": OUTPUTS_MANIFEST_SCHEMA_VERSION,
        "engine": engine,
        "run_id": run_id,
        "entries": [],
    }


def append_entries(
    existing_text: str | bytes | None,
    *,
    engine: str,
    run_id: str,
    new: list[dict[str, Any]],
) -> str:
    """The safe-append core (Section 2): read the current array, append, return
    the WHOLE array serialized for one atomic-per-object PUT.

    ``existing_text`` is the current ``outputs.json`` body (``None``/empty on the
    first frame). The caller owns the object-store GET/PUT; this function owns
    the pure array manipulation. Entries are appended in order; a prior entry is
    never edited or removed (immutable-once-written).

    Raises ``ValueError`` if ``existing_text`` carries a foreign
    ``schema_version`` (the writer must never straddle two versions).
    """
    if existing_text:
        if isinstance(existing_text, (bytes, bytearray)):
            existing_text = existing_text.decode("utf-8")
        data = json.loads(existing_text)
        sv = data.get("schema_version")
        if sv is not None and int(sv) != OUTPUTS_MANIFEST_SCHEMA_VERSION:
            raise ValueError(
                f"cannot append to outputs.json schema_version {sv!r} "
                f"(writer is {OUTPUTS_MANIFEST_SCHEMA_VERSION})"
            )
        entries = list(data.get("entries") or [])
    else:
        data = new_manifest(engine=engine, run_id=run_id)
        entries = []
    entries.extend(new)
    data["schema_version"] = OUTPUTS_MANIFEST_SCHEMA_VERSION
    data["engine"] = engine
    data["run_id"] = run_id
    data["entries"] = entries
    return serialize(data)


def serialize(manifest: dict[str, Any]) -> str:
    """Serialize a manifest dict to a compact, stable JSON string."""
    return json.dumps(manifest, separators=(",", ":"), sort_keys=False)
