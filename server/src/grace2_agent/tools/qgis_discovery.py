"""QGIS capability discovery atomic tools — Level 1a (FR-AS-9, FR-TA-2).

This module registers the two algorithm-discovery tools that, together with
the ``qgis_process`` pass-through (job-0032 ``passthroughs.py``), implement
the *capability discovery Level 1a* loop described in SRS FR-AS-9::

    list_qgis_algorithms  →  describe_qgis_algorithm  →  qgis_process

The agent uses this triple to handle queries that don't match a pre-wired
typed wrapper — enumerate candidate algorithms, learn the signature of a
chosen candidate, then invoke it. With 1000+ algorithms across native QGIS +
GDAL + GRASS + SAGA providers, exhaustively wrapping the catalog is
out-of-scope; the discovery loop is the substitute.

Both tools are cacheable under the FR-DC-2 ``static-30d`` class with
``source_class="qgis_algorithms_catalog"``. The catalog rarely changes — only
on a QGIS Server / worker image rebuild (~1× per quarter at most). When the
worker image rotates, the lifecycle policy (job-0031) will evict the cache
within 30 days and the next call re-fetches.

Worker substrate (Option B per kickoff Decisions)
-------------------------------------------------

The kickoff frames two options:

* **Option A** — extend the deployed PyQGIS worker (``grace-2-pyqgis-worker``
  Cloud Run Job, image @sha256:fffd7e0f) to handle ``{command: "list_algorithms"}``
  and ``{command: "describe_algorithm", algorithm_id: ...}`` payloads. This
  touches the worker image; ``services/workers/**`` is FROZEN for this job.

* **Option B** — wrap the existing worker substrate via a one-shot Python
  script that runs ``qgis_process list`` / ``qgis_process help <alg>`` and
  returns the result, without touching the worker image.

The kickoff resolves to **Option B**. Implementation note: the deployed
worker's container entrypoint is ``python3 -m services.workers.pyqgis`` with
fixed argparse semantics (``--qgs-uri`` required), and the Cloud Run Jobs v2
``RunJob.Overrides`` API supports overriding ``args`` but NOT ``command`` at
exec time. Running ``qgis_process`` against the deployed Job therefore
requires either (a) an ``UpdateJob`` mutation of ``template.containers[0]``
(rejected at sandbox layer as mutating the deployed shared Job), or (b) a
sibling Cloud Run Job with ``command=qgis_process`` (infra, FROZEN), or (c)
a worker-side command-surface extension (worker code, FROZEN).

**Option B′ (this job's pragmatic resolution):** the submitter wired into
``set_worker_submitter(...)`` runs ``qgis_process`` as a subprocess. In the
dev environment that's the local ``~/miniforge3/envs/grace2/bin/qgis_process``
(QGIS 3.40.3-Bratislava per PROJECT_STATE.md / job-0022). In production this
seam will route to the deployed worker substrate once the Cloud Run Jobs
override surface is sorted — see Open Question OQ-34-WORKER-DISCOVERY-SUBSTRATE
in this job's report. The catalog shape is stable across the QGIS 3.x line,
so the substitution is materially equivalent for the M4 discovery loop.

The substrate seam is the ``_WORKER_SUBMITTER`` module variable in
``passthroughs.py`` (job-0032's DI hook). This module imports the seam at
call time so the submitter binding can be changed via
``set_worker_submitter`` without touching this module.

TTL choice (TENTATIVE per kickoff)
----------------------------------

``static-30d`` for both tools — the algorithm catalog changes only on a
container image rebuild (FR-DC-2 boundary: "static-30d for upstream catalogs
that change on a quarterly or longer rhythm"). Alternative ``semi-static-7d``
would be over-fetching: the deployed worker image is digest-pinned in
``infra/worker.tf`` and only rotates via an explicit ``tofu apply``. Surfaced
as OQ-34-DISCOVERY-TTL-CLASS in the report.

Return shapes (kickoff: dicts with documented keys, no new pydantic models)
--------------------------------------------------------------------------

Per the kickoff "Do NOT add new pydantic models (FROZEN packages/contracts)",
both tools return plain ``dict`` shapes:

* ``list_qgis_algorithms`` -> ``list[QGISAlgorithmSummary]`` where the
  ``QGISAlgorithmSummary`` TypedDict has::

      {algorithm_id: str, name: str, provider: str, brief_description: str}

* ``describe_qgis_algorithm`` -> ``QGISAlgorithmDescription`` TypedDict::

      {algorithm_id: str, name: str, description: str,
       parameters: list[QGISAlgorithmParameter],
       outputs: list[QGISAlgorithmOutput],
       raw_help: str}

  where ``raw_help`` carries the full unparsed ``qgis_process help`` text so a
  future QGIS major-version change doesn't break the tool — the agent can
  still read the raw text. Parameter parsing is intentionally tolerant:
  unknown sections are skipped without raising (TENTATIVE per kickoff OQ on
  parameter-parsing tolerance).

Invariants honored
------------------

* **Invariant 1 (Determinism boundary):** algorithm enumeration is
  deterministic at the catalog-version layer (a given worker image always
  produces the same algorithm list). The ``static-30d`` cache amplifies that
  determinism across a 30-day window.
* **Invariant 8 (Cancellation is first-class):** the subprocess invocation
  honors the ``timeout_s`` argument and raises ``subprocess.TimeoutExpired``
  which propagates through the agent's WebSocket cancel chain. No separate
  cancel mechanism is introduced.
* **FR-CE-8 (fail-fast registration):** ``@register_tool`` validates the
  metadata at import time; an invalid registration raises before the agent
  service starts.
* **FR-AS-9 Level 1a:** this module completes the discovery triple alongside
  ``passthroughs.qgis_process``.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, TypedDict

from grace2_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from .cache import read_through

__all__ = [
    "list_qgis_algorithms",
    "describe_qgis_algorithm",
    "QGISAlgorithmSummary",
    "QGISAlgorithmDescription",
    "QGISAlgorithmParameter",
    "QGISAlgorithmOutput",
    "MAX_LIST_RESULTS",
    "SOURCE_CLASS",
    "CURATED_ALLOWLIST",
    "curated_allowlist",
]

logger = logging.getLogger("grace2_agent.tools.qgis_discovery")

#: Max results returned per ``list_qgis_algorithms`` call (FR-TA-2 prose:
#: "returns at most ~50 results per call to keep responses focused").
MAX_LIST_RESULTS = 50

#: Bucket prefix under ``cache/static-30d/`` for the discovery cache.
SOURCE_CLASS = "qgis_algorithms_catalog"

# ---------------------------------------------------------------------------
# Curated allowlist (job-0308 Q-discovery lane).
#
# A bare ``qgis_process list`` on the deployed worker image surfaces ~695
# algorithms across native QGIS + GDAL + GRASS + SAGA. Handing all 695 to the
# LLM is illegible: most are niche transforms the agent never needs, and the
# noise crowds out the high-value families. The curated allowlist trims the
# default surface to the families that earn their place in a hazard-modeling
# workbench: the native QGIS Processing core, the full GDAL raster/vector
# toolbox, the QGIS-prefixed legacy algorithms, the GRASS hydrology set the
# watershed/stream-delineation roadmap leans on, and a small slice of SAGA.
#
# Matching is by provider PREFIX (the part before the ``:`` in an
# ``algorithm_id``) for the wildcard families, plus an explicit set of
# fully-qualified ids for the curated GRASS hydrology / SAGA picks (so we get
# the watershed tools without dragging in all ~300 GRASS algorithms).
#
# The agent always has an ESCAPE HATCH: ``list_qgis_algorithms(include_all=
# True)`` (or the ``GRACE2_QGIS_ALLOWLIST=all`` env flip) returns the full
# unfiltered catalog. The curated set is a default for legibility, not a
# capability ceiling.
# ---------------------------------------------------------------------------

#: Provider prefixes whose algorithms pass the curated allowlist wholesale.
_CURATED_PROVIDER_PREFIXES: frozenset[str] = frozenset(
    {
        "native",  # QGIS native C++ Processing core (the workhorse)
        "gdal",  # GDAL/OGR raster + vector toolbox
        "qgis",  # legacy QGIS-prefixed Processing algorithms
        "3d",  # QGIS 3D (tessellate etc.) - small, high-signal
    }
)

#: Fully-qualified ids curated in from otherwise-excluded providers: the
#: GRASS hydrology set the watershed/stream-network roadmap depends on, plus a
#: few high-value SAGA terrain/hydrology picks. Kept explicit so we surface the
#: watershed tools without flooding the LLM with all ~300 GRASS algorithms.
_CURATED_EXPLICIT_IDS: frozenset[str] = frozenset(
    {
        # GRASS hydrology core (watershed + stream delineation).
        "grass:r.watershed",
        "grass:r.water.outlet",
        "grass:r.stream.extract",
        "grass:r.stream.order",
        "grass:r.stream.snap",
        "grass:r.fill.dir",
        "grass:r.flow",
        "grass:r.lake",
        "grass:r.basins.fill",
        # Key SAGA terrain/hydrology picks.
        "saga:fillsinkswangliu",
        "saga:channelnetwork",
        "saga:catchmentarea",
        "saga:flowaccumulationtopdown",
        "saga:slopeaspectcurvature",
    }
)

#: Module-level curated allowlist of fully-qualified ids (the explicit GRASS /
#: SAGA picks). Provider-prefix families are matched separately at filter time
#: via ``_CURATED_PROVIDER_PREFIXES``. Exposed as a module constant so tests
#: (and a future ops audit) can introspect the curated surface.
CURATED_ALLOWLIST: frozenset[str] = _CURATED_EXPLICIT_IDS


def curated_allowlist() -> tuple[frozenset[str], frozenset[str]]:
    """Resolve the effective curated allowlist (env-overridable).

    Returns ``(provider_prefixes, explicit_ids)``. The agent default is the
    module constants above; ops can override via ``GRACE2_QGIS_ALLOWLIST``:

    - ``GRACE2_QGIS_ALLOWLIST=all`` (or ``*``) -> sentinel: both sets empty,
      which ``_apply_curated_allowlist`` reads as "return everything" (the
      same effect as the ``include_all=True`` call-site escape hatch).
    - ``GRACE2_QGIS_ALLOWLIST=native:*,gdal:*,grass:r.watershed,...`` -> a
      comma-separated mix of ``<provider>:*`` prefix wildcards and
      fully-qualified ids replaces the built-in curated set entirely.
    - unset -> the built-in curated set.
    """
    raw = (os.environ.get("GRACE2_QGIS_ALLOWLIST") or "").strip()
    if not raw:
        return _CURATED_PROVIDER_PREFIXES, _CURATED_EXPLICIT_IDS
    if raw.lower() in ("all", "*"):
        # Sentinel: empty sets => no curation (return everything).
        return frozenset(), frozenset()
    prefixes: set[str] = set()
    explicit: set[str] = set()
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if tok.endswith(":*"):
            # ``<provider>:*`` -> provider-prefix wildcard.
            prefixes.add(tok[: -len(":*")])
        elif tok.endswith("*") and ":" not in tok:
            # Bare ``<provider>*`` -> provider-prefix wildcard.
            prefixes.add(tok[:-1])
        elif tok.endswith("*"):
            # ``<provider>:<stem>*`` (e.g. ``gdal:aspect*``) -> an id-PREFIX
            # match (handled in ``_apply_curated_allowlist``). Without this
            # branch a token like ``gdal:aspect*`` was neither a wildcard nor an
            # exact id, so it matched NOTHING. Keep the trailing ``*`` so the
            # matcher recognizes the entry as a prefix rather than an exact id.
            explicit.add(tok)
        else:
            explicit.add(tok)
    return frozenset(prefixes), frozenset(explicit)


def _provider_prefix(algorithm_id: str) -> str:
    """Return the provider prefix of a fully-qualified algorithm id.

    ``"native:zonalstatistics"`` -> ``"native"``; ``"gdal:aspect"`` ->
    ``"gdal"``. Ids without a ``:`` return the whole string (tolerant).
    """
    return algorithm_id.split(":", 1)[0] if ":" in algorithm_id else algorithm_id


def _apply_curated_allowlist(
    summaries: list[QGISAlgorithmSummary],
) -> list[QGISAlgorithmSummary]:
    """Filter summaries down to the curated allowlist (legibility default).

    Keeps an algorithm when its provider prefix is in the curated prefix set
    OR its fully-qualified id is in the curated explicit-id set OR its id starts
    with one of the curated id-PREFIX entries (an explicit token that ended in a
    trailing ``*``, e.g. ``gdal:aspect*`` -> keeps ``gdal:aspect``,
    ``gdal:aspectband``). When the resolved allowlist is the ``all`` sentinel
    (both sets empty) the full list passes through unchanged.
    """
    prefixes, explicit_ids = curated_allowlist()
    if not prefixes and not explicit_ids:
        return summaries  # "all" sentinel - no curation.
    # Split explicit entries into exact ids and trailing-* id-prefix stems.
    exact_ids = {e for e in explicit_ids if not e.endswith("*")}
    id_prefixes = tuple(e[:-1] for e in explicit_ids if e.endswith("*"))

    def _keep(alg_id: str) -> bool:
        if _provider_prefix(alg_id) in prefixes:
            return True
        if alg_id in exact_ids:
            return True
        return any(alg_id.startswith(p) for p in id_prefixes)

    return [s for s in summaries if _keep(s["algorithm_id"])]

#: Subprocess timeout for ``qgis_process list`` — typically completes in 2-3 s
#: locally; deployed worker may take longer through Cloud Run Job cold-start.
LIST_TIMEOUT_S = 120

#: Subprocess timeout for ``qgis_process help <alg>`` — small, fast.
HELP_TIMEOUT_S = 60


# ---------------------------------------------------------------------------
# Result TypedDicts (per kickoff: dicts, no new pydantic models).
# ---------------------------------------------------------------------------


class QGISAlgorithmSummary(TypedDict):
    """A single entry returned by ``list_qgis_algorithms``."""

    algorithm_id: str  # e.g. "native:zonalstatistics"
    name: str  # human-readable label
    provider: str  # e.g. "QGIS", "GDAL", "GRASS"
    brief_description: str  # one-line summary (the provider's display name)


class QGISAlgorithmParameter(TypedDict):
    """A parameter entry in the description's ``parameters`` list."""

    name: str  # parameter slot name, e.g. "INPUT_RASTER"
    label: str  # human label, e.g. "Raster layer"
    type: str  # argument type, e.g. "raster", "vector", "enum"
    description: str  # parsed acceptable-values block, joined
    default: str | None  # parsed "Default value" if present


