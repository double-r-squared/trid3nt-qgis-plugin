# 0015 - wheels/ holds PyPI-absent deps

Decision: pfdf (post-fire debris flow) is a main dependency absent from
PyPI; its wheel is committed at `wheels/` and every install path uses
`--find-links wheels`. Proven load-bearing by fresh-clone simulation:
resolution fails without it. Do not gitignore or delete.

AMENDED 2026-09-09: the path is `wheels/` at the repo root. It was
`server/wheels/` when this record was written; the repo unnested and the
directory moved with it.
