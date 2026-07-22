"""Lessons loop v1 -- failed-then-corrected tool calls distill into advisory
system-prompt lessons (trid3nt-local roadmap 2026-07-06, track 4).

Design (binding sketch, deliberately simple -- no training, no extra LLM call):

- **Store**: JSONL at ``TRID3NT_LESSONS_PATH`` (default: ``lessons.jsonl`` under
  the file-persistence dir, ``/tmp/trid3nt_lessons.jsonl`` fallback -- same
  degrade pattern as ``telemetry.py``). One row per lesson::

      {id, created, trigger_text, wrong, right, lesson, hits, last_hit}

  ``wrong`` is ``{tool, args_digest, error_code}`` or the string ``"NO_CALL"``
  (thumbs-down stub rows); ``right`` is ``{tool, args_digest, changed_args}``
  or ``None``.

- **Write side (automatic, never-raise)**: ``observe_turn(user_text, calls)``
  runs at end-of-turn. When a call raised a TYPED error (``error_code`` set)
  and a LATER call in the SAME turn succeeded with a corrected variant --
  the same tool with different args, or a different tool with the same intent
  (mechanical proxy: the immediately-next success that shares an arg key, or
  any next success after a TOOL_NOT_FOUND) -- the (bad -> good) delta is
  distilled TEMPLATE-BASED into one imperative sentence (<= 40 words). Dedup
  key = (tool, error code, corrected tool, changed-arg set): a repeat bumps
  ``hits`` instead of appending.

- **Read side (per turn)**: ``lessons_appendix(user_text)`` scores stored
  lessons against the user prompt with BM25 (``rank_bm25.BM25Okapi`` -- the
  same scorer family the tool-retrieval / discover_dataset index uses, reusing
  its ``_tokenize``; plain token-overlap fallback when either import is
  unavailable). Top ``MAX_INJECT_LESSONS`` above ``SCORE_FLOOR`` are rendered
  as a "Past corrections from this deployment:" appendix capped at
  ``TOKEN_BUDGET`` (~4 chars/token estimate). Injected rows get a hit-bump so
  useful lessons survive eviction.

- **Guardrails**: ADVISORY TEXT ONLY (never mutates schemas or tool behavior);
  capped store (``MAX_LESSONS`` rows, evict lowest ``(hits, recency)``); env
  gate ``TRID3NT_LESSONS=off|on`` -- DEFAULT OFF, dark until the pass-3 harness
  benchmarks it (A/B: flip on + rerun the routing sweep).

- **Thumbs-down stub**: ``register_lesson(text, trigger_text)`` writes a
  user-authored lesson row (wrong=``"NO_CALL"``); the ``lesson-add`` WS
  envelope in server.py calls it. The web UI is out of scope here.

ASCII only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("trid3nt_server.lessons")

__all__ = [
    "LESSONS_ENV",
    "LESSONS_PATH_ENV",
    "MAX_LESSONS",
    "MAX_INJECT_LESSONS",
    "TOKEN_BUDGET",
    "SCORE_FLOOR",
    "LessonStore",
    "get_lesson_store",
    "reset_lesson_store",
    "lessons_enabled",
    "observe_turn",
    "lessons_appendix",
    "register_lesson",
]

#: Feature gate. ``on`` enables both sides of the automatic loop; anything
#: else (including unset) is OFF -- byte-identical pre-feature behavior.
LESSONS_ENV = "TRID3NT_LESSONS"

#: Store path override. Default resolves like the file-persistence substrate;
#: /tmp fallback mirrors ``telemetry.py``'s local-file degrade.
LESSONS_PATH_ENV = "TRID3NT_LESSONS_PATH"
_FALLBACK_LESSONS_PATH = "/tmp/trid3nt_lessons.jsonl"

#: Store cap -- evict lowest (hits, recency) beyond this.
MAX_LESSONS = 200

#: Read-side selection: at most this many lessons injected per turn.
MAX_INJECT_LESSONS = 2

#: Read-side appendix budget in (approximate) tokens; ~4 chars/token.
TOKEN_BUDGET = 200
_CHARS_PER_TOKEN = 4

#: Relevance floor -- a lesson below this BM25/overlap score is never injected
#: (an all-zero score means "no lexical relation to this prompt at all").
SCORE_FLOOR = 0.1

#: Distilled lesson sentences are clamped to this many words (spec: <= 40).
_MAX_LESSON_WORDS = 40

_APPENDIX_HEADER = "Past corrections from this deployment:"

_WORD_RE = re.compile(r"[A-Za-z0-9_]+")


# --------------------------------------------------------------------------- #
# Gate + path resolution
# --------------------------------------------------------------------------- #


def lessons_enabled() -> bool:
    """Live env read (never cached at import) -- ``TRID3NT_LESSONS=on`` only."""
    return os.environ.get(LESSONS_ENV, "off").strip().lower() == "on"


def _resolve_lessons_path() -> str:
    """Resolve the JSONL store path.

    Precedence: ``TRID3NT_LESSONS_PATH`` env > ``<persistence dir>/lessons.jsonl``
    (the same dir the FilePersistence substrate uses locally) > the /tmp
    fallback (mirrors ``telemetry.py``'s ``_get_telemetry_path`` degrade).
    """
    override = os.environ.get(LESSONS_PATH_ENV)
    if override:
        return override
    try:
        from .persistence import _default_dev_persistence_dir

        base = _default_dev_persistence_dir()
        base.mkdir(parents=True, exist_ok=True)
        return str(base / "lessons.jsonl")
    except Exception:  # noqa: BLE001 -- degrade to /tmp, never raise
        return _FALLBACK_LESSONS_PATH


# --------------------------------------------------------------------------- #
# Row helpers
# --------------------------------------------------------------------------- #


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    try:
        from trid3nt_contracts.common import new_ulid

        return new_ulid()
    except Exception:  # noqa: BLE001 -- an id is an id
        return uuid.uuid4().hex


def _args_digest(args: Any) -> str:
    """Short stable digest of a tool-call args dict (order-insensitive)."""
    try:
        canon = json.dumps(args or {}, sort_keys=True, default=str)
    except Exception:  # noqa: BLE001
        canon = repr(args)
    return hashlib.sha1(canon.encode("utf-8")).hexdigest()[:10]


def _first_words(text: str, n: int = 8) -> str:
    return " ".join((text or "").split()[:n])


def _clamp_words(text: str, n: int = _MAX_LESSON_WORDS) -> str:
    words = (text or "").split()
    return " ".join(words[:n])


def _dedup_key(row: dict) -> tuple:
    """(tool, error code, corrected tool, changed-arg set) -- or a text key
    for user-registered (``NO_CALL``) rows."""
    wrong = row.get("wrong")
    right = row.get("right")
    if not isinstance(wrong, dict):
        # thumbs-down stub / manual rows dedup by normalized lesson text.
        return ("manual", " ".join(str(row.get("lesson", "")).lower().split()))
    changed: tuple = ()
    right_tool = None
    if isinstance(right, dict):
        right_tool = right.get("tool")
        changed = tuple(sorted(right.get("changed_args") or []))
    return (wrong.get("tool"), wrong.get("error_code"), right_tool, changed)


# --------------------------------------------------------------------------- #
# Scoring (reuses the retrieval machinery -- no second index system)
# --------------------------------------------------------------------------- #


def _tokenize(text: str) -> list[str]:
    """Tokenize with discover_dataset's tokenizer (the one BM25 channel of the
    tool-retrieval index uses) when importable; regex fallback otherwise."""
    try:
        from .tools.discover_dataset import _tokenize as _dd_tokenize

        return _dd_tokenize(text)
    except Exception:  # noqa: BLE001
        if not isinstance(text, str):
            return []
        return [t.lower() for t in _WORD_RE.findall(text)]


def _content_tokens(tokens: list[str]) -> set[str]:
    """Stopword-filtered token set (reuses discover_dataset's stopword list --
    the same one the tool-retrieval name channel filters with)."""
    try:
        from .tools.discover_dataset import _STOPWORDS as stop
    except Exception:  # noqa: BLE001
        stop = {
            "the", "a", "an", "of", "in", "on", "for", "to", "and", "or",
            "with", "is", "are", "me", "my", "show", "what", "how", "please",
        }
    return {t for t in tokens if t not in stop}


def _distinctive_query_tokens(user_text: str, rows: list[dict]) -> set[str]:
    """Query tokens that are NOT boilerplate across the lesson corpus.

    A token appearing in at least half the stored lessons' trigger texts
    (location stubs, "fetch", "layer"...) carries no routing signal -- overlap
    on such tokens must not qualify a lesson for injection. Boilerplate needs
    corpus evidence: below 3 occurrences a token is never boilerplate, so a
    1-2 row store (where every matching token trivially hits df == n) does
    not degenerate to rejecting everything.
    """
    q = set(_tokenize(user_text))
    if not q or not rows:
        return q
    n = len(rows)
    df: dict[str, int] = {}
    for row in rows:
        for tok in set(_tokenize(row.get("trigger_text", ""))):
            df[tok] = df.get(tok, 0) + 1
    boilerplate_at = max(3, n / 2)
    return {t for t in q if df.get(t, 0) < boilerplate_at}


def _score_rows(user_text: str, rows: list[dict]) -> list[float]:
    """Score every row against ``user_text``.

    BM25 (``rank_bm25.BM25Okapi`` -- same library backing discover_dataset /
    tool_retrieval's lexical channel) over ``trigger_text + lesson`` docs. The
    corpus is <= MAX_LESSONS tiny docs, so building BM25 per call is
    microseconds -- no persistent second index. BM25's idf degenerates to
    <= 0 on very small corpora (a term in 1-of-1 or 1-of-2 docs gets no
    weight), so when BM25 yields nothing above the floor -- or rank_bm25 is
    unavailable -- degrade to a stopword-filtered content-token overlap count.
    """
    q_tokens = _tokenize(user_text)
    if not q_tokens or not rows:
        return [0.0] * len(rows)
    docs = [
        _tokenize(f"{row.get('trigger_text', '')} {row.get('lesson', '')}")
        for row in rows
    ]
    try:
        from rank_bm25 import BM25Okapi

        # BM25Okapi requires non-empty docs; substitute a never-matching token.
        safe_docs = [d if d else ["__empty__"] for d in docs]
        raw = [float(s) for s in BM25Okapi(safe_docs).get_scores(q_tokens)]
        if max(raw) > SCORE_FLOOR:
            return raw
    except Exception:  # noqa: BLE001 -- fall through to the overlap degrade
        pass
    q_content = _content_tokens(q_tokens)
    return [float(len(q_content & _content_tokens(d))) for d in docs]


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #


class LessonStore:
    """Capped JSONL-backed lesson store. Thread-safe; every write rewrites the
    file atomically (tmp + ``os.replace``) -- at <= 200 tiny rows this is
    cheaper than reconciling appends with dedup hit-bumps."""

    def __init__(self, path: str | None = None) -> None:
        self._path = path or _resolve_lessons_path()
        self._lock = threading.Lock()
        self._rows: list[dict] = []
        self._load()

    @property
    def path(self) -> str:
        return self._path

    def rows(self) -> list[dict]:
        """Snapshot copy (rows are shared dicts -- treat as read-only)."""
        with self._lock:
            return list(self._rows)

    def __len__(self) -> int:
        with self._lock:
            return len(self._rows)

    # -- persistence ------------------------------------------------------- #

    def _load(self) -> None:
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except Exception:  # noqa: BLE001 -- skip corrupt lines
                        continue
                    if isinstance(row, dict) and row.get("lesson"):
                        self._rows.append(row)
        except FileNotFoundError:
            pass
        except Exception:  # noqa: BLE001 -- an unreadable store starts empty
            logger.warning("lessons: store load failed (%s)", self._path, exc_info=True)

    def _flush_locked(self) -> None:
        """Atomic rewrite. Caller holds ``self._lock``. Never raises."""
        try:
            tmp = f"{self._path}.tmp.{os.getpid()}"
            with open(tmp, "w", encoding="utf-8") as fh:
                for row in self._rows:
                    fh.write(json.dumps(row, default=str) + "\n")
            os.replace(tmp, self._path)
        except Exception:  # noqa: BLE001 -- lessons are advisory, never fatal
            logger.warning("lessons: store flush failed (%s)", self._path, exc_info=True)

    # -- mutation ---------------------------------------------------------- #

    def upsert(self, row: dict) -> dict:
        """Insert ``row`` or bump the existing row with the same dedup key.

        Returns the stored (possibly pre-existing, hit-bumped) row. Enforces
        the ``MAX_LESSONS`` cap by evicting the lowest ``(hits, recency)``.
        """
        key = _dedup_key(row)
        now = _now_iso()
        with self._lock:
            for existing in self._rows:
                if _dedup_key(existing) == key:
                    existing["hits"] = int(existing.get("hits", 0)) + 1
                    existing["last_hit"] = now
                    self._flush_locked()
                    return existing
            row.setdefault("id", _new_id())
            row.setdefault("created", now)
            row.setdefault("hits", 1)
            row.setdefault("last_hit", now)
            self._rows.append(row)
            self._evict_locked()
            self._flush_locked()
            return row

    def touch(self, ids: list[str]) -> None:
        """Read-side hit-bump for injected lessons (keeps useful lessons off
        the eviction floor). Best-effort."""
        if not ids:
            return
        wanted = set(ids)
        now = _now_iso()
        with self._lock:
            hit = False
            for row in self._rows:
                if row.get("id") in wanted:
                    row["hits"] = int(row.get("hits", 0)) + 1
                    row["last_hit"] = now
                    hit = True
            if hit:
                self._flush_locked()

    def _evict_locked(self) -> None:
        """Drop the lowest ``(hits, last_hit-or-created)`` rows beyond the cap."""
        overflow = len(self._rows) - MAX_LESSONS
        if overflow <= 0:
            return
        def _rank(row: dict) -> tuple:
            return (
                int(row.get("hits", 0)),
                str(row.get("last_hit") or row.get("created") or ""),
            )
        victims = sorted(self._rows, key=_rank)[:overflow]
        victim_ids = {id(v) for v in victims}
        self._rows = [r for r in self._rows if id(r) not in victim_ids]


# Module singleton -- keyed to the resolved path so tests can repoint via
# ``TRID3NT_LESSONS_PATH`` + ``reset_lesson_store()``.
_STORE: LessonStore | None = None
_STORE_LOCK = threading.Lock()


def get_lesson_store() -> LessonStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = LessonStore()
        return _STORE


def reset_lesson_store() -> None:
    """Drop the singleton (tests: set ``TRID3NT_LESSONS_PATH`` then reset)."""
    global _STORE
    with _STORE_LOCK:
        _STORE = None


# --------------------------------------------------------------------------- #
# Write side -- automatic distillation (template-based, no LLM call)
# --------------------------------------------------------------------------- #


def _changed_arg_names(a: Any, b: Any) -> list[str]:
    """Arg names added, removed, or value-changed between two args dicts."""
    a = a if isinstance(a, dict) else {}
    b = b if isinstance(b, dict) else {}
    changed = set(a.keys()) ^ set(b.keys())
    for k in set(a.keys()) & set(b.keys()):
        if a[k] != b[k]:
            changed.add(k)
    return sorted(changed)


def _find_correction(calls: list[dict], failed_idx: int) -> dict | None:
    """The corrected-variant success for ``calls[failed_idx]``, or ``None``.

    Preference order (mechanical v1 heuristics):
      1. SAME tool, DIFFERENT args -- any later success (the model retried the
         tool with fixed arguments). A later same-tool same-args success is a
         transient upstream flake, NOT a correction -> no lesson.
      2. DIFFERENT tool, same intent -- only the IMMEDIATELY-NEXT successful
         dispatch counts, and only when it shares at least one arg name with
         the failed call (or the failure was TOOL_NOT_FOUND, where a tool swap
         is the only possible fix).
    """
    failed = calls[failed_idx]
    later = calls[failed_idx + 1 :]
    # 1. same tool, corrected args
    for c2 in later:
        if not c2.get("success") or c2.get("tool") != failed.get("tool"):
            continue
        if (c2.get("args") or {}) != (failed.get("args") or {}):
            return c2
        return None  # same tool + same args succeeded -> transient, no lesson
    # 2. different tool, same intent (first success only)
    for c2 in later:
        if not c2.get("success"):
            continue
        if c2.get("tool") == failed.get("tool"):
            return None
        shared = set((failed.get("args") or {}).keys()) & set(
            (c2.get("args") or {}).keys()
        )
        if shared or failed.get("error_code") == "TOOL_NOT_FOUND":
            return c2
        return None
    return None


def _distill(user_text: str, failed: dict, corrected: dict) -> dict:
    """Template-distill one (bad -> good) delta into a lesson row."""
    tool = str(failed.get("tool"))
    code = str(failed.get("error_code"))
    same_tool = corrected.get("tool") == failed.get("tool")
    if same_tool:
        changed = _changed_arg_names(failed.get("args"), corrected.get("args"))
        what = "changed args: " + ", ".join(changed) if changed else "corrected args"
    else:
        changed = []
        what = f"tool {corrected.get('tool')}"
    lesson = _clamp_words(
        f"When the user asks about {_first_words(user_text)}: {tool} failed "
        f"with {code}; the working call used {what}."
    )
    return {
        "id": _new_id(),
        "created": _now_iso(),
        "trigger_text": (user_text or "")[:400],
        "wrong": {
            "tool": tool,
            "args_digest": _args_digest(failed.get("args")),
            "error_code": code,
        },
        "right": {
            "tool": str(corrected.get("tool")),
            "args_digest": _args_digest(corrected.get("args")),
            "changed_args": changed,
        },
        "lesson": lesson,
        "hits": 1,
        "last_hit": _now_iso(),
    }


def observe_turn(user_text: str, calls: list[dict]) -> int:
    """End-of-turn write hook. NEVER raises; returns lessons written/bumped.

    ``calls`` is the turn's dispatch record, in order:
    ``[{tool, args, success, error_code}, ...]``. Gated on ``TRID3NT_LESSONS``
    (fully dark by default). A failed call only distills when it raised a
    TYPED error (``error_code`` set) AND a later call is a corrected variant
    (see ``_find_correction``).
    """
    try:
        if not lessons_enabled() or not calls:
            return 0
        store = get_lesson_store()
        written = 0
        for i, call in enumerate(calls):
            if call.get("success"):
                continue
            if not call.get("error_code"):
                continue  # only TYPED failures distill
            corrected = _find_correction(calls, i)
            if corrected is None:
                continue
            store.upsert(_distill(user_text, call, corrected))
            written += 1
        return written
    except Exception:  # noqa: BLE001 -- advisory feature, never break the turn
        logger.warning("lessons: observe_turn failed", exc_info=True)
        return 0


# --------------------------------------------------------------------------- #
# Read side -- per-turn advisory appendix
# --------------------------------------------------------------------------- #


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN)


def lessons_appendix(user_text: str, *, top_k: int = MAX_INJECT_LESSONS) -> str | None:
    """The system-prompt appendix for this turn, or ``None``.

    ``None`` when the gate is off, the store is empty, or nothing scores above
    ``SCORE_FLOOR``. Result is capped at ``TOKEN_BUDGET`` (~4 chars/token):
    lessons are added best-first while they fit; a first lesson that alone
    overflows is hard-truncated. Injected rows get a hit-bump (LRU signal).
    NEVER raises.
    """
    try:
        if not lessons_enabled():
            return None
        store = get_lesson_store()
        rows = store.rows()
        if not rows:
            return None
        scores = _score_rows(user_text, rows)
        ranked = sorted(
            zip(scores, rows), key=lambda p: p[0], reverse=True
        )
        # Relevance gates (A/B finding 2026-07-07): a raw-BM25 floor of 0.1
        # injected the top-2 of a tiny store on nearly EVERY turn -- sweep
        # telemetry showed ~1.5 injections/turn, mostly rows whose only link
        # to the prompt was location boilerplate ("downtown Tampa, Florida").
        # Irrelevant "when the user asks X call Y" text actively biases a
        # small model toward the wrong tool, so weak matches must inject
        # NOTHING. Two gates:
        #   1. distinctive-overlap: the row's trigger must share >= 2 query
        #      tokens that are NOT corpus boilerplate (present in less than
        #      half the stored lessons);
        #   2. relative floor: score >= 35% of the turn's best score, so a
        #      long tail of weak BM25 matches under a strong best is cut.
        top_score = ranked[0][0] if ranked else 0.0
        q_distinct = _distinctive_query_tokens(user_text, rows)
        picked: list[dict] = []
        for score, row in ranked:
            if score <= SCORE_FLOOR or score < 0.35 * top_score:
                break
            trigger_tokens = set(_tokenize(row.get("trigger_text", "")))
            if len(q_distinct & trigger_tokens) < 2:
                continue
            picked.append(row)
            if len(picked) >= max(1, top_k):
                break
        if not picked:
            return None
        budget_chars = TOKEN_BUDGET * _CHARS_PER_TOKEN
        text = _APPENDIX_HEADER
        used: list[dict] = []
        for row in picked:
            line = f"\n- {row.get('lesson', '')}"
            if len(text) + len(line) > budget_chars:
                if not used:
                    # even the first lesson overflows -- hard-truncate it
                    text = (text + line)[:budget_chars]
                    used.append(row)
                break
            text += line
            used.append(row)
        if not used:
            return None
        store.touch([r.get("id") for r in used if r.get("id")])
        return text
    except Exception:  # noqa: BLE001 -- advisory feature, never break the turn
        logger.warning("lessons: lessons_appendix failed", exc_info=True)
        return None


# --------------------------------------------------------------------------- #
# Thumbs-down stub (explicit user feedback; UI out of scope here)
# --------------------------------------------------------------------------- #


def register_lesson(text: str, trigger_text: str = "") -> dict:
    """Write a user-authored lesson row (the thumbs-down / lesson-add path).

    NOT gated on ``TRID3NT_LESSONS`` -- an explicit user correction is never
    dropped; the READ side stays dark until the gate flips, so registering
    while off has zero prompt effect. Dedups by normalized lesson text.
    Raises ``ValueError`` on empty text (the WS handler surfaces it typed).
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("lesson text must be a non-empty string")
    lesson = _clamp_words(" ".join(text.split()))
    row = {
        "id": _new_id(),
        "created": _now_iso(),
        "trigger_text": (trigger_text or lesson)[:400],
        "wrong": "NO_CALL",
        "right": None,
        "lesson": lesson,
        "hits": 1,
        "last_hit": _now_iso(),
    }
    return get_lesson_store().upsert(row)