class QGISAlgorithmOutput(TypedDict):
    """An output entry in the description's ``outputs`` list."""

    name: str
    type: str
    description: str


class QGISAlgorithmDescription(TypedDict):
    """Result of ``describe_qgis_algorithm`` for a single algorithm."""

    algorithm_id: str
    name: str
    description: str
    parameters: list[QGISAlgorithmParameter]
    outputs: list[QGISAlgorithmOutput]
    raw_help: str  # full unparsed text — fallback for tolerant agents


# ---------------------------------------------------------------------------
# Metadata definitions (module-level so tests can introspect without
# triggering the registration decorator).
# ---------------------------------------------------------------------------


_LIST_METADATA = AtomicToolMetadata(
    name="list_qgis_algorithms",
    ttl_class="static-30d",
    source_class=SOURCE_CLASS,
    cacheable=True,
)

_DESCRIBE_METADATA = AtomicToolMetadata(
    name="describe_qgis_algorithm",
    ttl_class="static-30d",
    source_class=SOURCE_CLASS,
    cacheable=True,
)


# ---------------------------------------------------------------------------
# Subprocess invocation seam.
#
# We deliberately do NOT import the submitter binding at module load — the
# binding is set at agent service startup via ``set_worker_submitter`` (job-
# 0032 DI seam) and may be unbound during tests. Import at call time so test
# fixtures can swap it.
# ---------------------------------------------------------------------------


