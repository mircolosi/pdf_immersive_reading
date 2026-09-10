"""pymupdf4llm: heuristic markdown extraction on top of PyMuPDF. No ML model,
pure CPU, fast. Good precision on digital-native PDFs; weak on scanned/complex
layouts."""
from pdf.converters.base import PDFConverter


class PyMuPDF4LLMConverter(PDFConverter):
    def extract_markdown_pages(self, pdf_path: str) -> list[str]:
        import pymupdf4llm  # deferred import, optional dependency

        chunks = pymupdf4llm.to_markdown(pdf_path, page_chunks=True)
        return [chunk["text"] for chunk in chunks]
