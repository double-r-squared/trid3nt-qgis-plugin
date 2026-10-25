"""A run carried on from a finished GeoClaw run's checkpoint, as the engine restarts it.

The previous run is named by the URI of one of its checkpoint files (``fort.chkNNNNN``, or the alternating
``fort.chkaaaaa`` / ``fort.chkbbbbb``) in the runs bucket. The engine restores the AMR hierarchy, the fixed-grid
maxima and the gauges' last-written times from it, and re-reads every other input from the staged .data set, so the
set must agree with the previous run's wherever the checkpoint holds the answer. Gauge files are appended to on a
restart, so the previous run's gauge and fixed-grid records are staged beside the checkpoint.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ..errors import GeoclawError

__all__ = ["Previous", "checkpoint_outputs", "check_matches", "previous_run"]

_CHECKPOINT = re.compile(r"fort\.chk(\d{5}|aaaaa|bbbbb)\Z")
_STAMP = re.compile(r"Checkpoint file at time t =\s*([-0-9.EeDd+]+)")

#: What the worker uploads from a run that states a checkpoint, beside the products: the checkpoint with its time stamp
#: and the .data set the next run is held to.
CHECKPOINT_OUTPUTS = ("fort.chk*", "fort.tck*", "*.data")

#: The previous run's records the engine appends to on a restart.
_RECORDS = ("gauge", "fgout")

#: claw.data and amr.data keywords the checkpoint fixes: the grid it was cut on and its clock.
_GRID = {"claw.data": ("num_dim", "lower", "upper", "num_cells", "num_eqn", "num_aux", "num_ghost", "t0"),
         "amr.data": ("memsize", "amr_levels_max", "refinement_ratios_x", "refinement_ratios_y",
                      "refinement_ratios_t", "aux_type")}

#: Files the engine re-reads that must be the previous run's whole: the bed, the seafloor motion, the initial surface,
#: the physical constants and the storm.
_WHOLE = ("topo.data", "dtopo.data", "qinit.data", "geoclaw.data", "surge.data")


@dataclass(frozen=True)
class Previous:
    """The checkpoint a run restarts from and what is staged with it."""

    uri: str
    name: str
    time: float
    inputs: tuple[Mapping[str, str], ...]
    data: Mapping[str, str]


def _refuse(code: str, message: str) -> None:
    raise GeoclawError(message, error_code=f"GEOCLAW_RESTART_{code}")


def checkpoint_outputs(stated: Mapping[str, Any]) -> list[str]:
    """The extra patterns the worker uploads when the run states a checkpoint, else none."""
    return list(CHECKPOINT_OUTPUTS) if stated.get("CLAW_CHECKPT_STYLE") else []


def previous_run(uri: str) -> Previous:
    """The checkpoint a URI names -> what the continued run stages; a URI that is no GeoClaw checkpoint refuses by name."""
    from trid3nt_server.store import objects as storage

    try:
        _scheme, bucket, key = storage.split_object_uri(str(uri))
    except storage.StorageError as exc:
        _refuse("FILE", f"CLAW_RESTART_FILE {uri!r} names no checkpoint: {exc}")
    prefix, _, name = key.rpartition("/")
    if not prefix or not _CHECKPOINT.fullmatch(name):
        _refuse("FILE", f"CLAW_RESTART_FILE {uri!r} is no GeoClaw checkpoint: it names fort.chkNNNNN, fort.chkaaaaa or "
                "fort.chkbbbbb in a finished run's folder of the runs bucket.")
    client = storage.client()
    held = {item["Key"][len(prefix) + 1:]: item["Key"]
            for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=f"{prefix}/")
            for item in page.get("Contents", ())}
    if "geoclaw_metrics.json" not in held:
        _refuse("NOT_GEOCLAW", f"CLAW_RESTART_FILE {uri!r} names a run that is no GeoClaw run (it holds no "
                "geoclaw_metrics.json): a run is continued from a run of the same engine.")
    checkpoints = sorted(item for item in held if _CHECKPOINT.fullmatch(item))
    stamp = name.replace("chk", "tck")
    if name not in held or stamp not in held:
        _refuse("NO_CHECKPOINT", f"CLAW_RESTART_FILE {uri!r}: the run holds no {name} with its {stamp}; it holds "
                f"{', '.join(checkpoints) if checkpoints else 'no checkpoint'} (a run writes one when it states "
                "CLAW_CHECKPT_STYLE).")
    missing = [item for item in ("claw.data", "amr.data", *_WHOLE) if item not in held]
    if missing:
        _refuse("NO_DATA", f"the run behind {uri!r} uploaded no {', '.join(missing)}: it was not run with a checkpoint stated, "
                "so the set it was cut on cannot be held to.")

    def text(item: str) -> str:
        return client.get_object(Bucket=bucket, Key=held[item])["Body"].read().decode("utf-8", errors="replace")

    found = _STAMP.search(text(stamp))
    if found is None:
        _refuse("NO_CHECKPOINT", f"{stamp} states no checkpoint time.")
    moment = float(found.group(1).replace("D", "E").replace("d", "e"))
    reached = json.loads(text("geoclaw_metrics.json")).get("final_time")
    if reached is None or abs(float(reached) - moment) > 1e-9 * max(1.0, abs(moment)):
        _refuse("NOT_FINAL", f"{name} was written at t={moment:g} s and the run integrated to {reached} s: the gauge and "
                "fixed-grid records the engine appends to would run past the checkpoint. Continue from a run that ended at "
                "the instant to carry on from (run to it, then continue).")
    records = [{"gs_uri": f"s3://{bucket}/{held[item]}", "dest": item}
               for item in sorted(held) if item.startswith(_RECORDS) and "/" not in item]
    inputs = (({"gs_uri": f"s3://{bucket}/{held[name]}", "dest": name},) + tuple(records))
    return Previous(uri=str(uri), name=name, time=moment, inputs=inputs,
                    data={item: text(item) for item in ("claw.data", "amr.data", *_WHOLE)})


def _values(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for line in text.splitlines():
        value, sep, name = line.partition("=:")
        tokens = name.split("#", 1)[0].split()
        if sep and tokens:
            found[tokens[0]] = " ".join(value.split())
    return found


def check_matches(previous: Previous, rundir: Path | str, tfinal: float) -> None:
    """The set just written held to the checkpoint's: every disagreement refuses, all named, before the solve."""
    work = Path(rundir)
    differing: list[str] = []
    for file, names in _GRID.items():
        before, now = _values(previous.data[file]), _values((work / file).read_text(encoding="utf-8"))
        differing += [f"{file} {name} was {before.get(name)} and is {now.get(name)}"
                      for name in names if before.get(name) != now.get(name)]
    for file in _WHOLE:
        if previous.data[file] != (work / file).read_text(encoding="utf-8"):
            differing.append(f"{file} differs from the previous run's")
    if differing:
        _refuse("MISMATCH", f"the run does not match the checkpoint {previous.name} it restarts from: " + "; ".join(differing)
                + ". The engine restores its grid hierarchy from the checkpoint, so state the previous run's grid, bed, "
                "seafloor motion, constants and storm; the final time, the output times and the checkpoints may change.")
    if tfinal <= previous.time:
        _refuse("TFINAL", f"the run ends at CLAW_TFINAL {tfinal:g} s and the checkpoint {previous.name} stands at "
                f"{previous.time:g} s: there is nothing to integrate.")