def _get_worker_submitter():
    """Return the current ``_WORKER_SUBMITTER`` binding from passthroughs.

    Imported lazily so tests can swap it without import-order coupling. Raises
    ``RuntimeError`` if the binding is unset (an unbound submitter is a
    configuration error, surfaced fast per FR-CE-8).
    """
    from . import passthroughs

    submitter = passthroughs._WORKER_SUBMITTER
    if submitter is None:
        raise RuntimeError(
            "QGIS discovery tool invoked but worker submitter is not bound; "
            "agent service startup should call set_worker_submitter(...) "
            "before any discovery call."
        )
    return submitter


# ---------------------------------------------------------------------------
# Parsing — qgis_process list output.
#
# Format (QGIS 3.40 / 3.44, stable across the 3.x line):
#
#     Available algorithms
#
#     QGIS (3D)
#         3d:tessellate    Tessellate
#
#     GDAL
#         gdal:aspect    Aspect
#         gdal:assignprojection    Assign projection
#         ...
#
# Provider headers are unindented lines that don't contain a tab + colon-id;
# algorithm lines are TAB-indented and shaped ``<id>\t<name>`` (the separator
# is one tab between id and label; a second tab can appear as padding).
# ---------------------------------------------------------------------------


_ALG_LINE_RE = re.compile(r"^\t+([a-zA-Z0-9_.]+:[a-zA-Z0-9_.]+)\t+(.+)$")
_PROVIDER_LINE_RE = re.compile(r"^(?!\t)([A-Z][^\n]*?)\s*$")
_HEADER_BLACKLIST = {"Available algorithms"}


