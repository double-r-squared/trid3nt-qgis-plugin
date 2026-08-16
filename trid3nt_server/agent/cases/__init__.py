"""Validation / demonstration CASE builders (ingestion helpers).

Small, offline, pure-Python helpers that turn a bundled validation case's
transcribed observation tables (``data/cases/<case>/observations.json``) into the
FlatGeobuf observation layers the model-vs-observation pairing + skill tools
consume. These are ingestion utilities, NOT registered LLM tools -- they are
driven by the direct-call validation harnesses under ``scripts/`` (e.g.
``run_l2_malpasset.py``) and exercised by offline unit tests.
"""
