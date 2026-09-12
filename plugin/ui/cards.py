"""TRID3NT chat-surface widgets: message bubbles, tool card, gate + sim cards.

EVERY gate card follows ONE pattern: it locks after exactly one answer, folds to
a one-line chip, and "show details" re-expands it read-only. Its buttons are
plain QPushButtons, so a composer ENTER can never fire one as a dialog default."""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QColor, QSyntaxHighlighter, QTextCharFormat
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
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
from ..net.run_invocation import RUN_PREFIX, is_run_prefix
from ..net.trid3nt_client import PipelineStep


_ASSISTANT_BUBBLE_STYLE = (
    "background-color: palette(midlight); border-radius: 8px; padding: 6px 9px;"
)
_ERROR_LINE_STYLE = "color: #f85149; font-size: 8pt; padding-left: 4px;"
# CARD CHROME, the discipline every card below follows. A card carries its own
# accent colour, a 1px border and a LOW-ALPHA fill, so it tints faintly over a
# light or a dark QGIS window alike and never assumes one theme. The fill is
# scoped to the FRAME ID, never a bare ``QFrame`` selector: that selector
# cascades onto every descendant QLabel and highlights the text too.
#
# Amber: the caution gate.
_GATE_CARD_STYLE = (
    "QFrame#gatecard { border: 1px solid #d29922; border-radius: 8px; "
    "background-color: rgba(210, 153, 34, 7%); }"
)
_GATE_TITLE_STYLE = "color: #d29922; font-weight: bold; border: none;"
_GATE_BODY_STYLE = "border: none; font-size: 9pt;"
_GATE_NOTE_STYLE = "border: none; color: palette(mid); font-size: 8pt;"
# The tool chip is a compact monospace badge, so a tool call reads as a
# visually DISTINCT class from the grey info notes. Its colour tracks the step
# STATE, and it stays OUTLINED rather than filled.
_CHIP_STATE_COLORS = {
    "complete": "#3fb950",   # green -- success
    "failed": "#f85149",     # red -- failure
    "cancelled": "#f85149",  # red -- failure
}
_CHIP_PENDING_COLOR = "#8b949e"  # grey -- pending / running / unknown


def _tool_chip_style(state: Optional[str]) -> str:
    """The outlined tool-chip stylesheet, its border and text colour driven
    off the step state: green complete, grey in progress, red failed."""
    color = _CHIP_STATE_COLORS.get((state or "").lower(), _CHIP_PENDING_COLOR)
    return (
        f"font-family: monospace; font-size: 8pt; color: {color}; "
        f"border: 1px solid {color}; border-radius: 7px; padding: 0px 6px;"
    )


_TOOL_CHIP_DETAIL_STYLE = "color: palette(mid); font-size: 8pt; border: none;"
# A nested tool row renders as a directory tree: an ASCII connector before the
# child, so a parent-to-child hierarchy reads as one rather than as a flat
# indented list.
_TREE_CONNECTOR_STYLE = (
    "font-family: monospace; font-size: 8pt; color: #58a6ff; border: none;"
)

# The constants below drive the parent tool card. Its border spans the FULL
# chat width and adapts on resize, because the frame carries no fixed or
# minimum width and simply fills whatever the resizable dock gives it.
def _toolcard_frame_style(border_color: Optional[str] = None) -> str:
    """The parent tool-card BORDER, coloured off the AGGREGATE tool state:
    green once every inner tool succeeded, red when any failed, neutral while
    anything still runs or the card is empty."""
    color = border_color or "palette(mid)"
    return (
        f"QFrame#toolcard {{ border: 1px solid {color}; border-radius: 8px; "
        "background-color: rgba(128, 128, 128, 6%); }"
    )


_TOOLCARD_FRAME_STYLE = _toolcard_frame_style()
# The chevron + "Tools (N)" header line at the top of the card.
_TOOLCARD_HEADER_STYLE = (
    "color: palette(text); font-size: 9pt; border: none; text-align: left;"
)
# The muted metadata block pinned at the BOTTOM of the card body.
_TOOLCARD_META_STYLE = "color: palette(mid); font-size: 8pt; border: none;"
# The small ">" nesting prefix on every inner row.
_TOOLCARD_PREFIX_STYLE = (
    "font-family: monospace; font-size: 9pt; color: palette(mid); border: none;"
)


def _tool_row_text_style(state: Optional[str]) -> str:
    """An inner tool-row LABEL: a coloured monospace label with no border and
    no background, its colour driven off the step state."""
    color = _CHIP_STATE_COLORS.get((state or "").lower(), _CHIP_PENDING_COLOR)
    return f"font-family: monospace; font-size: 9pt; color: {color}; border: none;"


def _tool_status_style(state: Optional[str]) -> str:
    """The right-edge status glyph -- spinner, check or x -- coloured off the
    same state map."""
    color = _CHIP_STATE_COLORS.get((state or "").lower(), _CHIP_PENDING_COLOR)
    return f"font-family: monospace; font-size: 9pt; color: {color}; border: none;"


# The ascii spinner cycles on a QTimer while a row is RUNNING; a terminal row
# shows a check or an x instead. These are text symbols, never emoji.
_SPINNER_FRAMES = ("|", "/", "-", "\\")
_STATUS_GLYPH_DONE = "✓"  # check mark
_STATUS_GLYPH_FAIL = "✗"  # ballot x
_TERMINAL_STATES = {"complete", "failed", "cancelled"}


def _is_running_state(state: Optional[str]) -> bool:
    """A row is RUNNING (spinner) when its state is not one of the terminal
    values -- pending / running / unknown all animate; complete/failed/
    cancelled show the terminal glyph."""
    return (state or "").lower() not in _TERMINAL_STATES

# Blue, the accent the tool tree already uses: the code-exec approval gate.
_CODE_CARD_STYLE = (
    "QFrame#codeexeccard { border: 1px solid #58a6ff; border-radius: 8px; "
    "background-color: rgba(88, 166, 255, 7%); }"
)
_CODE_TITLE_STYLE = "color: #58a6ff; font-weight: bold; border: none;"
# The collapsed read-only code preview: monospace over the theme Base color
# (palette-derived -- no hardcoded hex that assumes one theme).
_CODE_PREVIEW_STYLE = (
    "font-family: monospace; font-size: 8pt; "
    "background-color: palette(base); border: 1px solid palette(mid); "
    "border-radius: 2px;"
)

