"""
Adds an invisible, searchable text layer to PDF pages that don't have
real text yet (scanned pages), using RapidOCR for recognition.

This mirrors what OCRmyPDF does with Tesseract, but with a pure-Python/
ONNX OCR engine (RapidOCR) so it can run inside a Vercel Python
serverless function without any system binary dependency -- Tesseract
itself was ruled out for that reason (see project chat history: Render/
Vercel-style serverless filesystems are read-only and can't apt-install
a binary at runtime).

Key implementation detail that took real trial and error to find:
pdf2docx (the next stage in this pipeline) reads PyMuPDF's
`get_texttrace()` and, by default, IGNORES any text span with
`type == 3` (PDF render mode 3 = invisible) -- it assumes invisible
text is OCR noise layered under a "real" born-digital page, not the
actual content. Pass `ocr=2` to pdf2docx's `Converter.convert()` to
flip that filter around: extract ONLY the hidden/invisible spans
instead of ignoring them. Forgetting this flag silently produces an
empty DOCX with a logged warning ("Words count: 0... scanned pdf") and
no exception -- easy to miss in production.
"""
import fitz
import numpy as np
from rapidocr import RapidOCR

RENDER_DPI = 200

# Loading the OCR engine (and its ONNX models) is the expensive part of
# this module -- keep one instance at module scope so a warm serverless
# function instance reuses it across invocations instead of re-loading
# on every request.
_engine = None


def _get_engine() -> RapidOCR:
    global _engine
    if _engine is None:
        _engine = RapidOCR()
    return _engine


def ocr_page_to_invisible_text(page: "fitz.Page") -> int:
    """
    Renders a single PDF page to a raster image, runs OCR on it, and
    writes the recognized text back onto the page as an invisible
    (render_mode=3) text layer at the corresponding coordinates.

    The page's visual appearance is unchanged -- this only adds a
    machine-readable layer underneath what's already there, exactly
    like OCRmyPDF's approach with Tesseract.

    Returns the number of text spans inserted, for logging/diagnostics.
    """
    engine = _get_engine()

    zoom = RENDER_DPI / 72.0
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat)
    img_array = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    if pix.n == 4:
        img_array = img_array[:, :, :3]

    result = engine(img_array)
    if result.txts is None:
        return 0

    inserted = 0
    for box, text in zip(result.boxes, result.txts):
        if not text.strip():
            continue

        # box is a quad of 4 points in pixel space at RENDER_DPI;
        # convert to PDF point space (72 dpi) by dividing out zoom.
        # float() casts are mandatory -- RapidOCR returns numpy
        # float32, and PyMuPDF's insert_text rejects numpy scalars with
        # an opaque TypeError deep in the MuPDF C binding rather than
        # coercing them, even though they look like plain floats.
        xs = [float(pt[0]) / zoom for pt in box]
        ys = [float(pt[1]) / zoom for pt in box]
        x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)

        # Approximate font size from box height. This is never
        # rendered visibly (render_mode=3), so exact font metrics don't
        # matter visually -- but pdf2docx uses the size to infer
        # heading vs. body text styling in the reconstructed DOCX, so
        # it's worth keeping proportional to the actual detected text
        # height rather than a flat constant.
        font_size = float(max((y1 - y0) * 0.8, 4))

        page.insert_text(
            fitz.Point(x0, y1 - (y1 - y0) * 0.15),
            text,
            fontsize=font_size,
            render_mode=3,
        )
        inserted += 1

    return inserted


def ocr_pdf(pdf_bytes: bytes, page_indices: list[int] | None = None) -> bytes:
    """
    Returns a new PDF (as bytes) with an invisible OCR text layer added
    to the specified pages (or all pages, if page_indices is None).
    Pages not in page_indices are left untouched.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        targets = range(len(doc)) if page_indices is None else page_indices
        for i in targets:
            ocr_page_to_invisible_text(doc[i])
        return doc.write()
    finally:
        doc.close()
