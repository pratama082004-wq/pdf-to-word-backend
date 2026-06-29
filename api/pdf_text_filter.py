"""
Removes text spans that are visually hidden behind an image on the
same page -- a common design pattern in professionally-designed PDFs
(headers/logos exported from Canva, PowerPoint, etc.) where a styled
image (gradient text, drop-shadow effect, etc.) is placed on top of a
plain-color text span carrying the same words. The plain text exists
for searchability/accessibility, not for visual display -- it is never
meant to be seen, and PDF viewers never show it because the image
covers it.

pdf2docx doesn't model element z-order/layering: it extracts text
based on what's present on the page, with no concept of "this text is
covered by an image and was never meant to be visible." Real-world
case that motivated this (see project chat history): a header reading
"PT. Enerwise Solusi Indonesia" rendered as a styled gradient+shadow
PNG in the source PDF, with a plain black 20pt text span in the exact
same position underneath for search purposes. pdf2docx ignored the
image and surfaced the plain text, producing a giant black headline
overlapping the table header in the resulting DOCX -- something no
end user would expect, since they never saw that text in the PDF at
all.

Detection heuristic: for each text span, check whether (the union of)
image bounding boxes on the same page cover more than a threshold
fraction of the span's own bounding box. If so, drop the span entirely
before handing the page to pdf2docx. This is a heuristic, not a
guarantee -- a text span that's only PARTIALLY covered by a small
decorative image (e.g. a bullet icon next to a line of body text)
won't hit the threshold and is correctly left alone.
"""
import hashlib

import fitz

# 50% was chosen empirically against the one confirmed real-world case
# (a header where two overlapping images covered roughly 75% and 27%
# of the underlying text span respectively, clearly indicating "this
# text is meant to be obscured," not "an icon happens to slightly
# overlap this line of running text"). A lower threshold risks
# deleting legitimate text that merely sits near a small inline image;
# a higher threshold risks missing decorative text that's mostly, but
# not fully, covered.
COVERAGE_THRESHOLD = 0.5