def _html_escape(text: str) -> str:
    """Minimal HTML escape for server-sourced strings interpolated into a
    Qt.TextFormat.RichText label (the credential card's signup link)."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# Green: a "provide something and the run continues" affordance, distinct from
# the amber caution gate and the blue code gate.
_CRED_CARD_STYLE = (
    "QFrame#credentialcard { border: 1px solid #3fb950; border-radius: 8px; "
    "background-color: rgba(63, 185, 80, 7%); }"
)
_CRED_TITLE_STYLE = "color: #3fb950; font-weight: bold; border: none;"

# Teal: a "steer the routing" affordance, distinct from the caution, code and
# credential cards.
_PICKER_CARD_STYLE = (
    "QFrame#toolpickercard { border: 1px solid #39c5cf; border-radius: 8px; "
    "background-color: rgba(57, 197, 207, 7%); }"
)
_PICKER_TITLE_STYLE = "color: #39c5cf; font-weight: bold; border: none;"
# The candidate radio row: the tool NAME is monospace (it is a registry
# identifier, same class of text as the tool-card rows), the one-line summary
# rides under it as a muted indented note.
_PICKER_RADIO_STYLE = "font-family: monospace; font-size: 9pt; border: none;"
_PICKER_SUMMARY_STYLE = (
    "color: palette(mid); font-size: 8pt; border: none; padding-left: 22px;"
)

# Purple: simulation progress.
_SIM_CARD_STYLE = (
    "QFrame#simcard { border: 1px solid #8957e5; border-radius: 8px; "
    "background-color: rgba(137, 87, 229, 7%); }"
)
_SIM_TITLE_STYLE = "color: #8957e5; font-weight: bold; border: none;"

# Blue: a "pick a place" affordance.
_REGION_CARD_STYLE = (
    "QFrame#regionchoicecard { border: 1px solid #58a6ff; border-radius: 8px; "
    "background-color: rgba(88, 166, 255, 7%); }"
)
_REGION_TITLE_STYLE = "color: #58a6ff; font-weight: bold; border: none;"

# Green-cyan: a "click the map" affordance.
_SPATIAL_CARD_STYLE = (
    "QFrame#spatialinputcard { border: 1px solid #2dd4bf; border-radius: 8px; "
    "background-color: rgba(45, 212, 191, 7%); }"
)
_SPATIAL_TITLE_STYLE = "color: #2dd4bf; font-weight: bold; border: none;"
#: The pick-affordance button label per capture kind. Keyed by ``draw_kind`` when
#: the request declares one, else by ``mode``.
_SPATIAL_PICK_LABEL = {
    "point": "Click a point on the map",
    "bbox": "Draw a box on the map",
    "polygon": "Draw a polygon on the map",
    "polyline": "Draw a line on the map",
}

#: The FORM card, which renders a param sheet. Its own accent so an input
#: REVIEW never reads as the amber payload/resolution gate beside it.
_FORM_CARD_STYLE = (
    "QFrame#formcard { border: 1px solid #a78bfa; border-radius: 8px; "
    "background-color: rgba(167, 139, 250, 7%); }"
)
_FORM_TITLE_STYLE = "color: #a78bfa; font-weight: bold; border: none;"
_FORM_BADGE_STYLE = "color: palette(mid); font-size: 8pt; border: none;"
#: A row whose value has a declared ORIGIN wears it as a chip, so where the
#: value came from is legible at a glance rather than read out of a sentence.
_FORM_ORIGIN_CHIP_STYLE = (
    "font-family: monospace; font-size: 8pt; color: palette(mid); "
    "border: 1px solid palette(mid); border-radius: 7px; padding: 0px 6px;"
)


class _WrapLabel(QLabel):
    """Word-wrapping QLabel whose WRAPPED height the layouts actually honor,
    plain text and rich text alike."""

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


class _RunPrefixHighlighter(QSyntaxHighlighter):
    """Colours the leading anchored ``!run`` token blue. Only the FIRST block
    is considered, and leading whitespace is honoured so the colour tracks the
    literal characters."""

    _BLUE = QColor("#2f81f7")  # the web/accent blue, legible on light+dark

    def highlightBlock(self, text: str) -> None:  # noqa: N802 -- Qt name
        # Only the composer's first block can carry the anchored token.
        if self.currentBlock().blockNumber() != 0:
            return
        # ONE shared predicate with the parse-first routing, so the visual
        # signal can never disagree with where the message actually goes.
        if not is_run_prefix(text):
            return
        start = len(text) - len(text.lstrip())
        fmt = QTextCharFormat()
        fmt.setForeground(self._BLUE)
        fmt.setFontWeight(75)  # bold-ish, so the signal reads at a glance
        self.setFormat(start, len(RUN_PREFIX), fmt)


class _ChatInput(QPlainTextEdit):
    """The composer input: a MULTI-LINE auto-growing field. ENTER sends;
    SHIFT+ENTER and any Ctrl or Meta chord insert a newline. It grows to
    ``_MAX_LINES`` and then scrolls."""

    _MIN_LINES = 1
    _MAX_LINES = 10

    def __init__(self, send_callback, parent=None):
        super().__init__(parent)
        self._send_callback = send_callback
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Grow RELIABLY one row per new line / wrap. Drive
        # off the document LAYOUT's documentSizeChanged -- it fires AFTER the
        # relayout resolves the new document height, so the box never lags a row
        # behind (the old textChanged hook fired BEFORE relayout, so the height
        # was computed from the pre-edit layout and trailed the text). Grow, do
        # not scroll, until the _MAX_LINES cap (scrollbar stays OFF above).
        self.document().documentLayout().documentSizeChanged.connect(
            self._adjust_height
        )
        # colour the leading ``!run`` token blue while the composer is
        # anchored as a direct invocation (same predicate as parse-first
        # routing). Held on self so it is not GC'd.
        self._run_highlighter = _RunPrefixHighlighter(self.document())
        self._adjust_height()

    def keyPressEvent(self, event) -> None:  # noqa: N802 -- Qt-mandated name
        key = event.key()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            # SHIFT+ENTER inserts a newline; a bare ENTER sends. Ctrl/Meta are
            # treated like Shift (never send) so a stray chord never fires an
            # accidental send mid-edit.
            if event.modifiers() & (
                Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier
            ):
                super().keyPressEvent(event)
                return
            self._send_callback()
            return
        super().keyPressEvent(event)

    def _adjust_height(self, *args) -> None:  # documentSizeChanged passes a QSizeF
        # Compute the field height FROM the document layout
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
    """Render assistant markdown to Qt rich-text HTML. Colours come from
    ``palette``, never a hardcoded hex, so a light and a dark QGIS theme both
    stay readable. RAISES on broken input; the caller keeps plain text."""
    # The route is deliberately QTextDocument.setMarkdown then toHtml, rather
    # than a label's own MarkdownText format: the label route parses the same
    # dialect but offers ZERO styling hooks, so a fenced code block comes out
    # in the default proportional font with no background and a table gets no
    # cell padding. Going through the document allows styling the model before
    # serializing, and the result renders through the same engine anyway.
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

    code_bg = palette.color(QPalette.ColorRole.Base)
    border = palette.color(QPalette.ColorRole.Mid)

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
        cur.setPosition(pos + length, QTextCursor.MoveMode.KeepAnchor)
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
    """Whether a materializer note reports a FAILURE. The notes are plain
    strings, so this classifies by the honest failure vocabulary they use; an
    error-ish note has to stay visible."""
    lowered = note.lower()
    return any(
        token in lowered
        for token in ("fail", "error", "skipp", "reject", "unknown")
    )


# Chat NOTE text (status + error lines) rides
# plain-text QLabels, and plain-text word-wrap breaks at whitespace ONLY -- so
# one long unbroken token (a store URL inside a rehydrate failure note)
# reported an unbreakable preferred/minimum width (measured: label
# sizeHint 400px at a 320px view; the message host's sizeHint ballooned
# 145 -> 444px, which is what dragged the DOCK wider than its usual minimum).
# Fix: seed invisible break OPPORTUNITIES -- a zero-width space every
# ``_BREAK_CHUNK`` chars inside any longer unbroken run -- so the Qt line
# breaker can wrap ANYWHERE inside such tokens (measured post-fix: sizeHint
# 175px, host 193px, the URL wraps over 7 lines at 320px). Display-only
# munging of note text; the full text stays readable (no elide -- an error URL
# must stay diagnosable), matching the wrap-never-force-width discipline the
# other chat text already follows.
_ZWSP = "\u200b"  # zero-width space: an invisible line-break opportunity
_BREAK_CHUNK = 24
_UNBROKEN_RUN = re.compile(r"\S{%d,}" % (_BREAK_CHUNK + 1))


def _breakable_display_text(text: str) -> str:
    """Note text -> the same text with zero-width break opportunities seeded
    into every unbroken run longer than ``_BREAK_CHUNK`` chars."""

    def _chunk(match: "re.Match[str]") -> str:
        token = match.group(0)
        return _ZWSP.join(
            token[i:i + _BREAK_CHUNK]
            for i in range(0, len(token), _BREAK_CHUNK)
        )

    return _UNBROKEN_RUN.sub(_chunk, text)


# The folded-errors toggle keeps the exact charts/thinking collapse
# affordance chrome (_THINKING_TOGGLE_STYLE) but in the error red, so the
# collapsed row still reads as errors at a glance.
_ERROR_FOLD_TOGGLE_STYLE = (
    "color: #f85149; font-size: 8pt; border: none; text-align: left;"
)


class _ErrorFold(QWidget):
    """ONE run of consecutive error notes, folded inline in chat scroll order
    and never moved to a panel. A single error renders plainly with NO toggle;
    the fold appears from the second consecutive error onward."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self.toggle = QPushButton("ERRORS (1)")
        self.toggle.setFlat(True)
        self.toggle.setCheckable(True)
        self.toggle.setChecked(False)  # collapsed by default
        self.toggle.setStyleSheet(_ERROR_FOLD_TOGGLE_STYLE)
        self.toggle.clicked.connect(self._apply)
        self.toggle.setVisible(False)  # hidden while N == 1 (plain-line look)
        lay.addWidget(self.toggle)

        self._body = QWidget()
        self._body_lay = QVBoxLayout(self._body)
        self._body_lay.setContentsMargins(0, 0, 0, 0)
        self._body_lay.setSpacing(0)
        lay.addWidget(self._body)

        self.count = 0

    def _apply(self) -> None:
        self._body.setVisible(self.toggle.isChecked())

    def add_error(self, text: str) -> None:
        """Append one error line to the run as a wrapped red label, with the
        break-anywhere text and the min-width cap, so a long URL reflows with
        the dock instead of forcing its width."""
        lbl = _WrapLabel(_breakable_display_text(text))
        lbl.setTextFormat(Qt.TextFormat.PlainText)
        lbl.setStyleSheet(_ERROR_LINE_STYLE)
        lbl.setMinimumWidth(1)  # wrap, never a horizontal scrollbar
        self._body_lay.addWidget(lbl)
        self.count += 1
        if self.count == 1:
            # A single error stays a plain wrapped line -- no toggle chrome.
            self._body.setVisible(True)
        else:
            self.toggle.setText(f"ERRORS ({self.count})")
            self.toggle.setVisible(True)
            # Collapsed by default; a user-expanded fold stays expanded.
            self._body.setVisible(self.toggle.isChecked())


