"""Docling (IBM): small layout-analysis ML model, downloaded once then fully
local CPU inference. Good precision on tables/reading-order, lighter and
faster than marker."""
from pdf.converters.base import PDFConverter


class DoclingConverter(PDFConverter):
    def extract_markdown_pages(self, pdf_path: str) -> list[str]:
        from docling.document_converter import DocumentConverter  # deferred import, optional dependency

        doc = DocumentConverter().convert(pdf_path).document
        page_count = len(doc.pages) if getattr(doc, "pages", None) else 1
        if page_count <= 1:
            return [doc.export_to_markdown()]

        pages = []
        for page_no in range(1, page_count + 1):
            try:
                pages.append(doc.export_to_markdown(page_no=page_no))
            except TypeError:
                # older/newer docling versions may not support per-page export
                self.supports_pages = False
                return [doc.export_to_markdown()]
        return pages
