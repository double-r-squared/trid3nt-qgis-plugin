"""AWS isolation hardening for the local-subprocess Python sandbox (sprint-14-aws).

Proves the Invariant-5 boundary the GCP VPC egress-deny used to provide is now
enforced on the single-EC2 AWS stack by the hardened local path:

  (1) BENIGN compute returns a correct result envelope via the local path
      (hardened, default-on) — no regression to the result shape.
  (2) The child env has NO AWS_*/credential vars + AWS_EC2_METADATA_DISABLED=true
      (assert the scrub) — both as a unit on ``build_child_env`` AND end-to-end by
      reading the child's own ``os.environ`` back out of the result.
  (3) Resource-limit preexec is process-group-aware (setsid) so the outer hard-
      kill is killpg-wide; NPROC is dropped under the jail (would block bwrap).
  (4) The bubblewrap jail (when present on the box) makes IMDS + egress
      KERNEL-unreachable even from a subprocess that bypasses the Python guard,
      and the GCP path selection / confirm-gate are untouched.

The bwrap-dependent assertions are skipped where ``bwrap`` is absent so the suite
is green on any box; the env-scrub + rlimit + mode-selection assertions run
everywhere. No network. No Gemini. Pure local subprocess.
"""

from __future__ import annotations

import os

import pytest

from trid3nt_server import sandbox_hardening as H
from trid3nt_server import sandbox_runner as sr
from trid3nt_server.sandbox_runner import run_sandbox_local

_HAS_BWRAP = H.jail_available()
_requires_bwrap = pytest.mark.skipif(
    not _HAS_BWRAP, reason="bubblewrap (bwrap) not installed on this box"
)


@pytest.fixture(autouse=True)
def _local_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRID3NT_SANDBOX_LOCAL", "1")
    monkeypatch.setenv("MPLBACKEND", "Agg")
    # Default ON for hardening; individual tests toggle TRID3NT_SANDBOX_BWRAP.
    monkeypatch.delenv("TRID3NT_SANDBOX_HARDENED", raising=False)


# --------------------------------------------------------------------------- #
# (1) Benign compute returns a correct result envelope via the hardened local path
# --------------------------------------------------------------------------- #


def test_benign_compute_hardened_layer_a_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Layer-A-only (jail disabled): a benign numpy script returns the right
    result envelope — the hardening must not change the result shape."""
    monkeypatch.setenv("TRID3NT_SANDBOX_BWRAP", "0")
    env = run_sandbox_local(
        "import numpy as np\nresult = float(np.mean([10, 20, 30, 40]))"
    )
    assert env["status"] == "ok", env
    assert env["result"] == {"kind": "json", "value": 25.0}


@_requires_bwrap
def test_benign_compute_jailed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Through the bwrap jail a benign numpy script still returns the right
    result (the venv deps import inside the read-only jail via PYTHONPATH)."""
    monkeypatch.setenv("TRID3NT_SANDBOX_BWRAP", "1")
    env = run_sandbox_local(
        "import numpy as np\nprint('inside jail')\nresult = float(np.mean([2, 4, 6]))"
    )
    assert env["status"] == "ok", env
    assert env["result"] == {"kind": "json", "value": 4.0}
    assert "inside jail" in env["stdout"]


# --------------------------------------------------------------------------- #
# (2) Child env has NO AWS_*/credential vars + metadata disabled
# --------------------------------------------------------------------------- #


