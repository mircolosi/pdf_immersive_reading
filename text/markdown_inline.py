"""Inline markdown (bold/italic/code/links) — stripped for speech, rendered
as HTML for display. Block-level markup (#, -, *) is handled separately in
segmenter.py; this only deals with markers that can appear inside one line.
"""
import re
from html import escape, unescape

_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_CODE = re.compile(r"`([^`]+)`")

# Underscore emphasis requires a non-word character on the outside (CommonMark
# rule: intraword underscores are not emphasis). Without the lookarounds,
# "variable_name_here" rendered as "variable<em>name</em>here" and was spoken
# with the underscores silently dropped — snake_case identifiers are common in
# the kind of PDF this app reads. Asterisk emphasis has no such rule.
_BOLD = re.compile(r"\*\*([^*]+)\*\*|(?<![\w_])__([^_]+)__(?![\w_])")
_ITALIC = re.compile(r"\*([^*]+)\*|(?<![\w_])_([^_]+)_(?![\w_])")

# Private-use characters delimit a stashed fragment. They cannot appear in PDF
# text and contain none of the markdown delimiters, so a later pass can neither
# match inside a placeholder nor be confused by one.
_STASH_OPEN, _STASH_CLOSE = "\ue000", "\ue001"
_STASHED = re.compile(_STASH_OPEN + r"(\d+)" + _STASH_CLOSE)


class _Stash:
    """Holds emitted HTML that later markdown passes must not see.

    Each pass rewrites the whole string, so anything a pass emits is still on
    the table for the next one. That corrupted real output: the `_` in a link's
    own `target="_blank"` paired with any later underscore in the sentence and
    swallowed the tag, and inline-code content was re-read as bold/italic.
    Stash such fragments behind a placeholder and put them back at the end.
    """

    def __init__(self):
        self._items: list[str] = []

    def keep(self, html: str) -> str:
        self._items.append(html)
        return f"{_STASH_OPEN}{len(self._items) - 1}{_STASH_CLOSE}"

    def restore(self, text: str) -> str:
        return _STASHED.sub(lambda m: self._items[int(m.group(1))], text)


# Some converters (docling, markitdown, marker) pass through literal HTML for
# things markdown has no syntax for: <sup>2</sup> footnote markers, <sub>,
# <br>, <span id="page-1-0">, whole <table>...</table> blocks, "<!-- image -->"
# placeholders and &amp;/&nbsp; entities. None of that may reach TTS as spoken
# markup, while the text *inside* it must survive intact.
# Requiring a letter right after "<" or "</" is what keeps this from eating a
# stray math inequality like "x < 5" or "y > 3".
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_HTML_TAG = re.compile(
    r"""
    (?P<lead>[ \t]*)
    <(?P<close>/?)
    (?P<name>[a-zA-Z][a-zA-Z0-9]*(?::[a-zA-Z][a-zA-Z0-9]*)?)  # optional xml namespace: <mml:math>
    (?:\s+(?:"[^"]*"|'[^']*'|[^>"'])*)?  # attributes — a quoted value may itself contain ">"
    \s*/?>
    (?P<trail>[ \t]*)
    """,
    re.VERBOSE,
)

# Inline tags can wrap text mid-word ("water<sup>2</sup>O"), so they vanish
# without a trace, taking the whitespace on their *inner* side with them
# ("note<sup> 2 </sup>." -> "note2."). Every other tag is treated as block
# level and leaves one space behind, so dropping a "</td><td>" or a "<br>"
# can't fuse the words on either side of it into one.
_INLINE_TAGS = frozenset(
    "a abbr b big cite code del em font i ins kbd mark q s samp small span "
    "strong sub sup tt u var wbr".split()
)


def _replace_tag(match: re.Match) -> str:
    if match.group("name").lower() in _INLINE_TAGS:
        # keep the whitespace on the outer side of the tag pair, or
        # "is <b>bold</b> text" would collapse into "isbold text"
        return match.group("trail") if match.group("close") else match.group("lead")
    return " "


