"""Production auth-hardening surface (job-0252, sprint-13.5 Stage 1).

This module is the **production-hardening DELTA** layered on top of the
sprint-12 Wave 2 connect handshake (``auth_handshake.py``). It does NOT
re-implement Firebase verification — that already exists. It adds the two
things sprint-13.5 Decision #6 requires for a shareable production deploy:

1. **The ``AUTH_REQUIRED`` gate.** When ``AUTH_REQUIRED`` is on, an
   unauthenticated WebSocket (no valid Firebase ID token resolved by the
   ``auth_handshake`` path within the handshake window) is REJECTED — the
   socket closes with the Appendix A.5 close code ``4401`` and an A.6
   ``AUTH_FAILED`` error envelope. There is NO anonymous fallback on the
   required path (remove-don't-shim). When the gate is off (dev), the
   anonymous-fallback behavior from Wave 2 is preserved verbatim.

2. **The pre-Auth migration UID** (``MIGRATION_ANON_UID``). Cases written
   before the Auth track had no ``user_id`` field; a one-time startup
   migration (``persistence.migrate_preauth_cases``) stamps them with this
   constant so they belong to a single synthetic owner instead of leaking
   to every signed-in user. See OQ-0115-CASE-USER-LINK.

Decision #6 (sprint-13-5-decisions.md): production REQUIRES sign-in;
anonymous stays dev-only behind ``AUTH_REQUIRED=false``.

------------------------------------------------------------------------
DEFAULT-FLIP DECISION (job-0252, shipped 2026-06-11)
------------------------------------------------------------------------
The shipped CODE default for ``AUTH_REQUIRED`` is **"false"**, NOT "true".

Why: the running dev agent has no ``AUTH_REQUIRED`` env set. A default of
"true" would cause that agent to reject EVERY connection on its next
restart — breaking the user's live demo session. So the code default is
"false" for now, and the production deploy (job-0257) flips it to "true"
via the Cloud Run service env (``AUTH_REQUIRED=true``). The local-dev
runbooks/restart docs set ``AUTH_REQUIRED=false`` explicitly so the intent
is documented at both ends.

TODO(job-0257 / production deploy): the production Cloud Run env MUST set
``AUTH_REQUIRED=true``. Once production is the source of truth for the gate
and the dev runbooks reliably set ``AUTH_REQUIRED=false``, flip THIS code
default to "true" so "fail closed" is the out-of-the-box posture. Until
then the default stays "false" to protect the live dev/demo agent.
------------------------------------------------------------------------

SRS references:
- Appendix A.5 — Connection lifecycle: "agent validates the session; on
  failure, closes with code 4401 (unauthorized)".
- Appendix A.6 — ``AUTH_FAILED`` error code.
- Appendix H.1/H.3/H.5 — Firebase identity + anonymous fallback (the path
  this gate sits in front of).
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("grace2_agent.auth")

# --------------------------------------------------------------------------- #
# AUTH_REQUIRED gate
# --------------------------------------------------------------------------- #

#: Env var controlling the production sign-in gate (Decision #6).
AUTH_REQUIRED_ENV = "AUTH_REQUIRED"

#: Shipped CODE default. Deliberately "false" — see the DEFAULT-FLIP
#: DECISION block in this module's docstring. job-0257 flips production to
#: "true" via Cloud Run env; the dev runbooks set "false" explicitly.
#: TODO(job-0257): flip this default to "true" once prod is the gate's
#: source of truth and dev reliably sets AUTH_REQUIRED=false.
AUTH_REQUIRED_DEFAULT = "false"

#: WebSocket close code for an auth failure, per SRS Appendix A.5 step 2.
AUTH_CLOSE_CODE = 4401

#: A.6 error code emitted alongside the close.
AUTH_FAILED_ERROR_CODE = "AUTH_FAILED"

_TRUE_TOKENS = frozenset({"1", "true", "yes", "on"})


def auth_required() -> bool:
    """Return whether the production sign-in gate is engaged.

    Precedence (clear + explicit):
    1. The ``AUTH_REQUIRED`` env var, if set, wins. Truthy tokens are
       ``{"1", "true", "yes", "on"}`` (case-insensitive); everything else
       (including ``"false"``, ``"0"``, ``""``) is False.
    2. Absent → ``AUTH_REQUIRED_DEFAULT`` ("false" — the shipped default;
       see the DEFAULT-FLIP DECISION in the module docstring).

    Read at call time (not import time) so tests + Cloud Run env injection
    take effect without re-import, and so the running dev agent — which has
    NO ``AUTH_REQUIRED`` set — keeps its anonymous-fallback behavior.
    """
    raw = os.environ.get(AUTH_REQUIRED_ENV, AUTH_REQUIRED_DEFAULT)
    return raw.strip().lower() in _TRUE_TOKENS


# --------------------------------------------------------------------------- #
# Pre-Auth migration owner (OQ-0115-CASE-USER-LINK)
# --------------------------------------------------------------------------- #

#: Synthetic owner UID assigned to every pre-Auth Case (a Case written
#: before the Auth track carried no ``user_id`` field). The one-time
#: idempotent startup migration (``persistence.migrate_preauth_cases``)
#: stamps these orphan Cases with this constant so they belong to a single
#: synthetic owner instead of leaking to every signed-in user via the old
#: ``$exists:false`` backward-compat clause (now removed).
#:
#: Chosen as a fixed, non-ULID, obviously-synthetic sentinel so it is
#: trivially greppable in logs/Mongo and can never collide with a real
#: Firebase UID (which is a 28-char alphanumeric) or a ULID (26-char
#: Crockford base32).
MIGRATION_ANON_UID = "__preauth_migration_anon__"


__all__ = [
    "AUTH_REQUIRED_ENV",
    "AUTH_REQUIRED_DEFAULT",
    "AUTH_CLOSE_CODE",
    "AUTH_FAILED_ERROR_CODE",
    "MIGRATION_ANON_UID",
    "auth_required",
]
