"""GRACE-2 Python sandbox executor harness (sprint-13 Stage 2 / job-0232).

This module is the entrypoint baked into the ``grace-2-python-sandbox`` Cloud Run
Job image. It receives a job payload — a ``python_code`` string plus a
``layer_refs`` mapping (LayerURI -> ``gs://`` path) — runs the user code under a
60-second wallclock cap with bounded output capture, auto-converts a final
``result`` variable to a JSON-serializable / chart-emission-shaped descriptor, and
prints a single JSON envelope ``{stdout, stderr, result, ...}`` to stdout.

The SAME module is the host-side local-subprocess fallback
(``services/agent/sandbox_runner.py`` with ``GRACE2_SANDBOX_LOCAL=1``) so dev/test
on a box with no docker daemon exercises identical harness logic.

Security model — TWO LAYERS, the VPC is the real boundary
---------------------------------------------------------
1. **VPC egress control (the real boundary).** In production the Cloud Run Job
   runs with a VPC connector + egress firewall that allows ONLY GCS
   (``restricted.googleapis.com`` Private Google Access range) + the MongoDB
   Atlas endpoint. Arbitrary internet egress is dropped at the network layer.
   This is provisioned in ``infra/python-sandbox.tf``; the harness CANNOT and does
   NOT claim to be that boundary.
2. **In-process socket guard (defense-in-depth, best-effort).** Before user code
   runs, the harness overrides ``socket.socket.connect`` /
   ``socket.create_connection`` (and clears proxy env vars) so image-local code
   that tries ``urllib``/``requests``/raw sockets to an arbitrary host is blocked
   with a ``SandboxNetworkBlocked`` error in-process. This is a usability/
   defense-in-depth layer that catches the common case early and produces a clean
   error in ``stderr`` — it is explicitly NOT a security boundary (a determined
   payload could rebind the C-level socket or shell out). The VPC layer is what
   actually contains a hostile payload.

The guard's allowlist is the set of host suffixes the legitimate runtime needs
(GCS endpoints + the Atlas host, supplied via ``GRACE2_SANDBOX_NET_ALLOW``). A
loopback connection (matplotlib's Agg backend, multiprocessing) is always allowed
because it never leaves the host.

Wallclock cap
-------------
The HARNESS itself does not fork the user code into a child — it runs it inline
under a ``SIGALRM`` watchdog (POSIX) so a runaway loop is interrupted at the cap.
The host-side ``sandbox_runner`` runs THIS module in a child subprocess and ALSO
imposes a subprocess-level ``communicate(timeout=...)`` hard kill, so even if the
in-process alarm is defeated (user code installs its own SIGALRM handler, or runs
a C extension that blocks signals), the outer subprocess wall-clock kill still
terminates the run. Belt and suspenders.

Output bounds
-------------
stdout/stderr from the user code are captured into in-memory buffers and truncated
to ``MAX_OUTPUT_CHARS`` each. The ``result`` descriptor is size-bounded too
(DataFrame rows capped, figure PNG base64 only emitted up to a byte ceiling).
"""

from __future__ import annotations

import base64
import builtins
import io
import json
import os
import signal
import sys
import traceback
from contextlib import redirect_stderr, redirect_stdout
from typing import Any

# --------------------------------------------------------------------------- #
# Bounds + config
# --------------------------------------------------------------------------- #

# Wallclock cap (seconds). Overridable via env for tests; the Cloud Run Job task
# timeout (infra/python-sandbox.tf) is a hard outer bound at 60s + buffer.
WALLCLOCK_CAP_SECONDS = int(os.environ.get("GRACE2_SANDBOX_TIMEOUT", "60"))

# Per-stream output truncation. Generous enough for a useful traceback / print
# debug, bounded enough that a `while True: print(...)` can't blow the envelope.
MAX_OUTPUT_CHARS = int(os.environ.get("GRACE2_SANDBOX_MAX_OUTPUT", "65536"))

# DataFrame -> records JSON row cap. A histogram-feeding frame is small; this
# stops a million-row frame from being serialized whole into the result.
MAX_DATAFRAME_ROWS = int(os.environ.get("GRACE2_SANDBOX_MAX_DF_ROWS", "5000"))

# Figure PNG byte ceiling (raw bytes, pre-base64). ~2 MB covers a dense default
# matplotlib figure; above this we emit a descriptor without the inline PNG.
MAX_FIGURE_PNG_BYTES = int(os.environ.get("GRACE2_SANDBOX_MAX_FIG_BYTES", str(2 * 1024 * 1024)))