def _parse_qgis_list_output(stdout: str) -> list[QGISAlgorithmSummary]:
    """Parse ``qgis_process list`` stdout into a flat algorithm list.

    Tolerant of unknown sections — lines that don't match the algorithm or
    provider regexes are skipped. ``qgis_process`` emits Qt warnings to
    stderr (display server hints, etc.) which are not captured here.
    """
    summaries: list[QGISAlgorithmSummary] = []
    current_provider = "Unknown"
    for raw_line in stdout.splitlines():
        if not raw_line.strip():
            continue
        alg_m = _ALG_LINE_RE.match(raw_line)
        if alg_m:
            alg_id, label = alg_m.group(1), alg_m.group(2).strip()
            summaries.append(
                {
                    "algorithm_id": alg_id,
                    "name": label,
                    "provider": current_provider,
                    "brief_description": label,
                }
            )
            continue
        # Provider header — unindented line.
        if not raw_line.startswith("\t"):
            stripped = raw_line.strip()
            if stripped in _HEADER_BLACKLIST:
                continue
            # Some provider headers carry a parenthetical, e.g. "QGIS (3D)".
            # Skip lines that look like warnings: starting with "Warning:" or
            # "inotify".
            if stripped.startswith(("Warning:", "inotify", "qt.qpa")):
                continue
            current_provider = stripped
    return summaries


