#!/usr/bin/env python
"""The DOC size: the weight a template page's figures carry, and their commit stamp.

A doc render is the packet's own picture at a page's weight - a composite near
300 KB, an animation near 1-2 MB - stamped with the commit that produced it, so a
figure that predates its template's declaration is a red test rather than a memory.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

__all__ = ["ANIMATION_DPI", "ANIMATION_FRAMES", "CHART_DPI", "SHEET_DPI",
           "STAMP_COMMIT", "head_commit", "quantize_png", "read_commit_stamp",
           "stamp_commit"]

#: Contact-sheet density for a page figure. The proof sheet renders at 130 and
#: lands near 10 MB; a page embeds the same panels at a third of that density and
#: the palette pass below takes the rest.
SHEET_DPI = 62
#: A GIF is already palette-coded, so the frame count and this density are the
#: whole size budget.
ANIMATION_DPI = 60
#: Frames a doc animation is thinned to. A 61-frame solve at full density is a
#: seven-megabyte GIF; the stride keeps the first and last instants, so a reader
#: watches the same field over the same window at a weight a page can carry.
ANIMATION_FRAMES = 30
#: A chart is line art: it costs almost nothing at any density a reader can read.
CHART_DPI = 120
#: Colours a doc PNG is quantized to. A basemap photograph in 24-bit truecolour
#: is what makes a proof sheet ten megabytes; 256 indexed colours is the
#: difference between a figure a page can carry and one it cannot.
_PALETTE_COLORS = 256
#: The PNG text key the commit rides in, beside the assembler's run-id stamp.
STAMP_COMMIT = "trid3nt_commit"
#: The GIF comment block's key=value payload. A GIF carries no text chunks, so
#: the run and the commit ride in the one comment the format does have.
_GIF_COMMENT = "trid3nt_run_id={run_id} trid3nt_commit={commit}"


def head_commit(repo: Path | str | None = None) -> str:
    """The commit a render is stamped with: HEAD, plus ``-dirty`` on a dirty tree.
    A dirty stamp is honest about a figure drawn from uncommitted code."""
    root = str(repo or Path(__file__).resolve().parents[2])
    head = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    # TRACKED changes only: the renders themselves land untracked on a first
    # pass, and a stamp that called every first render dirty would say nothing.
    dirty = subprocess.run(
        ["git", "-C", root, "status", "--porcelain", "--untracked-files=no"],
        capture_output=True, text=True, check=True).stdout.strip()
    return f"{head}-dirty" if dirty else head


def quantize_png(path: Path) -> int:
    """Re-save a PNG as a 256-colour paletted image, in place. Returns its bytes."""
    from PIL import Image

    with Image.open(path) as image:
        existing = {k: v for k, v in image.info.items() if isinstance(v, str)}
        payload = image.convert("RGB").quantize(colors=_PALETTE_COLORS)
    from PIL import PngImagePlugin

    meta = PngImagePlugin.PngInfo()
    for key, value in existing.items():
        meta.add_text(key, value)
    payload.save(path, optimize=True, pnginfo=meta)
    return path.stat().st_size


def stamp_commit(path: Path, *, run_id: str, commit: str) -> None:
    """Write the commit stamp onto one doc render, PNG chunk or GIF comment."""
    from PIL import Image, PngImagePlugin

    with Image.open(path) as image:
        payload = image.copy()
        existing = dict(image.info)
        fmt = image.format
    if fmt == "GIF":
        # A GIF is re-saved from its own frames, so the animation survives the
        # stamp; Pillow's ``save_all`` needs the sequence, not the first frame.
        from PIL import ImageSequence

        with Image.open(path) as image:
            frames = [frame.copy() for frame in ImageSequence.Iterator(image)]
        frames[0].save(
            path, save_all=True, append_images=frames[1:],
            duration=existing.get("duration", 250), loop=existing.get("loop", 0),
            comment=_GIF_COMMENT.format(run_id=run_id, commit=commit).encode())
        return
    meta = PngImagePlugin.PngInfo()
    for key, value in existing.items():
        if isinstance(value, str) and key != STAMP_COMMIT:
            meta.add_text(key, value)
    meta.add_text(STAMP_COMMIT, commit)
    payload.save(path, pnginfo=meta)


def read_commit_stamp(path: Path) -> str | None:
    """The commit a doc render carries, or ``None`` when it carries none."""
    from PIL import Image

    with Image.open(path) as image:
        if image.format == "GIF":
            comment = image.info.get("comment") or b""
            text = comment.decode("utf-8", "replace") if isinstance(comment, bytes) \
                else str(comment)
            for token in text.split():
                if token.startswith(f"{STAMP_COMMIT}="):
                    return token.split("=", 1)[1]
            return None
        value = image.info.get(STAMP_COMMIT)
    return str(value) if value else None