# Total serialized-byte ceiling for the RESULT DESCRIPTOR's payload (job-0233
# FINDING 1). The per-kind caps above bound DataFrame ROWS, array SIZE, and PNG
# BYTES — but NOT the total serialized bytes of a JSON-native str/list/dict
# (e.g. ``result = "x" * 9_000_000`` or a deeply-nested dict). Without this cap a
# multi-megabyte JSON-native result would balloon the envelope, blow the host
# runner's MAX_ENVELOPE_BYTES bound, and corrupt the JSON on the way out. We cap
# the descriptor's serialized size and mark ``truncated=true`` HONESTLY (never a
# silent drop): an oversized scalar is hard-truncated with a marker; an oversized
# container is replaced by a typed too-large descriptor that still carries the
# size + a repr head so the agent can narrate the truncation truthfully.
MAX_RESULT_BYTES = int(os.environ.get("GRACE2_SANDBOX_MAX_RESULT_BYTES", str(2 * 1024 * 1024)))

# Host suffixes the in-process net guard permits (comma-separated). Defaults to
# the GCS + Atlas surface; the VPC layer is the real allowlist, this mirrors it
# so legitimate gcsfs reads aren't tripped by the guard in local mode.
DEFAULT_NET_ALLOW = (
    "googleapis.com,"          # storage.googleapis.com, restricted.googleapis.com
    "google.internal,"         # metadata server (ADC token mint)
    "mongodb.net,"             # MongoDB Atlas SRV hosts (*.mongodb.net)
    "localhost,127.0.0.1,::1"  # loopback (Agg backend, multiprocessing)
)
NET_ALLOW_SUFFIXES = tuple(
    s.strip().lower()
    for s in os.environ.get("GRACE2_SANDBOX_NET_ALLOW", DEFAULT_NET_ALLOW).split(",")
    if s.strip()
)

# Always-allowed loopback hosts (never leave the host regardless of allowlist).
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}

# --------------------------------------------------------------------------- #
# Envelope marker (Cloud Logging readback transport — sprint-13.5 job-0265)
# --------------------------------------------------------------------------- #
#
# In CLOUD mode the executor's stdout lands in Cloud Run logs, NOT a GCS object
# (the runtime SA is objectViewer-only and must not be widened — Invariant 5).
# The host-side ``read_sandbox_result`` reads the result envelope back from Cloud
# Logging filtered on the execution name PLUS this unambiguous marker prefix, so
# it never mistakes a user ``print`` of a ``{...}`` line (which the user code's
# captured stdout already truncates into the envelope's ``stdout`` field, but a
# defensive marker removes all ambiguity) for the result envelope.
#
# Wire format: the marker token, a single space, then the one-line JSON envelope:
#     GRACE2_SANDBOX_ENVELOPE_V1 {"status": "ok", ...}
# This is ONE log entry / ONE stdout line, so a single Cloud Logging textPayload
# filter pins it. The marker is ALSO embedded inside the JSON as
# ``_envelope_marker`` so a consumer that parses the JSON can assert provenance
# without re-matching the prefix. The local-subprocess parser
# (``sandbox_runner._parse_envelope``) tolerates the prefix by extracting from
# the first ``{`` — so the SAME emit path works for both transports.
ENVELOPE_MARKER = "GRACE2_SANDBOX_ENVELOPE_V1"


def _emit_envelope(envelope: dict[str, Any], stream: Any) -> None:
    """Print the result ``envelope`` as a single marker-prefixed JSON line.

    The marker prefix (``ENVELOPE_MARKER``) lets the host-side Cloud Logging
    readback pin the result line unambiguously; it is also stamped INSIDE the
    JSON as ``_envelope_marker`` for parse-side provenance. Both transports (the
    local subprocess that reads stdout directly and the cloud readback that reads
    Cloud Logging) consume this same line."""
    stamped = dict(envelope)
    stamped.setdefault("_envelope_marker", ENVELOPE_MARKER)
    print(f"{ENVELOPE_MARKER} {json.dumps(stamped)}", file=stream)


class SandboxTimeout(Exception):
    """User code exceeded the wallclock cap."""


class SandboxNetworkBlocked(Exception):
    """User code attempted a network connection to a non-allowlisted host."""


# --------------------------------------------------------------------------- #
# In-process network guard (defense-in-depth — NOT the security boundary)
# --------------------------------------------------------------------------- #


def _host_allowed(host: str | None) -> bool:
    if not host:
        return False
    h = str(host).strip().strip("[]").lower()
    if h in _LOOPBACK_HOSTS:
        return True
    for suffix in NET_ALLOW_SUFFIXES:
        if h == suffix or h.endswith("." + suffix) or h.endswith(suffix):
            return True
    return False