def strip_html_tags(text: str) -> str:
    """Drop HTML markup, keep its content — "<sup> 2 </sup>" -> "2". Entities
    are resolved to the character they stand for, so "&amp;" reaches TTS as
    "&" rather than as the literal word "amp"."""
    text = _HTML_COMMENT.sub("", text)
    text = _HTML_TAG.sub(_replace_tag, text)
    text = unescape(text).replace("\xa0", " ")  # non-breaking space -> plain space
    return re.sub(r"[ \t]{2,}", " ", text)


def strip_inline_markdown(text: str) -> str:
    """Plain speakable text — markup removed, visible content kept. Code spans
    go first and are stashed, so a `**` or `_` *inside* inline code stays part
    of the literal it belongs to instead of being read as emphasis."""
    stash = _Stash()
    text = _CODE.sub(lambda m: stash.keep(m.group(1)), text)
    text = _LINK.sub(r"\1", text)
    text = _BOLD.sub(lambda m: m.group(1) or m.group(2), text)
    text = _ITALIC.sub(lambda m: m.group(1) or m.group(2), text)
    return stash.restore(text)


# One alternative per group: a tag, a run of non-space non-tag text, or
# whitespace. Matching tags explicitly is what lets the word wrapper step over
# them instead of splitting them in half.
_HTML_PIECE = re.compile(r"(<[^>]+>)|([^<\s]+)|(\s+)")


def wrap_words_in_html(html: str) -> str:
    """Wrap every visible word of already-rendered inline HTML in a
    `<span class="word" data-w="N">`, leaving tags untouched.

    Word spans have to be present from the first render, before any audio
    exists, or the panel changes shape mid-playback when timings arrive. They
    can't be produced by splitting the *markdown* on whitespace either, since
    that cuts delimiters in half ("**two words**" -> "**two" + "words**"), so
    the markdown is rendered first and the words are wrapped in the output.
    Emphasis spanning several words simply nests: "<strong><span
    class="word">two</span> <span class="word">words</span></strong>".

    Index N counts *whitespace-delimited* words in document order — the same
    way `align_timings_to_words` numbers them — so the client matches a timing
    to its span by index alone. A tag boundary inside a word ("`code_x`," ->
    "<code>code_x</code>,") necessarily produces two spans, and both carry the
    same index rather than consuming two: the index has to track the spoken
    word, and one span can't swallow the tag pair without breaking nesting
    when emphasis runs across several words.
    """
    parts = []
    index = 0
    started = False  # has any word been emitted yet
    after_space = False
    for tag, word, space in _HTML_PIECE.findall(html):
        if tag:
            parts.append(tag)
        elif space:
            parts.append(space)
            after_space = True
        else:
            if started and after_space:
                index += 1
            started, after_space = True, False
            parts.append(f'<span class="word" data-w="{index}">{word}</span>')
    return "".join(parts)


def render_inline_html(text: str) -> str:
    """HTML-safe rendering of the same markup, for display. Escapes first —
    none of the markdown delimiters collide with html.escape's targets."""
    html_text = escape(text)  # quote=True, so a stray '"' can't break out of href
    stash = _Stash()
    # Code spans are literal all the way through: stash tag *and* content.
    html_text = _CODE.sub(lambda m: stash.keep(f"<code>{m.group(1)}</code>"), html_text)
    # Only the opening <a> tag is stashed (it carries the underscore-bearing
    # target and the caller-supplied href). The link *text* stays in play, so
    # emphasis inside it still renders and word-wrapping can reach it.
    html_text = _LINK.sub(
        lambda m: stash.keep(f'<a href="{m.group(2)}" target="_blank" rel="noopener">')
        + m.group(1)
        + "</a>",
        html_text,
    )
    html_text = _BOLD.sub(lambda m: f"<strong>{m.group(1) or m.group(2)}</strong>", html_text)
    html_text = _ITALIC.sub(lambda m: f"<em>{m.group(1) or m.group(2)}</em>", html_text)
    return stash.restore(html_text)