class _ToolCard(QFrame):
    """ONE parent card containing this turn's tool calls. There is exactly one
    representation, and the SAME widget serves both the live pipeline and the
    case-open replay, so a reopened case reads identically."""
    # Collapse: the body is EXPANDED while any inner tool runs and collapses
    # once every row is terminal, re-expanding if a new running row appears
    # afterwards. The FIRST chevron click latches ``_user_toggled``, and
    # auto-collapse never fights the user's own choice from then on.

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("toolcard")
        self.setStyleSheet(_TOOLCARD_FRAME_STYLE)
        self.setFrameShape(QFrame.Shape.NoFrame)
        # Fill the chat width, adapt on resize -- no fixed/min width.
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 4, 8, 4)
        outer.setSpacing(2)

        # Header: chevron + "Tools (N)" title. The chevron is a native
        # QToolButton arrow (style-drawn triangle -- not an emoji/text glyph).
        header = QWidget()
        hl = QHBoxLayout(header)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(4)
        self._chevron = QToolButton()
        self._chevron.setAutoRaise(True)
        self._chevron.setArrowType(Qt.ArrowType.DownArrow)  # expanded by default
        self._chevron.setStyleSheet("QToolButton { border: none; }")
        self._chevron.clicked.connect(self._toggle)
        hl.addWidget(self._chevron)
        self._title = QLabel("Tools")
        self._title.setTextFormat(Qt.TextFormat.PlainText)
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

        self._expanded = True             # default EXPANDED while running
        # The collapse is AUTO (expand while any inner
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
            Qt.ArrowType.DownArrow if self._expanded else Qt.ArrowType.RightArrow
        )

    def _toggle(self) -> None:
        self._user_toggled = True  # from now on, auto-collapse never overrides
        self._expanded = not self._expanded
        self._apply_expanded()

    # -- spinner ------------------------------------------------------------ #

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

    # -- the ONE builder used by live + replay ------------------------------ #

    def set_content(self, inner_rows: List[dict], meta_lines: List[str]) -> None:
        """Rebuild the inner rows and bottom metadata. Called on EVERY live
        pipeline frame: the rebuild is cheap, and the frame, chevron and
        collapse state persist on ``self`` across it."""
        self._clear_body()
        any_running = False
        any_failed = False
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
            # A failed/cancelled row (or a replayed error row) taints the
            # whole card's aggregate state -> red border below.
            if (state or "").lower() in ("failed", "cancelled") or bool(
                row.get("is_error")
            ):
                any_failed = True

            row_w = QWidget()
            rl = QHBoxLayout(row_w)
            # A small ">" prefix for visual nesting (drops the old bubble
            # frame + tree-connector arrow that cut into the text); a deeper
            # inset for a sub-step child so hierarchy still reads.
            rl.setContentsMargins(4 + (12 if nested else 0), 0, 0, 0)
            rl.setSpacing(6)
            prefix = QLabel(">")
            prefix.setTextFormat(Qt.TextFormat.PlainText)
            prefix.setStyleSheet(_TOOLCARD_PREFIX_STYLE)
            rl.addWidget(prefix)
            # A plain, non-wrapping label in the state-driven text colour.
            name_lbl = QLabel(label)
            name_lbl.setTextFormat(Qt.TextFormat.PlainText)
            name_lbl.setStyleSheet(_tool_row_text_style(state))
            rl.addWidget(name_lbl)
            rl.addStretch(1)
            # The ONLY right-edge element is the status glyph; the arg and
            # metadata summary rides the bottom block instead. Running is an
            # animated spinner, complete a check, terminal-failed an x.
            status = QLabel()
            status.setTextFormat(Qt.TextFormat.PlainText)
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

            # The tool RESULT shows UNDER its row, inside the card,
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
                body.setTextFormat(Qt.TextFormat.PlainText)
                body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                body.setStyleSheet(
                    _PROBE_ERROR_BLOCK_STYLE if is_error else _THINKING_BLOCK_STYLE
                )
                body.setMinimumWidth(1)  # never force a horizontal scrollbar
                body.setVisible(False)
                toggle.clicked.connect(
                    lambda _c=False, b=body, t=toggle: b.setVisible(t.isChecked())
                )
                self._body_lay.addWidget(toggle)
                self._body_lay.addWidget(body)

        # One muted metadata block pinned at the BOTTOM of the body, under
        # all the inner rows, so every bit of text lives inside the card border.
        clean_meta = [m for m in meta_lines if m]
        if clean_meta:
            meta = _WrapLabel("\n".join(clean_meta))
            meta.setTextFormat(Qt.TextFormat.PlainText)
            meta.setStyleSheet(_TOOLCARD_META_STYLE)
            meta.setMinimumWidth(1)  # wrap, never a horizontal scrollbar
            self._body_lay.addWidget(meta)

        self._title.setText(f"Tools ({n_tools})" if n_tools else "Tools")

        # The card BORDER carries the aggregate
        # outcome -- red the moment any tool failed/cancelled, green once
        # every tool is terminal-and-successful, neutral while running.
        if any_failed:
            border = _CHIP_STATE_COLORS["failed"]
        elif n_tools and not any_running:
            border = _CHIP_STATE_COLORS["complete"]
        else:
            border = None
        self.setStyleSheet(_toolcard_frame_style(border))

        # Run the spinner only while a row is live.
        if any_running and not self._spinner_timer.isActive():
            self._spinner_timer.start()
        elif not any_running and self._spinner_timer.isActive():
            self._spinner_timer.stop()

        # Auto behavior, until the user takes manual control: EXPANDED while
        # any inner tool runs, so it can be watched live, and COLLAPSED once
        # every row is terminal. It re-expands if a new running row appears
        # after an all-terminal frame; a manual chevron click disables it.
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

        # The thinking block: a toggle button plus a collapsible text label.
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
        thinking_lay.addWidget(self._thinking_toggle)

        self._thinking_label = _WrapLabel("")
        self._thinking_label.setTextFormat(Qt.TextFormat.PlainText)
        self._thinking_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._thinking_label.setStyleSheet(_THINKING_BLOCK_STYLE)
        thinking_lay.addWidget(self._thinking_label)
        # Wire the fold WIDGET-to-WIDGET (toggled ->
        # setVisible) instead of through a bound method of this plain-python
        # wrapper. Replayed entries (case reopen) are not retained by the dock,
        # so a connection to ``self`` died with the wrapper's GC and the
        # replayed fold became a dead button; a QObject-to-QObject connection
        # lives as long as the widgets themselves.
        self._thinking_toggle.toggled.connect(self._thinking_label.setVisible)

        self._thinking_container.setVisible(False)
        lay.addWidget(self._thinking_container)
        self._thinking_text = ""

        self.label = _WrapLabel("")
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        # Link policy: a markdown link renders STYLED but is not clickable.
        # The interaction flags stay selectable-only and openExternalLinks is
        # explicitly off, so there is no silent click-to-open-any-URL surface.
        self.label.setOpenExternalLinks(False)
        self.label.setStyleSheet(_ASSISTANT_BUBBLE_STYLE)
        self.label.setVisible(False)  # only once NON-whitespace text arrives
        # While STREAMING, the bubble used
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

        # The tool calls of this turn live in ONE
        # parent ``_ToolCard`` hosted here (lazily created on the first pipeline
        # frame). ``pipeline_area`` is the slot it sits in; the card itself
        # persists across frames (chevron state, auto-collapse memory, spinner)
        # -- only its inner rows re-render per frame.
        self.pipeline_area = QVBoxLayout()
        self.pipeline_area.setSpacing(0)
        lay.addLayout(self.pipeline_area)
        self._tool_card: Optional[_ToolCard] = None

        # The "Layers (N)" toggle is GONE -- the user sees
        # rendered layers in the QGIS map / layer tree already, so the in-chat
        # listing was clutter. The materialization path (``materializer.
        # materialize`` -> actual QGIS layers) is untouched; only layer FAILURE
        # notes still surface (via ``add_layer_notes`` -> ``add_note`` below).

        # Persistent notes (layer adds, errors) -- append-only.
        self.notes_area = QVBoxLayout()
        self.notes_area.setSpacing(0)
        lay.addLayout(self.notes_area)
        # The OPEN run of consecutive error notes (one _ErrorFold widget);
        # a non-error note (or an explicit break_error_run) ends the run so
        # the next error starts a fresh fold.
        self._error_fold: Optional[_ErrorFold] = None

        # Insert above the terminal stretch.
        parent_layout.insertWidget(parent_layout.count() - 1, self.container)
        self.text = ""
        # True once the FIRST non-whitespace
        # answer token arrived (reveals the bubble + collapses thinking).
        self._answer_started = False
        # True once the final markdown render happened
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
        # Qwen3 emits whitespace-only text
        # deltas after </think>, and revealing the bubble on ANY delta
        # painted an empty grey box on thinking+tool-only turns. Reveal the
        # label (and collapse the thinking block) only on the first
        # NON-whitespace content; a turn whose text is all whitespace keeps
        # the bubble hidden and the thinking block expanded.
        self.text += delta
        if not self.text.strip():
            return
        if self._finalized:
            # A delta arriving AFTER the final markdown render drops back to
            # plain-text streaming, so raw text is never fed through a
            # RichText label.
            self._finalized = False
            self.label.setTextFormat(Qt.TextFormat.PlainText)
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
        """The turn is FINAL: re-render the accumulated answer as markdown,
        exactly ONCE, because a half-open fence must never flicker through a
        parser mid-stream. Never raises; the plain text stands on failure."""
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
        self.label.setTextFormat(Qt.TextFormat.RichText)
        self.label.setText(html)
        # Rich-text QLabels misreport widths two ways (both measured):
        # minimumSizeHint = the document's ideal UNWRAPPED width (553px for
        # a code-block+table answer -- would force the scroll host wider
        # than a narrow dock and grow a horizontal scrollbar), while the
        # wrapped sizeHint heuristic picks ~217px (the AlignLeft cell would
        # pin the bubble that narrow even in a wide dock). Cap the explicit
        # minimum width to defeat the first, and drop the AlignLeft
        # constraint so the finalized bubble takes the layout's full width:
        # it wraps at narrow docks and uses the room at wide ones, while
        # ``_WrapLabel``'s min-height re-assert keeps the wrapped height
        # honored. The streaming label already carries the same cap, so both
        # lines below are defensive no-ops.
        self.label.setMinimumWidth(1)
        layout = self.container.layout()
        if layout is not None:
            layout.setAlignment(self.label, Qt.AlignmentFlag(0))
        self.label.setVisible(True)

    def render_tool_card(
        self, inner_rows: List[dict], meta_lines: List[str]
    ) -> None:
        """Update this turn's parent tool card, creating it lazily on the
        first frame so the chevron, collapse and spinner state persist while
        the inner rows re-render."""
        if self._tool_card is None:
            # NEVER mint the card shell for a frame with no tool content: a
            # turn whose pipeline frames carry only bookkeeping steps would
            # leave an empty "Tools" card sitting stale in the transcript.
            # Once minted, later frames update it whether empty or not.
            has_rows = any(str(r.get("label") or "") for r in inner_rows)
            if not has_rows and not any(m for m in meta_lines):
                return
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
        # Consecutive ERROR notes fold into one
        # collapsed "ERRORS (N)" toggle row (single errors stay a plain
        # wrapped line -- _ErrorFold docstring). The fold sits inline in the
        # notes area, in chat scroll order; a non-error note breaks the run.
        if error:
            if self._error_fold is None:
                self._error_fold = _ErrorFold()
                self.notes_area.addWidget(self._error_fold)
            self._error_fold.add_error(text)
            return
        self._error_fold = None  # a status line ends the consecutive-error run
        # An error or status line WRAPS with the resizable chat panel and
        # never pins the scroll host wide: the minimum width is capped so even
        # a long unbroken token reflows instead of forcing a horizontal
        # scrollbar, and break-anywhere opportunities inside such tokens keep
        # the label's own sizeHint narrow too.
        lbl = _WrapLabel(_breakable_display_text(text))
        lbl.setTextFormat(Qt.TextFormat.PlainText)
        lbl.setStyleSheet(_STATUS_LINE_STYLE)
        lbl.setMinimumWidth(1)
        self.notes_area.addWidget(lbl)

    def break_error_run(self) -> None:
        """End the current consecutive-error run WITHOUT adding a note, so
        errors separated by conversation never fold into one row."""
        self._error_fold = None

    def add_layer_notes(self, notes: List[str]) -> None:
        """A SUCCESSFUL layer note is dropped from chat -- the layer itself is
        the feedback. Only a FAILURE note surfaces, so a materialization
        failure is never silently swallowed."""
        for note in notes:
            if _is_error_note(note):
                self.add_note(note, error=True)