def install_network_guard() -> None:
    """Override socket connect paths + strip proxy env so image-local user code
    cannot trivially open a connection to a non-allowlisted host.

    Best-effort. Documented as defense-in-depth; the VPC egress firewall is the
    real boundary. We patch:
      - ``socket.socket.connect`` / ``connect_ex`` (covers raw sockets, the
        urllib/http.client/requests stack, which all bottom out here)
      - ``socket.create_connection`` (the high-level helper urllib3 uses)
    and clear ``*_proxy`` env vars so a proxy can't be used to tunnel out.
    """
    import socket as _socket

    # Strip proxy env so user code can't route through a proxy.
    for var in (
        "http_proxy", "https_proxy", "all_proxy", "ftp_proxy", "no_proxy",
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "FTP_PROXY", "NO_PROXY",
    ):
        os.environ.pop(var, None)

    _orig_connect = _socket.socket.connect
    _orig_connect_ex = _socket.socket.connect_ex
    _orig_create_connection = _socket.create_connection

    def _guarded_connect(self: Any, address: Any) -> Any:  # noqa: ANN401
        host = address[0] if isinstance(address, tuple) and address else None
        if not _host_allowed(host):
            raise SandboxNetworkBlocked(
                f"network egress to {host!r} blocked by sandbox guard "
                f"(allowlist: {', '.join(NET_ALLOW_SUFFIXES)})"
            )
        return _orig_connect(self, address)

    def _guarded_connect_ex(self: Any, address: Any) -> Any:  # noqa: ANN401
        host = address[0] if isinstance(address, tuple) and address else None
        if not _host_allowed(host):
            raise SandboxNetworkBlocked(
                f"network egress to {host!r} blocked by sandbox guard "
                f"(allowlist: {', '.join(NET_ALLOW_SUFFIXES)})"
            )
        return _orig_connect_ex(self, address)

    def _guarded_create_connection(address: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        host = address[0] if isinstance(address, tuple) and address else None
        if not _host_allowed(host):
            raise SandboxNetworkBlocked(
                f"network egress to {host!r} blocked by sandbox guard "
                f"(allowlist: {', '.join(NET_ALLOW_SUFFIXES)})"
            )
        return _orig_create_connection(address, *args, **kwargs)

    _socket.socket.connect = _guarded_connect  # type: ignore[method-assign]
    _socket.socket.connect_ex = _guarded_connect_ex  # type: ignore[method-assign]
    _socket.create_connection = _guarded_create_connection  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# Layer-ref injection — pre-opened gcsfs-backed handles
# --------------------------------------------------------------------------- #


def build_layer_handles(layer_refs: dict[str, Any]) -> dict[str, Any]:
    """Turn ``{layer_name: path}`` into pre-opened rasterio/geopandas handles.

    ``layer_refs`` accepts TWO value shapes (the ADDITIVE multi-frame extension):
      - a SINGLE path/URI string  -> one handle bound to ``var``
      - a LIST of frame paths     -> an ordered LIST of handles bound to ``var``
        (so a snippet can iterate animation frames: ``for f in frames: ...``)

    The agent pre-fetches every URI to a LOCAL file and rewrites the refs to those
    local paths BEFORE this runs (the jail is network-denied), so in production
    the values are local paths; a bare ``gs://``/``s3://`` URI still works in
    un-jailed local dev (rasterio's /vsi drivers).

    For each ref we sniff the extension and open the appropriate handle:
      - ``.tif`` / ``.tiff`` / ``.cog`` / ``.vrt``  -> ``rasterio.open``
      - ``.geojson`` / ``.json`` / ``.fgb`` /
        ``.gpkg`` / ``.shp`` / ``.parquet``         -> ``geopandas.read_file`` / ``read_parquet``
      - anything else                               -> the raw path/URI string

    Opening is best-effort and lazy-tolerant: a failed open hands back the raw
    string under the same key and records the error in ``_layer_errors``. We NEVER
    let a layer-open failure crash the harness — the user code decides what to do.

    The handles are exposed in the exec namespace under BOTH the layer name AND a
    ``layers`` dict so user code can index either way. A list-valued ref also
    exposes a ``<var>_uris`` alias (the ordered list of source strings) alongside
    the ``<var>_uri`` alias (the first frame, for single-frame fallbacks).
    """
    handles: dict[str, Any] = {}
    errors: dict[str, str] = {}

    for name, ref in (layer_refs or {}).items():
        var = _sanitize_var_name(name)
        if isinstance(ref, list):
            # Ordered frame set -> a list of handles (open failures degrade
            # per-frame to the raw string, recorded under name[i]).
            frame_handles: list[Any] = []
            for i, frame in enumerate(ref):
                try:
                    frame_handles.append(_open_layer(frame))
                except Exception as exc:  # noqa: BLE001 — never crash on a bad frame
                    frame_handles.append(frame)
                    errors[f"{name}[{i}]"] = f"{type(exc).__name__}: {exc}"
            handles[var] = frame_handles
            # Aliases: the ordered source list + the first frame's URI.
            handles[f"{var}_uris"] = list(ref)
            handles[f"{var}_uri"] = ref[0] if ref else None
        else:
            # Single ref (legacy, byte-identical) -> one handle.
            handles[f"{var}_uri"] = ref
            try:
                handles[var] = _open_layer(ref)
            except Exception as exc:  # noqa: BLE001 — never crash on a bad layer
                handles[var] = ref  # hand back the URI/path string
                errors[name] = f"{type(exc).__name__}: {exc}"

    handles["layers"] = {
        _sanitize_var_name(n): handles.get(_sanitize_var_name(n)) for n in (layer_refs or {})
    }
    # Two dict aliases over the same {original_name: staged-local-path} mapping:
    #   - ``layer_uris`` (the documented name), and
    #   - ``layer_refs`` (the tool PARAMETER name the model passed) -- agents
    #     reach for ``layer_refs[name]`` by instinct, so expose it as a working
    #     path dict too. Both let user code do ``rasterio.open(layer_refs[name])``
    #     when it prefers a path over the pre-opened ``<name>`` handle.
    handles["layer_uris"] = dict(layer_refs or {})
    handles["layer_refs"] = dict(layer_refs or {})
    if errors:
        handles["_layer_errors"] = errors
    return handles


def _sanitize_var_name(name: str) -> str:
    """Make a Python-identifier-safe variable name from a layer name/URI."""
    base = name.rsplit("/", 1)[-1]
    base = base.split(".", 1)[0]
    cleaned = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in base)
    if not cleaned or cleaned[0].isdigit():
        cleaned = f"layer_{cleaned}"
    return cleaned


