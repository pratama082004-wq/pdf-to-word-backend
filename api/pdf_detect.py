"""
Deteksi apakah sebuah PDF sudah punya teks asli (born-digital) atau
murni gambar/hasil scan, supaya pipeline tahu apakah OCR diperlukan.

Pendekatan: untuk setiap halaman, bandingkan luas area yang ditutupi
teks asli terhadap luas halaman. PDF born-digital biasanya punya teks
yang tersebar di sebagian besar area konten; PDF hasil scan biasanya
0% teks (gambar polos) atau, dalam kasus PDF yang sudah pernah di-OCR
sebelumnya oleh tool lain, punya teks tapi tersembunyi di balik gambar
full-page -- kasus itu di luar scope deteksi ini dan akan tetap
diperlakukan sebagai "perlu OCR" karena hasilnya sama-sama aman
(OCR ulang pada PDF yang sudah punya teks asli hanya menambah waktu
proses, tidak merusak akurasi).
"""
import fitz


# Threshold rendah secara sengaja: bahkan satu halaman dengan sedikit
# teks asli (misal sampul dengan judul saja) seharusnya tetap
# diklasifikasikan sebagai "ada teks", karena tujuan deteksi ini bukan
# "apakah SELURUH halaman penuh teks" tapi "apakah perlu OCR sama
# sekali". Salah klasifikasi ke arah "perlu OCR" padahal sudah ada teks
# hanya costnya tambahan waktu proses; klasifikasi sebaliknya
# (melewatkan OCR padahal perlu) costnya dokumen jadi tidak punya teks
# sama sekali -- lebih baik over-trigger OCR.
MIN_CHARS_PER_PAGE_TO_SKIP_OCR = 20


def detect_needs_ocr(pdf_bytes: bytes) -> dict:
    """
    Returns a dict describing whether OCR is needed, plus a per-page
    breakdown so the API layer can report something meaningful back to
    the user (e.g. "3 of 12 pages appear to be scanned").
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        page_results = []
        for page in doc:
            text = page.get_text().strip()
            page_results.append({
                "char_count": len(text),
                "has_text": len(text) >= MIN_CHARS_PER_PAGE_TO_SKIP_OCR,
            })

        pages_without_text = sum(1 for p in page_results if not p["has_text"])
        total_pages = len(page_results)

        return {
            "total_pages": total_pages,
            "pages_without_text": pages_without_text,
            # Recommend OCR if ANY page lacks text -- a single scanned
            # page in an otherwise born-digital manual is still a page
            # the user wants readable in the output DOCX.
            "needs_ocr": pages_without_text > 0,
            "pages": page_results,
        }
    finally:
        doc.close()