def test_build_child_env_scrubs_aws_and_disables_imds() -> None:
    """``build_child_env`` drops every credential/cloud var and sets
    AWS_EC2_METADATA_DISABLED=true, regardless of what the parent env held."""
    parent = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/home/agent",
        "LANG": "C.UTF-8",
        "AWS_ACCESS_KEY_ID": "AKIAEXFILTRATE",
        "AWS_SECRET_ACCESS_KEY": "supersecret",
        "AWS_SESSION_TOKEN": "tok",
        "AWS_REGION": "us-west-2",
        "AWS_PROFILE": "grace2",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI": "http://169.254.170.2/creds",
        "GOOGLE_APPLICATION_CREDENTIALS": "/adc.json",
        "BEDROCK_MODEL_ID": "anthropic...",
        "MONGODB_URI": "mongodb+srv://u:p@host",
        "SOME_API_KEY": "k",
        "MY_SECRET_TOKEN": "t",
        "PYTHONPATH": "/opt/grace2/services/agent/src",  # excluded from allowlist
    }
    child = H.build_child_env(parent, cap_seconds=42)

    # No credential / cloud var survived.
    for denied in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_REGION",
        "AWS_PROFILE",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "BEDROCK_MODEL_ID",
        "MONGODB_URI",
        "SOME_API_KEY",
        "MY_SECRET_TOKEN",
        "PYTHONPATH",
    ):
        assert denied not in child, f"{denied} leaked into the sandbox child env"

    # The harmless allowlisted vars survived.
    assert child["PATH"] == "/usr/bin:/bin"
    assert child["HOME"] == "/home/agent"
    assert child["LANG"] == "C.UTF-8"

    # IMDS is disabled + the cap is pinned.
    assert child["AWS_EC2_METADATA_DISABLED"] == "true"
    assert child["AWS_METADATA_SERVICE_NUM_ATTEMPTS"] == "1"
    assert child["TRID3NT_SANDBOX_TIMEOUT"] == "42"

    # The leak-assertion accepts this env (intentional IMDS knobs are exempt).
    H.assert_env_scrubbed(child)


def test_assert_env_scrubbed_rejects_leaked_credential() -> None:
    """The fail-loud invariant catches a credential var that slipped through."""
    bad = {"PATH": "/bin", "AWS_EC2_METADATA_DISABLED": "true", "AWS_SECRET_ACCESS_KEY": "x"}
    with pytest.raises(AssertionError, match="leaked denied keys"):
        H.assert_env_scrubbed(bad)


def test_assert_env_scrubbed_requires_metadata_disabled() -> None:
    with pytest.raises(AssertionError, match="AWS_EC2_METADATA_DISABLED"):
        H.assert_env_scrubbed({"PATH": "/bin"})


