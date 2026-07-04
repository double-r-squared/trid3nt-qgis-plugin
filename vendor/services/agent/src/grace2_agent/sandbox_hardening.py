"""AWS isolation hardening for the local-subprocess Python sandbox (sprint-14-aws).

On the GCP stack the real containment boundary for ``code_exec_request`` was the
Cloud Run Job's VPC connector + egress-deny firewall + read-only runtime SA
(``infra/python-sandbox.tf``). On the single-EC2 AWS stack there is NO Cloud Run,
NO VPC sandbox boundary, and the box's instance-role credentials are reachable by
ANY process via the IMDS endpoint (``169.254.169.254``). Running the existing
local-subprocess executor naively on the agent box would let untrusted user code:

  * read ``AWS_*`` / ``AWS_PROFILE`` / ``AWS_CONTAINER_CREDENTIALS_*`` env vars
    that the runner copied verbatim into the child (``dict(os.environ)``),
  * hit IMDS to mint the instance-role STS credentials,
  * egress arbitrary internet hosts to exfiltrate data,
  * read the editable install / ``~/.aws`` / systemd ``EnvironmentFile`` secrets,
  * fork-bomb / OOM the shared box (the only cap today is wallclock).

This module is the faithful, cheap AWS analogue of that boundary, built in two
kernel-enforced + always-on layers (recon Option 1 + Option 2):

LAYER A — always-on, no host dependency (recon Option 1, the immediate stopgap)
-------------------------------------------------------------------------------
* :func:`build_child_env` — an ALLOWLISTED minimal child env. Every ``AWS_*`` /
  ``GOOGLE_*`` / credential / token var is DROPPED (never the wholesale
  ``dict(os.environ)`` copy), ``AWS_EC2_METADATA_DISABLED=true`` is set so boto3
  inside the child never even attempts IMDS, and proxy vars are stripped.
* :func:`preexec_resource_limits` — a ``preexec_fn`` that (a) ``os.setsid`` so the
  child is a process-group LEADER (the runner's hard-kill can ``killpg`` the whole
  group, defeating double-fork survivors) and (b) ``setrlimit`` caps on address
  space, CPU seconds, file size, and process/thread count (kills fork-bombs +
  unbounded allocations the old wallclock-only path could not).

LAYER B — kernel-enforced jail (recon Option 2, the REAL boundary)
------------------------------------------------------------------
* :func:`wrap_with_jail` — when a ``bubblewrap`` (``bwrap``) binary is available
  and enabled, wraps the child command in a namespace jail with:
    - ``--unshare-net`` — a fresh NETWORK namespace with NO interfaces, so IMDS
      (169.254.169.254) and ALL egress are unreachable by ANY method (curl,
      ctypes, a C-extension socket) — kernel-enforced, not the bypassable Python
      monkeypatch the executor's in-process guard relies on,
    - ``--unshare-pid``/``--unshare-ipc``/``--unshare-uts``/``--unshare-cgroup``
      — process/IPC isolation,
    - read-only binds of only the runtime dirs (interpreter + libs + the agent
      venv + the executor file), a private writable ``tmpfs`` for ``/tmp`` and the
      cwd, and ``--die-with-parent`` so the jail dies if the runner is killed.
  The jail needs NO Docker daemon (bwrap is a setuid/userns binary) and deploys
  through the existing SSM file-swap plus the one ``bubblewrap`` package on the
  AMI.

Both layers are independent: Layer A runs unconditionally on the local path;
Layer B is added when ``bwrap`` is enabled+present. Tests assert Layer A on any
box; Layer B is exercised where ``bwrap`` exists (this AMI) and skipped otherwise.

Env seams (read at call time so a deploy / test injection takes effect):
  * ``GRACE2_SANDBOX_HARDENED`` — default ON. Set ``0`` ONLY for a controlled
    local debug; production AWS leaves it unset (== on).
  * ``GRACE2_SANDBOX_BWRAP`` — ``auto`` (default; jail iff ``bwrap`` present),
    ``1`` (require jail; error if absent), ``0`` (Layer A only).
  * ``GRACE2_SANDBOX_BWRAP_BIN`` — override the ``bwrap`` binary path.
  * ``GRACE2_SANDBOX_RLIMIT_AS_MB`` / ``_CPU_SECONDS`` / ``_NPROC`` /
    ``_FSIZE_MB`` — resource caps (sensible defaults below).
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path

LOG = logging.getLogger("grace2.agent.sandbox_hardening")

# --------------------------------------------------------------------------- #
# Env allowlist + credential scrub (Layer A)
# --------------------------------------------------------------------------- #

#: The ONLY host env keys copied verbatim into the sandbox child. Everything else
#: (notably every credential / cloud / token var) is dropped. Kept deliberately
#: tiny: the interpreter + locale + the sandbox's own knobs. NB: ``PYTHONPATH`` is
#: intentionally EXCLUDED — the executor is invoked by absolute file path and must
#: not inherit an import path that could shadow stdlib or leak the agent package.
_ENV_ALLOW_KEYS: tuple[str, ...] = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "TMPDIR",
    "MPLBACKEND",  # matplotlib headless backend (Agg)
    "PROJ_LIB",  # pyproj/rasterio CRS db (needed for geo compute)
    "GDAL_DATA",  # GDAL support files (needed for rasterio)
)

#: Env-key PREFIXES that are ALWAYS dropped even if (defensively) allowlisted —
#: the credential/cloud surface untrusted code must never see. Matched
#: case-insensitively against the FULL key.
_ENV_DENY_PREFIXES: tuple[str, ...] = (
    "AWS_",
    "AMAZON_",
    "GOOGLE_",
    "GCP_",
    "GCLOUD_",
    "CLOUDSDK_",
    "BEDROCK_",
    "ATLAS_",
    "MONGODB_",
    "MONGO_",
)

#: Exact env keys that are always dropped (the credential/discovery surface that
#: does not share a common prefix).
_ENV_DENY_EXACT: frozenset[str] = frozenset(
    {
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
        "AWS_SHARED_CREDENTIALS_FILE",
        "AWS_CONFIG_FILE",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "AWS_ROLE_ARN",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
        "AWS_CONTAINER_AUTHORIZATION_TOKEN",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "MONGODB_URI",
        "MONGO_URI",
        # proxy vars (also cleared by the executor guard; belt-and-suspenders)
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "ftp_proxy",
        "no_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "FTP_PROXY",
        "NO_PROXY",
    }
)

#: Substrings that, if present anywhere in a key (case-insensitive), force a drop.
#: Catches bespoke ``*_TOKEN`` / ``*_SECRET`` / ``*_PASSWORD`` / ``*_API_KEY`` vars
#: an operator may have set on the box that untrusted code should never inherit.
_ENV_DENY_SUBSTRINGS: tuple[str, ...] = (
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "PASSWD",
    "CREDENTIAL",
    "API_KEY",
    "APIKEY",
    "PRIVATE_KEY",
    "ACCESS_KEY",
)

#: Intentional ``AWS_*`` CONTROL knobs the runner SETS in the child to HARDEN it
#: (disable IMDS, zero its timeout/retries). These match the ``AWS_`` deny prefix
#: but carry no credential material — they exist to block credential discovery,
#: so they are explicitly exempt from the deny rule (and from the leak assertion).
_ENV_INTENTIONAL_AWS_KEYS: frozenset[str] = frozenset(
    {
        "AWS_EC2_METADATA_DISABLED",
        "AWS_METADATA_SERVICE_TIMEOUT",
        "AWS_METADATA_SERVICE_NUM_ATTEMPTS",
    }
)


def _key_is_denied(key: str) -> bool:
    up = key.upper()
    # Intentional IMDS-disable knobs are NOT credentials — allow them through.
    if up in _ENV_INTENTIONAL_AWS_KEYS:
        return False
    if key in _ENV_DENY_EXACT or up in {k.upper() for k in _ENV_DENY_EXACT}:
        return True
    if up.startswith(_ENV_DENY_PREFIXES):
        return True
    for sub in _ENV_DENY_SUBSTRINGS:
        if sub in up:
            return True
    return False


def hardening_enabled() -> bool:
    """True unless ``GRACE2_SANDBOX_HARDENED`` is explicitly falsey.

    Default ON: the AWS box leaves it unset and gets the hardened child. A
    developer may set ``0`` for a controlled local debug (the unit tests assert
    the scrub is on by default — they do NOT depend on this being toggled)."""
    raw = os.environ.get("GRACE2_SANDBOX_HARDENED")
    if raw is None:
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def build_child_env(base_env: dict[str, str], *, cap_seconds: int) -> dict[str, str]:
    """Build the ALLOWLISTED, credential-scrubbed env for the sandbox child.

    Replaces the old ``dict(os.environ)`` wholesale copy (the worst exfil hole).
    Only :data:`_ENV_ALLOW_KEYS` survive from ``base_env``, and any of those that
    nonetheless match a deny rule are dropped (defense-in-depth). We then add the
    sandbox's own knobs and, critically, ``AWS_EC2_METADATA_DISABLED=true`` so a
    boto3 inside the child never attempts the IMDS credential mint.

    Always-passed sandbox knobs the executor + guard read:
      * ``GRACE2_SANDBOX_TIMEOUT`` — pinned to the runner's cap.
      * ``MPLBACKEND=Agg`` — headless matplotlib.
      * ``GRACE2_SANDBOX_NET_ALLOW=""`` — empty allowlist so the in-process guard
        (defense-in-depth on top of the netns) blocks ALL non-loopback hosts;
        IMDS/egress is denied at the kernel by the jail, this just makes the
        Python-level guard match the intent.
      * ``AWS_EC2_METADATA_DISABLED=true`` + ``GRACE2_SANDBOX_HARDENED=1`` so the
        child is self-consistent even if it re-reads the env.

    ``base_env`` is the parent env to filter (normally ``os.environ``); passed in
    so tests can inject a synthetic parent env with AWS creds and assert the scrub.
    """
    child: dict[str, str] = {}
    for key in _ENV_ALLOW_KEYS:
        if key in base_env and not _key_is_denied(key):
            child[key] = base_env[key]

    # Ensure a usable PATH/HOME even if the parent env lacked them.
    child.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    child.setdefault("HOME", "/tmp")
    child.setdefault("MPLBACKEND", "Agg")

    # Sandbox knobs.
    child["GRACE2_SANDBOX_TIMEOUT"] = str(cap_seconds)
    child["GRACE2_SANDBOX_HARDENED"] = "1"
    # Empty net allowlist => the executor's in-process guard blocks every
    # non-loopback host (the kernel netns is the real boundary; this aligns the
    # Python guard so it never silently permits a host the jail would block).
    child["GRACE2_SANDBOX_NET_ALLOW"] = "localhost,127.0.0.1,::1,0.0.0.0"
    # Block boto3 / AWS SDK IMDS discovery inside the child (no creds to mint).
    child["AWS_EC2_METADATA_DISABLED"] = "true"
    # Belt-and-suspenders: a tiny IMDS timeout + zero retries in case any SDK
    # ignores the disable flag (it still cannot reach IMDS through the netns).
    child["AWS_METADATA_SERVICE_TIMEOUT"] = "0"
    child["AWS_METADATA_SERVICE_NUM_ATTEMPTS"] = "1"
    return child


def assert_env_scrubbed(child_env: dict[str, str]) -> None:
    """Raise ``AssertionError`` if any denied key leaked into ``child_env``.

    A cheap invariant the runner calls right before ``Popen`` (and tests call
    directly) so a future edit to the allowlist that re-admits a credential var
    fails loudly rather than silently re-opening the exfil hole."""
    leaked = [k for k in child_env if _key_is_denied(k)]
    if leaked:
        raise AssertionError(
            f"sandbox child env leaked denied keys: {sorted(leaked)} — "
            "Invariant 5 (no AWS-cred exfil) would be violated"
        )
    if child_env.get("AWS_EC2_METADATA_DISABLED") != "true":
        raise AssertionError(
            "sandbox child env must set AWS_EC2_METADATA_DISABLED=true "
            "(IMDS credential mint must be blocked)"
        )


# --------------------------------------------------------------------------- #
# Resource limits + process-group leadership (Layer A)
# --------------------------------------------------------------------------- #


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def rlimit_spec() -> dict[str, int]:
    """Resource caps for the child (read at call time so a deploy can tune them).

    Defaults sized for an interactive geo-analytical snippet on a shared box:
      * address space 2 GiB (matches the GCP Job's 2 GiB mem cap),
      * 120 CPU-seconds (a hard CPU bound independent of the wallclock alarm —
        kills a busy spin that the SIGALRM might miss in a C extension),
      * 256 processes/threads (kills a fork-bomb; generous enough for numpy/BLAS
        thread pools),
      * 512 MiB max single-file write (stops the child filling the disk).
    """
    return {
        "as_bytes": _int_env("GRACE2_SANDBOX_RLIMIT_AS_MB", 2048) * 1024 * 1024,
        "cpu_seconds": _int_env("GRACE2_SANDBOX_RLIMIT_CPU_SECONDS", 120),
        "nproc": _int_env("GRACE2_SANDBOX_RLIMIT_NPROC", 256),
        "fsize_bytes": _int_env("GRACE2_SANDBOX_RLIMIT_FSIZE_MB", 512) * 1024 * 1024,
    }


def preexec_resource_limits(*, jailed: bool = False):  # noqa: ANN201 — callable|None
    """Return a ``preexec_fn`` that sets the process group + resource rlimits.

    Runs in the FORKED child between ``fork`` and ``exec`` (POSIX only). It:
      1. ``os.setsid()`` — make the child a new session + process-group leader so
         the runner's outer hard-kill can ``os.killpg`` the WHOLE group (a
         double-forked grandchild can no longer outlive the wallclock kill). This
         is the always-on backstop for the recon's "outer hard-kill is not
         process-group-wide" hole.
      2. ``setrlimit`` AS / CPU / FSIZE — hard caps with no soft headroom for the
         categories the wallclock-only path left unbounded (address space, CPU
         seconds, single-file write size). These are inherited cleanly by bwrap's
         children inside the jail too.

    NB: ``RLIMIT_NPROC`` is DELIBERATELY NOT used. It is a PER-UID limit that
    counts ALL of the uid's existing processes on this shared single-uid box, so
    a cap low enough to stop a fork-bomb (a) starves OpenBLAS/numpy thread pools
    the legitimate analytical code needs ("OpenBLAS blas_thread_init: RLIMIT_NPROC
    ... current") and (b) blocks bwrap from forking its own namespace helper. The
    correct fork-bomb containment is therefore the JAIL'S PID namespace
    (``--unshare-pid`` + ``--die-with-parent`` reap the whole tree) when jailed,
    and the process-group ``SIGKILL`` + wallclock cap on the Layer-A-only path —
    NOT an NPROC rlimit. ``jailed`` is accepted for symmetry but no longer changes
    the rlimit set.

    Returns ``None`` on non-POSIX (no ``resource`` / ``setsid``), where the outer
    subprocess timeout remains the only (still-present) bound. ``preexec_fn`` is
    not thread-safe in the parent, but ``run_sandbox_local`` does a single
    synchronous ``Popen`` per call so this is safe here."""
    if sys.platform == "win32":
        return None
    try:
        import resource  # noqa: PLC0415 — POSIX-only
    except ImportError:  # pragma: no cover — non-POSIX
        return None

    spec = rlimit_spec()
    limits = [
        ("RLIMIT_AS", spec["as_bytes"]),
        ("RLIMIT_CPU", spec["cpu_seconds"]),
        ("RLIMIT_FSIZE", spec["fsize_bytes"]),
    ]

    def _apply() -> None:  # pragma: no cover — runs in the forked child
        # New session/process group: killpg from the parent reaches descendants.
        try:
            os.setsid()
        except OSError:
            pass
        # Hard resource caps. Each is wrapped so one unsupported limit on an
        # unusual kernel does not abort the whole child launch.
        for res_name, value in limits:
            res = getattr(resource, res_name, None)
            if res is None:
                continue
            try:
                resource.setrlimit(res, (value, value))
            except (ValueError, OSError):
                pass

    return _apply


# --------------------------------------------------------------------------- #
# Bubblewrap namespace jail (Layer B — the real, kernel-enforced boundary)
# --------------------------------------------------------------------------- #


def _bwrap_mode() -> str:
    """``auto`` | ``1`` (require) | ``0`` (disabled).

    Invariant 5 (job-0301): on the AWS deploy (``GRACE2_STORAGE_BACKEND`` in
    {s3, aws}) the host carries the agent's instance-role creds + reachable IMDS,
    so the kernel netns jail is the ONLY real boundary — the default there is
    ``1`` (REQUIRE, fail-closed): if ``bwrap`` is missing the sandbox refuses to
    run rather than silently degrading to the bypassable Layer-A-only path. An
    operator can still force ``GRACE2_SANDBOX_BWRAP=0`` to opt out explicitly.
    Off-AWS (dev/test/run-local) the default stays ``auto`` (jail iff present).
    """
    raw = (os.environ.get("GRACE2_SANDBOX_BWRAP") or "").strip().lower()
    if raw in ("1", "true", "yes", "on", "require"):
        return "1"
    if raw in ("0", "false", "no", "off"):
        return "0"
    if not raw:
        backend = (os.environ.get("GRACE2_STORAGE_BACKEND") or "").strip().lower()
        if backend in ("s3", "aws"):
            return "1"
    return "auto"


def _bwrap_bin() -> str | None:
    override = (os.environ.get("GRACE2_SANDBOX_BWRAP_BIN") or "").strip()
    if override:
        return override if os.path.exists(override) else None
    return shutil.which("bwrap")


def jail_available() -> bool:
    """True iff a usable ``bwrap`` binary is resolvable."""
    return _bwrap_bin() is not None


class JailUnavailable(RuntimeError):
    """``GRACE2_SANDBOX_BWRAP=1`` required the jail but ``bwrap`` is absent."""


def _ro_bind_args(paths: list[str]) -> list[str]:
    """``--ro-bind-try`` each existing path (``-try`` so a missing dir is skipped
    rather than aborting the jail)."""
    args: list[str] = []
    seen: set[str] = set()
    for p in paths:
        if not p or p in seen:
            continue
        seen.add(p)
        if os.path.exists(p):
            args += ["--ro-bind-try", p, p]
    return args


def _venv_site_packages() -> str | None:
    """The active interpreter's purelib (venv site-packages) for PYTHONPATH.

    The jail invokes the RESOLVED base interpreter (a venv ``bin/python`` symlink
    does not exec cleanly inside a namespace whose root is empty), so the venv's
    site-packages must be re-supplied via ``PYTHONPATH`` for the agent's deps
    (numpy/rasterio/geopandas/...) to import. Read at call time."""
    try:
        import sysconfig  # noqa: PLC0415

        return sysconfig.get_paths().get("purelib")
    except Exception:  # noqa: BLE001
        return None


def _editable_source_roots() -> list[str]:
    """Source roots that the venv's editable-install ``.pth`` files point at.

    The grace2 packages (``grace2_contracts`` for chart-emission shaping that the
    executor's ``_convert_figure`` imports, ``grace2_agent`` for the executor's
    own dir) are EDITABLE installs: a ``.pth`` in site-packages adds their ``src``
    dir to ``sys.path``. Those source dirs live OUTSIDE the venv (the repo /
    ``/opt/grace2`` on the box) and so must be bound read-only into the jail for
    the import to succeed. We resolve them from the already-imported packages'
    ``__file__`` (the dir two levels up from ``<pkg>/__init__.py``), which is
    exactly what the ``.pth`` lists — robust to repo vs ``/opt/grace2`` layout."""
    roots: list[str] = []
    for mod_name in ("grace2_contracts", "grace2_agent"):
        try:
            mod = __import__(mod_name)
            f = getattr(mod, "__file__", None)
            if f:
                # <root>/<pkg>/__init__.py -> <root>
                root = os.path.dirname(os.path.dirname(os.path.abspath(f)))
                if root not in roots:
                    roots.append(root)
        except Exception:  # noqa: BLE001
            continue
    return roots


def jail_setenv_args(child_env: dict[str, str]) -> list[str]:
    """``--setenv K V`` pairs to re-inject the scrubbed env after ``--clearenv``.

    Kept separate from :func:`wrap_with_jail` so the wrapper stays env-agnostic
    and easy to unit-test."""
    args: list[str] = []
    for k, v in child_env.items():
        args += ["--setenv", k, v]
    return args


def wrap_with_jail(
    cmd: list[str],
    child_env: dict[str, str],
    *,
    executor_path: str,
    payload_path: str,
    workdir: str,
    staged_inputs_dir: str | None = None,
) -> tuple[list[str], dict[str, str]] | None:
    """Build the bubblewrap-jailed command, or ``None`` when the jail is off/absent.

    The jail is the AWS analogue of the GCP VPC egress-deny boundary:
      * ``--unshare-net`` — fresh netns with NO interfaces => IMDS + all egress
        unreachable by ANY method (kernel-enforced, defeats the curl/ctypes
        bypass the Python in-process guard cannot),
      * ``--unshare-pid/ipc/uts/cgroup`` — process/IPC/host isolation,
      * read-only binds of ONLY the base interpreter + the agent venv +
        ``/usr``//``/lib`` + the executor + the payload file (the child needs to
        READ these and nothing else),
      * a private ``tmpfs`` for ``/tmp`` and the writable ``workdir`` (so the child
        can write scratch + matplotlib's cache, but sees NONE of the host fs —
        no ``~/.aws``, no ``/opt/grace2`` secrets, no other Cases' payloads),
      * ``--die-with-parent`` so the jail dies if the runner is killed,
      * ``--new-session`` so the child cannot reuse the parent's controlling tty,
      * ``--clearenv`` + ``--setenv`` so ONLY the scrubbed allowlist env is visible.

    The interpreter is RESOLVED to its real path (``cmd[0]`` rewritten) because a
    venv ``bin/python`` symlink does not exec inside the empty namespace root; the
    venv site-packages is re-supplied via ``PYTHONPATH`` so the agent's deps still
    import. The returned env dict is the child_env augmented with that PYTHONPATH.

    Returns ``None`` when the jail is disabled (``GRACE2_SANDBOX_BWRAP=0``) or
    ``auto`` + no ``bwrap`` present (caller falls back to Layer-A-only). Raises
    :class:`JailUnavailable` when ``GRACE2_SANDBOX_BWRAP=1`` but ``bwrap`` is
    absent (fail-closed: an operator who DEMANDED the jail must not silently get
    the weaker posture)."""
    mode = _bwrap_mode()
    if mode == "0":
        return None
    bwrap = _bwrap_bin()
    if bwrap is None:
        if mode == "1":
            raise JailUnavailable(
                "GRACE2_SANDBOX_BWRAP=1 requires the bubblewrap (bwrap) binary, "
                "which was not found on PATH (set GRACE2_SANDBOX_BWRAP_BIN or "
                "install bubblewrap on the AMI). Refusing to run the sandbox "
                "without the kernel-enforced jail."
            )
        return None  # auto + absent => Layer A only

    py_prefix = sys.prefix  # the active venv (editable install venv on the box)
    py_base = sys.base_prefix  # the base interpreter (stdlib + the real binary)
    real_python = os.path.realpath(sys.executable)

    ro_paths = [
        "/usr",
        "/bin",
        "/lib",
        "/lib64",
        "/etc/ssl",  # TLS roots (harmless; no egress to use them anyway)
        "/etc/ld.so.cache",
        "/etc/alternatives",
        py_base,
        py_prefix,
        real_python,
        os.path.dirname(os.path.abspath(executor_path)),
        executor_path,
        payload_path,
    ]
    for var in ("PROJ_LIB", "GDAL_DATA"):
        val = os.environ.get(var)
        if val:
            ro_paths.append(val)

    # Editable-install source roots (grace2_contracts for the executor's chart
    # shaping, grace2_agent for the executor dir) — the .pth in site-packages
    # adds these to sys.path, so they must be readable inside the jail.
    editable_roots = _editable_source_roots()
    ro_paths += editable_roots

    # Augment the env with the venv site-packages + the editable source roots so
    # the resolved base interpreter still imports the agent's deps + the grace2
    # packages inside the read-only jail. We put the editable roots DIRECTLY on
    # PYTHONPATH (rather than relying on the venv's .pth files, which are only
    # processed for site dirs the `site` module discovers — NOT for PYTHONPATH
    # entries) so the import is robust regardless of .pth processing.
    jail_env = dict(child_env)
    pythonpath_parts: list[str] = []
    site = _venv_site_packages()
    if site:
        pythonpath_parts.append(site)
    pythonpath_parts += editable_roots
    if pythonpath_parts:
        jail_env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)

    jail = [
        bwrap,
        "--clearenv",
        "--unshare-net",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-cgroup-try",
        "--die-with-parent",
        "--new-session",
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--tmpfs", workdir,
    ]
    jail += _ro_bind_args(ro_paths)
    # Re-bind the payload file ON TOP of the tmpfs workdir (later mounts layer
    # over earlier ones) so the child can read it despite the tmpfs.
    jail += ["--ro-bind-try", payload_path, payload_path]
    # Re-bind the pre-fetched staged-inputs dir (the layer/frame COGs the agent
    # downloaded before the run) READ-ONLY on top of the tmpfs workdir, so the
    # network-denied executor can open them as LOCAL files. --unshare-net stays
    # in force: the staged bytes are already local, no egress is needed or
    # possible. The dir lives INSIDE workdir, so it must be re-bound after the
    # workdir tmpfs mount (later mounts layer over earlier ones).
    if staged_inputs_dir:
        jail += ["--ro-bind-try", staged_inputs_dir, staged_inputs_dir]
    jail += ["--chdir", workdir]
    jail += jail_setenv_args(jail_env)
    # Rewrite the interpreter (cmd[0]) to the resolved real path; keep the rest.
    inner = [real_python] + list(cmd[1:])
    return jail + ["--"] + inner, jail_env


def build_jailed_cmd(
    cmd: list[str],
    child_env: dict[str, str],
    *,
    executor_path: str,
    payload_path: str,
    workdir: str,
    staged_inputs_dir: str | None = None,
) -> tuple[list[str], dict[str, str], bool]:
    """Return ``(final_cmd, popen_env, jailed)``.

    When the jail is in force ``final_cmd`` is the bwrap-wrapped command and
    ``popen_env`` is the env augmented with the venv ``PYTHONPATH``; otherwise
    ``cmd`` + ``child_env`` are returned unchanged with ``jailed=False``. The
    boolean lets the runner log the posture honestly + decide on the killpg /
    chdir handling.

    ``staged_inputs_dir`` (when present) is the agent-prefetched layer/frame dir
    that gets a READ-ONLY bind into the jail so the network-denied executor can
    open the COGs as local files (``--unshare-net`` is preserved)."""
    wrapped = wrap_with_jail(
        cmd,
        child_env,
        executor_path=executor_path,
        payload_path=payload_path,
        workdir=workdir,
        staged_inputs_dir=staged_inputs_dir,
    )
    if wrapped is None:
        return cmd, child_env, False
    final, jail_env = wrapped
    return final, jail_env, True
