"""Allow ``python -m trid3nt_server`` invocation (job-0032 startup verification).

Delegates to ``trid3nt_server.main.run``, which supports ``--startup-only`` for
the M4 substrate acceptance criterion.
"""

from __future__ import annotations

from .main import run

raise SystemExit(run())
