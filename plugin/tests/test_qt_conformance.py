"""Static Qt5 / Qt6 dual-compatibility conformance guard.

The shipped package FAILS this on a direct ``PyQt5`` or ``PyQt6`` import - Qt
must route through the ``qgis.PyQt`` shim - or on a known UNSCOPED enum access,
since only the fully scoped spelling resolves under both bindings. The scan is
tokenize-aware, so a mention in a string or a comment never trips it."""
from __future__ import annotations

import io
import os
import re
import tokenize
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_PACKAGE_ROOT = os.path.normpath(os.path.join(_HERE, ".."))

# --- direct-binding import: Qt must go through qgis.PyQt --------------------- #
# Matches ``import PyQt5``, ``from PyQt6.QtCore import ...`` but NOT the
# allowed ``from qgis.PyQt.QtCore import ...`` (there ``PyQt`` is preceded by
# ``qgis.`` so the ``(?:from|import)\s+PyQt`` anchor never lines up).
_DIRECT_IMPORT = re.compile(r"(?m)^\s*(?:from|import)\s+PyQt[56]\b")

# --- unscoped Qt-namespace enums -------------------------------------------- #
# Each alternative is a leaf enum member that Qt6 no longer exposes unscoped.
# The scoped replacement (``Qt.<EnumType>.<Member>``) is NOT matched, because
# the char after ``Qt.`` is then the EnumType, not the member.
_QT_UNSCOPED = re.compile(
    r"\bQt\.(?:"
    r"Align(?:Left|Right|HCenter|VCenter|Center|Top|Bottom|Justify|Baseline)"
    r"|Alignment"
    r"|UserRole|DisplayRole|EditRole|DecorationRole|ToolTipRole"
    r"|PlainText|RichText|MarkdownText|AutoText"
    r"|TextSelectableByMouse|TextSelectableByKeyboard|TextBrowserInteraction"
    r"|LinksAccessibleByMouse|NoTextInteraction"
    r"|ScrollBarAsNeeded|ScrollBarAlwaysOff|ScrollBarAlwaysOn"
    r"|Checked|Unchecked|PartiallyChecked"
    r"|Horizontal|Vertical"
    r"|red|green|blue|black|white|gray|darkGray|lightGray|transparent|yellow|cyan|magenta"
    r"|UpArrow|DownArrow|LeftArrow|RightArrow|NoArrow"
    r"|NoPen|SolidLine|DashLine|DotLine|DashDotLine"
    r"|NoBrush|SolidPattern"
    r"|ShiftModifier|ControlModifier|AltModifier|MetaModifier|NoModifier"
    r"|LeftButton|RightButton|MidButton|MiddleButton|NoButton"
    r"|Key_[A-Za-z0-9_]+"
    r"|ItemIsEditable|ItemIsSelectable|ItemIsEnabled|ItemIsUserCheckable|ItemIsDragEnabled|ItemIsDropEnabled|NoItemFlags"
    r"|ISODate|TextDate|RFC2822Date"
    r"|CustomContextMenu|DefaultContextMenu|NoContextMenu|ActionsContextMenu|PreventContextMenu"
    r"|NoFocus|TabFocus|ClickFocus|StrongFocus|WheelFocus"
    r"|ElideLeft|ElideRight|ElideMiddle|ElideNone"
    r"|LeftDockWidgetArea|RightDockWidgetArea|TopDockWidgetArea|BottomDockWidgetArea|NoDockWidgetArea"
    r"|CaseInsensitive|CaseSensitive"
    r"|SmoothTransformation|FastTransformation|KeepAspectRatio|IgnoreAspectRatio|KeepAspectRatioByExpanding"
    r"|WaitCursor|ArrowCursor|PointingHandCursor|CrossCursor|OpenHandCursor|ClosedHandCursor|BusyCursor"
    r")\b"
)

# --- unscoped enums on specific widget / gui classes ------------------------ #
# member -> the class holding it, with the scoped form NOT matched (the char
# after ``<Class>.`` becomes the EnumType).
_CLASS_UNSCOPED = re.compile(
    r"\b(?:"
    r"QMessageBox\.(?:Ok|Cancel|Yes|No|Save|Discard|Close|Apply|Abort|Retry|Ignore|YesToAll|NoToAll|Reset|Help)"
    r"|QDialogButtonBox\.(?:Ok|Cancel|Yes|No|Save|Discard|Close|Apply|Abort|Retry|Ignore|Reset|Help)"
    r"|QSizePolicy\.(?:Fixed|Minimum|Maximum|Preferred|Expanding|MinimumExpanding|Ignored)"
    r"|QFrame\.(?:NoFrame|Box|Panel|StyledPanel|HLine|VLine|WinPanel|Plain|Raised|Sunken)"
    r"|QLineEdit\.(?:Normal|NoEcho|Password|PasswordEchoOnEdit)"
    r"|QPlainTextEdit\.(?:NoWrap|WidgetWidth)"
    r"|QTextEdit\.(?:NoWrap|WidgetWidth|FixedPixelWidth|FixedColumnWidth)"
    r"|QPalette\.(?:Window|WindowText|Base|AlternateBase|Text|Button|ButtonText|BrightText|Highlight|HighlightedText|Mid|Midlight|Dark|Light|Shadow|Link|LinkVisited|ToolTipBase|ToolTipText|PlaceholderText)"
    r"|QTextCursor\.(?:MoveAnchor|KeepAnchor|Start|End|StartOfLine|EndOfLine|StartOfBlock|EndOfBlock|NextCharacter|PreviousCharacter|Up|Down|Left|Right|WordLeft|WordRight)"
    r"|QEvent\.(?:KeyPress|KeyRelease|MouseButtonPress|MouseButtonRelease|MouseButtonDblClick|MouseMove|Wheel|Resize|Close|Show|Hide|FocusIn|FocusOut|Enter|Leave|Paint|Timer|ContextMenu)"
    r"|QAbstractItemView\.(?:NoSelection|SingleSelection|MultiSelection|ExtendedSelection|ContiguousSelection|SelectItems|SelectRows|SelectColumns|NoEditTriggers|CurrentChanged|DoubleClicked|SelectedClicked|EditKeyPressed|AnyKeyPressed|AllEditTriggers)"
    r"|QHeaderView\.(?:Interactive|Fixed|Stretch|ResizeToContents|Custom)"
    r"|QComboBox\.(?:NoInsert|InsertAtTop|InsertAtCurrent|InsertAtBottom|InsertAfterCurrent|InsertBeforeCurrent|InsertAlphabetically|AdjustToContents|AdjustToContentsOnFirstShow)"
    r"|QFont\.(?:Thin|Light|Normal|Medium|DemiBold|Bold|ExtraBold|Black|StyleNormal|StyleItalic|StyleOblique)"
    r"|QPainter\.(?:Antialiasing|TextAntialiasing|SmoothPixmapTransform|HighQualityAntialiasing)"
    r"|QToolButton\.(?:DelayedPopup|MenuButtonPopup|InstantPopup)"
    r")\b"
)