def _open_layer(uri: str) -> Any:
    """Open one layer URI as a rasterio/geopandas handle (raises on failure)."""
    lower = uri.lower()
    raster_exts = (".tif", ".tiff", ".cog", ".vrt")
    vector_exts = (".geojson", ".json", ".fgb", ".gpkg", ".shp")
    if lower.endswith(raster_exts):
        import rasterio  # noqa: PLC0415 — optional, container-only

        # rasterio reads gs:// via the GDAL /vsigs/ driver.
        gdal_path = uri.replace("gs://", "/vsigs/") if uri.startswith("gs://") else uri
        return rasterio.open(gdal_path)
    if lower.endswith(".parquet"):
        import geopandas  # noqa: PLC0415

        return geopandas.read_parquet(uri)
    if lower.endswith(vector_exts):
        import geopandas  # noqa: PLC0415

        return geopandas.read_file(uri)
    # Unknown: hand back the URI string for the user to open as they wish.
    return uri


# --------------------------------------------------------------------------- #
# result auto-conversion
# --------------------------------------------------------------------------- #


def convert_result(result: Any) -> dict[str, Any]:
    """Convert a final ``result`` variable into a JSON-serializable descriptor.

    Returns a dict with a ``kind`` discriminator:
      - ``matplotlib.figure.Figure``  -> {"kind": "chart", "chart_emission": {...},
                                          "png_base64": "..."} where chart_emission
                                          is a ChartEmissionPayload-shaped dict IF
                                          grace2_contracts is importable, else a
                                          PNG-fallback descriptor.
      - ``pandas.DataFrame``          -> {"kind": "dataframe", "records": [...],
                                          "columns": [...], "row_count": N,
                                          "truncated": bool}
      - numpy scalar / array          -> {"kind": "scalar"/"array", "value": ...}
      - JSON-native (number/str/bool/
        None/list/dict)               -> {"kind": "json", "value": ...}
      - anything else                 -> {"kind": "repr", "value": repr(result)}

    Every descriptor is passed through :func:`_bound_result_descriptor` before
    return so its total serialized size is ``<= MAX_RESULT_BYTES`` (job-0233
    FINDING 1): an oversized JSON-native / repr result is HONESTLY truncated with
    a ``truncated=true`` marker rather than silently corrupting the envelope.
    """
    descriptor = _convert_result_inner(result)
    return _bound_result_descriptor(descriptor)


