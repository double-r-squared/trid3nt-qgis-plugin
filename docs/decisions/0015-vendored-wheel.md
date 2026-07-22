# 0015 - server/wheels holds PyPI-absent deps

Decision: pfdf (post-fire debris flow) is a main dependency absent from
PyPI; its wheel is committed at server/wheels/ and every install path uses
--find-links server/wheels. Proven load-bearing by fresh-clone simulation:
resolution fails without it. Do not gitignore or delete.
