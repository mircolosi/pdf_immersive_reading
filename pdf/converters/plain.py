"""Baseline converter: raw PyMuPDF text extraction, no markdown structure.
Zero extra dependencies — always available, used as the safe default."""
from pdf.converters.base import PDFConverter
from pdf.extractor import extract_pages


class PlainTextConverter(PDFConverter):
    produces_inline_markdown = False

    def extract_markdown_pages(self, pdf_path: str) -> list[str]:
        return extract_pages(pdf_path, self.strip_headers_footers)
