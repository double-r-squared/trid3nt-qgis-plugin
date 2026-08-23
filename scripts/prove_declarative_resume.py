"""Resume-from-failed-step proof for the declarative interpreter, on a live run.

Two invocations of the SAME question:
  1. the declared chart node is forced to raise -> the plan fails AFTER the
     expensive TELEMAC solve, which the step ledger records;
  2. the same invocation reruns unpatched -> the solve REPLAYS from the ledger's
     cached artifact and execution resumes at the chart.

The forced failure is a harness-only monkeypatch of the chart builder; nothing in
product code changes. The second invocation's layer is a real solve result and is
printed as ``PHYSICAL_ANSWER`` for the old-vs-new comparison.

Run:
  cd /home/nate/Documents/trid3nt-local
  set -a; source .env.local; set +a
  PYTHONPATH=.:contracts venvs/agent/bin/python scripts/prove_declarative_resume.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("prove_declarative_resume")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from run_do_sag_direct import ARGS, _physical_answer  # noqa: E402


class _ForcedChartFailure(RuntimeError):
    pass


async def main() -> int:
    from trid3nt_server.declarative.ledger import StepLedger, invocation_key
    from trid3nt_server.declarative.resolver import resolve_params
    from trid3nt_server.workflows.telemac.do_sag import steps as do_sag_steps
    from trid3nt_server.workflows.telemac.do_sag.do_sag import PARAMS, telemac_do_sag

    resolved = await resolve_params(PARAMS, ARGS)
    key = invocation_key("telemac_do_sag", resolved.values_dict())
    log.info("invocation key %s", key)
    await (await StepLedger.load(key, "telemac_do_sag")).clear()

    log.info("=== PASS 1: chart node forced to fail AFTER the solve ===")
    real_builder = do_sag_steps.build_sag_chart

    def _boom(**_kw):
        raise _ForcedChartFailure("HARNESS: forced chart-node failure")

    do_sag_steps.build_sag_chart = _boom
    try:
        first = await telemac_do_sag(**ARGS)
    finally:
        do_sag_steps.build_sag_chart = real_builder

    if not (isinstance(first, dict) and first.get("status") == "error"):
        log.error("PASS 1 did not fail as arranged: %r", first)
        return 1
    log.info("PASS 1 failed as arranged: %s", first.get("error_code"))

    ledger = await StepLedger.load(key, "telemac_do_sag")
    recorded = [(r.index, r.node, r.artifact_uris) for r in ledger.records]
    log.info("LEDGER after pass 1: %s", recorded)
    if not any(r.node == "do_field" for r in ledger.records):
        log.error("the solve was not recorded in the ledger; resume cannot work")
        return 1

    log.info("=== PASS 2: same invocation, unpatched - expect the solve to REPLAY ===")
    second = await telemac_do_sag(**ARGS)
    if isinstance(second, dict) and second.get("status") == "error":
        log.error("PASS 2 failed: %s", second)
        return 1

    ans = _physical_answer(second)
    ans["tag"] = "NEW"
    print("PHYSICAL_ANSWER " + json.dumps(ans, default=str))
    Path("/tmp/do_sag_NEW.json").write_text(json.dumps(ans, indent=2, default=str))
    log.info("RESUME PROOF: read the telemac_do_sag 'executed=/replayed=' log line above")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
