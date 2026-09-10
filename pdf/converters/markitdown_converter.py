"""MarkItDown (Microsoft): lightweight generalist converter, no PDF-specific
layout model. Produces one flat markdown document, no native page split."""
from pdf.converters.base import PDFConverter


class MarkItDownConverter(PDFConverter):
    supports_pages = False  # whole document comes back as one block

    def extract_markdown_pages(self, pdf_path: str) -> list[str]:
        from markitdown import MarkItDown  # deferred import, optional dependency

        result = MarkItDown().convert(pdf_path)
        return [result.text_content]