class GateCard(QFrame):
    """Inline confirmation card for one ``tool-payload-warning``: the honest
    envelope numbers, the granularity ladder and time scale when they ride
    along, and Proceed or Cancel."""

    def __init__(self, warning: gate.PayloadWarning, on_decide, parent=None):
        super().__init__(parent)
        self._warning = warning
        self._on_decide = on_decide
        self._decided: Optional[str] = None
        self.setObjectName("gatecard")  # scope the fill; no text highlight
        self.setStyleSheet(_GATE_CARD_STYLE)
        self.setFrameShape(QFrame.Shape.StyledPanel)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(3)

        # The collapsed one-line summary,
        # shown only once the card is answered (``_collapse``). "show
        # details" re-expands ``self._body`` read-only (its buttons stay
        # disabled -- see ``_commit``).
        summary_row = QHBoxLayout()
        self.summary_lbl = QLabel("")
        self.summary_lbl.setWordWrap(True)
        self.summary_lbl.setTextFormat(Qt.TextFormat.PlainText)
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
            lbl.setTextFormat(Qt.TextFormat.PlainText)
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

        # -- buttons ----------------------------------------------------------- #
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.proceed_btn = QPushButton("Proceed")
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
        )

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
        for widget in (self.proceed_btn, self.cancel_btn, self.res_combo,
                       self.interval_edit, self.duration_edit):
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

    # -- collapse --------------------------------------------------------------- #

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


class CodeExecCard(QFrame):
    """Inline approval card for one ``code-exec-request``: a HARD confirm
    gate. The code preview is the EXACT code, VERBATIM and never a paraphrase,
    because the user is confirming precisely what will run."""

    def __init__(self, request: gate.CodeExecRequest, on_decide, parent=None):
        super().__init__(parent)
        self._request = request
        self._on_decide = on_decide
        self._decided: Optional[str] = None
        self.setObjectName("codeexeccard")  # scope the fill to the frame
        self.setStyleSheet(_CODE_CARD_STYLE)
        self.setFrameShape(QFrame.Shape.StyledPanel)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(3)

        # Collapsed one-line summary (hidden until answered) + "show details"
        # re-expand -- the GateCard affordance verbatim.
        summary_row = QHBoxLayout()
        self.summary_lbl = QLabel("")
        self.summary_lbl.setWordWrap(True)
        self.summary_lbl.setTextFormat(Qt.TextFormat.PlainText)
        self.summary_lbl.setStyleSheet(_CODE_TITLE_STYLE)
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

        # Full card content -- visible until answered, then folded behind the
        # summary line (re-expandable read-only; buttons stay disabled).
        self._body = QWidget()
        lay = QVBoxLayout(self._body)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(3)
        outer.addWidget(self._body)

        title_lbl = QLabel("Run Python code?")
        title_lbl.setStyleSheet(_CODE_TITLE_STYLE)
        lay.addWidget(title_lbl)

        if request.rationale:
            rationale_lbl = QLabel(request.rationale)
            rationale_lbl.setWordWrap(True)
            rationale_lbl.setTextFormat(Qt.TextFormat.PlainText)
            rationale_lbl.setStyleSheet(_GATE_BODY_STYLE)
            lay.addWidget(rationale_lbl)

        # Collapsed monospace preview of the EXACT code -- expandable,
        # read-only (verbatim, never a paraphrase).
        self.code_toggle = QPushButton("show code")
        self.code_toggle.setFlat(True)
        self.code_toggle.setCheckable(True)
        self.code_toggle.setChecked(False)
        self.code_toggle.setStyleSheet(_THINKING_TOGGLE_STYLE)
        self.code_toggle.clicked.connect(self._toggle_code)
        lay.addWidget(self.code_toggle)
        self.code_view = QPlainTextEdit()
        self.code_view.setPlainText(request.python_code)
        self.code_view.setReadOnly(True)
        self.code_view.setStyleSheet(_CODE_PREVIEW_STYLE)
        # The preview scrolls INSIDE its own box -- it never
        # pins the chat host wide or tall (long snippets get scrollbars).
        self.code_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.code_view.setMinimumWidth(1)
        self.code_view.setMaximumHeight(180)
        self.code_view.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.code_view.setVisible(False)
        lay.addWidget(self.code_view)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.run_btn = QPushButton("Run")
        self.run_btn.clicked.connect(self._run)
        btn_row.addWidget(self.run_btn)
        self.deny_btn = QPushButton("Deny")
        self.deny_btn.clicked.connect(self._deny)
        btn_row.addWidget(self.deny_btn)
        lay.addLayout(btn_row)

        self.result_lbl = QLabel("")
        self.result_lbl.setStyleSheet(_GATE_NOTE_STYLE)
        self.result_lbl.setVisible(False)
        lay.addWidget(self.result_lbl)

    # -- toggles ---------------------------------------------------------- #

    def _toggle_code(self) -> None:
        checked = self.code_toggle.isChecked()
        self.code_view.setVisible(checked)
        self.code_toggle.setText("hide code" if checked else "show code")

    def _toggle_details(self, checked: bool) -> None:
        self._body.setVisible(checked)
        self.details_toggle.setText("hide details" if checked else "show details")

    # -- actions ---------------------------------------------------------- #

    def _run(self) -> None:
        self._commit(gate.resolve_code_exec_decision(True))

    def _deny(self) -> None:
        self._commit(gate.resolve_code_exec_decision(False))

    def _commit(self, decision: gate.GateDecision) -> None:
        if self._decided is not None:
            return  # locked -- a gate is answered exactly once
        self._decided = decision.decision
        self._on_decide(self._request.code_exec_id, decision.decision)
        for widget in (self.run_btn, self.deny_btn):
            widget.setEnabled(False)
        self.result_lbl.setText(
            "Approved -- running the code."
            if decision.decision == "proceed"
            else "Denied -- the code will not run."
        )
        self.result_lbl.setVisible(True)
        self._collapse()

    # -- collapse (the GateCard affordance) -------------------------------- #

    def _collapse(self) -> None:
        """Fold to a single one-line state chip once answered; the body stays
        intact underneath (buttons already disabled) so "show details" can
        re-expand a read-only view."""
        self.summary_lbl.setText(
            "Code run: approved"
            if self._decided == "proceed"
            else "Code run: denied"
        )
        self._summary_container.setVisible(True)
        self._body.setVisible(False)
        self.details_toggle.setChecked(False)
        self.details_toggle.setText("show details")

    # -- run outcome

    def update_from_result(self, result: gate.CodeExecResult) -> None:
        """Fold the run OUTCOME into this card. The chip carries the HONEST
        terminal status -- a blocked or timed-out run is never dressed up as
        ok. A NO-OP on a denied card: the deny chip stands."""
        if self._decided != "proceed":
            return
        self.summary_lbl.setText(gate.code_exec_result_chip(result))
        for line in gate.code_exec_result_lines(result):
            lbl = QLabel(line)
            lbl.setWordWrap(True)
            lbl.setTextFormat(Qt.TextFormat.PlainText)
            lbl.setStyleSheet(
                _ERROR_LINE_STYLE if not result.ok and "stderr" in line
                else _GATE_NOTE_STYLE
            )
            self._body.layout().addWidget(lbl)


