"""Name -> converter class, for the GUI dropdown and config.yaml. All entries
are listed regardless of whether their optional dependency is installed —
picking an uninstalled one just surfaces a clear ImportError in the UI."""
from pdf.converters.base import PDFConverter
from pdf.converters.docling_converter import DoclingConverter
from pdf.converters.marker_converter import MarkerConverter
from pdf.converters.markitdown_converter import MarkItDownConverter
from pdf.converters.plain import PlainTextConverter
from pdf.converters.pymupdf4llm_converter import PyMuPDF4LLMConverter

CONVERTERS = {
    "plain": PlainTextConverter,
    "pymupdf4llm": PyMuPDF4LLMConverter,
    "markitdown": MarkItDownConverter,
    "docling": DoclingConverter,
    "marker": MarkerConverter,
}


def build_converter(name: str, strip_headers_footers: bool = True) -> PDFConverter:
    if name not in CONVERTERS:
        raise ValueError(f"Unknown pdf converter {name!r}; choices: {list(CONVERTERS)}")
    return CONVERTERS[name](strip_headers_footers=strip_headers_footers)