def strip_image_obscured_text(pdf_bytes: bytes) -> bytes:
    """
    Returns a new PDF (as bytes) with any text span that's mostly
    covered by an image on the same page redacted (replaced with
    nothing -- not just visually hidden, actually removed from the
    page's content so pdf2docx never sees it).
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        for page in doc:
            image_boxes = [fitz.Rect(info["bbox"]) for info in page.get_image_info()]
            if not image_boxes:
                continue

            redact_boxes = []
            text_dict = page.get_text("dict")
            for block in text_dict.get("blocks", []):
                if block.get("type") != 0:  # not a text block
                    continue
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        span_rect = fitz.Rect(span["bbox"])
                        if span_rect.is_empty:
                            continue
                        covered_area = _union_overlap_area(span_rect, image_boxes)
                        span_area = span_rect.width * span_rect.height
                        if span_area > 0 and (covered_area / span_area) >= COVERAGE_THRESHOLD:
                            redact_boxes.append(span_rect)

            for rect in redact_boxes:
                # add_redact_annot + apply_redactions removes the
                # underlying text content (not just draws over it),
                # which matters here -- pdf2docx reads the content
                # stream directly, so a visual-only cover-up wouldn't
                # stop it from extracting the text underneath.
                #
                # images=0 is mandatory here: the default (images=2,
                # "blank out overlapping image parts") rasterizes and
                # regenerates the covering image for every redaction
                # rectangle, which is the OPPOSITE of what's wanted --
                # the whole point is that the decorative image stays
                # exactly as it was, only the redundant hidden text
                # underneath it gets removed. Leaving this at the
                # default blew up a 3.2MB source PDF to 55MB in
                # testing (one redaction per small text span, each
                # forcing a re-encode of the overlapping image region).
                page.add_redact_annot(rect)
            if redact_boxes:
                page.apply_redactions(images=0, graphics=0, text=0)

        # garbage=4 (full garbage collection, renumbers objects and
        # drops unreferenced ones), deflate=True (compress streams),
        # and clean=True together matter here -- without them, a plain
        # doc.write() after add_redact_annot/apply_redactions calls
        # produced output roughly 2x the size of the source PDF in
        # testing, even though the actual image/content data was
        # unchanged. The redaction process leaves behind bookkeeping
        # that a naive write() doesn't clean up; these flags do.
        return doc.write(garbage=4, deflate=True, clean=True)
    finally:
        doc.close()


def _union_overlap_area(target: "fitz.Rect", others: list) -> float:
    """
    Approximates the area of `target` covered by the union of `others`
    by summing individual overlaps. This double-counts area covered by
    more than one box in `others`, which means the result is an
    overestimate when boxes overlap each other -- acceptable here
    since overestimating coverage only makes this function MORE likely
    to flag a span as covered, and the real-world motivating case
    involves exactly that (two overlapping decorative images stacked
    on the same text). A pixel-exact union would need rasterization or
    interval-splitting; not worth the complexity for a heuristic.
    """
    total = 0.0
    for box in others:
        overlap = target & box
        if not overlap.is_empty:
            total += overlap.width * overlap.height
    return total


# pdf2docx has no header/footer detection at all -- its own source
# code marks this explicitly: pdf2docx/page/Pages.py's
# `_parse_document` (the function meant to detect document-level
# header/footer structure) is a literal `# TODO: return '', ''`,
# never implemented. Every page is parsed as a fully independent
# layout problem. For a repeated decorative header built from multiple
# overlapping images plus the search-text span stripped above, this
# means pdf2docx's image/text clustering heuristics can group things
# differently from one page to the next depending on how much OTHER
# content (charts, screenshots, numbered-step diagrams) shares the
# page -- confirmed empirically against this project's test document:
# pages with only 2 images came out with a clean header, pages with
# 4-6 images came out with the header text visibly split into
# individual oddly-spaced characters or overlapping a nearby image.
#
# strip_image_obscured_text() alone doesn't fix this because the
# header's visual appearance comes from two separate overlapping
# images (a gradient-fill title and a drop-shadow layer), and
# pdf2docx's inconsistency is in how it reconstructs THOSE images'
# relative positioning, not just in the now-removed hidden text.
#
# The fix: collapse the whole header region into a SINGLE flat image
# before pdf2docx ever sees it. One image has no internal
# layering/positioning ambiguity to get wrong.
def flatten_repeated_header(pdf_bytes: bytes, header_rect: "fitz.Rect" = None) -> bytes:
    """
    Detects a header region that renders pixel-identical across every
    page (a strong signal it's a repeated template element, not
    per-page content), and replaces whatever PDF objects occupy that
    region -- images, text, vector graphics, all of it -- with a
    single flattened raster image in the same position. This sidesteps
    pdf2docx's per-page layout reconstruction inconsistency entirely
    for that region, at the cost of the header no longer being
    selectable/searchable text in the resulting DOCX (it was never
    meant to be visible text anyway -- see strip_image_obscured_text's
    docstring for why a hidden duplicate existed in the first place).

    If `header_rect` is not given, defaults to a generous top-of-page
    band (0 to 90pt from the top, full page width) -- wide enough to
    catch a typical letterhead-style header without needing per-
    document tuning, at the cost of also flattening any genuine
    page content that happens to start very close to the top margin
    on pages that don't have a decorative header at all (harmless: if
    that region has no images, there's nothing to flatten, and if it's
    plain text well within a normal top margin, flattening still
    preserves it visually -- it just becomes part of the new image
    rather than extractable text for that band specifically).

    Returns the original bytes unchanged if fewer than 3 pages render
    identically in the header region (not enough evidence of a
    genuine repeated template to justify altering every page).
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        if len(doc) < 3:
            return pdf_bytes

        if header_rect is None:
            page0 = doc[0]
            header_rect = fitz.Rect(0, 0, page0.rect.width, 90)

        # Render the candidate header region from every page at a
        # moderate, comparison-friendly resolution and hash each
        # render. A render-level (not PDF-object-level) comparison is
        # used deliberately: two pages could express "the same visual
        # header" via different underlying PDF object structures
        # (e.g. a slightly different image compression pass), and a
        # byte-identical PDF object comparison would miss that, while
        # a render hash correctly treats them as the same header.
        compare_zoom = 2
        compare_mat = fitz.Matrix(compare_zoom, compare_zoom)
        render_hashes: dict[str, int] = {}
        for page in doc:
            pix = page.get_pixmap(matrix=compare_mat, clip=header_rect)
            digest = hashlib.md5(pix.tobytes()).hexdigest()
            render_hashes[digest] = render_hashes.get(digest, 0) + 1

        most_common_hash, count = max(render_hashes.items(), key=lambda kv: kv[1])
        if count < 3:
            return pdf_bytes

        # Render the clean reference image once, at a sharper
        # resolution than the comparison pass (comparison only needed
        # enough fidelity to hash-match; the actual replacement image
        # benefits from being crisper since it ends up in the final
        # document).
        reference_page = None
        render_zoom = 4
        render_mat = fitz.Matrix(render_zoom, render_zoom)
        for page in doc:
            pix = page.get_pixmap(matrix=compare_mat, clip=header_rect)
            if hashlib.md5(pix.tobytes()).hexdigest() == most_common_hash:
                reference_page = page
                break
        header_png = reference_page.get_pixmap(matrix=render_mat, clip=header_rect).tobytes("png")

        for page in doc:
            # Remove every object type in the header band -- images,
            # vector graphics, and text alike -- since the replacement
            # image will visually cover all of it. Leaving old objects
            # in place underneath would bloat the file for no benefit
            # (they're entirely hidden by the new image on top).
            page.add_redact_annot(header_rect)
            page.apply_redactions(images=2, graphics=2, text=0)
            page.insert_image(header_rect, stream=header_png)

        return doc.write(garbage=4, deflate=True, clean=True)
    finally:
        doc.close()