def test_child_env_scrubbed_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: inject AWS creds in the PARENT, run user code that reads its own
    os.environ back — the child must see NONE of them + metadata disabled. Runs in
    Layer-A-only mode so the assertion holds even where bwrap is absent."""
    monkeypatch.setenv("TRID3NT_SANDBOX_BWRAP", "0")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXFILTRATE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "supersecret")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "tok")
    monkeypatch.setenv("AWS_PROFILE", "grace2")
    code = (
        "import os\n"
        "aws = {k: v for k, v in os.environ.items() if 'AWS' in k or 'SECRET' in k.upper()}\n"
        "result = {\n"
        "  'aws_env': aws,\n"
        "  'meta_disabled': os.environ.get('AWS_EC2_METADATA_DISABLED'),\n"
        "}\n"
    )
    env = run_sandbox_local(code)
    assert env["status"] == "ok", env
    val = env["result"]["value"]
    # The only AWS_* the child sees are the intentional disable knobs (no creds).
    for k in val["aws_env"]:
        assert k in H._ENV_INTENTIONAL_AWS_KEYS, f"credential var leaked: {k}"
    assert "AKIAEXFILTRATE" not in str(env)
    assert "supersecret" not in str(env)
    assert val["meta_disabled"] == "true"


# --------------------------------------------------------------------------- #
# (3) Resource-limit preexec: setsid + jail-aware NPROC
# --------------------------------------------------------------------------- #


def test_preexec_is_callable_on_posix() -> None:
    fn = H.preexec_resource_limits()
    assert callable(fn)


def test_rlimit_spec_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRID3NT_SANDBOX_RLIMIT_AS_MB", "1024")
    monkeypatch.setenv("TRID3NT_SANDBOX_RLIMIT_NPROC", "64")
    spec = H.rlimit_spec()
    assert spec["as_bytes"] == 1024 * 1024 * 1024
    assert spec["nproc"] == 64


def test_memory_cap_kills_unbounded_allocation(monkeypatch: pytest.MonkeyPatch) -> None:
    """A child that allocates past RLIMIT_AS dies (MemoryError) rather than
    OOM-ing the shared box. Layer-A-only so it runs without bwrap."""
    monkeypatch.setenv("TRID3NT_SANDBOX_BWRAP", "0")
    monkeypatch.setenv("TRID3NT_SANDBOX_RLIMIT_AS_MB", "256")
    code = (
        "try:\n"
        "    big = bytearray(1024 * 1024 * 1024)  # 1 GiB > 256 MiB AS cap\n"
        "    result = 'ALLOCATED'\n"
        "except MemoryError:\n"
        "    result = 'MEMORY_CAPPED'\n"
    )
    env = run_sandbox_local(code, timeout_seconds=20)
    # Either the allocation raised MemoryError (caught -> result) or the child was
    # killed outright; in NO case did it allocate the full gigabyte.
    assert "ALLOCATED" not in str(env["result"]), env
    assert env["status"] in ("ok", "error", "timeout"), env


# --------------------------------------------------------------------------- #
# (4) Bubblewrap jail: kernel-enforced IMDS/egress block + mode selection
# --------------------------------------------------------------------------- #


def test_bwrap_mode_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRID3NT_SANDBOX_BWRAP", "0")
    assert H._bwrap_mode() == "0"
    monkeypatch.setenv("TRID3NT_SANDBOX_BWRAP", "1")
    assert H._bwrap_mode() == "1"
    monkeypatch.setenv("TRID3NT_SANDBOX_BWRAP", "auto")
    assert H._bwrap_mode() == "auto"
    monkeypatch.delenv("TRID3NT_SANDBOX_BWRAP", raising=False)
    assert H._bwrap_mode() == "auto"  # default


def test_jail_required_but_absent_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """TRID3NT_SANDBOX_BWRAP=1 with no bwrap binary fails closed (JailUnavailable),
    never silently degrading to the weaker Layer-A-only posture."""
    monkeypatch.setenv("TRID3NT_SANDBOX_BWRAP", "1")
    monkeypatch.setenv("TRID3NT_SANDBOX_BWRAP_BIN", "/nonexistent/bwrap")
    with pytest.raises(H.JailUnavailable):
        H.build_jailed_cmd(
            ["python", "x.py"],
            {"AWS_EC2_METADATA_DISABLED": "true"},
            executor_path="x.py",
            payload_path="p.json",
            workdir="/tmp/wd",
        )


def test_build_jailed_cmd_disabled_returns_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRID3NT_SANDBOX_BWRAP", "0")
    cmd = ["python", "x.py"]
    final, env, jailed = H.build_jailed_cmd(
        cmd, {"AWS_EC2_METADATA_DISABLED": "true"},
        executor_path="x.py", payload_path="p.json", workdir="/tmp/wd",
    )
    assert jailed is False
    assert final == cmd


@_requires_bwrap
def test_build_jailed_cmd_wraps_with_netns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRID3NT_SANDBOX_BWRAP", "1")
    final, env, jailed = H.build_jailed_cmd(
        ["python", "/x/executor.py", "--payload-file", "/tmp/wd/payload.json"],
        {"AWS_EC2_METADATA_DISABLED": "true", "PATH": "/usr/bin"},
        executor_path="/x/executor.py",
        payload_path="/tmp/wd/payload.json",
        workdir="/tmp/wd",
    )
    assert jailed is True
    assert final[0].endswith("bwrap")
    # The no-interface network namespace is the real boundary.
    assert "--unshare-net" in final
    assert "--clearenv" in final
    assert "--die-with-parent" in final
    # The scrubbed env is re-injected after --clearenv.
    assert "--setenv" in final
    # venv site-packages is supplied so deps import inside the read-only jail.
    assert "PYTHONPATH" in env


@_requires_bwrap
def test_jail_ro_binds_staged_inputs_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """sandbox-staging: the pre-fetched staged-inputs dir is bound READ-ONLY into
    the jail (so the network-denied executor can open the layer/frame COGs as
    local files) WHILE --unshare-net stays in force."""
    monkeypatch.setenv("TRID3NT_SANDBOX_BWRAP", "1")
    staged = "/tmp/wd/staged_inputs"
    final, env, jailed = H.build_jailed_cmd(
        ["python", "/x/executor.py", "--payload-file", "/tmp/wd/payload.json"],
        {"AWS_EC2_METADATA_DISABLED": "true", "PATH": "/usr/bin"},
        executor_path="/x/executor.py",
        payload_path="/tmp/wd/payload.json",
        workdir="/tmp/wd",
        staged_inputs_dir=staged,
    )
    assert jailed is True
    # The staged dir is ro-bound (the value appears as a --ro-bind-try src+dest).
    assert staged in final
    idx = final.index(staged)
    assert final[idx - 1] in ("--ro-bind-try", "--ro-bind")
    assert final[idx + 1] == staged  # src == dest (bound at the same path)
    # Network isolation is PRESERVED (the bytes are already local; no egress).
    assert "--unshare-net" in final


@_requires_bwrap
def test_jail_no_staged_bind_when_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """When no staged dir is supplied (no s3 refs), NO extra ro-bind is added —
    the jail args are unchanged from the no-staging baseline."""
    monkeypatch.setenv("TRID3NT_SANDBOX_BWRAP", "1")
    final, _env, jailed = H.build_jailed_cmd(
        ["python", "/x/executor.py", "--payload-file", "/tmp/wd/payload.json"],
        {"AWS_EC2_METADATA_DISABLED": "true", "PATH": "/usr/bin"},
        executor_path="/x/executor.py",
        payload_path="/tmp/wd/payload.json",
        workdir="/tmp/wd",
        staged_inputs_dir=None,
    )
    assert jailed is True
    assert "staged_inputs" not in " ".join(final)


def test_build_jailed_cmd_accepts_staged_inputs_kwarg(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ``staged_inputs_dir`` kwarg is accepted on the disabled path too (no
    bwrap needed): with the jail OFF, build_jailed_cmd returns the cmd unchanged
    and never trips on the new kwarg (back-compat for the un-jailed dev path)."""
    monkeypatch.setenv("TRID3NT_SANDBOX_BWRAP", "0")
    cmd = ["python", "x.py"]
    final, _env, jailed = H.build_jailed_cmd(
        cmd, {"AWS_EC2_METADATA_DISABLED": "true"},
        executor_path="x.py", payload_path="p.json", workdir="/tmp/wd",
        staged_inputs_dir="/tmp/wd/staged_inputs",
    )
    assert jailed is False
    assert final == cmd


