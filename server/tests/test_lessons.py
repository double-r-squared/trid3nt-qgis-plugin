"""Lessons loop v1 (track 4, local-roadmap-2026-07-06) -- unit coverage.

Covers the four halves of ``grace2_agent.lessons`` plus the server envelope
handler:

- WRITE side: template distillation from a synthetic failed-then-corrected
  turn record (same-tool arg fix + tool-swap), the no-lesson negatives
  (transient retry, untyped failure, no correction), dedup hit-bump, the
  GRACE2_LESSONS off-gate, and the <= 40-word clamp.
- STORE: MAX_LESSONS cap with lowest-(hits, recency) eviction + JSONL
  persistence across a singleton reset.
- READ side: relevant-lesson selection with the "Past corrections" header,
  the 200-token budget, the score floor, the off-gate, and the injected-row
  hit-bump.
- THUMBS-DOWN stub: ``register_lesson`` row shape + text dedup + empty-text
  rejection, and server.py's ``_handle_lesson_add`` envelope handler (ack on
  success, typed TOOL_PARAMS_INVALID on a malformed payload).

ASCII only.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from grace2_agent import lessons
from grace2_agent.lessons import (
    LessonStore,
    get_lesson_store,
    lessons_appendix,
    lessons_enabled,
    observe_turn,
    register_lesson,
    reset_lesson_store,
)


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #


@pytest.fixture()
def lessons_env(tmp_path, monkeypatch):
    """Point the store at a tmp JSONL, arm the gate, isolate the singleton."""
    path = tmp_path / "lessons.jsonl"
    monkeypatch.setenv("GRACE2_LESSONS_PATH", str(path))
    monkeypatch.setenv("GRACE2_LESSONS", "on")
    reset_lesson_store()
    yield path
    reset_lesson_store()


def _call(tool: str, args: dict | None, *, success: bool, error_code: str | None = None) -> dict:
    return {
        "tool": tool,
        "args": args or {},
        "success": success,
        "error_code": error_code,
    }


USER_TEXT = "show me terrain around Fort Myers Florida with a hillshade layer"

FAILED_DEM = _call(
    "fetch_dem",
    {"bbox": [1, 2, 3, 4], "resolution": "very-high"},
    success=False,
    error_code="DEM_ARG_INVALID",
)
FIXED_DEM = _call(
    "fetch_dem", {"bbox": [1, 2, 3, 4], "resolution": 30}, success=True
)


# --------------------------------------------------------------------------- #
# Write side -- distillation
# --------------------------------------------------------------------------- #


def test_distill_same_tool_corrected_args(lessons_env):
    n = observe_turn(USER_TEXT, [FAILED_DEM, FIXED_DEM])
    assert n == 1
    rows = get_lesson_store().rows()
    assert len(rows) == 1
    row = rows[0]
    # Row schema: {id, created, trigger_text, wrong, right, lesson, hits, last_hit}
    for key in ("id", "created", "trigger_text", "wrong", "right", "lesson", "hits", "last_hit"):
        assert key in row, f"missing row key {key}"
    assert row["wrong"]["tool"] == "fetch_dem"
    assert row["wrong"]["error_code"] == "DEM_ARG_INVALID"
    assert row["right"]["tool"] == "fetch_dem"
    assert row["right"]["changed_args"] == ["resolution"]
    assert row["wrong"]["args_digest"] != row["right"]["args_digest"]
    # Template: first 8 words of user_text + tool + code + what changed.
    assert "show me terrain around Fort Myers Florida with" in row["lesson"]
    assert "fetch_dem failed with DEM_ARG_INVALID" in row["lesson"]
    assert "resolution" in row["lesson"]
    assert row["hits"] == 1


def test_distill_tool_swap_shared_arg(lessons_env):
    failed = _call(
        "fetch_buildings", {"bbox": [0, 0, 1, 1]}, success=False,
        error_code="UPSTREAM_UNAVAILABLE",
    )
    fixed = _call("fetch_osm_buildings", {"bbox": [0, 0, 1, 1]}, success=True)
    assert observe_turn("building footprints in Tampa", [failed, fixed]) == 1
    row = get_lesson_store().rows()[0]
    assert row["right"]["tool"] == "fetch_osm_buildings"
    assert "tool fetch_osm_buildings" in row["lesson"]


def test_distill_tool_not_found_swap_without_shared_args(lessons_env):
    failed = _call("fetch_elevation", None, success=False, error_code="TOOL_NOT_FOUND")
    fixed = _call("fetch_dem", {"bbox": [0, 0, 1, 1]}, success=True)
    assert observe_turn("elevation of Denver", [failed, fixed]) == 1
    assert get_lesson_store().rows()[0]["right"]["tool"] == "fetch_dem"


def test_no_lesson_for_transient_same_args_retry(lessons_env):
    failed = _call("fetch_dem", {"bbox": [1, 2, 3, 4]}, success=False, error_code="TIMEOUT")
    retried = _call("fetch_dem", {"bbox": [1, 2, 3, 4]}, success=True)
    assert observe_turn(USER_TEXT, [failed, retried]) == 0
    assert len(get_lesson_store()) == 0


def test_no_lesson_without_correction_or_typed_code(lessons_env):
    # (a) failure with NO later success at all
    assert observe_turn(USER_TEXT, [FAILED_DEM]) == 0
    # (b) later success is a different tool sharing NO arg key (not intent-matched)
    unrelated = _call("list_tools_in_category", {"category_id": "terrain"}, success=True)
    assert observe_turn(USER_TEXT, [FAILED_DEM, unrelated]) == 0
    # (c) UNTYPED failure (no error_code) never distills, even with a fix after
    untyped = dict(FAILED_DEM, error_code=None)
    assert observe_turn(USER_TEXT, [untyped, FIXED_DEM]) == 0
    assert len(get_lesson_store()) == 0


def test_write_side_gated_off_by_default(lessons_env, monkeypatch):
    monkeypatch.setenv("GRACE2_LESSONS", "off")
    assert not lessons_enabled()
    assert observe_turn(USER_TEXT, [FAILED_DEM, FIXED_DEM]) == 0
    assert len(get_lesson_store()) == 0
    monkeypatch.delenv("GRACE2_LESSONS")
    assert not lessons_enabled()  # unset == off (dark by default)


def test_dedup_bumps_hits_instead_of_appending(lessons_env):
    assert observe_turn(USER_TEXT, [FAILED_DEM, FIXED_DEM]) == 1
    # Same (tool, error code, changed-arg set) -> hit-bump, not a second row.
    assert observe_turn("different prompt, same failure shape", [FAILED_DEM, FIXED_DEM]) == 1
    rows = get_lesson_store().rows()
    assert len(rows) == 1
    assert rows[0]["hits"] == 2


def test_lesson_sentence_clamped_to_40_words(lessons_env):
    long_text = " ".join(f"word{i}" for i in range(60))
    failed = _call(
        "compute_zonal_stats",
        {f"arg{i}": i for i in range(30)},
        success=False,
        error_code="ARGS_INVALID",
    )
    fixed = _call("compute_zonal_stats", {"raster_uri": "x"}, success=True)
    assert observe_turn(long_text, [failed, fixed]) == 1
    lesson = get_lesson_store().rows()[0]["lesson"]
    assert len(lesson.split()) <= 40


def test_observe_turn_never_raises(lessons_env):
    # Garbage records must not raise (never-raise write side).
    assert observe_turn(None, [{"bogus": object()}, 42]) == 0  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Store -- cap, eviction, persistence
# --------------------------------------------------------------------------- #


def test_eviction_cap_drops_lowest_hits_then_oldest(lessons_env, monkeypatch):
    monkeypatch.setattr(lessons, "MAX_LESSONS", 3)
    store = get_lesson_store()
    for i in range(3):
        failed = _call(f"tool_{i}", {"a": 1}, success=False, error_code=f"CODE_{i}")
        fixed = _call(f"tool_{i}", {"a": 2}, success=True)
        observe_turn(f"prompt {i}", [failed, fixed])
    assert len(store) == 3
    # Bump tool_0 (hits=2) so tool_1 (hits=1, oldest recency) is the floor.
    observe_turn(
        "again", [
            _call("tool_0", {"a": 1}, success=False, error_code="CODE_0"),
            _call("tool_0", {"a": 2}, success=True),
        ],
    )
    # A 4th distinct lesson overflows the cap -> tool_1 is evicted.
    observe_turn(
        "prompt 3", [
            _call("tool_3", {"a": 1}, success=False, error_code="CODE_3"),
            _call("tool_3", {"a": 2}, success=True),
        ],
    )
    tools = {r["wrong"]["tool"] for r in store.rows()}
    assert len(store) == 3
    assert "tool_1" not in tools
    assert {"tool_0", "tool_2", "tool_3"} == tools


def test_store_persists_jsonl_across_reset(lessons_env):
    observe_turn(USER_TEXT, [FAILED_DEM, FIXED_DEM])
    raw = lessons_env.read_text(encoding="utf-8").strip().splitlines()
    assert len(raw) == 1 and json.loads(raw[0])["wrong"]["tool"] == "fetch_dem"
    reset_lesson_store()  # fresh singleton -> reload from disk
    rows = get_lesson_store().rows()
    assert len(rows) == 1 and rows[0]["wrong"]["error_code"] == "DEM_ARG_INVALID"


def test_store_tolerates_corrupt_lines(lessons_env):
    lessons_env.write_text(
        'not-json\n{"id":"x","lesson":"When asked about DEMs, pass resolution=30.",'
        '"trigger_text":"terrain","wrong":"NO_CALL","right":null,"hits":1,'
        '"last_hit":"2026-07-07T00:00:00+00:00","created":"2026-07-07T00:00:00+00:00"}\n'
        '{"no_lesson_key": true}\n',
        encoding="utf-8",
    )
    reset_lesson_store()
    assert len(get_lesson_store()) == 1  # only the well-formed row survives


# --------------------------------------------------------------------------- #
# Read side -- selection, budget, gate
# --------------------------------------------------------------------------- #


def test_read_side_injects_relevant_lessons_top2(lessons_env):
    observe_turn(USER_TEXT, [FAILED_DEM, FIXED_DEM])
    register_lesson(
        "When asked for flood depth in coastal towns, use run_sfincs not run_swmm.",
        "flood depth Mexico Beach hurricane surge",
    )
    register_lesson(
        "When asked about earthquake shaking, openquake needs an imt argument.",
        "earthquake shaking hazard Tokyo",
    )
    text = lessons_appendix("terrain hillshade around Fort Myers")
    assert text is not None
    assert text.startswith("Past corrections from this deployment:")
    assert "fetch_dem failed with DEM_ARG_INVALID" in text
    assert "earthquake" not in text  # irrelevant lesson stays out
    # top-2 cap: never more than MAX_INJECT_LESSONS bullet lines
    assert text.count("\n- ") <= lessons.MAX_INJECT_LESSONS


def test_read_side_score_floor_rejects_unrelated(lessons_env):
    observe_turn(USER_TEXT, [FAILED_DEM, FIXED_DEM])
    assert lessons_appendix("earthquake shaking scenario near Tokyo Japan") is None


def test_read_side_location_boilerplate_does_not_qualify(lessons_env):
    """A/B regression (2026-07-07): with SCORE_FLOOR=0.1 vs raw BM25, lessons
    whose only link to the prompt was shared location boilerplate ("downtown
    Tampa, Florida") injected on nearly every sweep turn and biased the small
    model toward the wrong tool. Once enough lessons share those tokens they
    are corpus boilerplate -- overlap on them alone must inject NOTHING."""
    for i, (tool_hint, topic) in enumerate(
        [
            ("compute_zonal_statistics", "zonal statistics of parcels"),
            ("fetch_field_boundaries", "farm field boundaries"),
            ("run_sfincs", "coastal flood surge depth"),
            ("compute_slope", "terrain slope steepness"),
            ("fetch_nexrad", "radar reflectivity mosaic"),
            ("run_modflow_job", "groundwater drawdown pumping"),
        ]
    ):
        register_lesson(
            f"When asked about {topic}, call {tool_hint} directly.",
            f"{topic} for the downtown Tampa, Florida area",
        )
    # Prompt shares ONLY the location boilerplate with every trigger.
    assert lessons_appendix(
        "Fetch a digital elevation model for downtown Tampa, Florida."
    ) is None


def test_read_side_off_gate_returns_none(lessons_env, monkeypatch):
    observe_turn(USER_TEXT, [FAILED_DEM, FIXED_DEM])
    monkeypatch.setenv("GRACE2_LESSONS", "off")
    assert lessons_appendix("terrain hillshade around Fort Myers") is None


def test_read_side_respects_token_budget(lessons_env):
    # Two maximal (40-word) lessons whose combined length exceeds the budget.
    filler = " ".join(["hillshade terrain Fort Myers elevation raster"] * 7)
    register_lesson(filler + " one", "terrain hillshade Fort Myers elevation")
    register_lesson(filler + " two", "terrain hillshade Fort Myers elevation")
    text = lessons_appendix("terrain hillshade Fort Myers elevation")
    assert text is not None
    budget_chars = lessons.TOKEN_BUDGET * lessons._CHARS_PER_TOKEN
    assert len(text) <= budget_chars
    assert len(text) // lessons._CHARS_PER_TOKEN <= lessons.TOKEN_BUDGET


def test_read_side_empty_store_returns_none(lessons_env):
    assert lessons_appendix("anything at all") is None


def test_read_side_injection_bumps_hits(lessons_env):
    observe_turn(USER_TEXT, [FAILED_DEM, FIXED_DEM])
    before = get_lesson_store().rows()[0]["hits"]
    assert lessons_appendix("terrain hillshade around Fort Myers") is not None
    after = get_lesson_store().rows()[0]["hits"]
    assert after == before + 1  # LRU signal: injected rows survive eviction


# --------------------------------------------------------------------------- #
# Thumbs-down stub -- register_lesson + the WS envelope handler
# --------------------------------------------------------------------------- #


def test_register_lesson_row_shape_and_dedup(lessons_env):
    row = register_lesson("Should have used the slope tool.", "steepness of this ridge")
    assert row["wrong"] == "NO_CALL"
    assert row["right"] is None
    assert row["lesson"] == "Should have used the slope tool."
    assert row["trigger_text"] == "steepness of this ridge"
    # Same text again -> dedup hit-bump, still one row.
    row2 = register_lesson("  Should   have used the slope tool. ", "other trigger")
    assert len(get_lesson_store()) == 1
    assert row2["hits"] == 2
    # register_lesson is NOT gated (explicit user feedback), unlike the auto loop.
    with pytest.raises(ValueError):
        register_lesson("   ")


class _MockWebSocket:
    """Collects every envelope ``send`` would have written to the wire."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, raw: Any) -> None:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
        self.sent.append(json.loads(raw) if isinstance(raw, str) else raw)


def _run_handler(payload: Any) -> _MockWebSocket:
    from grace2_agent.server import SessionState, _handle_lesson_add
    from grace2_contracts.common import new_ulid

    ws = _MockWebSocket()
    state = SessionState(session_id=new_ulid())
    asyncio.run(_handle_lesson_add(ws, state, payload))
    return ws


def test_lesson_add_envelope_handler_happy_path(lessons_env):
    ws = _run_handler(
        {"text": "Should have used the slope tool.", "trigger_text": "ridge steepness"}
    )
    assert len(ws.sent) == 1
    env = ws.sent[0]
    assert env["type"] == "lesson-added"
    assert env["payload"]["lesson"] == "Should have used the slope tool."
    assert env["payload"]["lesson_id"]
    rows = get_lesson_store().rows()
    assert len(rows) == 1 and rows[0]["wrong"] == "NO_CALL"
    assert rows[0]["trigger_text"] == "ridge steepness"


@pytest.mark.parametrize(
    "payload", [{}, {"text": "   "}, {"text": 42}, "not-a-dict", None]
)
def test_lesson_add_envelope_handler_malformed_payload(lessons_env, payload):
    ws = _run_handler(payload)
    assert len(ws.sent) == 1
    env = ws.sent[0]
    assert env["type"] == "error"
    assert env["payload"]["error_code"] == "TOOL_PARAMS_INVALID"
    assert len(get_lesson_store()) == 0