class CredentialCard(QFrame):
    """Inline key-entry card for one ``credential-request``. KEY HYGIENE: the
    raw key is never logged, stored on ``self`` or named in the chip. A signup
    URL is shown only when the server sent one, never invented."""

    def __init__(self, request: gate.CredentialRequest, on_decide, parent=None):
        super().__init__(parent)
        self._request = request
        self._on_decide = on_decide
        self._decided: Optional[str] = None
        self.setObjectName("credentialcard")  # scope the fill to the frame
        self.setStyleSheet(_CRED_CARD_STYLE)
        self.setFrameShape(QFrame.Shape.StyledPanel)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(3)

        # Collapsed one-line summary (hidden until answered) + "show details"
        # re-expand -- the GateCard affordance verbatim.
        summary_row = QHBoxLayout()
        self.summary_lbl = QLabel("")
        self.summary_lbl.setWordWrap(True)
        self.summary_lbl.setTextFormat(Qt.TextFormat.PlainText)
        self.summary_lbl.setStyleSheet(_CRED_TITLE_STYLE)
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

        # Full card content -- visible until answered, then folded behind the
        # summary line (re-expandable read-only; controls stay disabled).
        self._body = QWidget()
        lay = QVBoxLayout(self._body)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(3)
        outer.addWidget(self._body)

        title_lbl = QLabel(f"API key needed: {request.display_label}")
        title_lbl.setWordWrap(True)
        title_lbl.setTextFormat(Qt.TextFormat.PlainText)
        title_lbl.setStyleSheet(_CRED_TITLE_STYLE)
        lay.addWidget(title_lbl)

        if request.message:
            # The agent's user-facing explanation, VERBATIM: never
            # paraphrased client-side.
            message_lbl = QLabel(request.message)
            message_lbl.setWordWrap(True)
            message_lbl.setTextFormat(Qt.TextFormat.PlainText)
            message_lbl.setStyleSheet(_GATE_BODY_STYLE)
            lay.addWidget(message_lbl)

        for line in gate.credential_note_lines(request):
            note_lbl = QLabel(line)
            note_lbl.setWordWrap(True)
            note_lbl.setTextFormat(Qt.TextFormat.PlainText)
            note_lbl.setStyleSheet(_GATE_NOTE_STYLE)
            lay.addWidget(note_lbl)

        if request.signup_url:
            # The server's REAL signup URL (registry-sourced; a name-only
            # generic card sends None and this label is simply absent --
            # the client never fabricates a URL).
            href = _html_escape(request.signup_url)
            link_lbl = QLabel(f'<a href="{href}">Get a key: {href}</a>')
            link_lbl.setTextFormat(Qt.TextFormat.RichText)
            link_lbl.setOpenExternalLinks(True)
            link_lbl.setWordWrap(True)
            link_lbl.setStyleSheet(_GATE_NOTE_STYLE)
            lay.addWidget(link_lbl)

        # The masked key field: password echo -- the value is never rendered
        # on screen, and nothing in this class ever reads it except the one
        # Submit commit (which clears it immediately).
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        placeholder = request.secret_key_name or "API key"
        self.key_edit.setPlaceholderText(f"Paste your {placeholder}")
        self.key_edit.returnPressed.connect(self._submit)
        lay.addWidget(self.key_edit)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.submit_btn = QPushButton("Submit")
        self.submit_btn.clicked.connect(self._submit)
        btn_row.addWidget(self.submit_btn)
        self.skip_btn = QPushButton("Skip")
        self.skip_btn.clicked.connect(self._skip)
        btn_row.addWidget(self.skip_btn)
        lay.addLayout(btn_row)

        self.result_lbl = QLabel("")
        self.result_lbl.setWordWrap(True)
        self.result_lbl.setTextFormat(Qt.TextFormat.PlainText)
        self.result_lbl.setStyleSheet(_GATE_NOTE_STYLE)
        self.result_lbl.setVisible(False)
        lay.addWidget(self.result_lbl)

    # -- toggles ---------------------------------------------------------- #

    def _toggle_details(self, checked: bool) -> None:
        self._body.setVisible(checked)
        self.details_toggle.setText("hide details" if checked else "show details")

    # -- actions ---------------------------------------------------------- #

    def _submit(self) -> None:
        if self._decided is not None:
            return  # locked -- a gate is answered exactly once
        key = self.key_edit.text().strip()
        if not key:
            # No decision consumed: an accidental empty Submit/Return must
            # not decline the agent's pause on the user's behalf.
            self.result_lbl.setText("Enter a key, or press Skip.")
            self.result_lbl.setVisible(True)
            return
        self._decided = "provided"
        # Clear + disable the field BEFORE anything else runs: after this
        # commit the key exists only in the local ``key`` handed to the send
        # hook (never on ``self``, never in a log, never in the chip text).
        self.key_edit.clear()
        self._lock()
        self.result_lbl.setText(
            "Key sent to the local vault -- retrying the paused tool."
        )
        self.result_lbl.setVisible(True)
        self._collapse()
        self._on_decide(self._request.request_id, self._request.provider_id, key)

    def _skip(self) -> None:
        if self._decided is not None:
            return  # locked -- a gate is answered exactly once
        self._decided = "skipped"
        self.key_edit.clear()
        self._lock()
        self.result_lbl.setText(
            "Skipped -- the tool will report its original error."
        )
        self.result_lbl.setVisible(True)
        self._collapse()
        self._on_decide(self._request.request_id, self._request.provider_id, None)

    def _lock(self) -> None:
        for widget in (self.submit_btn, self.skip_btn, self.key_edit):
            widget.setEnabled(False)

    # -- collapse (the GateCard affordance) -------------------------------- #

    def _collapse(self) -> None:
        """Fold to a single one-line state chip once answered; the body stays
        intact underneath (controls already disabled + field cleared) so
        "show details" can re-expand a read-only view."""
        label = self._request.display_label
        self.summary_lbl.setText(
            f"Key provided for {label}"
            if self._decided == "provided"
            else f"Key skipped for {label}"
        )
        self._summary_container.setVisible(True)
        self._body.setVisible(False)
        self.details_toggle.setChecked(False)
        self.details_toggle.setText("show details")


class ToolCandidatesCard(QFrame):
    """Inline tool-selection picker for one ``tool-candidates`` request.
    Typing in the free-text field selects ITS radio, so a typed answer is never
    attributed to a candidate; unanswered, the card never blocks the turn."""

    def __init__(
        self,
        request: gate.ToolCandidatesRequest,
        on_decide,
        parent=None,
        step_index: Optional[int] = None,
    ):
        super().__init__(parent)
        self._request = request
        self._on_decide = on_decide
        self._decided: Optional[str] = None
        # When multiple picker cards
        # land in one turn, ``step_index`` (1-based, dock-assigned off
        # arrival order -- "trivially derivable from card order", never a
        # server field) prefixes the title so a staged wave reads as a
        # stepped wizard ("Step 1", "Step 2", ...) instead of stacked
        # walls. None (a single/unstepped picker) omits the prefix.
        self._step_index = step_index
        self.setObjectName("toolpickercard")  # scope the fill to the frame
        self.setStyleSheet(_PICKER_CARD_STYLE)
        self.setFrameShape(QFrame.Shape.StyledPanel)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(3)

        # Collapsed one-line chip (hidden until answered/superseded) + "show
        # details" re-expand -- the GateCard affordance verbatim.
        summary_row = QHBoxLayout()
        self.summary_lbl = QLabel("")
        self.summary_lbl.setWordWrap(True)
        self.summary_lbl.setTextFormat(Qt.TextFormat.PlainText)
        self.summary_lbl.setStyleSheet(_PICKER_TITLE_STYLE)
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

        # Full card content -- visible until answered, then folded behind the
        # chip (re-expandable read-only; controls stay disabled).
        self._body = QWidget()
        lay = QVBoxLayout(self._body)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(3)
        outer.addWidget(self._body)

        # Title = the stage label ("Data step" etc.) -- the staged-waves
        # narrative anchor; a defensively-parsed envelope falls back to a
        # generic title rather than an empty line. The optional "Step N"
        # prefix reads as a stepped wizard when a turn emits several waves.
        title = request.stage_label or "Tool choice"
        prefix = f"Step {self._step_index} - " if self._step_index else ""
        title_lbl = QLabel(f"{prefix}{title}: pick a tool")
        title_lbl.setWordWrap(True)
        title_lbl.setTextFormat(Qt.TextFormat.PlainText)
        title_lbl.setStyleSheet(_PICKER_TITLE_STYLE)
        lay.addWidget(title_lbl)

        note = request.reason_note
        if note:
            note_lbl = QLabel(note)
            note_lbl.setWordWrap(True)
            note_lbl.setTextFormat(Qt.TextFormat.PlainText)
            note_lbl.setStyleSheet(_GATE_NOTE_STYLE)
            lay.addWidget(note_lbl)

        # Ranked candidates as radio choices: monospace tool name on the
        # radio, the one-line summary as a muted indented note under it. All
        # radios share this card as parent widget, so Qt keeps them mutually
        # exclusive (the free-text radio below included).
        self._candidate_radios: List[Tuple[QRadioButton, str]] = []
        for cand in request.candidates:
            radio = QRadioButton(cand.tool_name)
            radio.setStyleSheet(_PICKER_RADIO_STYLE)
            lay.addWidget(radio)
            self._candidate_radios.append((radio, cand.tool_name))
            if cand.summary:
                summary_lbl = QLabel(cand.summary)
                summary_lbl.setWordWrap(True)
                summary_lbl.setTextFormat(Qt.TextFormat.PlainText)
                summary_lbl.setStyleSheet(_PICKER_SUMMARY_STYLE)
                lay.addWidget(summary_lbl)

        # The free-text escape hatch, always the LAST option: "none of these
        # -- here is what I actually want". Typing selects its radio.
        free_row = QHBoxLayout()
        self.free_radio = QRadioButton("Other:")
        self.free_radio.setStyleSheet(_PICKER_RADIO_STYLE)
        free_row.addWidget(self.free_radio)
        self.free_edit = QLineEdit()
        self.free_edit.setPlaceholderText("describe what to do instead")
        self.free_edit.textEdited.connect(self._on_free_text_edited)
        self.free_edit.returnPressed.connect(self._confirm)
        free_row.addWidget(self.free_edit, 1)
        lay.addLayout(free_row)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.confirm_btn = QPushButton("Confirm")
        self.confirm_btn.clicked.connect(self._confirm)
        btn_row.addWidget(self.confirm_btn)
        self.decide_btn = QPushButton("Let agent decide")
        self.decide_btn.clicked.connect(self._let_agent_decide)
        btn_row.addWidget(self.decide_btn)
        lay.addLayout(btn_row)

        self.result_lbl = QLabel("")
        self.result_lbl.setWordWrap(True)
        self.result_lbl.setTextFormat(Qt.TextFormat.PlainText)
        self.result_lbl.setStyleSheet(_GATE_NOTE_STYLE)
        self.result_lbl.setVisible(False)
        lay.addWidget(self.result_lbl)

    # -- properties -------------------------------------------------------- #

    @property
    def request_id(self) -> str:
        return self._request.request_id

    @property
    def answered(self) -> bool:
        """True once ANY terminal state landed (user answer OR superseded)."""
        return self._decided is not None

    # -- toggles ----------------------------------------------------------- #

    def _toggle_details(self, checked: bool) -> None:
        self._body.setVisible(checked)
        self.details_toggle.setText("hide details" if checked else "show details")

    def _on_free_text_edited(self, _text: str) -> None:
        """Typing IS choosing the free-text option -- select its radio so the
        typed guidance is never silently attributed to a candidate pick."""
        if self._decided is None and not self.free_radio.isChecked():
            self.free_radio.setChecked(True)

    # -- UI state ----------------------------------------------------------- #

    def _picked_tool(self) -> Optional[str]:
        for radio, tool_name in self._candidate_radios:
            if radio.isChecked():
                return tool_name
        return None

    # -- actions ------------------------------------------------------------ #

    def _confirm(self) -> None:
        if self._decided is not None:
            return  # locked -- a picker is answered exactly once
        picked = self._picked_tool()
        free_text = (
            self.free_edit.text() if self.free_radio.isChecked() else None
        )
        if picked is None and not (free_text or "").strip():
            # No decision consumed: an accidental Confirm/Return with nothing
            # selected must not send "let the agent decide" on the user's
            # behalf (the empty-Submit honesty the credential card uses).
            self.result_lbl.setText(
                "Pick a tool, type what to do instead, or press "
                "Let agent decide."
            )
            self.result_lbl.setVisible(True)
            return
        tool_name, guidance = gate.resolve_tool_choice(picked, free_text)
        self._commit(tool_name, guidance)

    def _let_agent_decide(self) -> None:
        if self._decided is not None:
            return  # locked
        self._commit(None, None)

    def _commit(self, tool_name: Optional[str], free_text: Optional[str]) -> None:
        self._decided = "answered"
        self._lock()
        if tool_name:
            self.result_lbl.setText(f"Sent your pick: {tool_name}.")
        elif free_text:
            self.result_lbl.setText("Sent your guidance to the agent.")
        else:
            self.result_lbl.setText("Left the choice to the agent.")
        self.result_lbl.setVisible(True)
        self._collapse(gate.tool_choice_summary(tool_name, free_text))
        self._on_decide(self._request.request_id, tool_name, free_text)

    def mark_superseded(self) -> None:
        """The turn moved on while this card was open, so the server's own
        fail-open already resolved the selection. NO reply is sent: answering
        a request the server already resolved would be a lie on the wire."""
        if self._decided is not None:
            return
        self._decided = "superseded"
        self._lock()
        self.result_lbl.setText(
            "No answer before the agent moved on -- it proceeded with its "
            "own pick."
        )
        self.result_lbl.setVisible(True)
        self._collapse("agent proceeded")

    def _lock(self) -> None:
        widgets: List[QWidget] = [
            self.confirm_btn, self.decide_btn, self.free_radio, self.free_edit
        ]
        widgets.extend(radio for radio, _name in self._candidate_radios)
        for widget in widgets:
            widget.setEnabled(False)

    # -- collapse (the GateCard affordance) --------------------------------- #

    def _collapse(self, chip: str) -> None:
        """Fold to the one-line chip, the body intact underneath so "show
        details" re-expands a read-only view. The step prefix rides along so a
        folded wave still reads as a sequence, not an anonymous stack."""
        prefix = f"Step {self._step_index}: " if self._step_index else ""
        self.summary_lbl.setText(f"{prefix}{chip}")
        self._summary_container.setVisible(True)
        self._body.setVisible(False)
        self.details_toggle.setChecked(False)
        self.details_toggle.setText("show details")


