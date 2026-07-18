"""Zero-token driver validation against the repo stub server."""
import os, subprocess, sys
sys.path.insert(0, "/home/nate/Documents/trid3nt-local/qgis-plugin")
from tests.stub_server import StubAgentServer
srv = StubAgentServer(); srv.start()
env = dict(os.environ, E2E_STUB="1", E2E_URL=srv.url,
           E2E_PROMPT="please simulate the spill", E2E_DEADLINE_S="20")
out = subprocess.run(
    [sys.executable, os.path.join(os.path.dirname(__file__), "headless_bk3b_approve_mesh_drive.py")],
    env=env, capture_output=True, text=True, timeout=60,
    cwd="/home/nate/Documents/trid3nt-local/qgis-plugin")
print(out.stdout)
if out.returncode != 0: print("STDERR:", out.stderr[-800:])
ok = '"PASS": true' in out.stdout
print("OFFLINE VALIDATION:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
