"""Host-side dispatch shim for the Python sandbox (job-0232).

``submit_sandbox_job(python_code, layer_refs)`` is the agent-side entry point the
``code_exec_request`` tool (job-0233) calls. On the AWS stack it has a single
mode:

* **Local-subprocess execution.** Runs the ``executor.py`` harness in a child
  ``python`` subprocess on this machine — no docker daemon, no cloud control
  plane needed. The legacy GCP cloud path (a ``grace-2-python-sandbox`` Cloud Run
  Job staging its payload to GCS + a Cloud Logging result readback, OQ-SANDBOX-3
  option (b)) was removed in the GCP decommission; the EC2 stack has no Cloud Run
  / Cloud Logging, so the local-subprocess path is the only executor.
  ``run_sandbox_local`` enforces the 60s wallclock cap (via the executor's
  in-process SIGALRM watchdog) AND an outer subprocess hard-kill at cap+grace, and
  the same output bounds (the executor truncates; the runner caps the JSON it
  parses), then returns the parsed result envelope directly (synchronous).

Why the executor is invoked as a subprocess (not imported) even in local mode
-----------------------------------------------------------------------------
1. The 60s wallclock cap + the in-process net guard MONKEYPATCH process-global
   state (``socket.socket.connect``, proxy env). Running it inline in the agent
   process would poison the agent's own socket stack. A child process is disposed
   after each run — clean isolation.
2. The outer ``subprocess`` hard-kill (``communicate(timeout=...)`` + ``kill()``)
   is a belt-and-suspenders wallclock bound that survives even if user code
   installs its own SIGALRM handler or blocks signals in a C extension. An inline
   call has no such outer bound.
3. ``executor.py`` lives in ``infra/python-sandbox/`` (the container build
   context), NOT on the agent's import path — running it by file path keeps that
   single source of truth for the harness logic without copying it into the agent
   package.

The executor module is located by walking up from this file to the repo root and
joining ``infra/python-sandbox/executor.py``; an env override
(``GRACE2_SANDBOX_EXECUTOR``) lets tests / the container point elsewhere.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import sandbox_hardening

LOG = logging.getLogger("grace2.agent.sandbox_runner")


class SandboxCloudModeUnavailable(RuntimeError):
    """Cloud-mode sandbox dispatch/readback is unavailable.

    Retained as an importable typed error after the GCP decommission removed the
    cloud sandbox path (the Cloud Run Job dispatch + the Cloud Logging result
    readback, OQ-SANDBOX-3 option (b)). The EC2 stack has no Cloud Run / Cloud
    Logging, so the only executor is the local-subprocess path
    (:func:`run_sandbox_local`), which reads the child's stdout directly and
    returns a complete result envelope synchronously.

    ``error_code`` / ``retryable`` follow the FR-AS-11 typed-exception convention
    so ``summarize_tool_result`` surfaces a structured function_response to the
    model (narrate the limitation honestly; do NOT retry an identical cloud
    dispatch — there is none).
    """

    error_code: str = "SANDBOX_CLOUD_MODE_UNAVAILABLE"
    retryable: bool = False


class SandboxResultNotFound(RuntimeError):
    """A sandbox result envelope could not be found.

    Retained as an importable typed error after the GCP decommission removed the
    Cloud Logging result readback that originally raised it. ``retryable=True`` is
    preserved for back-compat with the FR-AS-11 typed-exception convention; the
    agent must NOT re-DISPATCH code on this error (that would double-run a
    user-confirmed snippet).
    """

    error_code: str = "SANDBOX_RESULT_NOT_FOUND"
    retryable: bool = True

# Wallclock cap (seconds) — matches infra/python-sandbox.tf's 60s Job timeout and
# the executor's GRACE2_SANDBOX_TIMEOUT. The runner's OUTER subprocess timeout is
# this + a grace window so the executor's own SIGALRM fires first (cleaner error:
# status="timeout" with captured partial output) and the outer kill is the
# backstop only.
WALLCLOCK_CAP_SECONDS = int(os.environ.get("GRACE2_SANDBOX_TIMEOUT", "60"))
# Grace window added to the outer subprocess timeout so the in-process alarm wins
# the race in the normal case (and the executor can flush its JSON envelope).
SUBPROCESS_GRACE_SECONDS = int(os.environ.get("GRACE2_SANDBOX_SUBPROC_GRACE", "10"))
# Max bytes of the child's stdout we will read/parse. The executor truncates its
# own stdout/stderr fields; this is the outer bound on the JSON envelope line.
MAX_ENVELOPE_BYTES = int(os.environ.get("GRACE2_SANDBOX_MAX_ENVELOPE_BYTES", str(8 * 1024 * 1024)))

# Envelope marker prefix the executor stamps on its result line — MUST stay in
# lockstep with ``infra/python-sandbox/executor.py``'s ``ENVELOPE_MARKER``. The
# executor lives in the container build context (not on the agent import path),
# so the constant is duplicated here rather than imported. The local parser uses
# it to tolerate the prefix on the child's stdout envelope line. A drift would
# break the prefix tolerance, so a unit test asserts the two literals match.
SANDBOX_ENVELOPE_MARKER = "GRACE2_SANDBOX_ENVELOPE_V1"


def _is_local_mode() -> bool:
    """True — the sandbox always dispatches via the local-subprocess path.

    The GCP decommission removed the cloud sandbox path (Cloud Run Job dispatch +
    Cloud Logging readback), so local-subprocess is the only executor. Retained as
    a function (and always ``True``) so the historical ``GRACE2_SANDBOX_LOCAL`` env
    knob and callers/tests that reference it still resolve to the live behaviour.
    """
    return True


def _repo_root() -> Path:
    """Walk up from this file to the repo root (the dir containing ``infra/``)."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "infra" / "python-sandbox" / "executor.py").exists():
            return parent
    # Fallback: four levels up from services/agent/src/grace2_agent/sandbox_runner.py
    return here.parents[4]