class SimCard(QFrame):
    """ONE collapsible card per off-box solver run, EXPANDED while running.
    A field neither wire shape carries honestly reads "-": nothing on this
    card is ever fabricated."""

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
        # The latest live-progress bits so
        # the collapsed summary's right-side readout can recompose from
        # whichever wire (step pct or progress-tick elapsed/phase) last spoke.
        self._pct: Optional[int] = None
        self._elapsed_str: str = ""
        self._phase: str = ""
        self.setObjectName("simcard")  # scope the fill; no text highlight
        self.setStyleSheet(_SIM_CARD_STYLE)
        self.setFrameShape(QFrame.Shape.StyledPanel)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(3)

        # The summary row + "show/hide
        # details" toggle is ALWAYS visible now (was: revealed only on the
        # terminal collapse, so a RUNNING card could not be folded). The
        # toggle is live from the first frame -- expanded while running,
        # foldable anytime; the terminal transition auto-collapses (kept).
        # The live progress readout (pct / elapsed / phase) rides on
        # the RIGHT of this row, next to the toggle, so a COLLAPSED card still
        # shows progress at a glance without expanding.
        summary_row = QHBoxLayout()
        self.summary_lbl = QLabel(f"Simulation running - {engine_label}")
        self.summary_lbl.setWordWrap(True)
        self.summary_lbl.setTextFormat(Qt.TextFormat.PlainText)
        self.summary_lbl.setStyleSheet(_SIM_TITLE_STYLE)
        summary_row.addWidget(self.summary_lbl, 1)
        self.progress_lbl = QLabel("")
        self.progress_lbl.setTextFormat(Qt.TextFormat.PlainText)
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
            val_lbl.setTextFormat(Qt.TextFormat.PlainText)
            val_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            val_lbl.setStyleSheet(_GATE_BODY_STYLE)
            grid.addWidget(key_lbl, i, 0)
            grid.addWidget(val_lbl, i, 1)
            self._values[key] = val_lbl
        grid.setColumnStretch(1, 1)
        body_lay.addLayout(grid)
        self._set("engine", engine_label)

        self.error_lbl = QLabel("")
        self.error_lbl.setWordWrap(True)
        self.error_lbl.setTextFormat(Qt.TextFormat.PlainText)
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
            # On the terminal transition the right-side readout shows
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
            # Refresh the collapsed summary's right-side readout live.
            self._refresh_progress_readout()

    def _refresh_progress_readout(self) -> None:
        """Recompose the summary row's progress readout, so a COLLAPSED card
        still shows progress at a glance. LIVE only: a terminal card shows its
        final duration there instead, so this no-ops once terminal."""
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
        """Auto-fold on the terminal transition. The summary row and toggle
        stay visible; only the body hides, and it re-expands."""
        self.summary_lbl.setText(line)
        self._body.setVisible(False)
        self.details_toggle.setChecked(False)
        self.details_toggle.setText("show details")

    def _toggle_details(self, checked: bool) -> None:
        # Live at ANY time -- the user can fold/unfold a RUNNING card,
        # not just a terminal one.
        self._body.setVisible(checked)
        self.details_toggle.setText("hide details" if checked else "show details")


class RegionChoiceCard(QFrame):
    """Inline picker for one ``region-choice-request``. Keep-the-whole-state
    is CHECKED by default, being the honest already-resolved answer, so the
    card always has a move that closes the paused gate."""

    #: The sentinel radio value for the whole-state option (never a region_id).
    _WHOLE_STATE = ""

    def __init__(self, request: gate.RegionChoiceRequest, on_decide, parent=None):
        super().__init__(parent)
        self._request = request
        self._on_decide = on_decide
        self._decided = False
        self._selected_region_id: Optional[str] = None
        self.setObjectName("regionchoicecard")  # scope the fill
        self.setStyleSheet(_REGION_CARD_STYLE)
        self.setFrameShape(QFrame.Shape.StyledPanel)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(3)

        # Collapsed one-line summary (hidden until answered) + "show details".
        summary_row = QHBoxLayout()
        self.summary_lbl = QLabel("")
        self.summary_lbl.setWordWrap(True)
        self.summary_lbl.setTextFormat(Qt.TextFormat.PlainText)
        self.summary_lbl.setStyleSheet(_REGION_TITLE_STYLE)
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

        self._body = QWidget()
        lay = QVBoxLayout(self._body)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(3)
        outer.addWidget(self._body)

        title_lbl = QLabel("Narrow the region?")
        title_lbl.setStyleSheet(_REGION_TITLE_STYLE)
        lay.addWidget(title_lbl)

        if request.message:
            # The agent's own prompt, VERBATIM.
            msg_lbl = QLabel(request.message)
            msg_lbl.setWordWrap(True)
            msg_lbl.setTextFormat(Qt.TextFormat.PlainText)
            msg_lbl.setStyleSheet(_GATE_BODY_STYLE)
            lay.addWidget(msg_lbl)

        # The whole-state option (CHECKED by default -- the honest already-
        # resolved answer), then each candidate sub-region.
        self._radios: List[Tuple[str, QRadioButton]] = []
        whole = QRadioButton(f"Keep the whole state ({request.state_label})")
        whole.setStyleSheet(_PICKER_RADIO_STYLE)
        whole.setChecked(True)
        lay.addWidget(whole)
        self._radios.append((self._WHOLE_STATE, whole))
        for cand in request.candidates:
            radio = QRadioButton(cand.name)
            radio.setStyleSheet(_PICKER_RADIO_STYLE)
            lay.addWidget(radio)
            self._radios.append((cand.region_id, radio))

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.confirm_btn = QPushButton("Confirm")
        self.confirm_btn.clicked.connect(self._confirm)
        btn_row.addWidget(self.confirm_btn)
        self.whole_btn = QPushButton("Use whole state")
        self.whole_btn.clicked.connect(self._use_whole_state)
        btn_row.addWidget(self.whole_btn)
        lay.addLayout(btn_row)

        self.result_lbl = QLabel("")
        self.result_lbl.setWordWrap(True)
        self.result_lbl.setStyleSheet(_GATE_NOTE_STYLE)
        self.result_lbl.setVisible(False)
        lay.addWidget(self.result_lbl)

    # -- state ------------------------------------------------------------- #

    def _checked_region_id(self) -> Optional[str]:
        for region_id, radio in self._radios:
            if radio.isChecked():
                return region_id or None  # "" whole-state sentinel -> None
        return None

    def _toggle_details(self, checked: bool) -> None:
        self._body.setVisible(checked)
        self.details_toggle.setText("hide details" if checked else "show details")

    # -- actions ----------------------------------------------------------- #

    def _confirm(self) -> None:
        self._commit(self._checked_region_id())

    def _use_whole_state(self) -> None:
        self._commit(None)

    def _commit(self, selected_region_id: Optional[str]) -> None:
        if self._decided:
            return  # locked -- a gate is answered exactly once
        self._decided = True
        self._selected_region_id = selected_region_id
        wire = gate.resolve_region_choice(self._request, selected_region_id)
        for _region_id, radio in self._radios:
            radio.setEnabled(False)
        self.confirm_btn.setEnabled(False)
        self.whole_btn.setEnabled(False)
        self._on_decide(
            wire["request_id"],
            wire["choice"],
            wire["selected_region_id"],
            wire["selected_bbox"],
        )
        self.result_lbl.setText(
            gate.region_choice_summary(self._request, selected_region_id)
        )
        self.result_lbl.setVisible(True)
        self._collapse()

    def _collapse(self) -> None:
        self.summary_lbl.setText(
            "Region: "
            + gate.region_choice_summary(self._request, self._selected_region_id)
        )
        self._summary_container.setVisible(True)
        self._body.setVisible(False)
        self.details_toggle.setChecked(False)
        self.details_toggle.setText("show details")


