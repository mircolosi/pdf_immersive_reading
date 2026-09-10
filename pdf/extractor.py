"""Per-page text extraction with repeated header/footer stripping.

Header/footer detection: collect the first and last non-empty line of every
page; any line whose normalized (digits stripped) form repeats on more than
half the pages is treated as running header/footer and dropped.
"""
import re
from collections import Counter

import pymupdf as fitz  # PyMuPDF (new import name, avoids deprecation warning)


def _normalize(line: str) -> str:
    return re.sub(r"\d+", "#", line.strip())


def extract_pages(pdf_path: str, strip_headers_footers: bool = True) -> list[str]:
    doc = fitz.open(pdf_path)
    raw_pages = [page.get_text("text") for page in doc]
    doc.close()

    if not strip_headers_footers or len(raw_pages) < 3:
        return raw_pages

    edge_lines = []
    for text in raw_pages:
        lines = [l for l in text.splitlines() if l.strip()]
        if lines:
            edge_lines.append(_normalize(lines[0]))
            edge_lines.append(_normalize(lines[-1]))

    counts = Counter(edge_lines)
    threshold = len(raw_pages) / 2
    repeated = {norm for norm, n in counts.items() if n > threshold}

    cleaned_pages = []
    for text in raw_pages:
        lines = text.splitlines()
        kept = [l for l in lines if not (l.strip() and _normalize(l) in repeated)]
        cleaned_pages.append("\n".join(kept))
    return cleaned_pages