def _convert_result_inner(result: Any) -> dict[str, Any]:
    """The per-kind conversion (pre size-bounding). See :func:`convert_result`."""
    if result is None:
        return {"kind": "none", "value": None}

    # matplotlib Figure -> chart payload / PNG-fallback descriptor.
    fig = _as_figure(result)
    if fig is not None:
        return _convert_figure(fig)

    # pandas DataFrame / Series -> records JSON (row-capped).
    df_desc = _as_dataframe_descriptor(result)
    if df_desc is not None:
        return df_desc

    # numpy scalar / ndarray.
    np_desc = _as_numpy_descriptor(result)
    if np_desc is not None:
        return np_desc

    # JSON-native types.
    if isinstance(result, (bool, int, float, str)):
        return {"kind": "json", "value": result}
    if isinstance(result, (list, tuple, dict)):
        try:
            json.dumps(result)
            return {"kind": "json", "value": list(result) if isinstance(result, tuple) else result}
        except (TypeError, ValueError):
            return {"kind": "repr", "value": repr(result)[:MAX_OUTPUT_CHARS]}

    return {"kind": "repr", "value": repr(result)[:MAX_OUTPUT_CHARS]}


def _descriptor_size_bytes(descriptor: dict[str, Any]) -> int:
    """Serialized UTF-8 byte size of ``descriptor`` (the size on the wire)."""
    try:
        return len(json.dumps(descriptor).encode("utf-8"))
    except (TypeError, ValueError):
        # Non-serializable descriptor — shouldn't happen (every kind above is
        # JSON-native) but be defensive: treat as over-cap so it gets replaced.
        return MAX_RESULT_BYTES + 1


def _bound_result_descriptor(descriptor: dict[str, Any]) -> dict[str, Any]:
    """Cap the serialized size of ``descriptor`` at ``MAX_RESULT_BYTES`` (FINDING 1).

    The per-kind converters already bound DataFrame rows, array size, and PNG
    bytes; this is the FINAL safety rail for the categories they do NOT bound —
    a multi-megabyte JSON-native string, a giant nested list/dict, or a huge
    repr. Truncation is HONEST: the returned descriptor always carries
    ``truncated=true`` and an ``original_bytes`` count so the agent narrates the
    truncation truthfully (Decision H / Invariant 1 — never fabricate a complete
    result from a truncated one).

    Strategy by kind:
      - ``json``/``repr`` with a STRING value -> hard-truncate the string to fit
        the budget, append a ``...[truncated N bytes]`` marker, keep ``kind``.
      - any other oversized descriptor (list/dict ``json`` value, ``array``,
        ``dataframe`` that is still too big after the row cap, ``chart`` whose
        PNG slipped through) -> replace the payload with a typed
        ``too_large`` descriptor carrying the size + a bounded repr head, so the
        envelope stays small and the agent can explain what happened.
    """
    size = _descriptor_size_bytes(descriptor)
    if size <= MAX_RESULT_BYTES:
        return descriptor

    kind = descriptor.get("kind")
    value = descriptor.get("value")

    # String-valued json/repr: hard-truncate the string to fit the budget.
    if kind in ("json", "repr") and isinstance(value, str):
        # Budget for the string itself = total cap minus the descriptor's
        # non-string overhead (keys, markers). Compute against a worst-case
        # skeleton so the final dumps stays under MAX_RESULT_BYTES.
        skeleton = {
            "kind": kind,
            "value": "",
            "truncated": True,
            "original_bytes": size,
            "note": "result string exceeded MAX_RESULT_BYTES; truncated honestly",
        }
        overhead = _descriptor_size_bytes(skeleton) + 64  # 64B slack for the marker
        budget = max(MAX_RESULT_BYTES - overhead, 0)
        # Truncate on a UTF-8 codepoint boundary: encode, slice, decode-ignore.
        truncated_str = value.encode("utf-8")[:budget].decode("utf-8", "ignore")
        marker = f"...[truncated {len(value) - len(truncated_str)} chars]"
        return {
            "kind": kind,
            "value": truncated_str + marker,
            "truncated": True,
            "original_bytes": size,
            "note": "result string exceeded MAX_RESULT_BYTES; truncated honestly",
        }

    # Any other oversized descriptor: replace with a typed too-large descriptor
    # that still carries the original kind + a bounded repr head so the result is
    # never silently dropped (honest truncation).
    head = ""
    try:
        head = repr(value)[:1024] if value is not None else repr(descriptor)[:1024]
    except Exception:  # noqa: BLE001
        head = "<unrepresentable>"
    return {
        "kind": "too_large",
        "original_kind": kind,
        "value": None,
        "truncated": True,
        "original_bytes": size,
        "max_result_bytes": MAX_RESULT_BYTES,
        "repr_head": head,
        "note": (
            f"result of kind {kind!r} serialized to {size} bytes, exceeding the "
            f"{MAX_RESULT_BYTES}-byte cap; payload omitted (truncated honestly)"
        ),
    }


