"""PDFConverter ABC — pluggable PDF -> markdown backends, swappable live from
the GUI dropdown (unlike the TTS engine, which is config-only)."""
from abc import ABC, abstractmethod


class PDFConverter(ABC):
    #: True if this backend gives real per-page text; converters that only
    #: produce one flat document (e.g. MarkItDown) return the whole thing as
    #: a single page and set this False so the UI can explain the tradeoff.
    supports_pages: bool = True

    #: True if this backend actually derives **/*/`` `` `` `` /[]() from real
    #: font-styling metadata (pymupdf4llm, markitdown, docling, marker) — so
    #: those characters are safe to strip for speech / render as HTML.
    #: False for converters whose text is raw extraction (plain): there, an
    #: asterisk/underscore is just a literal character (citation markers,
    #: variable names, footnotes) and must be left alone.
    produces_inline_markdown: bool = True

    def __init__(self, strip_headers_footers: bool = True):
        """Every converter accepts the flag uniformly so `build_converter` can
        stay a one-liner. Only `plain` acts on it — the markdown backends do
        their own layout analysis and have no line-level notion of a running
        header to strip."""
        self.strip_headers_footers = strip_headers_footers

    @abstractmethod
    def extract_markdown_pages(self, pdf_path: str) -> list[str]:
        """Return one markdown string per page."""
