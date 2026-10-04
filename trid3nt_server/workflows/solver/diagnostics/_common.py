"""Shared internals for the ``read_run_diagnostics`` dispatcher.

The typed errors, the normalized parser return type and the artifact seam, kept
out of ``__init__`` so a parser imports them without a circular import.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

__all__ = [
    "DiagnosticsError",
    "RunHandleUnresolved",
    "DiagnosticsRunNotFound",
    "DiagnosticsEngineUnknown",
    "DiagnosticsArtifactMissing",
    "DiagnosticsParseError",
    "EngineDiagnostics",
    "RunArtifacts",
    "basename_of",
]


# Typed errors (convention: class attrs ``error_code`` + ``retryable``)


class DiagnosticsError(RuntimeError):
    """Base class for ``read_run_diagnostics`` failures."""

    error_code: str = "DIAGNOSTICS_ERROR"
    retryable: bool = False


class RunHandleUnresolved(DiagnosticsError):
    """No run-id ULID could be recovered from the ``run_handle``."""

    error_code = "RUN_HANDLE_UNRESOLVED"
    retryable = False


class DiagnosticsRunNotFound(DiagnosticsError):
    """No ``completion.json`` at the resolved run prefix / fixture directory."""

    error_code = "DIAGNOSTICS_RUN_NOT_FOUND"
    retryable = False


class DiagnosticsEngineUnknown(DiagnosticsError):
    """Engine identity is not recoverable from the completion manifest."""

    error_code = "DIAGNOSTICS_ENGINE_UNKNOWN"
    retryable = False


class DiagnosticsArtifactMissing(DiagnosticsError):
    """completion.json is present but a REQUIRED diagnostics file is absent.
    Carries engine, run_id and the offending filename, never a healthy envelope."""

    error_code = "DIAGNOSTICS_ARTIFACT_MISSING"
    retryable = True

    def __init__(self, engine: str, run_id: str, filename: str, detail: str = "") -> None:
        self.engine = engine
        self.run_id = run_id
        self.filename = filename
        msg = (
            f"[{engine}] run {run_id}: required diagnostics artifact "
            f"{filename!r} is missing from the run outputs"
        )
        if detail:
            msg += f" ({detail})"
        super().__init__(msg)


class DiagnosticsParseError(DiagnosticsError):
    """A diagnostics file was found but could not be parsed.
    Raised rather than downgraded to a fabricated healthy result."""

    error_code = "DIAGNOSTICS_PARSE_ERROR"
    retryable = False

    def __init__(self, engine: str, run_id: str, filename: str, detail: str) -> None:
        self.engine = engine
        self.run_id = run_id
        self.filename = filename
        super().__init__(
            f"[{engine}] run {run_id}: could not parse diagnostics artifact "
            f"{filename!r}: {detail}"
        )




@dataclass
class EngineDiagnostics:
    """The normalized diagnostics a per-engine parser produces.
    Every scalar defaults to ``None`` - a value the engine does not report is null,
    never invented - and the raw fields stay authoritative over ``healthy``."""

    mass_balance_pct: float | None = None
    mass_balance_source: str | None = None  # "reported" | "derived" | None
    instability: Any | None = None
    nonconverged_pct: float | None = None
    dry_cells: int | None = None
    healthy: bool | None = None
    warnings: list[str] = field(default_factory=list)
    engine_specific: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    diagnostics_files: list[str] = field(default_factory=list)




def basename_of(uri: str) -> str:
    """Last path segment of a uri / path (``s3://b/<id>/_output/g.txt`` -> ``g.txt``)."""
    return uri.rstrip("/").rsplit("/", 1)[-1]


class RunArtifacts:
    """Reader over a run's completion.json + diagnostics files.
    ``run_dir`` reads a local directory by basename with zero network, ``reader``
    reads ``s3://`` bytes; a parser asks by basename or suffix and cannot tell."""

    def __init__(
        self,
        completion: dict[str, Any],
        *,
        engine: str,
        run_id: str,
        run_dir: str | None = None,
        reader: Callable[[str], bytes] | None = None,
    ) -> None:
        self.completion = completion
        self.engine = engine
        self.run_id = run_id
        self._run_dir = run_dir
        self._reader = reader
        self.output_uris: list[str] = list(completion.get("output_uris") or [])

    def _read_local(self, name: str) -> bytes | None:
        import os

        path = os.path.join(self._run_dir or "", name)
        if not os.path.exists(path):
            return None
        with open(path, "rb") as fh:
            return fh.read()

    def try_read_uri(self, uri: str) -> bytes | None:
        """Bytes for one uri, or ``None`` when the artifact is absent."""
        if self._run_dir is not None:
            return self._read_local(basename_of(uri))
        if self._reader is None:
            return None
        try:
            return self._reader(uri)
        except Exception:  # noqa: BLE001 -- absent/unreadable -> honest None
            return None

    def find_output_uri(
        self, *, basename: str | None = None, suffix: str | None = None
    ) -> str | None:
        """First ``output_uris`` entry matching an exact basename or a suffix."""
        for uri in self.output_uris:
            bn = basename_of(uri)
            if basename is not None and bn == basename:
                return uri
            if suffix is not None and bn.endswith(suffix):
                return uri
        return None

    def read_output_optional(
        self, *, basename: str | None = None, suffix: str | None = None
    ) -> tuple[str | None, bytes | None]:
        """Locate + read an OPTIONAL output; ``(None, None)`` when absent."""
        uri = self.find_output_uri(basename=basename, suffix=suffix)
        if uri is None:
            return None, None
        return uri, self.try_read_uri(uri)