def _as_figure(obj: Any) -> Any:
    try:
        from matplotlib.figure import Figure  # noqa: PLC0415

        if isinstance(obj, Figure):
            return obj
        # An Axes -> grab its parent figure.
        if hasattr(obj, "figure") and isinstance(getattr(obj, "figure"), Figure):
            return obj.figure
    except Exception:  # noqa: BLE001
        return None
    return None


def _convert_figure(fig: Any) -> dict[str, Any]:
    """Render a matplotlib Figure to PNG and wrap it as a chart descriptor.

    If ``grace2_contracts.chart_contracts`` is importable we emit a
    ChartEmissionPayload-shaped dict (soft import per kickoff — job-0223 finalizes
    that schema concurrently; drift is reconciled by job-0233). The PNG is a
    fallback the web client can render directly when a Vega-Lite spec isn't
    available (a raw matplotlib figure has no Vega-Lite spec — it's a rasterized
    image, so the "chart payload" here is the PNG-fallback path the kickoff
    names, not a true Vega-Lite spec).
    """
    png_b64: str | None = None
    try:
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=100, bbox_inches="tight")
        raw = buf.getvalue()
        if len(raw) <= MAX_FIGURE_PNG_BYTES:
            png_b64 = base64.b64encode(raw).decode("ascii")
    except Exception as exc:  # noqa: BLE001
        return {"kind": "chart", "error": f"figure render failed: {exc}"}

    title = _figure_title(fig)
    descriptor: dict[str, Any] = {
        "kind": "chart",
        "title": title,
        "png_base64": png_b64,
        "png_truncated": png_b64 is None,
    }

    # Soft import: shape a ChartEmissionPayload-compatible dict if the contract
    # is importable. We DON'T construct the pydantic model (a raw figure has no
    # Vega-Lite spec; ChartEmissionPayload requires a structurally-valid spec), so
    # we emit the descriptor fields the agent (job-0233) maps onto the envelope:
    # title + a PNG image mark wrapped in a minimal Vega-Lite image spec so the
    # payload is structurally valid if the agent forwards it verbatim.
    try:
        import grace2_contracts.chart_contracts as _cc  # noqa: PLC0415, F401

        if png_b64 is not None:
            vega_image_spec = {
                "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
                "title": title,
                "data": {"values": [{"img": f"data:image/png;base64,{png_b64}"}]},
                "mark": {"type": "image", "width": 320, "height": 240},
                "encoding": {"url": {"field": "img", "type": "nominal"}},
            }
            descriptor["chart_emission"] = {
                "title": title,
                "vega_lite_spec": vega_image_spec,
                "caption": "matplotlib figure (PNG-fallback render)",
            }
            descriptor["chart_contract_available"] = True
    except Exception:  # noqa: BLE001 — contract not importable in this image
        descriptor["chart_contract_available"] = False

    return descriptor


def _figure_title(fig: Any) -> str:
    try:
        if getattr(fig, "_suptitle", None) is not None and fig._suptitle.get_text():
            return fig._suptitle.get_text()
        for ax in fig.get_axes():
            t = ax.get_title()
            if t:
                return t
    except Exception:  # noqa: BLE001
        pass
    return "Sandbox figure"


def _as_dataframe_descriptor(obj: Any) -> dict[str, Any] | None:
    try:
        import pandas as pd  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    if isinstance(obj, pd.Series):
        obj = obj.to_frame()
    if not isinstance(obj, pd.DataFrame):
        return None
    total = len(obj)
    truncated = total > MAX_DATAFRAME_ROWS
    head = obj.head(MAX_DATAFRAME_ROWS)
    # to_dict("records") + a JSON round-trip to coerce numpy/np.datetime to native.
    try:
        records = json.loads(head.to_json(orient="records", date_format="iso"))
    except Exception:  # noqa: BLE001
        records = head.astype(str).to_dict("records")
    return {
        "kind": "dataframe",
        "columns": [str(c) for c in obj.columns],
        "records": records,
        "row_count": int(total),
        "returned_rows": int(len(head)),
        "truncated": truncated,
    }


