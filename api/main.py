"""
FastAPI backend for the PDF to Word (with optional OCR) feature.

Deployed as its OWN standalone Vercel project, separate from the
Next.js frontend (winpdf). This split happened because Vercel's
"Services" feature (which would have let both live in one project)
turned out to require a permission/plan that wasn't available on the
account this was deployed under -- the "Services" framework preset
simply didn't appear in that project's settings. Two separate Vercel
projects, talking over a plain HTTPS API call, is the fallback that
works on every plan without depending on an experimental feature.

Practical consequence of the split: frontend and backend are on
different *.vercel.app domains, so this is a genuine cross-origin
setup -- CORS middleware below is required, not optional, or the
browser blocks every request before it reaches this API at all.

Entrypoint convention: Vercel's Python runtime looks for a top-level
ASGI/WSGI app named `app` in app.py/main.py/etc. This file is named
main.py to match that. Routes are defined at their real paths
(/health, /pdf-to-word) since there's no routePrefix layer rewriting
them anymore -- whatever path is requested is the path FastAPI sees.
"""
import logging
import os

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

from pdf_to_word import ConversionError, convert_pdf_to_docx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

# Restricting allow_origins to the actual frontend domain (rather than
# "*") matters here specifically because this endpoint accepts file
# uploads and returns generated documents -- an open CORS policy would
# let any third-party site drive this conversion API from a visitor's
# browser using that visitor's bandwidth and this project's compute
# budget. FRONTEND_ORIGIN is an env var so the allowed origin can be
# updated (e.g. after a custom domain is attached to winpdf) without a
# code change -- set it in this project's Vercel dashboard under
# Settings > Environment Variables.
FRONTEND_ORIGIN = os.environ.get("FRONTEND_ORIGIN", "http://localhost:3000")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_ORIGIN],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB -- generous for a manual book PDF


@app.get("/health")
def health():
    """Cheap liveness check, also useful for confirming the Python
    service is reachable at all when wiring up the frontend during
    development."""
    return {"status": "ok"}


@app.post("/pdf-to-word")
async def pdf_to_word(file: UploadFile = File(...), ocr_mode: str = Form("auto")):
    if file.content_type not in ("application/pdf",) and not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="File harus berupa PDF.")

    pdf_bytes = await file.read()
    if not pdf_bytes:
        raise HTTPException(status_code=400, detail="File PDF kosong atau tidak terbaca.")
    if len(pdf_bytes) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"File terlalu besar (maksimal {MAX_UPLOAD_BYTES // (1024 * 1024)} MB).",
        )

    if ocr_mode not in ("auto", "force", "off"):
        raise HTTPException(status_code=400, detail="ocr_mode harus auto, force, atau off.")

    try:
        docx_bytes = convert_pdf_to_docx(pdf_bytes, ocr_mode=ocr_mode)
    except ConversionError as exc:
        logger.warning("Conversion failed: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Unexpected error during PDF to Word conversion")
        raise HTTPException(
            status_code=500, detail=f"Terjadi kesalahan tak terduga: {exc}"
        ) from exc

    out_name = file.filename.rsplit(".", 1)[0] + ".docx" if file.filename else "converted.docx"

    return Response(
        content=docx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{out_name}"'},
    )