# ``.exec_(`` (the Qt5-only method name) is gone in Qt6 -- ``.exec(`` works in
# both. ``code_exec_`` etc. are identifier substrings, never a ``.exec_(`` call.
_EXEC_UNDERSCORE = re.compile(r"\.exec_\s*\(")


def _iter_py_files(root):
    # Shipped package code only. The repo checkout co-locates the plugin's own
    # dev-only ``tests/`` (Qt5 harnesses -- exempt from the Qt6 scoped-enum
    # rule) and ``docs/`` beside the package; neither ships, so both are pruned.
    _skip = {"tests", "docs", "__pycache__"}
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _skip]
        for name in files:
            if name.endswith(".py"):
                yield os.path.join(dirpath, name)


def _mask_strings_and_comments(src):
    """Return ``src`` with every STRING / COMMENT / f-string-literal span
    blanked to spaces (offsets and line count preserved) so the regexes see
    executable code only."""
    grid = [list(line) for line in src.splitlines(keepends=True)]
    mask_types = {tokenize.STRING, tokenize.COMMENT}
    for attr in ("FSTRING_MIDDLE", "FSTRING_START", "FSTRING_END"):
        if hasattr(tokenize, attr):
            mask_types.add(getattr(tokenize, attr))
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type not in mask_types:
                continue
            (sr, sc), (er, ec) = tok.start, tok.end
            for row in range(sr, er + 1):
                line = grid[row - 1]
                c0 = sc if row == sr else 0
                c1 = ec if row == er else len(line)
                for col in range(c0, min(c1, len(line))):
                    if line[col] != "\n":
                        line[col] = " "
    except (tokenize.TokenError, IndentationError):
        # Unparseable partial file: fall back to raw source (worst case a
        # docstring mention could trip -- acceptable vs missing a real one).
        return src
    return "".join("".join(line) for line in grid)


class TestQtConformance(unittest.TestCase):
    """Regex-based portability guard over the shipped ``trid3nt`` package."""

    def setUp(self):
        self.files = sorted(_iter_py_files(_PACKAGE_ROOT))
        self.assertTrue(self.files, f"no .py files found under {_PACKAGE_ROOT}")

    def _scan(self, pattern, label):
        violations = []
        for path in self.files:
            with open(path, "r", encoding="utf-8") as fh:
                src = fh.read()
            code = _mask_strings_and_comments(src)
            for m in pattern.finditer(code):
                line_no = code.count("\n", 0, m.start()) + 1
                rel = os.path.relpath(path, _PACKAGE_ROOT)
                violations.append(f"  {rel}:{line_no}: {m.group(0)}")
        return violations

    def test_no_direct_pyqt_imports(self):
        v = self._scan(_DIRECT_IMPORT, "direct PyQt import")
        self.assertFalse(
            v,
            "Direct PyQt5/PyQt6 imports found -- route Qt through qgis.PyQt "
            "instead:\n" + "\n".join(v),
        )

    def test_no_unscoped_qt_namespace_enums(self):
        v = self._scan(_QT_UNSCOPED, "unscoped Qt.* enum")
        self.assertFalse(
            v,
            "Unscoped Qt.* enum(s) found (Qt6 requires the scoped form, e.g. "
            "Qt.ItemDataRole.UserRole):\n" + "\n".join(v),
        )

    def test_no_unscoped_widget_class_enums(self):
        v = self._scan(_CLASS_UNSCOPED, "unscoped widget-class enum")
        self.assertFalse(
            v,
            "Unscoped widget-class enum(s) found (Qt6 requires the scoped "
            "form, e.g. QMessageBox.StandardButton.Ok):\n" + "\n".join(v),
        )

    def test_no_exec_underscore(self):
        v = self._scan(_EXEC_UNDERSCORE, ".exec_( call")
        self.assertFalse(
            v,
            "``.exec_(`` call(s) found (removed in Qt6) -- use ``.exec(``:\n"
            + "\n".join(v),
        )


if __name__ == "__main__":
    unittest.main()