class SpatialInputCard(QFrame):
    """Inline pick card for one ``spatial-input-request``. Submit is disabled
    until a geometry is captured; installing a map tool SAVES the previous one,
    so the canvas is never left on a tool the user did not ask for."""

    def __init__(
        self, request: gate.SpatialInputRequest, on_decide, parent=None,
        iface=None, to_lonlat=None, to_bbox=None, default_name: str = "",
    ):
        super().__init__(parent)
        self._request = request
        self._on_decide = on_decide
        self._iface = iface
        self._to_lonlat = to_lonlat
        self._to_bbox = to_bbox
        self._default_name = default_name
        self._decided = False
        self._captured: Optional[dict] = None  # the wire reply once captured
        self._tool = None
        self._prev_tool = None
        self._marker = None
        self.setObjectName("spatialinputcard")  # scope the fill
        self.setStyleSheet(_SPATIAL_CARD_STYLE)
        self.setFrameShape(QFrame.Shape.StyledPanel)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(3)

        # Collapsed one-line summary (hidden until answered) + "show details".
        summary_row = QHBoxLayout()
        self.summary_lbl = QLabel("")
        self.summary_lbl.setWordWrap(True)
        self.summary_lbl.setTextFormat(Qt.TextFormat.PlainText)
        self.summary_lbl.setStyleSheet(_SPATIAL_TITLE_STYLE)
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

        self._body = QWidget()
        lay = QVBoxLayout(self._body)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(3)
        outer.addWidget(self._body)

        title_lbl = QLabel(request.title or "Pick a location on the map")
        title_lbl.setWordWrap(True)
        title_lbl.setTextFormat(Qt.TextFormat.PlainText)
        title_lbl.setStyleSheet(_SPATIAL_TITLE_STYLE)
        lay.addWidget(title_lbl)

        if request.description:
            desc_lbl = QLabel(request.description)
            desc_lbl.setWordWrap(True)
            desc_lbl.setTextFormat(Qt.TextFormat.PlainText)
            desc_lbl.setStyleSheet(_GATE_BODY_STYLE)
            lay.addWidget(desc_lbl)

        self.pick_btn: Optional[QPushButton] = None
        self.status_lbl = QLabel("")
        self.status_lbl.setWordWrap(True)
        self.status_lbl.setStyleSheet(_GATE_NOTE_STYLE)

        pick_row = QHBoxLayout()
        self.pick_btn = QPushButton(_SPATIAL_PICK_LABEL[
            request.draw_kind or request.mode])
        self.pick_btn.setCheckable(True)
        self.pick_btn.toggled.connect(self._toggle_pick)
        pick_row.addWidget(self.pick_btn)
        self.status_lbl.setText("nothing picked yet")
        pick_row.addWidget(self.status_lbl, 1)
        lay.addLayout(pick_row)

        # A picked POINT carries a name: the slot that asked for it calls the
        # thing it places by that name, so the user types it once, here.
        self.name_edit: Optional[QLineEdit] = None
        if request.mode == "point":
            name_row = QHBoxLayout()
            name_lbl = QLabel("name")
            name_lbl.setStyleSheet(_GATE_NOTE_STYLE)
            name_row.addWidget(name_lbl)
            self.name_edit = QLineEdit(default_name)
            self.name_edit.setPlaceholderText("what to call this point")
            name_row.addWidget(self.name_edit, 1)
            lay.addLayout(name_row)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.submit_btn = QPushButton("Submit")
        self.submit_btn.setEnabled(False)
        self.submit_btn.clicked.connect(self._submit)
        btn_row.addWidget(self.submit_btn)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self._cancel)
        btn_row.addWidget(self.cancel_btn)
        lay.addLayout(btn_row)

        self.result_lbl = QLabel("")
        self.result_lbl.setWordWrap(True)
        self.result_lbl.setStyleSheet(_GATE_NOTE_STYLE)
        self.result_lbl.setVisible(False)
        lay.addWidget(self.result_lbl)

    # -- map-tool pick (mirror of GateCard release-point discipline) -------- #

    def _toggle_pick(self, checked: bool) -> None:
        try:
            canvas = self._iface.mapCanvas()
        except Exception:  # noqa: BLE001 -- headless / no iface
            return
        if checked:
            if self._tool is None:
                self._tool = self._build_tool(canvas)
            self._prev_tool = canvas.mapTool()
            canvas.setMapTool(self._tool)
        else:
            if canvas.mapTool() is self._tool:
                canvas.setMapTool(self._prev_tool)
            self._prev_tool = None

    def _build_tool(self, canvas):
        if self._request.mode == "point":
            from qgis.gui import QgsMapToolEmitPoint

            tool = QgsMapToolEmitPoint(canvas)
            tool.canvasClicked.connect(self._on_point_clicked)
            return tool
        if self._request.mode == "bbox":
            from qgis.gui import QgsMapToolExtent

            tool = QgsMapToolExtent(canvas)
            tool.extentChanged.connect(self._on_extent_chosen)
            return tool
        from .draw_tools import VertexCaptureTool

        tool = VertexCaptureTool(canvas, self._request.draw_kind)
        tool.captured.connect(self._on_shape_captured)
        tool.changed.connect(self._on_vertex_added)
        tool.cancelled.connect(self._on_shape_abandoned)
        return tool

    # -- multi-vertex capture (polygon / polyline) -------------------------- #

    def _on_vertex_added(self, count: int) -> None:
        need = 3 if self._request.draw_kind == "polygon" else 2
        more = max(0, need - count)
        self.status_lbl.setText(
            f"{count} vertices - right-click to finish"
            if more == 0 else
            f"{count} vertices - {more} more, then right-click to finish"
        )

    def _on_shape_abandoned(self) -> None:
        self.status_lbl.setText("drawing abandoned - click the button to start over")

    def _on_shape_captured(self, points: list) -> None:
        lonlats = self._to_lonlats(points)
        kind = self._request.draw_kind
        if not gate.spatial_input_vertices_ready(kind, lonlats):
            self.status_lbl.setText(
                f"a {kind} needs at least {3 if kind == 'polygon' else 2} "
                "vertices - draw it again"
            )
            return
        self._captured = gate.resolve_spatial_input_features(
            self._request.request_id, kind, lonlats
        )
        self.status_lbl.setText(
            f"{kind}: {len(lonlats)} vertices - redraw to change, then Submit"
        )
        self.submit_btn.setEnabled(True)
        if self.pick_btn is not None and self.pick_btn.isChecked():
            self.pick_btn.setChecked(False)   # restores the previous map tool

    def _to_lonlats(self, points: list) -> list:
        """Canvas-CRS vertices -> ``[[lon, lat], ...]``. A vertex that will not
        transform is DROPPED, never guessed at, and the vertex-count gate then
        refuses a shape that lost too many to still be the one drawn."""
        try:
            authid = self._iface.mapCanvas().mapSettings().destinationCrs().authid()
        except Exception:  # noqa: BLE001 -- headless / no iface
            return []
        out = []
        for point in points:
            try:
                lonlat = self._to_lonlat(point, authid) if self._to_lonlat else None
            except Exception:  # noqa: BLE001
                lonlat = None
            if lonlat is not None:
                out.append([lonlat[0], lonlat[1]])
        if len(out) != len(points):
            self.status_lbl.setText(
                f"{len(points) - len(out)} of {len(points)} vertices could not be "
                "read in lon/lat - draw the shape again"
            )
            return []
        return out

    def _on_point_clicked(self, point, _button) -> None:
        try:
            canvas = self._iface.mapCanvas()
            authid = canvas.mapSettings().destinationCrs().authid()
            lonlat = self._to_lonlat(point, authid) if self._to_lonlat else None
        except Exception:  # noqa: BLE001
            lonlat = None
        if lonlat is None:
            self.status_lbl.setText("could not read the clicked point - try again")
            return
        lon, lat = lonlat
        self._captured = gate.resolve_spatial_input_point(
            self._request.request_id, lon, lat, self._point_name()
        )
        try:
            from qgis.gui import QgsVertexMarker

            if self._marker is None:
                self._marker = QgsVertexMarker(self._iface.mapCanvas())
                self._marker.setIconType(QgsVertexMarker.ICON_CROSS)
                self._marker.setColor(Qt.GlobalColor.red)
                self._marker.setPenWidth(3)
                self._marker.setIconSize(14)
            self._marker.setCenter(point)
        except Exception:  # noqa: BLE001 -- marker is cosmetic
            pass
        self.status_lbl.setText(
            f"point: ({lat:.5f}, {lon:.5f}) - click again to move, then Submit"
        )
        self.submit_btn.setEnabled(True)

    def _on_extent_chosen(self, *_args) -> None:
        try:
            canvas = self._iface.mapCanvas()
            extent = self._tool.extent() if self._tool is not None else None
            authid = canvas.mapSettings().destinationCrs().authid()
            bbox = (
                self._to_bbox(extent, authid)
                if (self._to_bbox and extent is not None) else None
            )
        except Exception:  # noqa: BLE001
            bbox = None
        if bbox is None:
            self.status_lbl.setText("could not read the drawn box - try again")
            return
        self._captured = gate.resolve_spatial_input_bbox(
            self._request.request_id, bbox
        )
        self.status_lbl.setText(
            f"box: [{bbox[0]:.4f}, {bbox[1]:.4f}, {bbox[2]:.4f}, {bbox[3]:.4f}]"
            " - redraw to change, then Submit"
        )
        self.submit_btn.setEnabled(True)

    def _pick_teardown(self, drop_marker: bool) -> None:
        if self.pick_btn is not None and self.pick_btn.isChecked():
            self.pick_btn.setChecked(False)  # restores the previous map tool
        if drop_marker and self._marker is not None:
            try:
                self._iface.mapCanvas().scene().removeItem(self._marker)
            except Exception:  # noqa: BLE001
                pass
            self._marker = None

    # -- actions ----------------------------------------------------------- #

    def _point_name(self) -> str:
        return self.name_edit.text() if self.name_edit is not None else ""

    def _submit(self) -> None:
        if self._captured is None:
            self.status_lbl.setText("pick a location first, or press Cancel")
            return
        if self._request.mode == "point":
            # The name is read at SUBMIT, so a name typed after the click rides.
            coords = self._captured["coordinates"]
            self._captured = gate.resolve_spatial_input_point(
                self._request.request_id, coords[0], coords[1], self._point_name()
            )
        self._commit(self._captured, drop_marker=False)

    def _cancel(self) -> None:
        self._commit(
            gate.resolve_spatial_input_cancel(self._request.request_id),
            drop_marker=True,
        )

    def _commit(self, wire: dict, drop_marker: bool) -> None:
        if self._decided:
            return  # locked -- a gate is answered exactly once
        self._decided = True
        self._pick_teardown(drop_marker=drop_marker)
        for widget in (self.submit_btn, self.cancel_btn, self.pick_btn,
                       self.name_edit):
            if widget is not None:
                widget.setEnabled(False)
        self._on_decide(wire)
        self.result_lbl.setText(
            "Cancelled -- the agent will proceed without a picked location."
            if wire.get("cancelled")
            else gate.spatial_input_summary(self._request, wire)
        )
        self.result_lbl.setVisible(True)
        self._collapse(wire)

    def _toggle_details(self, checked: bool) -> None:
        self._body.setVisible(checked)
        self.details_toggle.setText("hide details" if checked else "show details")

    def _collapse(self, wire: dict) -> None:
        self.summary_lbl.setText(
            "Spatial input: " + gate.spatial_input_summary(self._request, wire)
        )
        self._summary_container.setVisible(True)
        self._body.setVisible(False)
        self.details_toggle.setChecked(False)
        self.details_toggle.setText("show details")