def _as_numpy_descriptor(obj: Any) -> dict[str, Any] | None:
    try:
        import numpy as np  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    if isinstance(obj, np.generic):
        return {"kind": "scalar", "value": obj.item()}
    if isinstance(obj, np.ndarray):
        if obj.size <= MAX_DATAFRAME_ROWS:
            return {"kind": "array", "shape": list(obj.shape), "value": obj.tolist()}
        return {
            "kind": "array",
            "shape": list(obj.shape),
            "value": None,
            "truncated": True,
            "note": f"array of size {obj.size} exceeds cap {MAX_DATAFRAME_ROWS}; value omitted",
        }
    return None


# --------------------------------------------------------------------------- #
# Bounded buffer + wallclock watchdog
# --------------------------------------------------------------------------- #


class _BoundedStringIO(io.StringIO):
    """StringIO that stops growing past ``MAX_OUTPUT_CHARS`` (keeps the head)."""

    def __init__(self, cap: int = MAX_OUTPUT_CHARS) -> None:
        super().__init__()
        self._cap = cap
        self._truncated = False

    def write(self, s: str) -> int:  # type: ignore[override]
        cur = self.tell()
        if cur >= self._cap:
            self._truncated = True
            return len(s)
        remaining = self._cap - cur
        if len(s) > remaining:
            self._truncated = True
            super().write(s[:remaining])
            return len(s)
        return super().write(s)

    @property
    def truncated(self) -> bool:
        return self._truncated


def _install_alarm(seconds: int) -> bool:
    """Install a SIGALRM watchdog. Returns True if installed (POSIX), else False.

    The handler raises SandboxTimeout from inside the user-code call stack, which
    the harness catches. On non-POSIX (no SIGALRM) we return False and rely on the
    outer subprocess timeout (sandbox_runner) for the wallclock kill.
    """
    if not hasattr(signal, "SIGALRM"):
        return False

    def _on_alarm(signum: int, frame: Any) -> None:  # noqa: ANN401, ARG001
        raise SandboxTimeout(f"user code exceeded {seconds}s wallclock cap")

    signal.signal(signal.SIGALRM, _on_alarm)
    signal.alarm(seconds)
    return True


def _cancel_alarm() -> None:
    if hasattr(signal, "SIGALRM"):
        signal.alarm(0)


# --------------------------------------------------------------------------- #
# Core run
# --------------------------------------------------------------------------- #


