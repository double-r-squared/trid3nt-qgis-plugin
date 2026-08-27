"""Zero-token driver validation against the repo stub server.

The stub is loaded BY PATH: repo root carries a regular ``tests`` package, which
wins over this directory's namespace portion no matter what sys.path says, so
importing it by name would load the server suite instead of the stub.
"""
import importlib.util, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "plugin_stub_server", os.path.join(HERE, "stub_server.py"))
stub = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stub)

srv = stub.StubAgentServer(); srv.start()
env = dict(os.environ, E2E_STUB="1", E2E_URL=srv.url,
           E2E_PROMPT="please simulate the spill", E2E_DEADLINE_S="20")
out = subprocess.run(
    [sys.executable, os.path.join(HERE, "headless_mesh_gate_drive.py")],
    env=env, capture_output=True, text=True, timeout=60,
    cwd=os.path.dirname(HERE))
print(out.stdout)
if out.returncode != 0: print("STDERR:", out.stderr[-800:])
ok = '"PASS": true' in out.stdout
print("OFFLINE VALIDATION:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