class FormCard(QFrame):
    """The declarative FORM gate: the resolved param sheet, editable in place,
    each row badged with where its value came from. SUBMIT IS THE APPROVAL, so
    an empty edit set approves rather than revises."""

    def __init__(self, warning: gate.PayloadWarning, sheet: gate.ParamSheetRequest,
                 on_decide, parent=None):
        super().__init__(parent)
        self._warning = warning
        self._sheet = sheet
        self._on_decide = on_decide
        self._decided: Optional[str] = None
        self._editors: Dict[str, QLineEdit] = {}
        self.setObjectName("formcard")  # scope the fill to the frame
        self.setStyleSheet(_FORM_CARD_STYLE)
        self.setFrameShape(QFrame.Shape.StyledPanel)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(3)

        # Collapsed one-line summary (hidden until answered) + "show details" --
        # the GateCard affordance verbatim.
        summary_row = QHBoxLayout()
        self.summary_lbl = QLabel("")
        self.summary_lbl.setWordWrap(True)
        self.summary_lbl.setTextFormat(Qt.TextFormat.PlainText)
        self.summary_lbl.setStyleSheet(_FORM_TITLE_STYLE)
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

        self._body = QWidget()
        lay = QVBoxLayout(self._body)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(3)
        outer.addWidget(self._body)

        title_lbl = QLabel(sheet.title or f"Review the inputs for {sheet.workflow}")
        title_lbl.setWordWrap(True)
        title_lbl.setTextFormat(Qt.TextFormat.PlainText)
        title_lbl.setStyleSheet(_FORM_TITLE_STYLE)
        lay.addWidget(title_lbl)

        lay.addWidget(self._grid(sheet.basic))

        self._advanced = None
        if sheet.advanced:
            self.advanced_toggle = QPushButton(
                f"show advanced ({len(sheet.advanced)})")
            self.advanced_toggle.setFlat(True)
            self.advanced_toggle.setCheckable(True)
            self.advanced_toggle.setStyleSheet(_THINKING_TOGGLE_STYLE)
            self.advanced_toggle.clicked.connect(self._toggle_advanced)
            lay.addWidget(self.advanced_toggle)
            self._advanced = self._grid(sheet.advanced)
            self._advanced.setVisible(False)
            lay.addWidget(self._advanced)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.submit_btn = QPushButton("Run with these inputs")
        self.submit_btn.clicked.connect(self._submit)
        btn_row.addWidget(self.submit_btn)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self._cancel)
        btn_row.addWidget(self.cancel_btn)
        lay.addLayout(btn_row)

        self.result_lbl = QLabel("")
        self.result_lbl.setWordWrap(True)
        self.result_lbl.setTextFormat(Qt.TextFormat.PlainText)
        self.result_lbl.setStyleSheet(_GATE_NOTE_STYLE)
        self.result_lbl.setVisible(False)
        lay.addWidget(self.result_lbl)

    # -- rows ---------------------------------------------------------------- #

    def _grid(self, rows: List[gate.ParamRow]) -> QWidget:
        """One property-grid block: label, editor and source badge per row. A
        row naming a GROUP opens one under its heading, so a whole module's
        keyword surface reads down the sections it was written in."""
        holder = QWidget()
        grid = QGridLayout(holder)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(2)
        i = -1
        heading = None
        for row in rows:
            i += 1
            if row.group and row.group != heading:
                heading = row.group
                group_lbl = QLabel(heading)
                group_lbl.setStyleSheet(_FORM_TITLE_STYLE)
                grid.addWidget(group_lbl, i, 0, 1, 3)
                i += 1
            name_lbl = QLabel(row.label)
            name_lbl.setStyleSheet(_GATE_BODY_STYLE)
            name_lbl.setToolTip(row.desc)
            grid.addWidget(name_lbl, i, 0)

            editor = QLineEdit(row.display())
            editor.setEnabled(row.editable)
            editor.setToolTip(self._editor_tooltip(row))
            if row.bounds is not None:
                editor.setPlaceholderText(
                    f"{row.bounds[0]:g} to {row.bounds[1]:g}")
            grid.addWidget(editor, i, 1)
            self._editors[row.name] = editor

            badge = QLabel(row.origin or row.source_badge)
            badge.setWordWrap(not row.origin)
            badge.setStyleSheet(
                _FORM_ORIGIN_CHIP_STYLE if row.origin else _FORM_BADGE_STYLE)
            badge.setToolTip(row.source_badge if row.origin else (row.note or ""))
            grid.addWidget(badge, i, 2)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(2, 1)
        return holder

    @staticmethod
    def _editor_tooltip(row: gate.ParamRow) -> str:
        bits = [row.desc] if row.desc else []
        if row.bounds is not None:
            bits.append(f"allowed range {row.bounds[0]:g} to {row.bounds[1]:g}"
                        f"{' ' + row.units if row.units else ''}")
        if row.basis == "derived":
            bits.append("editing this overrides the derivation; the values "
                        "computed from it are re-derived server-side")
        if row.note:
            bits.append(row.note)
        return "\n".join(bits)

    # -- actions ------------------------------------------------------------- #

    def _edits(self) -> dict:
        return gate.resolve_param_sheet_edits(
            self._sheet.rows,
            {name: editor.text() for name, editor in self._editors.items()},
        )

    def _submit(self) -> None:
        if self._decided is not None:
            return  # locked -- a gate is answered exactly once
        revised = self._edits()
        self._decided = "narrow_scope" if revised else "proceed"
        # A sheet with no edits is an APPROVAL, not a revision: sending an empty
        # revised_args would say the user re-supplied every value they only read.
        self._commit(self._decided, revised or None,
                     gate.param_sheet_summary(self._sheet, revised))

    def _cancel(self) -> None:
        if self._decided is not None:
            return
        self._decided = "cancel"
        self._commit("cancel", None, "Inputs declined -- the run did not start")

    def _commit(self, decision: str, revised: Optional[dict], summary: str) -> None:
        for widget in [self.submit_btn, self.cancel_btn, *self._editors.values()]:
            widget.setEnabled(False)
        self._on_decide(self._warning.warning_id, decision, revised)
        self.result_lbl.setText(summary)
        self.result_lbl.setVisible(True)
        self.summary_lbl.setText(summary)
        self._summary_container.setVisible(True)
        self._body.setVisible(False)
        self.details_toggle.setChecked(False)
        self.details_toggle.setText("show details")

    # -- toggles -------------------------------------------------------------- #

    def _toggle_details(self, checked: bool) -> None:
        self._body.setVisible(checked)
        self.details_toggle.setText("hide details" if checked else "show details")

    def _toggle_advanced(self, checked: bool) -> None:
        if self._advanced is None:
            return
        self._advanced.setVisible(checked)
        self.advanced_toggle.setText(
            ("hide" if checked else "show") + f" advanced ({len(self._sheet.advanced)})")