# ---------------------------------------------------------------------------
# Parsing — qgis_process help <algorithm_id> output.
#
# Format::
#
#     <human label> (<algorithm_id>)
#
#     ----------------
#     Description
#     ----------------
#     <description text, possibly multi-paragraph>
#
#     ----------------
#     Arguments
#     ----------------
#
#     PARAM_NAME: Human label
#         Default value:    <value>
#         Argument type:    <type>
#         Acceptable values:
#             - line 1
#             - line 2
#     NEXT_PARAM: ...
#
#     ----------------
#     Outputs
#     ----------------
#
#     OUTPUT_NAME: Human label <output_type>
# ---------------------------------------------------------------------------


_HEADER_RE = re.compile(r"^-{3,}\s*$")
_PARAM_NAME_RE = re.compile(r"^([A-Z][A-Z0-9_]*):\s*(.*)$")
_PARAM_FIELD_RE = re.compile(r"^\t([A-Za-z ]+):\s*(.*)$")
_PARAM_VALUE_BULLET_RE = re.compile(r"^\t\t-\s*(.+)$")


def _parse_qgis_help_output(  # noqa: C901 — parser is intentionally linear
    stdout: str, algorithm_id: str
) -> QGISAlgorithmDescription:
    """Parse ``qgis_process help <id>`` into a structured description.

    Tolerant of unknown sections: any section header beyond Description /
    Arguments / Outputs is recorded under ``raw_help`` only. The agent can
    read ``raw_help`` directly if the parser missed something.
    """
    lines = stdout.splitlines()
    # Drop warning prelude.
    while lines and lines[0].strip().startswith(("Warning:", "inotify", "qt.qpa")):
        lines.pop(0)

    # Title: first non-empty line of the form "Label (algorithm_id)".
    title = ""
    title_idx = 0
    for i, line in enumerate(lines):
        if line.strip():
            title = line.strip()
            title_idx = i
            break
    # Extract label: everything before " (algorithm_id)" if present.
    name_label = title
    title_match = re.match(r"^(.*) \((" + re.escape(algorithm_id) + r")\)\s*$", title)
    if title_match:
        name_label = title_match.group(1)

    # Walk the rest, accumulating sections by header name.
    sections: dict[str, list[str]] = {}
    current_section: str | None = None
    in_header = False
    i = title_idx + 1
    while i < len(lines):
        line = lines[i]
        # Section header: a line of dashes (>=3) bounds the previous header
        # and the next header. We use a tiny state machine: when we hit a
        # dashes line, the next non-blank line is the header, then another
        # dashes line closes it.
        if _HEADER_RE.match(line):
            in_header = not in_header
            i += 1
            continue
        if in_header:
            current_section = line.strip()
            sections.setdefault(current_section, [])
            i += 1
            continue
        if current_section is not None:
            sections[current_section].append(line)
        i += 1

    # Description block — concatenate non-empty lines.
    description = "\n".join(
        ln.strip() for ln in sections.get("Description", []) if ln.strip()
    )

    # Arguments block — parse parameter slots.
    arg_lines = sections.get("Arguments", [])
    parameters = _parse_arguments_block(arg_lines)

    # Outputs block — parse output slots (same shape as arguments).
    out_lines = sections.get("Outputs", [])
    outputs = _parse_outputs_block(out_lines)

    return {
        "algorithm_id": algorithm_id,
        "name": name_label,
        "description": description,
        "parameters": parameters,
        "outputs": outputs,
        "raw_help": stdout,
    }