@_requires_bwrap
def test_jail_blocks_imds_via_subprocess_bypass(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE Invariant-5 proof: user code that shells out to a fresh python
    subprocess — bypassing the in-process socket monkeypatch entirely (the recon's
    curl/ctypes concern) — STILL cannot reach IMDS because the kernel network
    namespace has no interfaces. 'Network is unreachable' from the kernel, not a
    Python guard."""
    monkeypatch.setenv("TRID3NT_SANDBOX_BWRAP", "1")
    code = (
        "import subprocess, sys\n"
        "out = subprocess.run(\n"
        "    [sys.executable, '-c',\n"
        "     \"import socket; socket.create_connection(('169.254.169.254', 80), timeout=2)\"],\n"
        "    capture_output=True, text=True, timeout=10)\n"
        "result = {'rc': out.returncode, 'stderr': out.stderr.strip()[-200:]}\n"
    )
    env = run_sandbox_local(code, timeout_seconds=20)
    assert env["status"] == "ok", env
    val = env["result"]["value"]
    # The subprocess could NOT connect (non-zero rc) and the kernel said so.
    assert val["rc"] != 0, val
    assert (
        "Network is unreachable" in val["stderr"]
        or "Errno 101" in val["stderr"]
        or "unreachable" in val["stderr"].lower()
    ), val


@_requires_bwrap
def test_jail_hides_host_filesystem(monkeypatch: pytest.MonkeyPatch) -> None:
    """The jail's read-only/tmpfs mounts hide the host fs: ~/.aws and the editable
    install must NOT be readable from inside."""
    monkeypatch.setenv("TRID3NT_SANDBOX_BWRAP", "1")
    code = (
        "import os\n"
        "result = {\n"
        "  'home_aws': os.path.exists(os.path.expanduser('~/.aws')),\n"
        "  'opt_grace2': os.path.exists('/opt/grace2'),\n"
        "  'cwd_writable': os.access(os.getcwd(), os.W_OK),\n"
        "}\n"
    )
    env = run_sandbox_local(code)
    assert env["status"] == "ok", env
    val = env["result"]["value"]
    assert val["home_aws"] is False, val
    assert val["opt_grace2"] is False, val
