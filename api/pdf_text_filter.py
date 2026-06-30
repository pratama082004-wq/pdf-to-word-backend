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


# Numbered/lettered "callout" annotations (e.g. a small filled blue
# square with a white "1", "2", "a" inside, connected to a UI
# screenshot by an arrow) are a different visual pattern from the
# header case above, but they break for a related reason.
#
# Investigated against this project's test document (see project chat
# history): a single-page pdf2docx conversion renders these callouts
# correctly -- the white digit IS pdf2docx's own DrawingML shape with
# the correct anchor, same as a real Word floating shape. The bug only
# appears after docxcompose merges many single-page .docx files into
# one document. pdf2docx anchors these callouts with
# relativeFrom="page" (floating relative to the physical page, not to
# a paragraph) and behindDoc="1" (rendered behind text). docxcompose
# does NOT adjust page-relative anchors for the reflow that happens
# once many single-page documents are concatenated -- and reflow does
# happen, confirmed by isolating exactly which earlier page triggers
# it: merging page 9 alone with almost any other single page leaves it
# clean, but merging it together with the table-of-contents page
# (which contains live TOC/PAGEREF field codes that get recalculated
# on render) reliably breaks it. The page-relative callout shape stays
# pinned to a physical page number that no longer holds the content it
# was originally drawn for, so it ends up misplaced, overlapped by
# whatever now occupies that position, or rendered with the digit
# missing/garbled.
#
# This is a docxcompose/pdf2docx interaction, not a bug in this
# project's own code -- confirmed by testing each stage in isolation
# (strip_image_obscured_text and flatten_repeated_header both leave
# these callouts completely untouched; a single-page conversion is
# clean; only the multi-page merge reproduces the corruption).
#
# Rather than trying to patch docxcompose's merge behavior (out of
# this project's control, and brittle to depend on), this takes the
# same approach that already worked for the header: remove the
# fragile-to-reconstruct vector+text construct from the PDF entirely
# and replace it with one flat raster image per callout, anchored
# inline at the same position. A flattened image has no internal
# anchor semantics for pdf2docx to get wrong, and nothing for
# docxcompose's merge to misplace relative to page reflow.
CALLOUT_BOX_MIN_SIZE = 15
CALLOUT_BOX_MAX_SIZE = 32
CALLOUT_BOX_MIN_HEIGHT = 18
CALLOUT_BOX_MAX_HEIGHT = 28
CALLOUT_CONNECTOR_SEARCH_RADIUS = 15  # pt; matches the small gap observed
# between a callout box's edge and its connector arrow's start point