def _parse_arguments_block(lines: list[str]) -> list[QGISAlgorithmParameter]:
    """Parse the ``Arguments`` section into a list of parameter dicts.

    Tolerant of unrecognized field labels — anything beyond ``Default value``
    and ``Argument type`` lands in ``description`` (joined).
    """
    parameters: list[QGISAlgorithmParameter] = []
    current: dict[str, Any] | None = None

    def _close() -> None:
        nonlocal current
        if current is not None:
            parameters.append(_finalize_param(current))
            current = None

    for line in lines:
        if not line.strip():
            continue
        # New parameter — column 0 starts with PARAM_NAME: label.
        if not line.startswith("\t"):
            m = _PARAM_NAME_RE.match(line)
            if m:
                _close()
                current = {
                    "name": m.group(1),
                    "label": m.group(2).strip(),
                    "type": "",
                    "default": None,
                    "_value_lines": [],
                    "_misc_lines": [],
                }
            continue
        if current is None:
            continue
        # Indented tab line — field or bullet.
        bullet_m = _PARAM_VALUE_BULLET_RE.match(line)
        if bullet_m:
            current["_value_lines"].append(bullet_m.group(1).strip())
            continue
        field_m = _PARAM_FIELD_RE.match(line)
        if field_m:
            field_name = field_m.group(1).strip()
            field_val = field_m.group(2).strip()
            if field_name == "Default value":
                # Empty values appear as a bare colon with no value.
                current["default"] = field_val if field_val else None
            elif field_name == "Argument type":
                current["type"] = field_val
            elif field_name == "Acceptable values":
                # Bullets follow on subsequent lines; nothing to record here.
                pass
            else:
                # Tolerant fallback: keep around for the description.
                current["_misc_lines"].append(f"{field_name}: {field_val}")
            continue
        # Unrecognized line — append to misc for raw preservation.
        current["_misc_lines"].append(line.strip())

    _close()
    return parameters


def _finalize_param(d: dict[str, Any]) -> QGISAlgorithmParameter:
    """Compose the final ``QGISAlgorithmParameter`` from working state."""
    parts: list[str] = []
    if d["_value_lines"]:
        parts.append("Acceptable values: " + "; ".join(d["_value_lines"]))
    if d["_misc_lines"]:
        parts.append("; ".join(d["_misc_lines"]))
    description = " | ".join(parts)
    return {
        "name": d["name"],
        "label": d["label"],
        "type": d["type"],
        "description": description,
        "default": d["default"],
    }


def _parse_outputs_block(lines: list[str]) -> list[QGISAlgorithmOutput]:
    """Parse the ``Outputs`` section.

    qgis_process formats outputs as ``NAME: <label> <output_type>``. We
    extract the trailing ``<...>`` as type when present; otherwise the whole
    line is the label.
    """
    outputs: list[QGISAlgorithmOutput] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        m = re.match(r"^([A-Z][A-Z0-9_]*):\s*(.*)$", stripped)
        if not m:
            continue
        name = m.group(1)
        rest = m.group(2).strip()
        # Trailing "<...>" is the output type marker.
        type_m = re.search(r"<([^>]+)>\s*$", rest)
        if type_m:
            out_type = type_m.group(1)
            label = rest[: type_m.start()].strip()
        else:
            out_type = ""
            label = rest
        outputs.append(
            {
                "name": name,
                "type": out_type,
                "description": label,
            }
        )
    return outputs


