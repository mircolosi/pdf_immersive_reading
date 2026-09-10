"""Markdown page text -> list[Sentence], per page (no cross-page stitching,
v1 limitation).

Markdown-block-aware: splits into blocks on blank lines, headings, and list
items first, THEN runs pysbd sentence splitting inside each block — with
line-wrap newlines *within* a block joined into spaces first. Running pysbd
directly on raw extracted text (one hard '\\n' per visual PDF line) makes it
split on every line wrap instead of on punctuation; joining wrapped lines
before segmenting is the actual fix for that.
"""
import re

import pysbd

from models import Sentence
from text.markdown_inline import strip_html_tags, strip_inline_markdown

_seg = pysbd.Segmenter(language="en", clean=False)

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")

# pysbd's abbreviation handling still splits "et al." as a sentence end when
# followed by a capitalized word (common in citations: "Smith et al. In this
# paper...") — its period-then-capital heuristic overrides the abbreviation
# exception. Protect the period with a placeholder before segmenting, restore
# it right after (before the piece is used for anything else), so pysbd
# never sees a real period there and can't split on it.
_ET_AL_RE = re.compile(r"\bet al\.", re.IGNORECASE)


def _protect_et_al(text: str) -> str:
    return _ET_AL_RE.sub(lambda m: m.group(0)[:-1] + "\x00", text)


def _restore_et_al(text: str) -> str:
    return text.replace("\x00", ".")


def _split_blocks(page_text: str):
    """Yield (block_type, heading_level, raw_block_text) tuples in order."""
    blocks = []
    current: list[str] = []

    def flush():
        if current:
            joined = " ".join(l.strip() for l in current if l.strip())
            if joined:
                blocks.append(("paragraph", 0, joined))
            current.clear()

    for line in page_text.splitlines():
        if not line.strip():
            flush()
            continue
        heading_match = _HEADING_RE.match(line)
        if heading_match:
            flush()
            blocks.append(("heading", len(heading_match.group(1)), heading_match.group(2).strip()))
            continue
        list_match = _LIST_ITEM_RE.match(line)
        if list_match:
            flush()
            blocks.append(("list_item", 0, list_match.group(1).strip()))
            continue
        current.append(line)
    flush()
    return blocks


def segment_page(page_text: str, page_no: int, produces_inline_markdown: bool = True) -> list[Sentence]:
    """produces_inline_markdown=False (plain) skips
    inline-markdown stripping entirely — that text was never markdown, and
    an asterisk/underscore/bracket there is just a literal character
    (citation markers, variable names, footnotes), not a stray one to eat."""
    page_text = strip_html_tags(page_text)  # tags dropped, their content kept — for any converter
    sentences = []
    cursor = 0
    idx = 0
    for block_index, (block_type, heading_level, block_text) in enumerate(_split_blocks(page_text)):
        if block_type in ("heading", "list_item"):
            pieces = [block_text]
        else:
            pieces = [
                _restore_et_al(s.strip()) for s in _seg.segment(_protect_et_al(block_text)) if s.strip()
            ]

        for piece in pieces:
            start = page_text.find(piece, cursor)
            if start == -1:
                start = cursor
            end = start + len(piece)
            cursor = end
            speech_text = strip_inline_markdown(piece) if produces_inline_markdown else piece
            sentences.append(
                Sentence(
                    page_no=page_no,
                    index_in_page=idx,
                    text=speech_text,
                    char_start=start,
                    char_end=end,
                    block_type=block_type,
                    heading_level=heading_level,
                    block_index=block_index,
                    display_text=piece,
                )
            )
            idx += 1
    return sentences


def split_for_synthesis(text: str, max_chars: int) -> list[str]:
    """Split a long sentence at clause boundaries for synthesis granularity only
    (§10.3) — the text panel still highlights the full sentence as one unit."""
    if len(text) <= max_chars:
        return [text]

    parts = []
    for clause in re.split(r"(?<=[,;:])\s+", text):
        if not parts or len(parts[-1]) + 1 + len(clause) > max_chars:
            parts.append(clause)
        else:
            parts[-1] = parts[-1] + " " + clause
    return parts
