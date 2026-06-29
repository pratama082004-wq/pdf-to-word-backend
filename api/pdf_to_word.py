"""
Top-level orchestration for the PDF to Word feature.

Critical design constraint discovered through testing (not assumed
from docs): pdf2docx's `ocr` parameter is a per-conversion flag, not
per-page. Passing `ocr=2` makes it read ONLY invisible/OCR text spans
for the ENTIRE document being converted -- any page that has real
visible (born-digital) text gets silently skipped, because ocr=2
inverts the filter to "find hidden text and ignore [visible text]"
globally, not per-page. A single `Converter.convert()` call covering a
mixed document (some real-text pages, some scanned-then-OCR'd pages)
silently drops all the real-text pages' content with no error.

The fix: convert each page in its own `Converter.convert(start=i,
end=i+1, ocr=...)` call, with ocr=2 only for that specific page if OCR
was applied to it, then merge the resulting single-page DOCX files
together with docxcompose. This is slower than one bulk conversion
call would be, but it's the only way to get correct output for mixed
documents, which manual books realistically are (e.g. a few scanned
pages glued into an otherwise born-digital book).
"""
import logging
import os
import tempfile

from docx import Document
from docxcompose.composer import Composer
from pdf2docx import Converter

from pdf_detect import detect_needs_ocr
from pdf_ocr import ocr_pdf
from pdf_text_filter import flatten_repeated_header, strip_image_obscured_text

logger = logging.getLogger(__name__)


class ConversionError(Exception):
    pass


def convert_pdf_to_docx(pdf_bytes: bytes, ocr_mode: str = "auto") -> bytes:
    """
    ocr_mode:
      - "auto": detect per-page whether OCR is needed, run it only
        where needed. Matches a single "OCR" toggle left in its
        default/recommended state in the UI.
      - "force": run OCR on every page regardless of detection. The
        manual override for "dengan OCR" when the user explicitly
        wants it, e.g. if they suspect detection might be wrong for an
        edge-case page.
      - "off": never run OCR. The "tanpa OCR" choice -- scanned pages
        will contribute no extractable text to the output, matching
        what a bare pdf2docx conversion would do on its own.
    """
    if ocr_mode not in ("auto", "force", "off"):
        raise ConversionError(f"ocr_mode tidak dikenal: {ocr_mode}")

    # Strip decorative-image-covers-fallback-text spans (e.g. a styled
    # gradient/shadow logo header with a plain black copy of the same
    # text hidden underneath it for searchability) BEFORE OCR
    # detection runs. Doing this first means detect_needs_ocr never
    # sees the now-removed hidden text, so a page that's otherwise
    # scanned-image-only doesn't get miscounted as "has text" just
    # because of one decorative header span that was about to be
    # stripped anyway.
    try:
        pdf_bytes = strip_image_obscured_text(pdf_bytes)
    except Exception:
        # This is a best-effort visual-quality improvement, not a
        # correctness requirement -- if it fails for any reason (e.g.
        # a malformed PDF that confuses the image/text inspection),
        # fall through and convert the original bytes rather than
        # blocking the whole conversion over a cosmetic issue.
        logger.exception("strip_image_obscured_text failed, continuing with original PDF")

    # Collapses a repeated letterhead-style header (if one is
    # detected) into a single flat image per page, working around
    # pdf2docx's page-independent layout reconstruction producing
    # inconsistent results for the same header depending on how much
    # other content shares the page (see pdf_text_filter.py's
    # flatten_repeated_header docstring for the full investigation).
    # Runs AFTER strip_image_obscured_text so the hidden-text removal
    # doesn't get short-circuited by the header region already being a
    # single flattened image with no separate text span to find.
    try:
        pdf_bytes = flatten_repeated_header(pdf_bytes)
    except Exception:
        logger.exception("flatten_repeated_header failed, continuing without it")

    detection = detect_needs_ocr(pdf_bytes)
    total_pages = detection["total_pages"]
    if total_pages == 0:
        raise ConversionError("PDF tidak memiliki halaman.")

    if ocr_mode == "off":
        ocr_pages = set()
    elif ocr_mode == "force":
        ocr_pages = set(range(total_pages))
    else:  # auto
        ocr_pages = {i for i, p in enumerate(detection["pages"]) if not p["has_text"]}

    working_bytes = pdf_bytes
    if ocr_pages:
        working_bytes = ocr_pdf(pdf_bytes, page_indices=sorted(ocr_pages))

    with tempfile.TemporaryDirectory() as tmp_dir:
        src_path = os.path.join(tmp_dir, "source.pdf")
        with open(src_path, "wb") as f:
            f.write(working_bytes)

        page_docx_paths = []
        for i in range(total_pages):
            page_out = os.path.join(tmp_dir, f"page_{i}.docx")
            cv = Converter(src_path)
            try:
                cv.convert(page_out, start=i, end=i + 1, ocr=2 if i in ocr_pages else 0)
            except Exception as exc:
                raise ConversionError(
                    f"Gagal mengonversi halaman {i + 1}: {exc}"
                ) from exc
            finally:
                cv.close()
            page_docx_paths.append(page_out)

        if len(page_docx_paths) == 1:
            with open(page_docx_paths[0], "rb") as f:
                return f.read()

        master = Document(page_docx_paths[0])
        composer = Composer(master)
        for path in page_docx_paths[1:]:
            composer.append(Document(path))

        out_path = os.path.join(tmp_dir, "combined.docx")
        composer.save(out_path)

        with open(out_path, "rb") as f:
            return f.read()
