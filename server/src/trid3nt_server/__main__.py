"""Allow ``python -m trid3nt_server`` invocation (startup verification).

Delegates to ``trid3nt_server.main.run``, which supports ``--startup-only`` for
a startup-only verification run.
"""

from __future__ import annotations

from .main import run

raise SystemExit(run())
