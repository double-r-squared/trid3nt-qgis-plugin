"""Allow ``python -m grace2_agent`` invocation (job-0032 startup verification).

Delegates to ``grace2_agent.main.run``, which supports ``--startup-only`` for
the M4 substrate acceptance criterion.
"""

from __future__ import annotations

from .main import run

raise SystemExit(run())
