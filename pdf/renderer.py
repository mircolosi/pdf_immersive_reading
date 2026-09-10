"""Render PDF pages to PNG for the visual-fidelity panel."""
import os

import pymupdf as fitz  # PyMuPDF (new import name, avoids deprecation warning)


def render_page(pdf_path: str, page_no: int, out_dir: str, dpi: int = 150) -> str:
    """page_no is 1-based. Returns path to cached PNG, rendering if missing."""
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"page_{page_no:04d}.png")
    if os.path.exists(out_path):
        return out_path

    doc = fitz.open(pdf_path)
    page = doc.load_page(page_no - 1)
    zoom = dpi / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
    pix.save(out_path)
    doc.close()
    return out_path
