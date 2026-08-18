"""The ``outputs.json`` emit-on-solve manifest -- writer + typed reader.

The append-only manifest a solver leg writes under its run prefix so the
emission seam can publish entries as they land. Companion to
``docs/design/outputs-manifest-schema.md`` (the frozen schema) and
``docs/design/emission.md`` (the seam's folder).

Entry shape (flat, role-free -- NATE ruling): ``{kind, quantity, name, uri,
t?, units?}``. The wrapper carries the version marker:
``{schema_version, engine, run_id, entries: [...]}``.

TWO surfaces, ONE ``schema_version`` gate (the ``publish_manifest`` /
``output_quantities`` precedent, forced by the deploy boundary: the WORKER
images ship ``workers/**`` but NOT ``contracts``; the AGENT ships
``contracts`` but NOT ``workers``):

  * The WRITER half (``new_manifest`` / ``build_entry`` / ``append_entries`` /
    ``serialize``) is PURE STDLIB -- no pydantic, no engine deps -- so it is
    importable from BOTH the host-exec agent path (MODFLOW/SWMM run in the
    agent process, recon gotcha #2) AND a verbatim worker mirror
    (``workers/_raster_postprocess/outputs_manifest.py``, gated on the same
    ``OUTPUTS_MANIFEST_SCHEMA_VERSION``). The docker-worker path imports the
    mirror; the host-exec path imports THIS module.
  * The READER half (``OutputEntry`` / ``OutputsManifest`` /
    ``parse_outputs_manifest``) is tolerant pydantic (``extra="ignore"``),
    agent-side only -- the seam's consumer.
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

#: The ONE schema_version both the writer and the reader understand. A worker
#: MIRROR module gates on this exact value. Bumping it is a coordinated
#: worker-image + agent redeploy (a worker image is pinned to one version for
#: its whole life -- an unknown version NEVER happens on the write side).
OUTPUTS_MANIFEST_SCHEMA_VERSION: int = 1

#: The seam's routing keys (Section 1). Temporality rides ``t``, NOT a distinct
#: kind: a ``raster`` with a ``t`` that shares a ``quantity`` with its siblings
#: forms a temporal group; a ``raster`` with no ``t`` is a single layer.
OUTPUT_KINDS: frozenset[str] = frozenset({"raster", "mesh", "vector", "scalar"})

#: The object basename a leg writes under its run prefix.
OUTPUTS_MANIFEST_BASENAME: str = "outputs.json"


# --------------------------------------------------------------------------- #
# WRITER (pure stdlib -- worker-mirrorable; NO pydantic on this path).
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
) -> dict[str, Any]:
    """Build ONE flat manifest entry dict (``{kind, quantity, name, uri, t?,
    units?}`` plus the OPTIONAL render-hint fields ``bbox?`` / ``band_stats?`` /
    ``crs_authid?``).

    Raises ``ValueError`` on an unrecognized ``kind`` (a typed reject at write
    time, never a silent drop -- Section 6) or a missing required field. ``t`` /
    ``units`` are omitted from the dict (absent, not null) when ``None`` so the
    object stays as small as the schema promises.

    RENDER-HINT AMENDMENT (ADR 0280 EXECUTED, schema_version 1): ``bbox`` (the
    per-COG EPSG:4326 ``[minlon,minlat,maxlon,maxlat]``) and ``band_stats``
    (``{is_categorical, is_rgba, p2, p98}``) are OPTIONAL fields a producer that
    ALREADY computed them (every docker raster worker does) writes so the seam
    resolves the SAME bbox + rescale the register-only fast path did WITHOUT a
    COG re-read. Absent (host-exec engines that don't precompute) the seam
    degrades to the workflow AOI bbox + a lazy per-COG stats touch. They are the
    minimal set the byte-equivalence bar (Section 7.1 lists bbox + band stats)
    needs; the flat ``{kind,quantity,name,uri,t,units}`` core is unchanged and
    still the only REQUIRED shape. All are omitted from the dict when ``None``.

    CRS AMENDMENT (ADR 0283, schema_version 1): ``crs_authid`` is an OPTIONAL EPSG
    authority id (``"EPSG:32616"``) a ``kind="mesh"`` entry carries, because a
    SELAFIN mesh sibling carries NO CRS of its own -- the plugin's ``_add_mesh``
    sets ``QgsMeshLayer.setCrs`` from this field (0116). It is per-run (the reach's
    UTM zone), so it cannot live in the quantity->style registry; it rides the
    entry. Absent for raster/vector entries (their COGs are self-describing).
    Tolerant-read: an old producer that omits it is byte-unchanged.
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
    if crs_authid:
        entry["crs_authid"] = str(crs_authid)
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
    the pure array manipulation so BOTH the worker and host-exec paths share it
    verbatim. Entries are appended in order; a prior entry is never edited or
    removed (immutable-once-written).

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


# --------------------------------------------------------------------------- #
# READER (tolerant pydantic -- agent-side; the seam's consumer).
# --------------------------------------------------------------------------- #
class _ReaderModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class OutputBandStats(_ReaderModel):
    """Optional per-COG render stats a producer precomputed (ADR 0280 amendment).

    Mirrors the register-only path's ``band_stats``: ``is_categorical`` /
    ``is_rgba`` short-circuit the palette / composite passthroughs and
    ``p2`` / ``p98`` feed the generic percentile rescale (an UNREGISTERED
    quantity's neutral ramp) -- so the seam never re-reads the COG when the
    producer already computed them.
    """

    is_categorical: bool = False
    is_rgba: bool = False
    p2: float | None = None
    p98: float | None = None


class OutputEntry(_ReaderModel):
    """One ``entries[]`` row (Section 1).

    ``t`` is seconds-from-run-start (``None`` for a non-temporal artifact). The
    seam maps a bare ``t`` to Temporal-Controller stamps; the entry carries only
    the raw physical time.

    ``bbox`` (per-COG EPSG:4326) and ``band_stats`` are the OPTIONAL render-hint
    fields (ADR 0280 EXECUTED amendment): present when the producer precomputed
    them (docker raster workers), absent for host-exec engines (the seam then
    uses the workflow bbox + a lazy stats touch). ``crs_authid`` is the OPTIONAL
    EPSG authority id a ``kind="mesh"`` entry carries (ADR 0283): a SELAFIN sibling
    has no CRS, so the seam threads this onto the mesh ``LayerURI`` for the
    plugin's ``QgsMeshLayer.setCrs``. Tolerant-read: an old producer that omits any
    of them is byte-unchanged.
    """

    kind: str
    quantity: str
    name: str
    uri: str
    t: float | None = None
    units: str | None = None
    bbox: list[float] | None = None
    band_stats: OutputBandStats | None = None
    crs_authid: str | None = None


class OutputsManifest(_ReaderModel):
    """The full ``outputs.json`` body (gated on ``schema_version``)."""

    schema_version: int
    engine: str = ""
    run_id: str = ""
    entries: list[OutputEntry] = []


def parse_outputs_manifest(text: str | bytes) -> OutputsManifest:
    """Parse + schema-gate an ``outputs.json`` body into a typed model.

    Raises ``ValueError`` on a non-dict body, a missing ``schema_version``, an
    UNKNOWN ``schema_version``, or an entry carrying a ``kind`` outside
    ``OUTPUT_KINDS`` -- the READ-side hard reject (Section 4: fall back to
    completion-only, never a best-guess parse). A known-version, well-kinded
    body validates into ``OutputsManifest``.
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
