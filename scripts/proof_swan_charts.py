"""Render the two SWAN CAND-S template charts EXACTLY as the plugin chart dock
draws them: the template's own spec builders + the dock's render_spec interpreter
+ the dock's 6.0x2.2in geometry. Fed with the REAL scalars from the live smoke.

No suptitle (the spec carries its own title -- avoids the double-title collision);
the workflow name goes only in the small bottom caption strip.
"""
import importlib.util
import json
import sys

import matplotlib

matplotlib.use("Agg")
from matplotlib.figure import Figure

REPO = "/home/nate/Documents/trid3nt-local"
OUT = REPO + "/docs/proof/templates"
SCR = ("/tmp/claude-1000/-home-nate-Documents-GRACE-2/"
       "fd2df08a-a572-4b62-ba9a-e82d8a0a740e/scratchpad/")
SWEEP_JSON = SCR + "sweep_final.json"   # friction sweep on the shallow shelf
BATCH_JSON = SCR + "swan_sweep_smoke.json"  # snapshot batch (Huntington Beach)

sys.path.insert(0, REPO)
sys.path.insert(0, REPO + "/contracts/src")
from trid3nt_server.agent.workflows.swan.physics_sensitivity_sweep.physics_sensitivity_sweep import (  # noqa: E402
    build_sweep_chart_spec,
)
from trid3nt_server.agent.workflows.swan.stationary_snapshot_batch.stationary_snapshot_batch import (  # noqa: E402
    build_snapshot_chart_spec,
)


def load_charts():
    spec = importlib.util.spec_from_file_location(
        "trid3nt_charts", REPO + "/qgis-plugin/trid3nt/ui/charts.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if mod.Figure is None:
        mod.Figure = Figure
        mod._MATPLOTLIB_ERROR = None
    return mod


def render(charts, spec, out_name, caption):
    fig = Figure(figsize=(6.0, 2.2), dpi=100)
    summary = charts.render_spec(fig, spec)
    fig.text(0.01, 0.005, caption, fontsize=6.5, color="#888888")
    out = f"{OUT}/{out_name}"
    fig.savefig(out, dpi=200, bbox_inches="tight")
    print("wrote", out, "| render summary:", summary)


def main():
    charts = load_charts()

    sweep = json.load(open(SWEEP_JSON))
    spec = build_sweep_chart_spec(sweep["axis"], sweep["schemes"])
    render(charts, spec, "swan_physics_sensitivity_sweep_chart.png",
           "swan_physics_sensitivity_sweep -- mean Hs (dissipation-sensitive) + "
           "peak Hs (boundary-pinned reference) vs JONSWAP bottom friction, "
           "each relative to baseline; shared boundary + shallow-shelf bathymetry.")

    batch = json.load(open(BATCH_JSON))["batch"]
    spec = build_snapshot_chart_spec(batch["snapshots"])
    render(charts, spec, "swan_stationary_snapshot_batch_chart.png",
           "swan_stationary_snapshot_batch -- peak Hs + wave footprint across "
           "stationary storm snapshots (discrete-time sampling of the event).")


if __name__ == "__main__":
    main()