def _executor_path() -> Path:
    override = os.environ.get("GRACE2_SANDBOX_EXECUTOR", "").strip()
    if override:
        return Path(override)
    return _repo_root() / "infra" / "python-sandbox" / "executor.py"


# --------------------------------------------------------------------------- #
# Result + handle shapes
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SandboxExecutionHandle:
    """VESTIGIAL pending-result handle for the (removed) cloud sandbox dispatch.

    Retained as an importable dataclass after the GCP decommission removed the
    cloud sandbox path (Cloud Run Job dispatch + Cloud Logging readback). Nothing
    constructs it in product code anymore — ``submit_sandbox_job`` always runs the
    local-subprocess path and returns a result envelope dict directly. The shape
    is kept for back-compat with callers that ``isinstance``-branch on it.
    """

    handle_id: str
    execution_name: str
    payload_uri: str
    result_uri: str
    submitted_at: datetime
    mode: str = "cloud"


# --------------------------------------------------------------------------- #
# Local-subprocess execution — reuses executor.py
# --------------------------------------------------------------------------- #


def _hard_kill(proc: subprocess.Popen, *, group: bool) -> None:
    """Hard-kill a timed-out child (process-group-wide when it is a group leader).

    When the child was launched with the resource-limit ``preexec_fn`` it called
    ``os.setsid``, so it leads its own process group; ``os.killpg(pgid, SIGKILL)``
    then reaps double-forked / detached descendants that a bare ``proc.kill()``
    (single PID) would leave running — closing the recon's "outer hard-kill is not
    process-group-wide" hole. Falls back to ``proc.kill()`` if killpg is
    unavailable or the group is already gone."""
    if group and hasattr(os, "killpg"):
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        proc.kill()
    except (ProcessLookupError, OSError):
        pass


