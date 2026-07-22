"""OpenQuake worker CLI shim -- materialize deck and run oq engine (local build).

The out-of-process (local-exec) lane of the OpenQuake PSHA engine.
Mirrors ``services/workers/swmm/run_inp.py``: a thin solver shim that reads a
build_spec (or manifest) JSON, renders the OpenQuake deck, runs
``oq engine --run job.ini``, and exits 0 on success.

The SUPERVISOR (in the agent process) handles all S3/file I/O, completion.json
writing, and stdout/stderr upload -- this shim only materializes the deck and
drives the CLI.

Usage:
    python run_oq.py --manifest manifest.json   (preferred; reads build_spec from it)
    python run_oq.py --build-spec build_spec.json  (direct build_spec path)

The manifest must be in the shape the OpenQuake Batch worker entrypoint uses
(schema documented in services/workers/openquake/entrypoint.py). When the
manifest contains no ``inputs[]``, the shim treats the manifest ITSELF as the
build_spec (both shapes work: the Batch path stages a raw build_spec JSON as the
manifest; the local path writes the same JSON to manifest.json).

Outputs (written to CWD):
    output/hazard_curve-*.csv    -- OpenQuake hazard-curve CSV export
    output/hazard_map-*.csv      -- OpenQuake hazard-map CSV export
    job.ini                      -- rendered deck entrypoint
    *.xml                        -- source-model / logic-tree XML deck files

Exit codes:
    0  -- oq engine ran and exported outputs
    1  -- oq engine failure (see stderr)
    2  -- manifest / build_spec not found or malformed
   127 -- oq binary not found on PATH
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

LOG = logging.getLogger("grace2.worker.openquake.run_oq")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)

#: The OpenQuake CLI binary (overridable for a non-standard install).
OQ_BIN = os.environ.get("GRACE2_OQ_BIN", "oq")


def run_oq(build_spec_path: str) -> int:
    """Render the OpenQuake deck from the build_spec and run the engine.

    Args:
        build_spec_path: path to a JSON file containing the build_spec dict.

    Returns:
        0 on success; 1 on engine failure; 2 on I/O/parse failure; 127 if
        the ``oq`` binary is not found on PATH.
    """
    spec_path = Path(build_spec_path)
    if not spec_path.exists():
        sys.stderr.write(f"run_oq.py: build_spec not found: {build_spec_path}\n")
        return 2

    try:
        raw = json.loads(spec_path.read_text(encoding="utf-8"))
    except Exception as exc:
        sys.stderr.write(f"run_oq.py: could not parse build_spec: {exc}\n")
        return 2

    # Accept either a raw build_spec dict OR a manifest dict (the Batch path
    # stages a raw build_spec JSON as the ``--manifest-uri``; the local path
    # writes the same manifest shape to manifest.json). When the dict has a
    # top-level ``build_spec`` key we unwrap it; otherwise treat the whole
    # dict as the build_spec.
    build_spec: dict = raw.get("build_spec", raw) if isinstance(raw, dict) else raw
    if not isinstance(build_spec, dict):
        sys.stderr.write("run_oq.py: build_spec must be a JSON object\n")
        return 2

    cwd = spec_path.parent

    # Import the deck renderer (must be on PYTHONPATH; the LocalSolverSpec
    # env_overrides prepends the repo root).
    try:
        from services.workers.openquake.job_ini import render_openquake_deck  # type: ignore[import]
    except ImportError as exc:
        sys.stderr.write(
            f"run_oq.py: could not import openquake worker modules -- "
            f"is PYTHONPATH set to the repo root? ({exc})\n"
        )
        return 2

    # Materialize the deck into CWD.
    try:
        deck = render_openquake_deck(build_spec)
        files = {
            "job_ini": deck.job_ini,
            "source_model_xml": deck.source_model_xml,
            "source_model_logic_tree_xml": deck.source_model_logic_tree_xml,
            "gmpe_logic_tree_xml": deck.gmpe_logic_tree_xml,
        }
        for logical, text in files.items():
            fname = deck.filenames[logical]
            (cwd / fname).write_text(text, encoding="utf-8")
            LOG.info("rendered %s -> %s", logical, cwd / fname)
    except Exception as exc:
        sys.stderr.write(f"run_oq.py: deck render failed: {exc}\n")
        return 2

    # Run oq engine.
    oq_args = ["engine", "--run", "job.ini", "--exports", "csv"]
    LOG.info("running: %s %s (cwd=%s)", OQ_BIN, " ".join(oq_args), cwd)
    try:
        proc = subprocess.run(  # noqa: S603 -- fixed argv, no shell
            [OQ_BIN, *oq_args],
            cwd=str(cwd),
            check=False,
        )
        return int(proc.returncode)
    except FileNotFoundError:
        sys.stderr.write(
            f"run_oq.py: '{OQ_BIN}' not found on PATH -- "
            "install openquake.engine or set GRACE2_OQ_BIN\n"
        )
        return 127
    except Exception as exc:
        sys.stderr.write(f"run_oq.py: oq engine raised: {exc}\n")
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="grace2-openquake-run-oq",
        description="OpenQuake runner (local subprocess shim).",
    )
    parser.add_argument(
        "--manifest",
        default="manifest.json",
        help="Path to the manifest / build_spec JSON (default: manifest.json in CWD).",
    )
    parser.add_argument(
        "--build-spec",
        dest="build_spec",
        default=None,
        help="Explicit build_spec JSON path (overrides --manifest).",
    )
    args = parser.parse_args(argv)
    spec_path = args.build_spec or args.manifest
    return run_oq(spec_path)


if __name__ == "__main__":
    raise SystemExit(main())