# ---------------------------------------------------------------------------
# Registered tools.
# ---------------------------------------------------------------------------


@register_tool(
    _LIST_METADATA,
    # Annotations: readOnlyHint=True (queries QGIS Server capabilities; no
    # state mutation), openWorldHint=False (calls intra-GCP QGIS Server WMS
    # GetCapabilities; not an external public API), destructiveHint=False,
    # idempotentHint=True (same capabilities response for same server state).
)
def list_qgis_algorithms(
    category_filter: str | None = None,
    search_terms: str | None = None,
    include_all: bool = False,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> list[QGISAlgorithmSummary]:
    """Enumerate QGIS Processing algorithms available on the worker substrate.

    Use this when: the agent's typed-wrapper tools (``run_storm_surge_flood``,
    ``clip_to_basin``, etc.) don't cover the user's request and the agent
    needs to discover a candidate Processing algorithm to chain through
    ``qgis_process``. Implements FR-AS-9 Level 1a (capability discovery) when
    paired with ``describe_qgis_algorithm`` and ``qgis_process``.

    Do NOT use this for: finding the right pre-wired typed wrapper (use the
    agent's tool registry); discovering hazard layers (use
    ``hazard_catalog_search``).

    Curated default:
        The deployed worker exposes ~695 algorithms across native QGIS + GDAL +
        GRASS + SAGA. By default this returns only a CURATED set of high-value
        families (native QGIS Processing core, the GDAL raster/vector toolbox,
        legacy ``qgis:*`` algorithms, the GRASS hydrology set
        (``r.watershed`` / ``r.water.outlet`` / ``r.stream.extract`` /
        ``r.fill.dir`` etc.) and key SAGA terrain/hydrology picks) so the
        candidate list stays legible. Pass ``include_all=True`` to see the
        full unfiltered catalog (or set ``GRACE2_QGIS_ALLOWLIST=all`` ops-side).

    Params:
        category_filter: optional substring matched case-insensitively
            against the provider name (e.g. ``"native"``, ``"gdal"``,
            ``"grass"``). Pass ``None`` to enumerate across all providers.
        search_terms: optional substring matched case-insensitively against
            the algorithm id and human label. Pass ``None`` to skip
            full-text filtering. Useful for narrowing 1000+ entries to a
            handful relevant to the task.
        include_all: when ``True``, bypass the curated allowlist and return the
            full unfiltered catalog (still capped + ranked). Default ``False``
            (curated). Use this only when the curated set demonstrably lacks
            the algorithm you need.

    Returns:
        A list of ``QGISAlgorithmSummary`` dicts (``algorithm_id``, ``name``,
        ``provider``, ``brief_description``). Capped at ``MAX_LIST_RESULTS``
        (50) per FR-TA-2 prose; the ranking is "matching entries first,
        sorted by provider then algorithm_id".

    Caching:
        ``ttl_class="static-30d"``, ``source_class="qgis_algorithms_catalog"``.
        The algorithm catalog only changes on a worker image rebuild
        (~quarterly); a 30-day TTL is comfortable. Cache hits return the same
        bytes without re-invoking the worker. The curated allowlist is applied
        AFTER the cache read (a pure post-filter), so a single cached raw
        listing serves both curated and ``include_all`` calls.

    Substrate:
        Wraps ``qgis_process list`` via the worker submitter bound at agent
        service startup. See module docstring for the Option B / Option B′
        discussion.
    """
    # Cache params — what the agent passes, deterministically canonicalized.
    # NOTE: the curated allowlist is intentionally NOT part of the cache key;
    # it is a pure post-filter over the same cached raw listing.
    cache_params: dict[str, Any] = {
        "subcommand": "list",
    }

    def _fetch() -> bytes:
        submitter = _get_worker_submitter()
        result = submitter(["list"], LIST_TIMEOUT_S)
        # Submitter contract: returns a dict with ``stdout`` (str) at minimum.
        stdout = result.get("stdout", "")
        return stdout.encode("utf-8")

    rt = read_through(
        _LIST_METADATA,
        cache_params,
        ext="txt",
        fetch_fn=_fetch,
    )
    stdout = rt.data.decode("utf-8", errors="replace")
    summaries = _parse_qgis_list_output(stdout)
    total_raw = len(summaries)

    # Curated allowlist (legibility default) unless the caller escapes to all.
    if not include_all:
        summaries = _apply_curated_allowlist(summaries)
    curated_n = len(summaries)

    # Filter + rank.
    filtered = _filter_and_rank_summaries(summaries, category_filter, search_terms)
    logger.info(
        "list_qgis_algorithms cache_hit=%s total=%d curated=%d filtered=%d "
        "include_all=%s category=%r search=%r",
        rt.hit,
        total_raw,
        curated_n,
        len(filtered),
        include_all,
        category_filter,
        search_terms,
    )
    return filtered[:MAX_LIST_RESULTS]


def _filter_and_rank_summaries(
    summaries: list[QGISAlgorithmSummary],
    category_filter: str | None,
    search_terms: str | None,
) -> list[QGISAlgorithmSummary]:
    """Apply category + search filtering and sort by provider then id."""
    if category_filter:
        needle = category_filter.lower()
        summaries = [s for s in summaries if needle in s["provider"].lower()]
    if search_terms:
        needle = search_terms.lower()
        # Score: hits in id + name (any token in needle). Keep matching first.
        matching = [
            s for s in summaries
            if needle in s["algorithm_id"].lower() or needle in s["name"].lower()
        ]
        non_matching = [s for s in summaries if s not in matching]
        return sorted(matching, key=lambda s: (s["provider"], s["algorithm_id"])) + sorted(
            non_matching, key=lambda s: (s["provider"], s["algorithm_id"])
        )
    return sorted(summaries, key=lambda s: (s["provider"], s["algorithm_id"]))


@register_tool(
    _DESCRIBE_METADATA,
    # Annotations: readOnlyHint=True (queries QGIS Server algorithm details;
    # no state mutation), openWorldHint=False (intra-GCP QGIS Server only),
    # destructiveHint=False, idempotentHint=True (deterministic algorithm
    # description for same algorithm id on same server).
)
def describe_qgis_algorithm(algorithm_id: str, **_extra_ignored: Any) -> QGISAlgorithmDescription:
    """Describe a single QGIS Processing algorithm's signature.

    Use this when: ``list_qgis_algorithms`` surfaced a candidate algorithm
    id and the agent now needs to know its parameter names, types,
    acceptable values, and outputs in order to construct a valid
    ``qgis_process`` call. Implements the middle hop of FR-AS-9 Level 1a.

    Do NOT use this for: enumerating algorithms (use
    ``list_qgis_algorithms``); invoking the algorithm (use
    ``qgis_process``); inferring the canonical typed wrapper for a hazard
    (engine-owned workflows are the right path when they cover the case).

    Params:
        algorithm_id: the fully qualified Processing algorithm id, e.g.
            ``"native:zonalstatistics"``. Must include the provider prefix.

    Returns:
        A ``QGISAlgorithmDescription`` dict (``algorithm_id``, ``name``,
        ``description``, ``parameters`` list, ``outputs`` list, and
        ``raw_help`` carrying the full unparsed help text — a tolerance
        hatch for future QGIS versions whose help format the parser doesn't
        recognize).

    Caching:
        ``ttl_class="static-30d"``, ``source_class="qgis_algorithms_catalog"``.

    Substrate:
        Wraps ``qgis_process help <algorithm_id>`` via the worker submitter
        bound at agent service startup.
    """
    cache_params: dict[str, Any] = {
        "subcommand": "help",
        "algorithm_id": algorithm_id,
    }

    def _fetch() -> bytes:
        submitter = _get_worker_submitter()
        result = submitter(["help", algorithm_id], HELP_TIMEOUT_S)
        stdout = result.get("stdout", "")
        return stdout.encode("utf-8")

    rt = read_through(
        _DESCRIBE_METADATA,
        cache_params,
        ext="txt",
        fetch_fn=_fetch,
    )
    stdout = rt.data.decode("utf-8", errors="replace")
    description = _parse_qgis_help_output(stdout, algorithm_id)
    logger.info(
        "describe_qgis_algorithm cache_hit=%s id=%s params=%d outputs=%d",
        rt.hit,
        algorithm_id,
        len(description["parameters"]),
        len(description["outputs"]),
    )
    return description