def _cleanup_workdir(workdir: str) -> None:
    """Best-effort recursive removal of the per-run scratch dir."""
    try:
        shutil.rmtree(workdir, ignore_errors=True)
    except OSError:
        pass


#: Subdir of the per-run workdir that holds the pre-fetched layer/frame bytes.
#: It is bound READ-ONLY into the bwrap jail (the jail is network-denied +
#: credential-scrubbed by design, so the executor inside it CANNOT fetch S3 —
#: the agent process, which HAS creds + network, pre-fetches here and rewrites
#: the refs to these local paths).
STAGED_INPUTS_DIRNAME = "staged_inputs"


def _needs_staging(uri: Any) -> bool:
    """True when ``uri`` is an ``s3://`` URI the agent must pre-fetch.

    The live stack is S3-only (GCP decommissioned). A local path, a legacy
    ``gs://`` string, or any non-``s3://`` value is handed through UNCHANGED — the
    executor opens local paths directly and the un-jailed dev path can still use
    rasterio's /vsi drivers for the rare legacy URI. Only ``s3://`` (which the
    network-denied jail cannot reach) is staged here in the agent process."""
    return isinstance(uri, str) and uri.startswith("s3://")


def _stage_one_uri(uri: str, staged_dir: str, label: str, idx: int | None = None) -> str:
    """Download ONE ``s3://`` URI to a local file under ``staged_dir``; return the path.

    Reuses the shared boto3 reader (``cache.read_object_bytes_s3``) — the same
    instance-role-correct S3 path ``analytical_qa._materialize_uri`` uses — so the
    agent process (which HAS creds + network) materializes the bytes the jailed,
    network-denied executor will open as a LOCAL file. A non-``s3://`` value is
    returned unchanged (it never needed staging)."""
    if not _needs_staging(uri):
        return uri
    from .tools.cache import read_object_bytes_s3

    base = uri.rstrip("/").rsplit("/", 1)[-1] or f"{label}.bin"
    # Sanitize + de-collide the on-disk name (frames share a stem).
    safe = "".join(ch if (ch.isalnum() or ch in "._-") else "_" for ch in base)
    prefix = f"{label}_{idx}_" if idx is not None else f"{label}_"
    local_path = os.path.join(staged_dir, f"{prefix}{safe}")
    data = read_object_bytes_s3(uri)
    with open(local_path, "wb") as fh:
        fh.write(data)
    return local_path


def stage_layer_refs_locally(
    layer_refs: dict[str, Any] | None, workdir: str
) -> tuple[dict[str, Any], str | None]:
    """Pre-fetch every ``layer_refs`` URI to a local file; rewrite refs to paths.

    Accepts BOTH ref shapes (the ADDITIVE multi-frame extension):
      - ``{var: "s3://.../layer.tif"}``        -> ``{var: "<staged>/var_layer.tif"}``
      - ``{var: ["s3://.../f0.tif", ...]}``    -> ``{var: ["<staged>/var_0_f0.tif", ...]}``

    Returns ``(rewritten_refs, staged_dir_or_None)``. ``staged_dir`` is the dir the
    caller binds READ-ONLY into the jail; it is ``None`` when nothing needed
    staging (every ref was already a local path / unknown scheme), so the caller
    skips the extra bind. A single FAILED fetch is non-fatal: the original URI
    string is handed through under the same key (the executor's ``_open_layer``
    falls back to the raw URI -> ``_layer_errors``), preserving the honest
    degrade-don't-crash contract.
    """
    refs = layer_refs or {}
    if not refs:
        return {}, None

    staged_dir = os.path.join(workdir, STAGED_INPUTS_DIRNAME)
    rewritten: dict[str, Any] = {}
    used_staging = False

    for var, ref in refs.items():
        if isinstance(ref, list):
            out_list: list[Any] = []
            for i, frame_uri in enumerate(ref):
                if not _needs_staging(frame_uri):
                    out_list.append(frame_uri)
                    continue
                try:
                    os.makedirs(staged_dir, exist_ok=True)
                    local = _stage_one_uri(frame_uri, staged_dir, var, idx=i)
                    used_staging = True
                    out_list.append(local)
                except Exception as exc:  # noqa: BLE001 — degrade, never crash
                    LOG.warning("sandbox stage frame failed var=%s idx=%d uri=%s: %s", var, i, frame_uri, exc)
                    out_list.append(frame_uri)
            rewritten[var] = out_list
        elif _needs_staging(ref):
            try:
                os.makedirs(staged_dir, exist_ok=True)
                local = _stage_one_uri(ref, staged_dir, var)
                used_staging = True
                rewritten[var] = local
            except Exception as exc:  # noqa: BLE001 — degrade, never crash
                LOG.warning("sandbox stage layer failed var=%s uri=%s: %s", var, ref, exc)
                rewritten[var] = ref
        else:
            # A single non-s3 string, or any other shape — pass through unchanged
            # (local path / legacy gs:// / unknown scheme the executor handles).
            rewritten[var] = ref

    return rewritten, (staged_dir if used_staging else None)


