# Repo metrics ledger

Progress tracking toward web-orphan removal + modular architecture (NATE
2026-07-27). Append a row per milestone; regenerate counts with the command
below. LOC = python lines incl. comments/blank (consistent measure - trend
matters, not the absolute).

    for d in server/src/trid3nt_server server/tests contracts/src \
      contracts/tests services/workers qgis-plugin/trid3nt scripts; do \
      find $d -name "*.py" -not -path "*__pycache__*" | xargs wc -l \
      | tail -1; done

| date | server pkg | server tests | contracts | workers | plugin | scripts | registry | server.py | notes |
|---|---|---|---|---|---|---|---|---|---|
| 2026-07-27 | 224,142 (563f) | 179,409 (423f) | 11,675+9,483 | 47,897 | 14,300 | 6,255 | 211 | 15,441 | post engine-door rollout, pre cull-pass-2 landing; server.py monolith flagged (cards extraction queued); Mexico Beach scripts + hygiene sweep pending |
| 2026-07-27b | 219,681 (552f) | 176,598 (414f) | - | - | - | - | 202 | 15,441 | post cull (9 tools, replication-proven) + structural batch (agent/ umbrella, search/, 35 dead files); -4,461 pkg lines, -11 files, -9 registered tools vs morning row; suite true baseline = 10 |
| 2026-07-28 | 219757 (558f) | - | - | - | - | - | 200 | 13,910 | post hygiene sweep: server.py -1,529 (cards extracted), comment archaeology -1,785 across 264 files (comment lines 25,317 -> 25301), meta renames, ADR offload notes; suite baseline 10 |
