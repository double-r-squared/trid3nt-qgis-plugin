"""TRID3NT chat-surface widgets: message bubbles, tool card, gate + sim cards.

Split out of dock.py (2026-07-21 flat->package restructure). The QDockWidget shell
stays in ``dock.py`` and imports these. Behavior identical -- this is a move.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import gate
from ._style import (
    _PROBE_ERROR_BLOCK_STYLE,
    _STATUS_LINE_STYLE,
    _THINKING_BLOCK_STYLE,
    _THINKING_TOGGLE_STYLE,
)
from ..net.trid3nt_client import PipelineStep


_ASSISTANT_BUBBLE_STYLE = (
    "background-color: palette(midlight); border-radius: 8px; padding: 6px 9px;"
)
_ERROR_LINE_STYLE = "color: #f85149; font-size: 8pt; padding-left: 4px;"
# Amber caution frame for the gate card (mirrors the web's warning palette).
# Item N6 (live-feedback 2026-07-19): a SUBTLE amber fill (low-alpha accent,
# so it tints faintly over EITHER a light or dark QGIS window background --
# no hard color that assumes one theme) so the card reads as a distinct
# panel, not an outline floating on the chat.
# STYLE-1 (NATE 2026-07-20): scope the fill to the FRAME (#gatecard) so it does
# NOT cascade onto the child text labels. A bare "QFrame { background-color }"
# selector tints every descendant QLabel too -> the "highlighted text" NATE did
# not want. The card FILL is kept (it is enough); only the text highlight is gone.
_GATE_CARD_STYLE = (
    "QFrame#gatecard { border: 1px solid #d29922; border-radius: 8px; "
    "background-color: rgba(210, 153, 34, 7%); }"
)
_GATE_TITLE_STYLE = "color: #d29922; font-weight: bold; border: none;"
_GATE_BODY_STYLE = "border: none; font-size: 9pt;"
_GATE_NOTE_STYLE = "border: none; color: palette(mid); font-size: 8pt;"
# Item R2 (live-feedback 2026-07-18): tool-usage chip -- a compact bordered
# monospace badge so tool calls read as a visually DISTINCT class from the
# grey info notes (layer added / thinking / chart pointers). The muted
# detail text (state + substep + short arg summary) rides beside it in the
# same row.
#
# Item N3 (live-feedback 2026-07-19): the chip color now tracks STATE instead
# of a fixed blue -- GREEN on success (complete), GREY while in progress
# (pending / running / unknown), RED on failure (failed / cancelled). Kept
# subtle: an OUTLINED chip (border + text in the state color), never a loud
# fill. ``_tool_chip_style`` composes the stylesheet from the step state.
_CHIP_STATE_COLORS = {
    "complete": "#3fb950",   # green -- success
    "failed": "#f85149",     # red -- failure
    "cancelled": "#f85149",  # red -- failure
}
_CHIP_PENDING_COLOR = "#8b949e"  # grey -- pending / running / unknown


def _tool_chip_style(state: Optional[str]) -> str:
    """Item N3 (live-feedback 2026-07-19): outlined tool-chip stylesheet whose
    border+text color is driven off the step state (green complete / grey
    in-progress / red failed). Same monospace badge chrome as before."""
    color = _CHIP_STATE_COLORS.get((state or "").lower(), _CHIP_PENDING_COLOR)
    return (
        f"font-family: monospace; font-size: 8pt; color: {color}; "
        f"border: 1px solid {color}; border-radius: 7px; padding: 0px 6px;"
    )


_TOOL_CHIP_DETAIL_STYLE = "color: palette(mid); font-size: 8pt; border: none;"
# Item N2 (live-feedback 2026-07-19): nested/sub-step tool rows render as a
# directory tree -- an ASCII "|->" connector before the child chip, kept in
# the blue accent, so a parent -> child hierarchy reads clearly instead of a
# flat indented chip list.
_TREE_CONNECTOR_STYLE = (
    "font-family: monospace; font-size: 8pt; color: #58a6ff; border: none;"
)

# T1..T7 (NATE 2026-07-20): the tool-call surface is now ONE parent card (a
# QFrame containing the inner tool rows) -- NOT the flat chip pills above. The
# constants below drive ``_ToolCard`` (the single builder used by BOTH the live
# pipeline path AND the case-open replay path, T9).
#
# T2: the card BORDER spans the full chat width and adapts on resize (the frame
# is a plain QVBoxLayout child -- no fixed/min width -- so it fills whatever the
# resizable dock gives it), and the font is a notch LARGER than the old 8pt
# pills (9pt here). A subtle border, no loud fill (theme-neutral -- reads over a
# light or dark QGIS window alike, matching the gate/sim card discipline).
_TOOLCARD_FRAME_STYLE = (
    "QFrame#toolcard { border: 1px solid palette(mid); border-radius: 8px; "
    "background-color: rgba(128, 128, 128, 6%); }"
)
# The chevron + "Tools (N)" header line at the top of the card (T3).
_TOOLCARD_HEADER_STYLE = (
    "color: palette(text); font-size: 9pt; border: none; text-align: left;"
)
# The muted metadata block pinned at the BOTTOM of the card body (T7).
_TOOLCARD_META_STYLE = "color: palette(mid); font-size: 8pt; border: none;"
# The small ">" nesting prefix on every inner row (T4).
_TOOLCARD_PREFIX_STYLE = (
    "font-family: monospace; font-size: 9pt; color: palette(mid); border: none;"
)


def _tool_row_text_style(state: Optional[str]) -> str:
    """T4 (NATE 2026-07-20): the inner tool-row LABEL keeps the exact
    state-driven TEXT COLOR the old chip used (green complete / grey in-progress
    / red failed via ``_CHIP_STATE_COLORS``) -- only the chip's border/padding/
    radius (the "bubble frame") is dropped, and the font bumps 8pt -> 9pt (T2's
    larger card). No border, no background -- just the coloured monospace label
    nested under the parent card."""
    color = _CHIP_STATE_COLORS.get((state or "").lower(), _CHIP_PENDING_COLOR)
    return f"font-family: monospace; font-size: 9pt; color: {color}; border: none;"


def _tool_status_style(state: Optional[str]) -> str:
    """T5: the right-edge status glyph (animated spinner while running, a check
    on success, an x on failure) is coloured off the same state map."""
    color = _CHIP_STATE_COLORS.get((state or "").lower(), _CHIP_PENDING_COLOR)
    return f"font-family: monospace; font-size: 9pt; color: {color}; border: none;"


# T5: the classic ascii spinner cycled on a QTimer while a row is RUNNING; the
# terminal glyphs replace the old "running..."/"completed" words (a check on
# success, an x on failure). These check/x symbols are the status glyphs NATE
# explicitly sanctioned (they are text symbols, not emoji).
_SPINNER_FRAMES = ("|", "/", "-", "\\")
_STATUS_GLYPH_DONE = "✓"  # check mark
_STATUS_GLYPH_FAIL = "✗"  # ballot x
_TERMINAL_STATES = {"complete", "failed", "cancelled"}


def _is_running_state(state: Optional[str]) -> bool:
    """A row is RUNNING (spinner) when its state is not one of the terminal
    values -- pending / running / unknown all animate; complete/failed/
    cancelled show the terminal glyph."""
    return (state or "").lower() not in _TERMINAL_STATES

# Item R4 (live-feedback 2026-07-18): simulation-card chrome -- purple, the
# color the web reserves for sim progress affordances; the collapse pattern
# itself is the exact GateCard summary + "show details" affordance.
# Item N6 (live-feedback 2026-07-19): a SUBTLE purple fill (low-alpha accent,
# tints faintly over either a light or dark QGIS window -- no hard theme
# assumption) so the card reads as a distinct panel, not a bare outline.
_SIM_CARD_STYLE = (
    "QFrame#simcard { border: 1px solid #8957e5; border-radius: 8px; "
    "background-color: rgba(137, 87, 229, 7%); }"
)
_SIM_TITLE_STYLE = "color: #8957e5; font-weight: bold; border: none;"


class _WrapLabel(QLabel):
    """Word-wrapping QLabel whose WRAPPED height the layouts actually honor.

    BUG 1 (live-feedback 2026-07-12): "show me the landcover over washington
    state" painted as ONE clipped visual line in the user bubble. Classic Qt
    wrapped-label clip: QBoxLayout gives an alignment-constrained item its
    sizeHint height -- computed at the UNWRAPPED width -- so the label's real
    wrapped height (heightForWidth of the width it actually got) is never
    honored (measured pre-fix: 73px painted vs 133px needed at a 320px-wide
    dock; the assistant bubble clipped the same way, 153px vs 193px). Fix:
    re-assert minimumHeight from heightForWidth(actual width) on every
    resize/setText -- minimum sizes propagate through every layout +
    scroll-area combination even where height-for-width does not.

    Feature 2026-07-13 (markdown answers): finalized assistant labels
    switch to Qt.RichText HTML -- heightForWidth then routes through the
    rich-text document layout instead of plain-text metrics, but the same
    min-height re-assert covers it (verified by the markdown-height case
    in tests/qt_dock_ui_harness.py at 320px and 640px dock widths).
    """

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self.setWordWrap(True)

    def _sync_min_height(self) -> None:
        width = self.width()
        if width <= 0:
            return
        # QLabel.heightForWidth CLAMPS to the current minimumHeight
        # (measured: minH=541 -> hfw(594px)=541 where the true wrapped
        # height is 409), which turned this re-assert into a ratchet: once
        # set tall at a narrow width it could never shrink back when the
        # dock got wider. Clear the minimum first so hfw reports the TRUE
        # wrapped height, then re-assert (no event loop runs in between).
        if self.minimumHeight() > 0:
            self.setMinimumHeight(0)
        wrapped = self.heightForWidth(width)
        if wrapped > 0 and wrapped != self.minimumHeight():
            self.setMinimumHeight(wrapped)

    def setText(self, text: str) -> None:  # noqa: N802 -- Qt-mandated name
        super().setText(text)
        self._sync_min_height()

    def resizeEvent(self, event) -> None:  # noqa: N802 -- Qt-mandated name
        super().resizeEvent(event)
        self._sync_min_height()


class _ChatInput(QPlainTextEdit):
    """Item A (qgis-ux-batch 2026-07-19): the composer input -- a MULTI-LINE
    auto-growing field (was a one-line QLineEdit that clipped long prompts, so
    a long self-audit prompt scrolled off the right edge invisibly). ENTER
    sends (calls ``send_callback``, the dock's ``_send``); SHIFT+ENTER (and any
    Ctrl/Meta chord) inserts a newline instead. The field grows with its
    content from one line up to ``_MAX_LINES`` then scrolls -- mirrors the web
    composer's textarea. Word-wrap is on so a long single-line prompt wraps
    into the growing box instead of scrolling horizontally.
    """

    _MIN_LINES = 1
    _MAX_LINES = 10

    def __init__(self, send_callback, parent=None):
        super().__init__(parent)
        self._send_callback = send_callback
        self.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # C2 (NATE 2026-07-20): grow RELIABLY one row per new line / wrap. Drive
        # off the document LAYOUT's documentSizeChanged -- it fires AFTER the
        # relayout resolves the new document height, so the box never lags a row
        # behind (the old textChanged hook fired BEFORE relayout, so the height
        # was computed from the pre-edit layout and trailed the text). Grow, do
        # not scroll, until the _MAX_LINES cap (scrollbar stays OFF above).
        self.document().documentLayout().documentSizeChanged.connect(
            self._adjust_height
        )
        self._adjust_height()

    def keyPressEvent(self, event) -> None:  # noqa: N802 -- Qt-mandated name
        key = event.key()
        if key in (Qt.Key_Return, Qt.Key_Enter):
            # SHIFT+ENTER inserts a newline; a bare ENTER sends. Ctrl/Meta are
            # treated like Shift (never send) so a stray chord never fires an
            # accidental send mid-edit.
            if event.modifiers() & (
                Qt.ShiftModifier | Qt.ControlModifier | Qt.MetaModifier
            ):
                super().keyPressEvent(event)
                return
            self._send_callback()
            return
        super().keyPressEvent(event)

    def _adjust_height(self, *args) -> None:  # documentSizeChanged passes a QSizeF
        # C2 (NATE 2026-07-20): compute the field height FROM the document layout
        # so it visibly grows one row per new line (or wrap) and shrinks back
        # when lines are removed. IMPORTANT quirk: for a QPlainTextEdit the
        # layout is QPlainTextDocumentLayout, whose ``documentSize().height()``
        # is the LINE COUNT (visual lines, wraps included) -- NOT pixels (that
        # lines-as-pixels confusion was the old break). So convert to pixels by
        # multiplying the row count by the per-line spacing:
        #   h = lineSpacing * ceil(doc.size().height()) + 2*documentMargin
        #       + 2*frameWidth
        # clamped between one line and _MAX_LINES lines. A single Shift+Enter
        # bumps the line count by one -> +one lineSpacing -> exactly one new row
        # (grow, do not scroll, until the cap -- the vertical scrollbar is OFF).
        import math

        doc = self.document()
        line_h = self.fontMetrics().lineSpacing()
        margin = int(doc.documentMargin()) * 2
        frame = int(self.frameWidth()) * 2
        chrome = margin + frame
        lines = max(self._MIN_LINES, math.ceil(doc.size().height()))
        min_h = line_h * self._MIN_LINES + chrome
        max_h = line_h * self._MAX_LINES + chrome
        needed = line_h * lines + chrome
        height = int(min(max(needed, min_h), max_h))
        # Apply BOTH bounds so the field is pinned to exactly this height (the
        # layout cannot stretch/squeeze it away from the row count).
        self.setMinimumHeight(height)
        self.setMaximumHeight(height)


def _markdown_to_display_html(text: str, palette) -> str:
    """Render assistant markdown to Qt rich-text HTML (feature 2026-07-13).

    Why md->HTML (QTextDocument.setMarkdown + toHtml) instead of the
    lighter QLabel.setTextFormat(Qt.MarkdownText): the label route parses
    the same GitHub dialect but offers ZERO styling hooks -- measured on
    this Qt build (5.15.15), fenced code blocks come out in the default
    PROPORTIONAL font with no background (the importer stamps a
    FontFamilies property that resolves empty instead of a real monospace
    family), and tables get no cell padding. Going through the document
    lets us style the model before serializing: code blocks get a
    palette-derived background + a real monospace font, inline code spans
    get the same treatment, tables get solid borders + cell padding. The
    HTML then renders in a Qt.RichText label -- same QTextDocument engine,
    so wrapping/heightForWidth behave like any rich-text label.

    Colors come from ``palette`` (Base for the code background, Mid for
    table borders) so light and dark QGIS themes both stay readable --
    no hardcoded hex that assumes one theme.

    Raises on truly broken input (caller catches and keeps plain text).
    """
    from qgis.PyQt.QtGui import (
        QBrush,
        QPalette,
        QTextCharFormat,
        QTextCursor,
        QTextDocument,
        QTextFormat,
        QTextFrameFormat,
        QTextTable,
    )

    code_bg = palette.color(QPalette.Base)
    border = palette.color(QPalette.Mid)

    doc = QTextDocument()
    doc.setMarkdown(text)  # default features = GitHub dialect (tables, fences)

    # Collect first, mutate after: merging char formats mid-iteration can
    # split/merge the fragment list under the iterator. Positions stay valid
    # across the merges (formatting never changes text length).
    code_blocks: List[int] = []                 # block positions
    code_spans: List[Tuple[int, int, bool]] = []  # (pos, length, in_code_block)
    block = doc.begin()
    while block.isValid():
        fmt = block.blockFormat()
        is_code_block = fmt.hasProperty(QTextFormat.BlockCodeLanguage) or (
            hasattr(QTextFormat, "BlockCodeFence")
            and fmt.hasProperty(QTextFormat.BlockCodeFence)
        )
        if is_code_block:
            code_blocks.append(block.position())
        it = block.begin()
        while not it.atEnd():
            frag = it.fragment()
            cf = frag.charFormat()
            # The markdown importer marks code (fenced AND inline) with a
            # FontFamilies property (empty on this build -- hence the
            # monospace re-stamp) and/or fontFixedPitch.
            if (
                is_code_block
                or cf.fontFixedPitch()
                or cf.hasProperty(QTextFormat.FontFamilies)
            ):
                code_spans.append((frag.position(), frag.length(), is_code_block))
            it += 1
        block = block.next()

    for pos in code_blocks:
        cur = QTextCursor(doc)
        cur.setPosition(pos)
        bf = cur.blockFormat()
        bf.setBackground(code_bg)
        cur.setBlockFormat(bf)
    for pos, length, in_code_block in code_spans:
        cur = QTextCursor(doc)
        cur.setPosition(pos)
        cur.setPosition(pos + length, QTextCursor.KeepAnchor)
        mono = QTextCharFormat()
        mono.setFontFamily("monospace")
        mono.setFontFixedPitch(True)
        if not in_code_block:
            # Inline code: per-span background (block bg covers the fences).
            mono.setBackground(code_bg)
        cur.mergeCharFormat(mono)

    def _style_tables(frame) -> None:
        for child in frame.childFrames():
            if isinstance(child, QTextTable):
                tf = child.format()
                tf.setBorder(0.5)
                tf.setBorderBrush(QBrush(border))
                tf.setBorderStyle(QTextFrameFormat.BorderStyle_Solid)
                tf.setCellPadding(4.0)
                tf.setCellSpacing(0.0)
                child.setFormat(tf)
            _style_tables(child)

    _style_tables(doc.rootFrame())
    return doc.toHtml()


def _is_error_note(note: str) -> bool:
    """BUG 3a (live-feedback 2026-07-12): materializer notes are plain
    strings, so classify by the honest failure vocabulary layers.py uses --
    error-ish notes must stay VISIBLE outside the collapsed Layers toggle."""
    lowered = note.lower()
    return any(
        token in lowered
        for token in ("fail", "error", "skipp", "reject", "unknown")
    )


class _ToolCard(QFrame):
    """T1..T7/T9 (NATE 2026-07-20): ONE parent tool card -- a full-width QFrame
    that CONTAINS the inner tool calls, replacing the old flat chip pills.

    There is exactly ONE representation: this parent card IS the container; the
    inner tool calls live inside it (never a small pill AND a large card). The
    SAME widget is built by both the live pipeline path (``_AssistantEntry.
    render_tool_card``) and the case-open replay path (``_replay_tool_group``),
    so a reopened case shows the identical parent format (T9).

    Layout:

      [chevron]  Tools (N)                      <- header (T3)
        > tool_a                            [glyph]
        > tool_b                            [glyph]   <- inner rows (T4/T5/T6)
        (optional collapsible result body under a row, replay only -- T9)
        <one muted metadata block>                    <- bottom of body (T7)

    T3 collapse rules: the inner body is EXPANDED while ANY inner tool runs (so
    the user can watch live) and COLLAPSES once every inner tool is terminal --
    and it re-expands if a new running row appears after an intermediate
    all-terminal frame. The FIRST chevron click latches ``_user_toggled`` and
    auto-collapse never fights the user's manual choice thereafter.

    T5 spinner: a shared ``QTimer`` cycles the ascii spinner frames across every
    RUNNING row's status glyph; terminal rows show a check (success) or x
    (failure) instead. The timer only runs while at least one row is running.
    """

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("toolcard")
        self.setStyleSheet(_TOOLCARD_FRAME_STYLE)
        self.setFrameShape(QFrame.NoFrame)
        # T2: fill the chat width, adapt on resize -- no fixed/min width.
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 4, 8, 4)
        outer.setSpacing(2)

        # Header: chevron (T3) + "Tools (N)" title. The chevron is a native
        # QToolButton arrow (style-drawn triangle -- not an emoji/text glyph).
        header = QWidget()
        hl = QHBoxLayout(header)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(4)
        self._chevron = QToolButton()
        self._chevron.setAutoRaise(True)
        self._chevron.setArrowType(Qt.DownArrow)  # expanded by default
        self._chevron.setStyleSheet("QToolButton { border: none; }")
        self._chevron.clicked.connect(self._toggle)
        hl.addWidget(self._chevron)
        self._title = QLabel("Tools")
        self._title.setTextFormat(Qt.PlainText)
        self._title.setStyleSheet(_TOOLCARD_HEADER_STYLE)
        hl.addWidget(self._title)
        hl.addStretch(1)
        outer.addWidget(header)

        # Collapsible body: the inner tool rows + the bottom metadata block.
        self._body = QWidget()
        self._body_lay = QVBoxLayout(self._body)
        self._body_lay.setContentsMargins(2, 0, 0, 0)
        self._body_lay.setSpacing(1)
        outer.addWidget(self._body)

        self._expanded = True             # default EXPANDED while running (T3)
        # T3 (fixed 2026-07-21): the collapse is AUTO (expand while any inner
        # tool runs, collapse once ALL are terminal) UNTIL the user clicks the
        # chevron -- then ``_user_toggled`` latches and auto never fights their
        # choice again. The old one-shot ``_auto_collapsed`` latched on the
        # FIRST all-terminal frame (tool A done before B starts) and never
        # re-expanded when B began; tracking manual intent instead fixes that.
        self._user_toggled = False
        self._spinner_labels: List[QLabel] = []
        self._spinner_frame = 0
        self._spinner_timer = QTimer(self)
        self._spinner_timer.setInterval(120)
        self._spinner_timer.timeout.connect(self._tick_spinner)

    # -- collapse ---------------------------------------------------------- #

    def _apply_expanded(self) -> None:
        self._body.setVisible(self._expanded)
        self._chevron.setArrowType(
            Qt.DownArrow if self._expanded else Qt.RightArrow
        )

    def _toggle(self) -> None:
        self._user_toggled = True  # from now on, auto-collapse never overrides
        self._expanded = not self._expanded
        self._apply_expanded()

    # -- spinner (T5) ------------------------------------------------------ #

    def _tick_spinner(self) -> None:
        self._spinner_frame = (self._spinner_frame + 1) % len(_SPINNER_FRAMES)
        frame = _SPINNER_FRAMES[self._spinner_frame]
        for lbl in self._spinner_labels:
            lbl.setText(frame)

    def _clear_body(self) -> None:
        self._spinner_labels = []
        while self._body_lay.count():
            item = self._body_lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    # -- the ONE builder used by live + replay (T9) ------------------------ #

    def set_content(self, inner_rows: List[dict], meta_lines: List[str]) -> None:
        """Rebuild the inner rows + bottom metadata from ``inner_rows`` (each a
        ``{"label", "state", "nested", "result", "is_error"}`` dict) and
        ``meta_lines`` (muted strings). Called every live pipeline frame (cheap
        rebuild -- the frame/chevron/collapse state persist on ``self``) and
        once on replay (all-terminal rows -> immediate auto-collapse)."""
        self._clear_body()
        any_running = False
        n_tools = 0
        for row in inner_rows:
            label = str(row.get("label") or "")
            if not label:
                continue
            n_tools += 1
            state = row.get("state")
            nested = bool(row.get("nested"))
            running = _is_running_state(state)
            if running:
                any_running = True

            row_w = QWidget()
            rl = QHBoxLayout(row_w)
            # T4: a small ">" prefix for visual nesting (drops the old bubble
            # frame + tree-connector arrow that cut into the text); a deeper
            # inset for a sub-step child so hierarchy still reads.
            rl.setContentsMargins(4 + (12 if nested else 0), 0, 0, 0)
            rl.setSpacing(6)
            prefix = QLabel(">")
            prefix.setTextFormat(Qt.PlainText)
            prefix.setStyleSheet(_TOOLCARD_PREFIX_STYLE)
            rl.addWidget(prefix)
            # T4: the label keeps the EXACT state-driven text colour + plain
            # non-wrapping behaviour of the old chip (only the frame is gone).
            name_lbl = QLabel(label)
            name_lbl.setTextFormat(Qt.PlainText)
            name_lbl.setStyleSheet(_tool_row_text_style(state))
            rl.addWidget(name_lbl)
            rl.addStretch(1)
            # T5/T6: the ONLY right-edge element is the status glyph now (the
            # per-row arg/metadata summary is dropped -- it moves to the bottom
            # block, T7). Running -> animated spinner; complete -> check;
            # failed/cancelled -> x.
            status = QLabel()
            status.setTextFormat(Qt.PlainText)
            status.setStyleSheet(_tool_status_style(state))
            if running:
                status.setText(_SPINNER_FRAMES[self._spinner_frame])
                self._spinner_labels.append(status)
            else:
                status.setText(
                    _STATUS_GLYPH_FAIL
                    if (state or "").lower() in ("failed", "cancelled")
                    else _STATUS_GLYPH_DONE
                )
            rl.addWidget(status)
            self._body_lay.addWidget(row_w)

            # T9 (replay): the tool RESULT shows UNDER its row, inside the card,
            # as a collapsed read-only body (error responses get the red block).
            result = row.get("result")
            if isinstance(result, str) and result:
                is_error = bool(row.get("is_error"))
                toggle = QPushButton("Result")
                toggle.setFlat(True)
                toggle.setCheckable(True)
                toggle.setChecked(False)
                toggle.setStyleSheet(_THINKING_TOGGLE_STYLE)
                body = _WrapLabel(result)
                body.setTextFormat(Qt.PlainText)
                body.setTextInteractionFlags(Qt.TextSelectableByMouse)
                body.setStyleSheet(
                    _PROBE_ERROR_BLOCK_STYLE if is_error else _THINKING_BLOCK_STYLE
                )
                body.setMinimumWidth(1)  # E1: never force a horizontal scrollbar
                body.setVisible(False)
                toggle.clicked.connect(
                    lambda _c=False, b=body, t=toggle: b.setVisible(t.isChecked())
                )
                self._body_lay.addWidget(toggle)
                self._body_lay.addWidget(body)

        # T7: one muted metadata block pinned at the BOTTOM of the body, under
        # all the inner rows, so every bit of text lives inside the card border.
        clean_meta = [m for m in meta_lines if m]
        if clean_meta:
            meta = _WrapLabel("\n".join(clean_meta))
            meta.setTextFormat(Qt.PlainText)
            meta.setStyleSheet(_TOOLCARD_META_STYLE)
            meta.setMinimumWidth(1)  # E1: wrap, never a horizontal scrollbar
            self._body_lay.addWidget(meta)

        self._title.setText(f"Tools ({n_tools})" if n_tools else "Tools")

        # T5: run the spinner only while a row is live.
        if any_running and not self._spinner_timer.isActive():
            self._spinner_timer.start()
        elif not any_running and self._spinner_timer.isActive():
            self._spinner_timer.stop()

        # T3: auto behavior (until the user takes manual control) -- EXPANDED
        # while any inner tool runs (so NATE can monitor), COLLAPSED once every
        # inner tool is terminal. Re-expands if a new running row appears after
        # an intermediate all-terminal frame; a manual chevron click disables it.
        if n_tools and not self._user_toggled:
            self._expanded = any_running
        self._apply_expanded()


class _AssistantEntry:
    """One pending/complete assistant bubble + its status-line area."""

    def __init__(self, parent_layout: QVBoxLayout):
        self.container = QWidget()
        lay = QVBoxLayout(self.container)
        lay.setContentsMargins(0, 2, 40, 2)
        lay.setSpacing(2)

        # F9 thinking block: toggle button + collapsible text label.
        # Hidden until the first thinking-chunk arrives.
        self._thinking_container = QWidget()
        thinking_lay = QVBoxLayout(self._thinking_container)
        thinking_lay.setContentsMargins(0, 0, 0, 0)
        thinking_lay.setSpacing(0)

        self._thinking_toggle = QPushButton("Thinking...")
        self._thinking_toggle.setFlat(True)
        self._thinking_toggle.setStyleSheet(_THINKING_TOGGLE_STYLE)
        self._thinking_toggle.setCheckable(True)
        self._thinking_toggle.setChecked(True)  # expanded while streaming
        self._thinking_toggle.clicked.connect(self._toggle_thinking)
        thinking_lay.addWidget(self._thinking_toggle)

        self._thinking_label = _WrapLabel("")
        self._thinking_label.setTextFormat(Qt.PlainText)
        self._thinking_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._thinking_label.setStyleSheet(_THINKING_BLOCK_STYLE)
        thinking_lay.addWidget(self._thinking_label)

        self._thinking_container.setVisible(False)
        lay.addWidget(self._thinking_container)
        self._thinking_text = ""

        self.label = _WrapLabel("")
        self.label.setTextFormat(Qt.PlainText)
        self.label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        # Feature 2026-07-13 (markdown answers) link policy: markdown links
        # render styled but are NOT clickable -- interaction flags stay
        # TextSelectableByMouse only (no LinksAccessibleByMouse), and
        # openExternalLinks is explicitly off. No silent click-to-open-
        # arbitrary-URL surface.
        self.label.setOpenExternalLinks(False)
        self.label.setStyleSheet(_ASSISTANT_BUBBLE_STYLE)
        self.label.setVisible(False)  # only once NON-whitespace text arrives
        # Item R1 (live-feedback 2026-07-18): while STREAMING, the bubble used
        # to sit in an AlignLeft cell, so every chunk re-measured the label's
        # preferred width and the bubble snapped to a different width per
        # chunk -- hard to read mid-stream. The label now takes the layout's
        # FULL width (chat width minus this entry's 40px right margin) from
        # the first chunk: the wrap width is STABLE and only the height grows
        # as text flows (no per-chunk width re-measurement -- the cell width
        # is dock-driven, not text-driven). setMinimumWidth(1) pre-applies
        # finalize_markdown's minimum-width cap so the plain-text wrapped
        # label can never force the scroll host wider either.
        self.label.setMinimumWidth(1)
        lay.addWidget(self.label)

        # T1/T9 (NATE 2026-07-20): the tool calls of this turn live in ONE
        # parent ``_ToolCard`` hosted here (lazily created on the first pipeline
        # frame). ``pipeline_area`` is the slot it sits in; the card itself
        # persists across frames (chevron state, auto-collapse memory, spinner)
        # -- only its inner rows re-render per frame.
        self.pipeline_area = QVBoxLayout()
        self.pipeline_area.setSpacing(0)
        lay.addLayout(self.pipeline_area)
        self._tool_card: Optional[_ToolCard] = None

        # T8 (NATE 2026-07-20): the "Layers (N)" toggle is GONE -- the user sees
        # rendered layers in the QGIS map / layer tree already, so the in-chat
        # listing was clutter. The materialization path (``materializer.
        # materialize`` -> actual QGIS layers) is untouched; only layer FAILURE
        # notes still surface (via ``add_layer_notes`` -> ``add_note`` below).

        # Persistent notes (layer adds, errors) -- append-only.
        self.notes_area = QVBoxLayout()
        self.notes_area.setSpacing(0)
        lay.addLayout(self.notes_area)

        # Insert above the terminal stretch.
        parent_layout.insertWidget(parent_layout.count() - 1, self.container)
        self.text = ""
        # BUG 2 (live-feedback 2026-07-12): True once the FIRST non-whitespace
        # answer token arrived (reveals the bubble + collapses thinking).
        self._answer_started = False
        # Feature 2026-07-13: True once the final markdown render happened
        # (turn-complete / gate closeout / replay) -- runs at most once.
        self._finalized = False

    # -- thinking block ---------------------------------------------------- #

    def _toggle_thinking(self) -> None:
        self._thinking_label.setVisible(self._thinking_toggle.isChecked())

    def append_thinking_delta(self, delta: str) -> None:
        """Accumulate a reasoning-channel token delta; show the thinking block."""
        self._thinking_text += delta
        self._thinking_label.setText(self._thinking_text)
        self._thinking_container.setVisible(True)

    def collapse_thinking(self) -> None:
        """Collapse the thinking block once the answer starts streaming."""
        self._thinking_toggle.setChecked(False)
        self._thinking_toggle.setText("Thought process")
        self._thinking_label.setVisible(False)

    # -- answer text ------------------------------------------------------- #

    def append_delta(self, delta: str) -> None:
        # BUG 2 (live-feedback 2026-07-12): qwen3 emits whitespace-only text
        # deltas after </think>, and revealing the bubble on ANY delta
        # painted an empty grey box on thinking+tool-only turns. Reveal the
        # label (and collapse the thinking block) only on the first
        # NON-whitespace content; a turn whose text is all whitespace keeps
        # the bubble hidden and the thinking block expanded.
        self.text += delta
        if not self.text.strip():
            return
        if self._finalized:
            # Defensive (feature 2026-07-13): a delta after the final
            # markdown render (should not happen -- finalize runs at the
            # terminal seams) drops back to plain-text streaming so raw
            # text is never fed through a RichText label.
            self._finalized = False
            self.label.setTextFormat(Qt.PlainText)
        if not self._answer_started:
            self._answer_started = True
            if self._thinking_text:
                # First NON-whitespace answer token: collapse the thinking
                # block (was: first token of any kind).
                self.collapse_thinking()
        # Display-side lstrip only: the whitespace-only prefix qwen3 emits
        # would otherwise pad the top of the bubble with blank lines.
        self.label.setText(self.text.lstrip())
        self.label.setVisible(True)

    def finalize_markdown(self) -> None:
        """Feature 2026-07-13 (markdown answers): the turn is FINAL --
        re-render the accumulated answer text as markdown.

        While STREAMING the label stays Qt.PlainText (``append_delta``
        re-sets it token by token) so a half-open ``` fence never flickers
        through a markdown parser mid-stream; this converts exactly once,
        at the terminal seams (turn-complete, gate-card closeout, chat
        replay -- replay text is always final). Never raises: a conversion
        failure keeps the already-painted plain text -- an honest
        degradation, never a crashed dock. The thinking block, notes and
        pipeline lines stay plain text by design (raw model musing / status
        vocabulary must never be interpreted as markup)."""
        if self._finalized:
            return
        self._finalized = True
        stripped = self.text.strip()
        if not stripped:
            return  # nothing revealed (whitespace-only turn) -- keep hidden
        try:
            # Palette from the CONTAINER, not the label: the bubble
            # stylesheet's background-color re-polishes the label's own
            # palette (measured: label Base -> #cacaca, the bubble grey),
            # which would generate an invisible code-block background. The
            # container has no stylesheet, so its palette is the real
            # theme palette (light or dark QGIS alike).
            html = _markdown_to_display_html(stripped, self.container.palette())
        except Exception:  # noqa: BLE001 -- plain text stays, never crash
            return
        self.label.setTextFormat(Qt.RichText)
        self.label.setText(html)
        # Rich-text QLabels misreport widths two ways (both measured):
        # minimumSizeHint = the document's ideal UNWRAPPED width (553px for
        # a code-block+table answer -- would force the scroll host wider
        # than a narrow dock and grow a horizontal scrollbar), while the
        # wrapped sizeHint heuristic picks ~217px (the AlignLeft cell would
        # pin the bubble that narrow even in a wide dock). Cap the explicit
        # minimum width to defeat the first, and drop the AlignLeft
        # constraint so the finalized bubble takes the layout's full width
        # -- wraps at narrow docks, uses the room at wide ones; the F36
        # _WrapLabel min-HEIGHT re-assert keeps the wrapped height honored.
        # (Item R1, 2026-07-18: the streaming label is now ALSO full-width
        # with the same min-width cap, so both lines below are defensive
        # no-ops kept for the rich-text swap's independence.)
        self.label.setMinimumWidth(1)
        layout = self.container.layout()
        if layout is not None:
            layout.setAlignment(self.label, Qt.Alignment())
        self.label.setVisible(True)

    def render_tool_card(
        self, inner_rows: List[dict], meta_lines: List[str]
    ) -> None:
        """T1/T9 (NATE 2026-07-20): update this turn's parent ``_ToolCard`` (the
        ONE tool-call representation). Lazily creates the card on the first
        frame, then feeds every subsequent pipeline frame through the same
        builder (``_ToolCard.set_content``) -- so the chevron/collapse/spinner
        state persists while the inner rows re-render. ``inner_rows`` are the
        ``{"label","state","nested","result","is_error"}`` dicts the pipeline
        handler assembles; ``meta_lines`` is the bottom metadata block (T7)."""
        if self._tool_card is None:
            self._tool_card = _ToolCard()
            self.pipeline_area.addWidget(self._tool_card)
        self._tool_card.set_content(inner_rows, meta_lines)

    def clear_tool_card(self) -> None:
        """Drop the parent tool card (used when a sim/gate card closes out the
        pending entry so a fresh entry below owns the next turn's tools -- the
        old ``set_pipeline_rows([])`` clear equivalent)."""
        if self._tool_card is not None:
            self._tool_card.deleteLater()
            self._tool_card = None

    def add_note(self, text: str, error: bool = False) -> None:
        lbl = _WrapLabel(text)
        lbl.setTextFormat(Qt.PlainText)
        lbl.setStyleSheet(_ERROR_LINE_STYLE if error else _STATUS_LINE_STYLE)
        # E1 (NATE 2026-07-20): an error/status line WRAPS with the resizable
        # chat panel and never pins the scroll host wide -- cap the minimum
        # width so even a long unbroken token reflows instead of forcing a
        # horizontal scrollbar.
        lbl.setMinimumWidth(1)
        self.notes_area.addWidget(lbl)

    def add_layer_notes(self, notes: List[str]) -> None:
        """T8 (NATE 2026-07-20): the collapsed "Layers (N)" toggle is gone -- a
        SUCCESSFUL layer note is dropped from chat (the user sees the layer in
        the map / layer tree). Only FAILURE notes (``_is_error_note``) still
        surface, as visible error lines, so a materialization failure is never
        silently swallowed. The actual layer materialization happens in the
        caller (``materializer.materialize``) and is untouched."""
        for note in notes:
            if _is_error_note(note):
                self.add_note(note, error=True)


class GateCard(QFrame):
    """Inline confirmation card for one ``tool-payload-warning``.

    Renders the honest envelope numbers (tool, estimated vs threshold MB,
    recommendation), the #154 granularity ladder when present (rung combo +
    live cells/ETA recompute via the mirrored web math), editable cadence /
    window when a ``time_scale`` rides along, and Proceed / Cancel. Decision
    mapping (proceed / narrow_scope+revised_args / cancel) is delegated to
    ``gate.resolve_gate_decision`` -- the exact web ResolutionPickerCard
    rules. Once answered the card locks (no re-answer) and folds to a
    one-line summary.
    """

    def __init__(self, warning: gate.PayloadWarning, on_decide, parent=None,
                 iface=None, to_lonlat=None):
        super().__init__(parent)
        self._warning = warning
        self._on_decide = on_decide
        self._decided: Optional[str] = None
        # BK-6 release-point picker state (only active when the envelope
        # flags release_point_required; None-safe everywhere else).
        self._iface = iface
        self._to_lonlat = to_lonlat
        self._release_point = None  # (lon, lat) EPSG:4326 once placed
        self._rp_tool = None
        self._rp_prev_tool = None
        self._rp_marker = None
        self.rp_button = None
        self.rp_status = None
        self.setObjectName("gatecard")  # STYLE-1: scope the fill, no text highlight
        self.setStyleSheet(_GATE_CARD_STYLE)
        self.setFrameShape(QFrame.StyledPanel)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(3)

        # Item 5 (live-feedback 2026-07-09): the collapsed one-line summary,
        # shown only once the card is answered (``_collapse``). "show
        # details" re-expands ``self._body`` read-only (its buttons stay
        # disabled -- see ``_commit``).
        summary_row = QHBoxLayout()
        self.summary_lbl = QLabel("")
        self.summary_lbl.setWordWrap(True)
        self.summary_lbl.setTextFormat(Qt.PlainText)
        self.summary_lbl.setStyleSheet(_GATE_TITLE_STYLE)
        summary_row.addWidget(self.summary_lbl, 1)
        self.details_toggle = QPushButton("show details")
        self.details_toggle.setFlat(True)
        self.details_toggle.setCheckable(True)
        self.details_toggle.setStyleSheet(_THINKING_TOGGLE_STYLE)
        self.details_toggle.clicked.connect(self._toggle_details)
        summary_row.addWidget(self.details_toggle)
        self._summary_container = QWidget()
        self._summary_container.setLayout(summary_row)
        self._summary_container.setVisible(False)
        outer.addWidget(self._summary_container)

        # The full card content -- visible until answered, then hidden
        # behind the summary line (re-expandable via "show details").
        self._body = QWidget()
        lay = QVBoxLayout(self._body)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(3)
        outer.addWidget(self._body)

        title = "Confirm run settings" if warning.granularity else "Large response expected"
        title_lbl = QLabel(title)
        title_lbl.setStyleSheet(_GATE_TITLE_STYLE)
        lay.addWidget(title_lbl)

        for line in gate.summary_lines(warning):
            lbl = QLabel(line)
            lbl.setWordWrap(True)
            lbl.setTextFormat(Qt.PlainText)
            lbl.setStyleSheet(_GATE_BODY_STYLE)
            lay.addWidget(lbl)

        # -- resolution ladder ------------------------------------------------ #
        self.res_combo: Optional[QComboBox] = None
        self.res_estimate_lbl: Optional[QLabel] = None
        rungs = warning.resolution_choices
        suggested = warning.suggested_resolution_m
        if warning.granularity and suggested is not None:
            if suggested not in rungs:
                rungs = sorted(set(rungs) | {suggested})
            row = QHBoxLayout()
            row.addWidget(self._plain_label("Resolution:"))
            self.res_combo = QComboBox()
            for rung in rungs:
                label = f"{rung:g} m" + (" (suggested)" if rung == suggested else "")
                self.res_combo.addItem(label, rung)
            self.res_combo.setCurrentIndex(rungs.index(suggested))
            self.res_combo.currentIndexChanged.connect(self._refresh_estimates)
            row.addWidget(self.res_combo)
            self.res_estimate_lbl = QLabel("")
            self.res_estimate_lbl.setStyleSheet(_GATE_NOTE_STYLE)
            row.addWidget(self.res_estimate_lbl, 1)
            lay.addLayout(row)

        # -- time scale (cadence + window) ------------------------------------ #
        self.interval_edit: Optional[QLineEdit] = None
        self.duration_edit: Optional[QLineEdit] = None
        self.frames_lbl: Optional[QLabel] = None
        ts = warning.time_scale
        if ts:
            row = QHBoxLayout()
            row.addWidget(self._plain_label("Frame every"))
            self.interval_edit = QLineEdit(f"{ts.get('suggested_interval_min') or 0:g}")
            self.interval_edit.setMaximumWidth(56)
            self.interval_edit.textChanged.connect(self._refresh_estimates)
            row.addWidget(self.interval_edit)
            row.addWidget(self._plain_label("min over"))
            self.duration_edit = QLineEdit(f"{ts.get('suggested_duration_hr') or 0:g}")
            self.duration_edit.setMaximumWidth(56)
            self.duration_edit.textChanged.connect(self._refresh_estimates)
            row.addWidget(self.duration_edit)
            row.addWidget(self._plain_label("h"))
            self.frames_lbl = QLabel("")
            self.frames_lbl.setStyleSheet(_GATE_NOTE_STYLE)
            row.addWidget(self.frames_lbl, 1)
            lay.addLayout(row)

        # -- BK-6: release-point picker row (telemac approve-mesh gates) ------- #
        if gate.release_point_required(warning):
            rp_row = QHBoxLayout()
            self.rp_button = QPushButton("Select release point")
            self.rp_button.setCheckable(True)
            self.rp_button.toggled.connect(self._toggle_release_pick)
            rp_row.addWidget(self.rp_button)
            self.rp_status = QLabel("no point placed yet")
            self.rp_status.setWordWrap(True)
            self.rp_status.setStyleSheet(_GATE_NOTE_STYLE)
            rp_row.addWidget(self.rp_status, 1)
            lay.addLayout(rp_row)

        # -- buttons ----------------------------------------------------------- #
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.proceed_btn = QPushButton(
            "Continue" if gate.release_point_required(warning) else "Proceed"
        )
        self.proceed_btn.clicked.connect(self._proceed)
        btn_row.addWidget(self.proceed_btn)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self._cancel)
        btn_row.addWidget(self.cancel_btn)
        lay.addLayout(btn_row)

        self.result_lbl = QLabel("")
        self.result_lbl.setStyleSheet(_GATE_NOTE_STYLE)
        self.result_lbl.setVisible(False)
        lay.addWidget(self.result_lbl)

        self._refresh_estimates()

    @staticmethod
    def _plain_label(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(_GATE_BODY_STYLE)
        return lbl

    # -- UI state ------------------------------------------------------------- #

    def _chosen_resolution(self) -> Optional[float]:
        if self.res_combo is None:
            return None
        value = self.res_combo.currentData()
        return float(value) if isinstance(value, (int, float)) else None

    def _edited_float(self, edit: Optional[QLineEdit]) -> Optional[float]:
        if edit is None:
            return None
        try:
            value = float(edit.text())
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    def _refresh_estimates(self, *_args) -> None:
        w = self._warning
        chosen = self._chosen_resolution()
        if w.granularity and chosen and self.res_estimate_lbl is not None:
            cells = gate.estimate_cells(w.granularity, chosen)
            eta = gate.estimate_eta_seconds(w.granularity, chosen)
            self.res_estimate_lbl.setText(f"~{cells:,} cells, est ~{eta:.0f}s")
        if w.time_scale and self.frames_lbl is not None:
            interval = self._edited_float(self.interval_edit) or 0.0
            duration = self._edited_float(self.duration_edit) or 0.0
            frames = gate.estimate_frames(w.time_scale, interval, duration)
            self.frames_lbl.setText(f"~{frames} frames")
        # Hard cap: Proceed stays enabled only when the current choice maps
        # to a decision the envelope's options actually allow.
        if self._decided is None:
            decision = self._current_decision()
            self.proceed_btn.setEnabled(decision.decision is not None)
            self.proceed_btn.setToolTip(decision.note or "")

    def _current_decision(self) -> gate.GateDecision:
        return gate.resolve_gate_decision(
            self._warning,
            cancel=False,
            chosen_resolution_m=self._chosen_resolution(),
            interval_min=self._edited_float(self.interval_edit),
            duration_hr=self._edited_float(self.duration_edit),
            release_point=self._release_point,
        )

    # -- BK-6: release-point map picking --------------------------------------- #

    def _toggle_release_pick(self, checked: bool) -> None:
        """Mirror of the dock's probe tool discipline: ON saves the current
        canvas tool and installs a point-emit tool; OFF restores it -- the
        canvas is never left on a tool the user did not ask for."""
        try:
            canvas = self._iface.mapCanvas()
        except Exception:  # noqa: BLE001 -- headless / no iface
            return
        if checked:
            if self._rp_tool is None:
                from qgis.gui import QgsMapToolEmitPoint

                self._rp_tool = QgsMapToolEmitPoint(canvas)
                self._rp_tool.canvasClicked.connect(self._on_release_clicked)
            self._rp_prev_tool = canvas.mapTool()
            canvas.setMapTool(self._rp_tool)
        else:
            if canvas.mapTool() is self._rp_tool:
                canvas.setMapTool(self._rp_prev_tool)
            self._rp_prev_tool = None

    def _on_release_clicked(self, point, _button) -> None:
        """Place (or MOVE -- click again) the release point. The click must
        land inside the previewed mesh bbox (small pad); the worker does the
        exact interior-node validation on submit."""
        try:
            canvas = self._iface.mapCanvas()
            authid = canvas.mapSettings().destinationCrs().authid()
            lonlat = self._to_lonlat(point, authid) if self._to_lonlat else None
        except Exception:  # noqa: BLE001
            lonlat = None
        if lonlat is None:
            if self.rp_status is not None:
                self.rp_status.setText("could not read the clicked point - try again")
            return
        lon, lat = lonlat
        bb = gate.release_point_bbox(self._warning)
        pad = 0.02  # ~2 km grace around the mesh bbox
        if bb and not (bb[0] - pad <= lon <= bb[2] + pad
                       and bb[1] - pad <= lat <= bb[3] + pad):
            if self.rp_status is not None:
                self.rp_status.setText(
                    f"({lat:.4f}, {lon:.4f}) is OUTSIDE the previewed mesh - "
                    "click inside the wireframe"
                )
            return
        self._release_point = (lon, lat)
        try:
            from qgis.gui import QgsVertexMarker

            if self._rp_marker is None:
                self._rp_marker = QgsVertexMarker(canvas)
                self._rp_marker.setIconType(QgsVertexMarker.ICON_CROSS)
                self._rp_marker.setColor(Qt.red)
                self._rp_marker.setPenWidth(3)
                self._rp_marker.setIconSize(14)
            self._rp_marker.setCenter(point)
        except Exception:  # noqa: BLE001 -- marker is cosmetic
            pass
        if self.rp_status is not None:
            self.rp_status.setText(
                f"release point: ({lat:.5f}, {lon:.5f}) - click again to move"
            )
        self._refresh_estimates()

    def _release_pick_teardown(self, drop_marker: bool) -> None:
        if self.rp_button is not None and self.rp_button.isChecked():
            self.rp_button.setChecked(False)  # restores the previous map tool
        if drop_marker and self._rp_marker is not None:
            try:
                self._iface.mapCanvas().scene().removeItem(self._rp_marker)
            except Exception:  # noqa: BLE001
                pass
            self._rp_marker = None

    # -- actions --------------------------------------------------------------- #

    def _proceed(self) -> None:
        decision = self._current_decision()
        if decision.decision is None:
            self.result_lbl.setText(decision.note)
            self.result_lbl.setVisible(True)
            return
        self._commit(decision)

    def _cancel(self) -> None:
        self._commit(gate.resolve_gate_decision(self._warning, cancel=True))

    def _commit(self, decision: gate.GateDecision) -> None:
        if self._decided is not None:
            return  # locked -- a gate is answered exactly once
        self._decided = decision.decision
        self._on_decide(self._warning.warning_id, decision.decision, decision.revised_args)
        self._release_pick_teardown(drop_marker=(decision.decision == "cancel"))
        for widget in (self.proceed_btn, self.cancel_btn, self.res_combo,
                       self.interval_edit, self.duration_edit, self.rp_button):
            if widget is not None:
                widget.setEnabled(False)
        summary = {
            "proceed": "Confirmed -- proceeding.",
            "cancel": "Cancelled.",
            "narrow_scope": f"Confirmed with overrides: {decision.revised_args}",
        }.get(decision.decision or "", "")
        self.result_lbl.setText(summary)
        self.result_lbl.setVisible(True)
        self._collapse()

    # -- collapse (item 5, live-feedback 2026-07-09) ---------------------------- #

    def _collapse(self) -> None:
        """Fold to a single amber summary line once answered. The body stays
        intact underneath (its buttons already disabled by ``_commit``) so
        "show details" can re-expand a read-only view."""
        chosen = self._chosen_resolution()
        if self._decided == "proceed" and chosen is not None:
            line = f"Resolution gate: proceeded at {chosen:g} m"
        elif self._decided == "proceed":
            line = "Resolution gate: proceeded"
        elif self._decided == "cancel":
            line = "Resolution gate: cancelled"
        elif self._decided == "narrow_scope":
            line = "Resolution gate: proceeded with overrides"
        else:
            line = f"Resolution gate: {self._decided or 'answered'}"
        self.summary_lbl.setText(line)
        self._summary_container.setVisible(True)
        self._body.setVisible(False)
        self.details_toggle.setChecked(False)
        self.details_toggle.setText("show details")

    def _toggle_details(self, checked: bool) -> None:
        self._body.setVisible(checked)
        self.details_toggle.setText("hide details" if checked else "show details")


class SimCard(QFrame):
    """Item R4 (live-feedback 2026-07-18): ONE collapsible card per off-box
    solver run -- parity with the cloud web's sim card -- replacing the grey
    pipeline rows for ``role="compute"`` steps.

    Wire sources (read from the live agent contracts, never guessed):

      * ``pipeline-state`` compute steps (contract ws.PipelineStep, the
        task-149 two-card sim observability): minted running by
        ``pipeline_emitter.mint_dispatch_and_sim_cards`` with
        ``tool_name="<solver>:solve"`` + ``batch_job_id``
        ("local-docker:<run_id>" on the local seam) + ``batch_status``;
        driven terminal (complete / failed / cancelled, ``duration_ms``,
        ``error_message``) by ``route_sim_terminal``.
      * ``solve-progress`` ticks (contract ws.SolveProgressPayload, emitted
        every ~10 s by ``workflows.solve_progress.drive_live_solve_progress``
        -- the exact path the TELEMAC dye composer
        ``model_river_dye_release_scenario`` / ``run_telemac`` arms): run_id /
        solver / grid_resolution_m / active_cell_count / vcpus /
        elapsed_seconds / eta_seconds / phase.

    The small metadata table updates IN PLACE as events arrive; unknown
    fields honestly read "-" (e.g. TELEMAC arms its progress driver with no
    cell count, and dt is on NEITHER wire shape -- nothing is fabricated,
    Invariant 1). The collapse affordance (one-line summary + "show details"
    re-expand) is the exact ``GateCard`` pattern reused verbatim: expanded
    while RUNNING, folding to "Simulation complete - TELEMAC" (or failed /
    cancelled) on the terminal transition, details re-expandable read-only.
    """

    _FIELDS: Tuple[Tuple[str, str], ...] = (
        ("engine", "Engine"),
        ("run_id", "Run id"),
        ("status", "Status"),
        ("progress", "Progress"),
        ("nodes", "Nodes"),
        ("grid", "Grid"),
        ("vcpus", "vCPUs"),
        ("elapsed", "Elapsed"),
        ("eta", "ETA"),
        ("duration", "Duration"),
    )

    def __init__(self, engine_label: str, parent=None):
        super().__init__(parent)
        self._engine = engine_label
        self._terminal = False
        # Item N4 (live-feedback 2026-07-19): the latest live-progress bits so
        # the collapsed summary's right-side readout can recompose from
        # whichever wire (step pct or progress-tick elapsed/phase) last spoke.
        self._pct: Optional[int] = None
        self._elapsed_str: str = ""
        self._phase: str = ""
        self.setObjectName("simcard")  # STYLE-1: scope the fill, no text highlight
        self.setStyleSheet(_SIM_CARD_STYLE)
        self.setFrameShape(QFrame.StyledPanel)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(3)

        # Item N1 (live-feedback 2026-07-19): the summary row + "show/hide
        # details" toggle is ALWAYS visible now (was: revealed only on the
        # terminal collapse, so a RUNNING card could not be folded). The
        # toggle is live from the first frame -- expanded while running,
        # foldable anytime; the terminal transition auto-collapses (kept).
        # Item N4: the live progress readout (pct / elapsed / phase) rides on
        # the RIGHT of this row, next to the toggle, so a COLLAPSED card still
        # shows progress at a glance without expanding.
        summary_row = QHBoxLayout()
        self.summary_lbl = QLabel(f"Simulation running - {engine_label}")
        self.summary_lbl.setWordWrap(True)
        self.summary_lbl.setTextFormat(Qt.PlainText)
        self.summary_lbl.setStyleSheet(_SIM_TITLE_STYLE)
        summary_row.addWidget(self.summary_lbl, 1)
        self.progress_lbl = QLabel("")
        self.progress_lbl.setTextFormat(Qt.PlainText)
        self.progress_lbl.setStyleSheet(_GATE_NOTE_STYLE)
        summary_row.addWidget(self.progress_lbl)
        self.details_toggle = QPushButton("hide details")
        self.details_toggle.setFlat(True)
        self.details_toggle.setCheckable(True)
        self.details_toggle.setChecked(True)  # expanded while running
        self.details_toggle.setStyleSheet(_THINKING_TOGGLE_STYLE)
        self.details_toggle.clicked.connect(self._toggle_details)
        summary_row.addWidget(self.details_toggle)
        outer.addLayout(summary_row)

        self._body = QWidget()
        body_lay = QVBoxLayout(self._body)
        body_lay.setContentsMargins(0, 0, 0, 0)
        body_lay.setSpacing(3)
        outer.addWidget(self._body)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(1)
        self._values: Dict[str, QLabel] = {}
        for i, (key, label) in enumerate(self._FIELDS):
            key_lbl = QLabel(label)
            key_lbl.setStyleSheet(_GATE_NOTE_STYLE)
            val_lbl = QLabel("-")
            val_lbl.setTextFormat(Qt.PlainText)
            val_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
            val_lbl.setStyleSheet(_GATE_BODY_STYLE)
            grid.addWidget(key_lbl, i, 0)
            grid.addWidget(val_lbl, i, 1)
            self._values[key] = val_lbl
        grid.setColumnStretch(1, 1)
        body_lay.addLayout(grid)
        self._set("engine", engine_label)

        self.error_lbl = QLabel("")
        self.error_lbl.setWordWrap(True)
        self.error_lbl.setTextFormat(Qt.PlainText)
        self.error_lbl.setStyleSheet(_ERROR_LINE_STYLE)
        self.error_lbl.setVisible(False)
        body_lay.addWidget(self.error_lbl)

    # -- table plumbing ------------------------------------------------------- #

    @property
    def engine(self) -> str:
        return self._engine

    @property
    def terminal(self) -> bool:
        return self._terminal

    def _set(self, key: str, text: str) -> None:
        lbl = self._values.get(key)
        if lbl is not None and text:
            lbl.setText(text)

    @staticmethod
    def _fmt_seconds(seconds: float) -> str:
        s = int(max(0.0, round(seconds)))
        return f"{s // 60}:{s % 60:02d}"

    # -- event folds ---------------------------------------------------------- #

    def update_from_step(self, step: PipelineStep) -> None:
        """Fold a ``role="compute"`` pipeline step into the table; the first
        terminal state (complete / failed / cancelled) flips the title and
        collapses the card (a later replayed frame never re-flips it)."""
        if step.batch_job_id:
            # Local-docker handles read "local-docker:<run_id>" -- show the
            # run_id part (a bare AWS Batch jobId passes through unchanged).
            self._set("run_id", step.batch_job_id.split(":", 1)[-1])
        status_bits = [step.state]
        if step.batch_status and step.batch_status.lower() != step.state:
            status_bits.append(step.batch_status)
        self._set("status", " / ".join(status_bits))
        if step.progress_percent is not None:
            self._set("progress", f"{step.progress_percent}%")
            self._pct = step.progress_percent
            self._refresh_progress_readout()
        if step.duration_ms is not None:
            self._set("duration", self._fmt_seconds(step.duration_ms / 1000.0))
        if step.state in ("complete", "failed", "cancelled") and not self._terminal:
            self._terminal = True
            if step.state == "failed" and step.error_message:
                self.error_lbl.setText(step.error_message)
                self.error_lbl.setVisible(True)
            title = {
                "complete": f"Simulation complete - {self._engine}",
                "failed": f"Simulation failed - {self._engine}",
                "cancelled": f"Simulation cancelled - {self._engine}",
            }[step.state]
            # Item N4: on the terminal transition the right-side readout shows
            # the final duration (the live pct/elapsed/phase is done).
            if step.duration_ms is not None:
                self.progress_lbl.setText(
                    self._fmt_seconds(step.duration_ms / 1000.0)
                )
            else:
                self.progress_lbl.setText("")
            self._collapse(title)

    def update_from_progress(self, data: dict) -> None:
        """Fold a ``solve-progress`` tick into the table. Defensive reads --
        every field is optional on the wire; a terminal card ignores the
        live-only fields (a straggler tick must not repaint 'running')."""
        run_id = data.get("run_id")
        if isinstance(run_id, str) and run_id:
            self._set("run_id", run_id)
        nodes = data.get("active_cell_count")
        if isinstance(nodes, (int, float)) and not isinstance(nodes, bool):
            self._set("nodes", f"{int(nodes):,}")
        grid_res = data.get("grid_resolution_m")
        if isinstance(grid_res, (int, float)) and not isinstance(grid_res, bool):
            self._set("grid", f"{grid_res:g} m")
        vcpus = data.get("vcpus")
        if isinstance(vcpus, (int, float)) and not isinstance(vcpus, bool):
            self._set("vcpus", f"{int(vcpus)}")
        if not self._terminal:
            elapsed = data.get("elapsed_seconds")
            if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool):
                self._elapsed_str = self._fmt_seconds(float(elapsed))
                self._set("elapsed", self._elapsed_str)
            eta = data.get("eta_seconds")
            if isinstance(eta, (int, float)) and not isinstance(eta, bool):
                self._set("eta", self._fmt_seconds(float(eta)))
            phase = data.get("phase")
            if isinstance(phase, str) and phase:
                self._phase = phase
                self._set("status", f"running / {phase}")
            # Item N4: refresh the collapsed summary's right-side readout live.
            self._refresh_progress_readout()

    def _refresh_progress_readout(self) -> None:
        """Item N4 (live-feedback 2026-07-19): recompose the summary row's
        right-side progress readout (pct - elapsed - phase) from the latest
        live bits, so a COLLAPSED card shows progress at a glance. Live only:
        a terminal card shows the final duration there instead (set in
        ``update_from_step``), so this no-ops once terminal."""
        if self._terminal:
            return
        parts: List[str] = []
        if self._pct is not None:
            parts.append(f"{self._pct}%")
        if self._elapsed_str:
            parts.append(self._elapsed_str)
        if self._phase:
            parts.append(self._phase)
        self.progress_lbl.setText(" - ".join(parts))

    # -- collapse (the GateCard affordance) ------------------------------------ #

    def _collapse(self, line: str) -> None:
        """Item N1 (live-feedback 2026-07-19): auto-fold on the terminal
        transition (kept). The summary row + toggle stay visible (they always
        are now); only the body hides, and the user can re-expand it."""
        self.summary_lbl.setText(line)
        self._body.setVisible(False)
        self.details_toggle.setChecked(False)
        self.details_toggle.setText("show details")

    def _toggle_details(self, checked: bool) -> None:
        # Item N1: live at ANY time -- the user can fold/unfold a RUNNING card,
        # not just a terminal one.
        self._body.setVisible(checked)
        self.details_toggle.setText("hide details" if checked else "show details")