def run_sandbox_local(
    python_code: str,
    layer_refs: dict[str, str] | None = None,
    *,
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    """Run ``python_code`` through ``executor.py`` in a child subprocess.

    Returns the parsed result envelope (the same shape the container emits):
        {"stdout", "stderr", "result", "status", "error",
         "stdout_truncated", "stderr_truncated", "wallclock_cap_seconds", ...}

    Enforces:
      - the executor's in-process 60s SIGALRM cap (via GRACE2_SANDBOX_TIMEOUT), AND
      - an OUTER subprocess hard-kill at cap + grace (belt-and-suspenders), AND
      - the executor's output truncation + this runner's MAX_ENVELOPE_BYTES bound.

    On the outer hard-kill (the in-process alarm was defeated) we synthesize a
    ``status="timeout"`` envelope so the caller always gets a well-formed result.
    """
    cap = timeout_seconds if timeout_seconds is not None else WALLCLOCK_CAP_SECONDS
    executor = _executor_path()
    if not executor.exists():
        raise FileNotFoundError(f"sandbox executor not found at {executor}")

    # Per-run scratch dir: holds the payload file and serves as the child's cwd
    # (and, under the jail, the only writable bind besides /tmp). A private dir
    # per run keeps one Case's payload out of another's reach.
    workdir = tempfile.mkdtemp(prefix="grace2_sandbox_")

    # PRE-FETCH stage (sandbox-staging): the jailed executor is network-denied +
    # credential-scrubbed BY DESIGN, so it cannot read S3 itself. The AGENT
    # process (which HAS creds + network) pre-fetches every layer_ref URI (single
    # OR a list of animation frames) into a staged-inputs subdir of the workdir
    # and rewrites the refs handed to the executor to LOCAL file paths. That dir
    # is then bound read-only into the jail (see build_jailed_cmd). This is the
    # substrate that lets a snippet read on-map layers + frames as local handles.
    staged_refs, staged_dir = stage_layer_refs_locally(layer_refs, workdir)

    payload = {"python_code": python_code, "layer_refs": staged_refs}
    payload_path = os.path.join(workdir, "payload.json")
    with open(payload_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)

    hardened = sandbox_hardening.hardening_enabled()

    # Run the executor as a script by path — its __main__ guard calls main().
    base_cmd = [sys.executable, str(executor), "--payload-file", payload_path]

    # Child env. HARDENED (default, the AWS posture): an ALLOWLISTED, credential-
    # scrubbed env — NO AWS_*/GOOGLE_*/token vars, AWS_EC2_METADATA_DISABLED=true
    # (Invariant 5). UNHARDENED (opt-out for a controlled local debug): the legacy
    # full-env copy. Either way pin the executor's cap so its in-process alarm
    # matches the outer timeout's intent.
    jailed = False
    popen_cwd: str | None = workdir
    if hardened:
        child_env = sandbox_hardening.build_child_env(dict(os.environ), cap_seconds=cap)
        # Fail-loud if a future allowlist edit re-admits a credential var.
        sandbox_hardening.assert_env_scrubbed(child_env)

        # Layer B: wrap in the bubblewrap namespace jail (fresh netns w/ NO
        # interfaces => IMDS + all egress kernel-unreachable; read-only host fs;
        # private tmpfs) when enabled+present. Degrades to the un-jailed cmd in
        # auto-mode if bwrap is absent; raises JailUnavailable if
        # GRACE2_SANDBOX_BWRAP=1 demanded it.
        cmd, popen_env, jailed = sandbox_hardening.build_jailed_cmd(
            base_cmd,
            child_env,
            executor_path=str(executor),
            payload_path=payload_path,
            workdir=workdir,
            staged_inputs_dir=staged_dir,
        )
        preexec_fn = sandbox_hardening.preexec_resource_limits(jailed=jailed)
        if jailed:
            # bwrap sets --chdir + --clearenv/--setenv; cwd/env are jail-owned.
            # We still pass popen_env to Popen so the bwrap process itself runs
            # with the scrubbed env (it cannot leak what it does not hold).
            popen_cwd = None
    else:
        child_env = dict(os.environ)
        child_env["GRACE2_SANDBOX_TIMEOUT"] = str(cap)
        child_env.setdefault("MPLBACKEND", "Agg")
        cmd = base_cmd
        popen_env = child_env
        preexec_fn = None

    LOG.info(
        "sandbox local run: cap=%ds hardened=%s jailed=%s exe=%s",
        cap,
        hardened,
        jailed,
        executor,
    )
    proc = subprocess.Popen(  # noqa: S603 — fixed cmd, no shell
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=popen_env,
        cwd=popen_cwd,
        text=True,
        # preexec_fn sets a new session/process-group + rlimits in the child so
        # the outer hard-kill can killpg the WHOLE group (double-fork survivors
        # included). On non-POSIX it is None (Popen ignores it).
        preexec_fn=preexec_fn,
    )
    outer_timeout = cap + SUBPROCESS_GRACE_SECONDS
    try:
        out, err = proc.communicate(timeout=outer_timeout)
    except subprocess.TimeoutExpired:
        # In-process alarm was defeated; hard-kill the child (process-group-wide
        # when we made it a group leader) and synthesize a timeout envelope. This
        # is the wallclock backstop the kickoff requires.
        _hard_kill(proc, group=(preexec_fn is not None))
        try:
            out, err = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            out, err = "", ""
        LOG.warning("sandbox local run exceeded outer timeout %ds; child killed", outer_timeout)
        return {
            "stdout": (out or "")[:MAX_ENVELOPE_BYTES],
            "stderr": (err or "")[:MAX_ENVELOPE_BYTES],
            "result": {"kind": "none", "value": None},
            "status": "timeout",
            "error": f"sandbox exceeded {cap}s wallclock cap (outer subprocess kill at {outer_timeout}s)",
            "stdout_truncated": False,
            "stderr_truncated": False,
            "wallclock_cap_seconds": cap,
        }
    finally:
        _cleanup_workdir(workdir)

    # The executor prints exactly one JSON envelope line on stdout. We do NOT
    # blind-slice ``out`` to MAX_ENVELOPE_BYTES (job-0233 FINDING 2): a raw byte
    # slice through a JSON document corrupts it (cuts mid-token / mid-escape) and
    # yields an un-parseable envelope. Instead we PARSE the full stdout, then
    # bound the string fields INSIDE the parsed envelope with honest markers
    # (``_parse_envelope`` -> ``_bound_envelope``). If the raw stdout itself is
    # absurdly large (a misbehaving harness that printed the unbounded result to
    # the real stdout) we reject with a typed too-large error rather than parse
    # gigabytes — also honest, never silently corrupt.
    out = out or ""
    if len(out) > MAX_ENVELOPE_BYTES:
        return {
            "stdout": "",
            "stderr": (err or "")[-2000:],
            "result": {"kind": "none", "value": None},
            "status": "error",
            "error": (
                f"sandbox stdout ({len(out)} bytes) exceeded MAX_ENVELOPE_BYTES "
                f"({MAX_ENVELOPE_BYTES}); refusing to parse a potentially-corrupt "
                "envelope (the executor's own MAX_RESULT_BYTES / MAX_OUTPUT_CHARS "
                "caps should keep a well-behaved envelope well under this bound)"
            ),
            "stdout_truncated": True,
            "stderr_truncated": False,
            "wallclock_cap_seconds": WALLCLOCK_CAP_SECONDS,
            "envelope_truncated": True,
        }
    envelope = _parse_envelope(out, err or "", proc.returncode)
    return envelope


#: Per-field char bound applied INSIDE a parsed envelope (FINDING 2). The
#: executor already caps stdout/stderr at MAX_OUTPUT_CHARS and the result
#: descriptor at MAX_RESULT_BYTES; this is the host-side defense-in-depth bound
#: that guarantees the envelope this runner returns can never carry an
#: unboundedly-large string field even if an env override loosened the executor's
#: own caps. Truncation is honest: a ``*_truncated`` flag is set when it fires.
MAX_ENVELOPE_FIELD_CHARS = int(
    os.environ.get("GRACE2_SANDBOX_MAX_ENVELOPE_FIELD_CHARS", str(256 * 1024))
)


def _bound_str_field(value: Any, *, cap: int | None = None) -> tuple[Any, bool]:
    """Bound a string field to ``cap`` chars; return ``(bounded, was_truncated)``.

    Non-string values pass through unchanged. The marker is appended so the
    truncation is visible in the field itself, and the boolean lets the caller
    flip the matching ``*_truncated`` flag (honest, never silent). ``cap``
    defaults to the module-level :data:`MAX_ENVELOPE_FIELD_CHARS` read at CALL
    time (so an env / monkeypatch override of the constant takes effect)."""
    if cap is None:
        cap = MAX_ENVELOPE_FIELD_CHARS
    if not isinstance(value, str) or len(value) <= cap:
        return value, False
    marker = f"...[truncated {len(value) - cap} chars]"
    return value[:cap] + marker, True


def _bound_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
    """Bound the string fields of a parsed envelope (FINDING 2 — parse-then-bound).

    Truncates INSIDE the already-parsed dict (so JSON validity is preserved by
    construction) rather than slicing the raw JSON string. Sets the matching
    ``stdout_truncated`` / ``stderr_truncated`` flags when a bound fires."""
    out_bounded, out_trunc = _bound_str_field(envelope.get("stdout"))
    err_bounded, err_trunc = _bound_str_field(envelope.get("stderr"))
    if out_trunc:
        envelope["stdout"] = out_bounded
        envelope["stdout_truncated"] = True
    if err_trunc:
        envelope["stderr"] = err_bounded
        envelope["stderr_truncated"] = True
    # ``error`` is a short message; bound it too for completeness.
    err_msg_bounded, _ = _bound_str_field(envelope.get("error"))
    if envelope.get("error") is not None:
        envelope["error"] = err_msg_bounded
    return envelope


def _parse_envelope(stdout: str, stderr: str, returncode: int | None) -> dict[str, Any]:
    """Parse the executor's JSON envelope from stdout; synthesize on parse failure.

    FINDING 2: we parse the FULL (unsliced) stdout line, then bound the string
    fields inside the parsed dict via :func:`_bound_envelope` so the returned
    envelope is always valid JSON with honestly-marked truncation — never a
    corrupt slice of a JSON document.

    job-0265: the executor now prefixes the envelope line with
    ``ENVELOPE_MARKER`` (``GRACE2_SANDBOX_ENVELOPE_V1 {...}``) so the cloud
    Cloud-Logging readback can pin it. We tolerate that prefix here by extracting
    the JSON from the first ``{`` on the line — a marker-prefixed line and a bare
    ``{...}`` line both parse, so the SAME emit path serves both transports."""
    candidate: str | None = None
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        # Tolerate the ``GRACE2_SANDBOX_ENVELOPE_V1 {...}`` marker prefix: take
        # the JSON from the first ``{`` to the last ``}`` on the line.
        if SANDBOX_ENVELOPE_MARKER in line:
            brace = line.find("{")
            if brace != -1 and line.endswith("}"):
                candidate = line[brace:]
                break
        if line.startswith("{") and line.endswith("}"):
            candidate = line
            break
    if candidate is not None:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict) and "status" in parsed:
                return _bound_envelope(parsed)
        except (TypeError, ValueError):
            pass
    # No well-formed envelope — the child crashed before emitting one.
    return {
        "stdout": _bound_str_field(stdout)[0],
        "stderr": _bound_str_field(stderr)[0],
        "result": {"kind": "none", "value": None},
        "status": "error",
        "error": (
            f"sandbox child produced no parseable result envelope "
            f"(returncode={returncode}); stderr tail: {stderr[-500:]!r}"
        ),
        "stdout_truncated": len(stdout) > MAX_ENVELOPE_FIELD_CHARS,
        "stderr_truncated": len(stderr) > MAX_ENVELOPE_FIELD_CHARS,
        "wallclock_cap_seconds": WALLCLOCK_CAP_SECONDS,
    }