def _find_callout_regions(page: "fitz.Page") -> list:
    """
    Detects numbered/lettered callout boxes on a page (small filled
    rect, roughly 15-32pt wide by 18-28pt tall, with a short white-text
    label mostly inside it) and returns the bounding region for each
    one -- the box itself plus any connector line/arrow whose start or
    end point lands near the box, so the flattened replacement image
    covers the whole visual unit instead of just the box.

    Deliberately conservative on the box-detection side (confirmed
    against this project's test document spanning callouts numbered up
    to "11", plus lettered ones "a" through "f"): the size window and
    the white-text-coverage check together are why this doesn't fire
    on ordinary small UI elements like table sort-arrow icons, which
    are a similar size but don't have a >30%-covering white text span
    inside them.
    """
    drawings = page.get_drawings()

    candidate_boxes = []
    for d in drawings:
        if d.get("type") != "f" or not d.get("fill"):
            continue
        rect = fitz.Rect(d["rect"])
        if (
            CALLOUT_BOX_MIN_SIZE <= rect.width <= CALLOUT_BOX_MAX_SIZE
            and CALLOUT_BOX_MIN_HEIGHT <= rect.height <= CALLOUT_BOX_MAX_HEIGHT
        ):
            candidate_boxes.append(rect)

    if not candidate_boxes:
        return []

    # Confirm each candidate actually has a short white-text label
    # mostly inside it -- this is what distinguishes an intentional
    # callout badge from any other small UI rectangle that happens to
    # fall in the same size range.
    text_dict = page.get_text("dict")
    confirmed_boxes = []
    for box in candidate_boxes:
        has_label = False
        for block in text_dict.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    span_rect = fitz.Rect(span["bbox"])
                    if span_rect.is_empty or span["color"] != 0xFFFFFF:
                        continue
                    overlap = span_rect & box
                    if overlap.is_empty:
                        continue
                    if (overlap.width * overlap.height) / (
                        span_rect.width * span_rect.height
                    ) > 0.3:
                        has_label = True
        if has_label:
            confirmed_boxes.append(box)

    # For each confirmed box, absorb the small arrowhead/connector
    # geometry immediately adjacent to it -- specifically, only the
    # individual line/curve SEGMENTS whose endpoint lands within
    # CALLOUT_CONNECTOR_SEARCH_RADIUS of the box, unioned using just
    # those segments' own points, not the full bounding rect of
    # whichever drawing object they belong to.
    #
    # This distinction matters: pdf2docx (and the source PDFs it's
    # fed) often draws one long connector line as a single multi-
    # segment path, e.g. "box -> short diagonal -> long horizontal run
    # across most of the page -> arrowhead near a distant table
    # column." That whole path shares one drawing dict with one
    # bounding rect spanning its full length. Unioning the box with
    # that drawing's FULL rect (rather than just the nearby segment)
    # ends up absorbing everything the line passes near on its way
    # to the box -- confirmed against this project's test document,
    # where a callout's arrow ran the width of a data table and the
    # naive full-rect union swallowed the entire table header row
    # into the flattened replacement image, blanking it out. Using
    # only the matched segment's own two endpoints keeps the
    # absorbed region tight around the box and its immediate
    # arrowhead, regardless of how far the rest of the connector line
    # travels across the page.
    regions = []
    for box in confirmed_boxes:
        union = fitz.Rect(box)
        search_zone = fitz.Rect(
            box.x0 - CALLOUT_CONNECTOR_SEARCH_RADIUS,
            box.y0 - CALLOUT_CONNECTOR_SEARCH_RADIUS,
            box.x1 + CALLOUT_CONNECTOR_SEARCH_RADIUS,
            box.y1 + CALLOUT_CONNECTOR_SEARCH_RADIUS,
        )
        for d in drawings:
            for item in d.get("items", []):
                if item[0] == "l":
                    points = [item[1], item[2]]
                elif item[0] == "c":
                    points = [item[1], item[2], item[3], item[4]]
                else:
                    continue
                if any(search_zone.contains(p) for p in points):
                    for p in points:
                        union |= fitz.Rect(p.x, p.y, p.x, p.y)
        regions.append(union)
    return regions


