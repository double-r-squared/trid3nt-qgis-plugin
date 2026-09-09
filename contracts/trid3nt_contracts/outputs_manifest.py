"""The ``outputs.json`` emit-on-solve manifest - writer and typed reader.

The append-only manifest a solver leg writes under its run prefix so entries
publish as they land. The WRITER half is PURE STDLIB so a deploy context
without pydantic can mirror it verbatim; the READER half is tolerant pydantic.
Two surfaces, ONE ``schema_version`` gate.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict

__all__ = [
    "OUTPUTS_MANIFEST_SCHEMA_VERSION",
    "OUTPUT_KINDS",
    "OUTPUTS_MANIFEST_BASENAME",
    "build_entry",
    "new_manifest",
    "append_entries",
    "serialize",
    "OutputEntry",
    "OutputBandStats",
    "OutputsManifest",
    "parse_outputs_manifest",
]

#: The ONE schema_version both the writer and the reader understand. Any mirror
#: gates on this exact value, so bumping it is a coordinated redeploy of both
#: sides; a writer is pinned to one version for its whole life.
OUTPUTS_MANIFEST_SCHEMA_VERSION: int = 1

#: The routing keys. Temporality rides ``t``, NOT a distinct kind: a ``raster``
#: with a ``t`` that shares a ``quantity`` with its siblings forms a temporal
#: group, and a ``raster`` with no ``t`` is a single layer.
OUTPUT_KINDS: frozenset[str] = frozenset({"raster", "mesh", "vector", "scalar"})

#: The object basename a leg writes under its run prefix.
OUTPUTS_MANIFEST_BASENAME: str = "outputs.json"


# --------------------------------------------------------------------------- #
# WRITER: pure stdlib, mirrorable verbatim. NO pydantic on this path.
# --------------------------------------------------------------------------- #
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
    crs_authid: str | None = None,
    reference_time: str | None = None,
    dataset_group: str | None = None,
) -> dict[str, Any]:
    """Build ONE flat manifest entry dict.
    ``ValueError`` on an unrecognized ``kind`` or a missing required field - a
    typed reject at write time, never a silent drop. A ``None`` optional is
    OMITTED from the dict rather than written as null, so the object stays as
    small as the schema promises.
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
    # Render hints, written by a producer that ALREADY computed them, so a
    # consumer resolves the same extent and rescale without re-reading the file.
    # Absent, the consumer falls back to the workflow AOI and a lazy stats touch.
    if bbox is not None:
        entry["bbox"] = [float(v) for v in bbox]
    if band_stats is not None:
        entry["band_stats"] = dict(band_stats)
    # A SELAFIN mesh carries no CRS of its own, so a mesh entry states the EPSG
    # authority id here. It is per-run - the reach's UTM zone - so it cannot
    # live in a quantity-keyed registry. Rasters and vectors are self-describing.
    if crs_authid:
        entry["crs_authid"] = str(crs_authid)
    # A SELAFIN counts seconds from an origin it does not record, so a temporal
    # layer built from one reads its first step as 1900 unless the run states
    # when it began.
    if reference_time:
        entry["reference_time"] = str(reference_time)
    # A results mesh carries every variable the solve wrote, so the quantity the
    # entry is FILED under is not the field a reader is meant to see. This names
    # that field in the mesh reader's own spelling; absent, the quantity stands in.
    if dataset_group:
        entry["dataset_group"] = str(dataset_group)
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
    """Read the current array, append ``new``, return the WHOLE array serialized
    for one atomic-per-object PUT. ``existing_text`` is ``None`` or empty on the
    first frame, and the caller owns the object-store GET/PUT. ``ValueError`` on
    a foreign ``schema_version``: a writer never straddles two versions.
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
    # Append-only: a prior entry is never edited and never removed.
    entries.extend(new)
    data["schema_version"] = OUTPUTS_MANIFEST_SCHEMA_VERSION
    data["engine"] = engine
    data["run_id"] = run_id
    data["entries"] = entries
    return serialize(data)


def serialize(manifest: dict[str, Any]) -> str:
    """Serialize a manifest dict to a compact, stable JSON string."""
    return json.dumps(manifest, separators=(",", ":"), sort_keys=False)


# --------------------------------------------------------------------------- #
# READER: tolerant pydantic, the consuming side only.
# --------------------------------------------------------------------------- #
class _ReaderModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class OutputBandStats(_ReaderModel):
    """Optional per-file render stats a producer already computed.
    ``is_categorical`` and ``is_rgba`` short-circuit the palette and composite
    passthroughs; ``p2`` / ``p98`` drive the generic percentile rescale."""

    is_categorical: bool = False
    is_rgba: bool = False
    p2: float | None = None
    p98: float | None = None


class OutputEntry(_ReaderModel):
    """One ``entries[]`` row.
    Tolerant-read throughout: a producer that omits any optional field is
    byte-unchanged to a consumer that reads them."""

    kind: str
    quantity: str
    name: str
    uri: str
    #: Seconds from run start, ``None`` for a non-temporal artifact. The raw
    #: physical time only - a consumer maps it onto its own temporal stamps.
    t: float | None = None
    units: str | None = None
    #: Render hints, present when the producer precomputed them.
    bbox: list[float] | None = None
    band_stats: OutputBandStats | None = None
    #: A mesh entry's EPSG authority id; a SELAFIN sibling carries no CRS.
    crs_authid: str | None = None
    #: The instant a mesh entry's seconds are counted from, so a scrubber reads
    #: the run's own clock instead of 1900.
    reference_time: str | None = None
    #: The ONE group a mesh entry's preset paints, in the mesh reader's own
    #: spelling. Absent, the quantity stands in.
    dataset_group: str | None = None


class OutputsManifest(_ReaderModel):
    """The full ``outputs.json`` body (gated on ``schema_version``)."""

    schema_version: int
    engine: str = ""
    run_id: str = ""
    entries: list[OutputEntry] = []


def parse_outputs_manifest(text: str | bytes) -> OutputsManifest:
    """Parse and schema-gate an ``outputs.json`` body into a typed model.
    ``ValueError`` on a non-dict body, a missing or unknown ``schema_version``,
    or an entry whose ``kind`` is outside ``OUTPUT_KINDS`` - a hard reject the
    caller falls back from, never a best-guess parse.
    """
    if isinstance(text, (bytes, bytearray)):
        text = text.decode("utf-8")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("outputs.json must be a JSON object")
    sv = data.get("schema_version")
    if sv is None:
        raise ValueError("outputs.json missing schema_version")
    try:
        sv_int = int(sv)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"outputs.json schema_version is not an int: {sv!r}"
        ) from exc
    if sv_int != OUTPUTS_MANIFEST_SCHEMA_VERSION:
        raise ValueError(
            f"unknown outputs.json schema_version {sv!r} "
            f"(this agent build understands {OUTPUTS_MANIFEST_SCHEMA_VERSION})"
        )
    for raw in data.get("entries") or []:
        if isinstance(raw, dict) and raw.get("kind") not in OUTPUT_KINDS:
            raise ValueError(
                f"outputs.json entry kind {raw.get('kind')!r} not in "
                f"{sorted(OUTPUT_KINDS)}"
            )
    return OutputsManifest.model_validate(data)