# --------------------------------------------------------------------------- #
# Dispatch entry point
# --------------------------------------------------------------------------- #


def submit_sandbox_job(
    python_code: str,
    layer_refs: dict[str, str] | None = None,
    *,
    timeout_seconds: int | None = None,
) -> SandboxExecutionHandle | dict[str, Any]:
    """Dispatch a sandbox run via the local-subprocess path.

    Runs synchronously via :func:`run_sandbox_local` and returns the parsed RESULT
    ENVELOPE dict directly (the run is already complete). The GCP cloud path (a
    pending :class:`SandboxExecutionHandle` to poll) was removed in the GCP
    decommission; the return-type union + handle class are retained for back-compat
    with callers (job-0233) that ``isinstance``-branch on the result.
    """
    return run_sandbox_local(python_code, layer_refs, timeout_seconds=timeout_seconds)


def read_sandbox_result(
    handle: SandboxExecutionHandle,
    *,
    timeout_seconds: int | None = None,
    poll_interval_seconds: float | None = None,
    logging_client: Any | None = None,
) -> dict[str, Any]:
    """Read a cloud sandbox dispatch's result envelope — REMOVED.

    The GCP cloud sandbox transport (the executor printing a marker-prefixed
    envelope to stdout -> Cloud Logging, read back via ``logging.Client``) was
    removed in the GCP decommission; the EC2 stack has no Cloud Logging. Retained
    as an importable symbol so callers (job-0233 ``code_exec_tool``) that branch on
    a cloud :class:`SandboxExecutionHandle` still resolve — but the live dispatch
    always runs the local-subprocess path (:func:`run_sandbox_local`) and returns a
    finished envelope dict directly, so this function is never reached in product
    code. If called, it raises the honest typed :class:`SandboxCloudModeUnavailable`
    (use local mode).
    """
    raise SandboxCloudModeUnavailable(
        "cloud-mode sandbox result readback was removed in the GCP decommission; "
        "the only executor is the local-subprocess path (submit_sandbox_job returns "
        "a finished result envelope synchronously)."
    )
