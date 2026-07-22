"""GRACE-2 SWAN (Simulating WAves Nearshore) AWS Batch worker package.

The deterministic ``.swn`` command-file deck author (``deck_builder``) is
clawpack-free / swan-free and unit-testable with no Fortran toolchain; the
``entrypoint`` runs the GPL SWAN binary in the container. Mirrors the geoclaw
worker package layout.
"""