def flatten_callout_annotations(pdf_bytes: bytes) -> bytes:
    """
    Replaces each detected callout-box-plus-arrow region (see
    `_find_callout_regions`) with a single flattened raster image in
    the same position, page by page. Pages with no detected callouts
    are left completely untouched (no redaction, no rewrite of that
    page's content).
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        render_zoom = 4  # crisp enough for a small inline badge
        render_mat = fitz.Matrix(render_zoom, render_zoom)

        for page in doc:
            regions = _find_callout_regions(page)
            if not regions:
                continue

            # Render each region BEFORE redacting anything on the
            # page -- apply_redactions mutates the page's content, so
            # rendering must happen first or later regions would
            # capture an already-modified page.
            region_pngs = [
                (region, page.get_pixmap(matrix=render_mat, clip=region).tobytes("png"))
                for region in regions
            ]

            for region in regions:
                page.add_redact_annot(region)
            page.apply_redactions(images=0, graphics=0, text=0)

            for region, png_bytes in region_pngs:
                page.insert_image(region, stream=png_bytes)

        return doc.write(garbage=4, deflate=True, clean=True)
    finally:
        doc.close()


# UI mockup screenshots in this project's source PDFs (Figma/design-
# tool exports, not actual OS screenshots) are consistently built from
# TWO overlapping raster images at ~100% mutual overlap -- a drop-
# shadow layer plus the main mockup content -- with vector rectangles
# (input field borders, button fills, focus-state outlines) drawn on
# top of them natively, rather than baked into the raster image
# itself. Confirmed across every mockup page in this project's test
# document: the two-image, ~100%-overlap pattern is completely
# consistent, including on pages that convert cleanly.
#
# That last point is what matters here: SOME of these per-mockup
# vector borders fail to survive pdf2docx's conversion (e.g. a
# dropdown's focus-state border, or a button's fill, rendered as a
# blank box with no visible outline in the resulting DOCX), while
# others on the very same page convert perfectly fine. Investigated
# at length against this project's test document (see project chat
# history) without finding a reliable geometric predictor -- two
# dropdown borders with structurally identical PDF representation
# (same single `re` rect operator, same stroke width, similar nearby
# shape density) diverge in output, one rendering correctly and the
# other not. This points to an inconsistency inside pdf2docx's own
# element-grouping/layout logic for this content shape, not anything
# about these specific PDFs that a smarter detection rule could route
# around.
#
# Given no reliable predictor exists, this takes the same fallback
# already used twice successfully elsewhere in this pipeline: instead
# of trying to guess which borders will fail, remove the need for
# pdf2docx to reconstruct ANY of them. Every such mockup region -- the
# union of the two overlapping images plus every vector shape drawn
# inside that union -- gets flattened into one raster image. A page
# whose mockup was already converting perfectly loses nothing
# visually (it becomes a pixel-identical flat image instead of a
# reconstructed one); a page that was silently dropping borders is
# fixed the same way. This is deliberately unconditional rather than
# trying to detect "is this specific mockup at risk" -- that's the
# same kind of prediction problem just shown not to have a reliable
# answer.
MOCKUP_IMAGE_MIN_SIZE = 100  # pt; excludes small icons/logos, keeps
# full-size UI mockup screenshots
MOCKUP_IMAGE_OVERLAP_RATIO = 0.5  # fraction of the smaller image's
# area that must be covered for two images to count as the same
# shadow+content mockup pair


def _find_mockup_regions(page: "fitz.Page") -> list:
    """
    Detects pairs of large raster images that overlap heavily (the
    shadow-layer + content-layer pattern described above) and returns
    one region per pair: the union of both images' bounding boxes,
    expanded to also cover any vector shape (filled or stroked) that
    falls inside that union, so borders/fills drawn on top of the
    mockup are captured in the same flattened replacement.

    A page can have more than one such pair (e.g. two separate mockup
    screenshots stacked on one page) -- each pair is detected and
    flattened independently. A page with only a single large image (a
    true single-layer screenshot, not this shadow+content pattern) or
    with no large images at all returns an empty list and is left
    completely untouched by `flatten_mockup_dialogs`.
    """
    images = [
        fitz.Rect(info["bbox"])
        for info in page.get_image_info()
        if info["bbox"][2] - info["bbox"][0] > MOCKUP_IMAGE_MIN_SIZE
        and info["bbox"][3] - info["bbox"][1] > MOCKUP_IMAGE_MIN_SIZE
    ]
    if len(images) < 2:
        return []

    # Pair up images with heavy mutual overlap. Each image participates
    # in at most one pair (matches the observed pattern of exactly one
    # shadow+content pair per mockup; a page with two separate mockups
    # has two non-overlapping pairs, not one image shared by both).
    used = [False] * len(images)
    unions = []
    for i in range(len(images)):
        if used[i]:
            continue
        for j in range(i + 1, len(images)):
            if used[j]:
                continue
            a, b = images[i], images[j]
            intersection = a & b
            if intersection.is_empty:
                continue
            smaller_area = min(a.width * a.height, b.width * b.height)
            if smaller_area <= 0:
                continue
            overlap_ratio = (intersection.width * intersection.height) / smaller_area
            if overlap_ratio >= MOCKUP_IMAGE_OVERLAP_RATIO:
                used[i] = used[j] = True
                unions.append(a | b)
                break

    if not unions:
        return []

    drawings = page.get_drawings()
    callout_regions = _find_callout_regions(page)

    # Expand each union to also cover: (1) vector shapes drawn inside
    # it (input borders, button fills, focus outlines), so the
    # flattened image includes everything visually layered on top of
    # the mockup, not just the two raster layers themselves; and (2)
    # any callout badge whose vertical span overlaps this mockup's
    # vertical span, even though the callout sits horizontally outside
    # the mockup (to the left or right, pointing in via an arrow).
    #
    # That second part matters for a reason distinct from (1): without
    # it, confirmed against this project's test document, a wide
    # mockup image with several callouts stacked down its left and
    # right edges -- each at a different height within the mockup's
    # vertical span -- gets corrupted by pdf2docx in a way worse than
    # the missing-border problem this function otherwise fixes. The
    # single flattened mockup image, instead of being placed once as
    # a floating shape, gets fragmented and re-inserted multiple times
    # inline, once per callout "row" it happens to vertically align
    # with. Folding those callouts into the SAME flattened region as
    # the mockup -- rather than leaving them as separate small images
    # for flatten_callout_annotations to handle independently --
    # removes the small-image-next-to-wide-image pattern entirely.
    # This only triggers for callouts whose vertical span genuinely
    # overlaps a mockup's; the much more common case of a callout near
    # a mockup but outside its vertical span (e.g. a callout above a
    # narrower mockup, like on every other page checked in this
    # project's test document) is unaffected and still flattened
    # independently and tightly by flatten_callout_annotations.
    regions = []
    for union in unions:
        expanded = fitz.Rect(union)
        for d in drawings:
            if d.get("type") not in ("f", "s"):
                continue
            rect = fitz.Rect(d["rect"])
            if rect.intersects(union):
                expanded |= rect
        for callout in callout_regions:
            vertical_overlap = not (callout.y1 <= union.y0 or callout.y0 >= union.y1)
            if vertical_overlap:
                expanded |= callout
        regions.append(expanded)
    return regions


def flatten_mockup_dialogs(pdf_bytes: bytes) -> bytes:
    """
    Replaces each detected mockup region (see `_find_mockup_regions`)
    with a single flattened raster image in the same position, page by
    page. Pages with no detected mockup pairs are left completely
    untouched.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        render_zoom = 3  # mockups are larger than callout badges;
        # 3x keeps file size reasonable while staying sharp for
        # on-screen UI text at typical screenshot resolution
        render_mat = fitz.Matrix(render_zoom, render_zoom)

        for page in doc:
            regions = _find_mockup_regions(page)
            if not regions:
                continue

            region_pngs = [
                (region, page.get_pixmap(matrix=render_mat, clip=region).tobytes("png"))
                for region in regions
            ]

            for region in regions:
                page.add_redact_annot(region)
            # images=2 is mandatory here, unlike the images=0 used in
            # strip_image_obscured_text and flatten_callout_annotations
            # elsewhere in this file: those two redact regions that
            # contain no raster image at all (a hidden text span, a
            # small vector callout box), so images=0 vs 2 made no
            # difference there. A mockup region DOES contain the two
            # original shadow+content raster images this function is
            # meant to replace -- images=0 ("don't touch images")
            # leaves both of them in place, underneath the newly
            # inserted flattened image, bloating the file and (worse)
            # leaving the originals available for pdf2docx to parse
            # independently of the replacement on top of them. This
            # was caught by checking the PDF's own image list after
            # this function ran: the two original images were still
            # present, plus the new one, when only the new one should
            # exist.
            page.apply_redactions(images=2, graphics=2, text=0)

            for region, png_bytes in region_pngs:
                page.insert_image(region, stream=png_bytes)

        return doc.write(garbage=4, deflate=True, clean=True)
    finally:
        doc.close()