def run_user_code(
    python_code: str,
    layer_refs: dict[str, str] | None = None,
    *,
    install_guard: bool = True,
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    """Execute ``python_code`` under the sandbox harness; return the result envelope.

    Envelope shape:
        {
          "stdout": "<captured stdout, truncated>",
          "stderr": "<captured stderr, truncated>",
          "result": <result descriptor from convert_result()>,
          "status": "ok" | "error" | "timeout" | "blocked",
          "error": "<message>" | None,
          "stdout_truncated": bool,
          "stderr_truncated": bool,
          "wallclock_cap_seconds": int,
          "layer_errors": {<name>: <msg>}  # only if any layer failed to open
        }
    """
    cap = timeout_seconds if timeout_seconds is not None else WALLCLOCK_CAP_SECONDS

    if install_guard:
        install_network_guard()

    # matplotlib must use a non-interactive backend in a headless container.
    os.environ.setdefault("MPLBACKEND", "Agg")

    handles = build_layer_handles(layer_refs or {})
    layer_errors = handles.pop("_layer_errors", None)

    # Build the exec namespace. We expose builtins + the layer handles + a place
    # for `result`. We do NOT attempt to build a restricted builtins sandbox here
    # (that is not a real security boundary in CPython); containment is the VPC +
    # the Cloud Run Job's resource caps + the read-only SA.
    namespace: dict[str, Any] = {
        "__builtins__": builtins,
        "__name__": "__grace2_sandbox__",
    }
    namespace.update(handles)

    out_buf = _BoundedStringIO()
    err_buf = _BoundedStringIO()

    status = "ok"
    error_msg: str | None = None
    result_descriptor: dict[str, Any] = {"kind": "none", "value": None}

    alarm_installed = _install_alarm(cap)
    try:
        with redirect_stdout(out_buf), redirect_stderr(err_buf):
            exec(compile(python_code, "<sandbox>", "exec"), namespace)  # noqa: S102
        result_descriptor = convert_result(namespace.get("result"))
    except SandboxTimeout as exc:
        status = "timeout"
        error_msg = str(exc)
    except SandboxNetworkBlocked as exc:
        status = "blocked"
        error_msg = str(exc)
        err_buf.write(f"\nSandboxNetworkBlocked: {exc}\n")
    except SystemExit as exc:  # user called sys.exit / exit()
        status = "ok" if (exc.code in (0, None)) else "error"
        if status == "error":
            error_msg = f"SystemExit({exc.code})"
    except BaseException as exc:  # noqa: BLE001 — capture ALL user-code failures
        status = "error"
        error_msg = f"{type(exc).__name__}: {exc}"
        err_buf.write("\n" + "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    finally:
        if alarm_installed:
            _cancel_alarm()

    envelope: dict[str, Any] = {
        "stdout": out_buf.getvalue(),
        "stderr": err_buf.getvalue(),
        "result": result_descriptor,
        "status": status,
        "error": error_msg,
        "stdout_truncated": out_buf.truncated,
        "stderr_truncated": err_buf.truncated,
        "wallclock_cap_seconds": cap,
    }
    if layer_errors:
        envelope["layer_errors"] = layer_errors
    return envelope


# --------------------------------------------------------------------------- #
# Payload loading (Cloud Run Job env / args / GCS staging)
# --------------------------------------------------------------------------- #


def load_payload(argv: list[str] | None = None) -> dict[str, Any]:
    """Load the job payload from (in priority order):

    1. ``--payload-file <path>`` CLI arg (a local JSON file — the local fallback
       path used by ``sandbox_runner`` in GRACE2_SANDBOX_LOCAL mode).
    2. ``GRACE2_SANDBOX_PAYLOAD_URI`` env — a ``gs://`` JSON staging file the
       Cloud Run Job reads via google-cloud-storage.
    3. ``GRACE2_SANDBOX_PAYLOAD`` env — an inline JSON string (small payloads).

    Payload schema:
        {"python_code": "<str>", "layer_refs": {"<name>": "gs://..."}}
    """
    argv = list(sys.argv[1:] if argv is None else argv)

    # 1. --payload-file
    if "--payload-file" in argv:
        idx = argv.index("--payload-file")
        path = argv[idx + 1]
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    # 2. gs:// staging file
    gs_uri = os.environ.get("GRACE2_SANDBOX_PAYLOAD_URI", "").strip()
    if gs_uri:
        return _load_payload_from_gcs(gs_uri)

    # 3. inline JSON env
    inline = os.environ.get("GRACE2_SANDBOX_PAYLOAD", "").strip()
    if inline:
        return json.loads(inline)

    raise ValueError(
        "no sandbox payload found: pass --payload-file, or set "
        "GRACE2_SANDBOX_PAYLOAD_URI (gs://) or GRACE2_SANDBOX_PAYLOAD (inline JSON)"
    )


def _load_payload_from_gcs(gs_uri: str) -> dict[str, Any]:
    from google.cloud import storage  # noqa: PLC0415 — container-only

    if not gs_uri.startswith("gs://"):
        raise ValueError(f"not a gs:// URI: {gs_uri!r}")
    bucket_name, _, blob_name = gs_uri[len("gs://") :].partition("/")
    client = storage.Client(project=os.environ.get("GCP_PROJECT"))
    blob = client.bucket(bucket_name).blob(blob_name)
    return json.loads(blob.download_as_text())


def main(argv: list[str] | None = None) -> int:
    """Container entrypoint: load payload, run, print the JSON envelope to stdout.

    The result JSON is written to a FRESH stdout (we swap the real stdout aside
    during user-code capture, so the only thing on the process's real stdout is
    the single envelope line — the host-side runner parses exactly that).
    """
    real_stdout = sys.stdout
    try:
        payload = load_payload(argv)
    except Exception as exc:  # noqa: BLE001
        _emit_envelope(
            {"status": "error", "error": f"payload load failed: {exc}",
             "stdout": "", "stderr": "", "result": {"kind": "none", "value": None}},
            real_stdout,
        )
        return 1

    code = payload.get("python_code", "")
    layer_refs = payload.get("layer_refs", {}) or {}
    if not isinstance(code, str) or not code.strip():
        _emit_envelope(
            {"status": "error", "error": "payload.python_code missing or empty",
             "stdout": "", "stderr": "", "result": {"kind": "none", "value": None}},
            real_stdout,
        )
        return 1

    envelope = run_user_code(code, layer_refs)

    # Emit exactly one marker-prefixed JSON line on the real stdout. The marker
    # lets the cloud readback pin this line in Cloud Logging; the local runner
    # extracts the JSON from the first ``{`` so the SAME line works for both.
    _emit_envelope(envelope, real_stdout)
    real_stdout.flush()
    # Process exit code: 0 on ok, non-zero on a harness-level failure category so
    # the Cloud Run Job execution status reflects the outcome. We DO NOT treat a
    # user-code error as a Job failure (max_retries would re-run identical code) —
    # only timeout/blocked/error-loading map to non-zero.
    return 0 if envelope["status"] in ("ok", "error") else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
