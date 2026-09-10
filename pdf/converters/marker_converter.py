"""marker (datalab-to/marker): full layout + OCR + equation ML pipeline
(Surya models), downloaded once then fully local CPU inference. Highest
precision of the bundled converters, also the heaviest/slowest. Produces one
flat markdown document — no reliable per-page split across marker versions,
so it comes back as a single page like MarkItDown."""
from pdf.converters.base import PDFConverter


class MarkerConverter(PDFConverter):
    supports_pages = False

    def extract_markdown_pages(self, pdf_path: str) -> list[str]:
        from marker.converters.pdf import PdfConverter  # deferred import, optional dependency
        from marker.models import create_model_dict
        from marker.output import text_from_rendered

        converter = PdfConverter(artifact_dict=create_model_dict())
        rendered = converter(pdf_path)
        text, _, _ = text_from_rendered(rendered)
        return [text]
