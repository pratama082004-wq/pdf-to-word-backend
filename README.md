# PDF to Word Backend

Backend Python (FastAPI) untuk fitur **PDF to Word + OCR** di Lock Watermark / winpdf.
Ini project Vercel yang **berdiri sendiri**, terpisah dari project Next.js (winpdf).

## Kenapa terpisah dari project Next.js?

Awalnya direncanakan jadi satu project lewat fitur Vercel "Services" (Next.js +
Python dalam 1 project). Tapi opsi "Services" di Framework Preset tidak muncul
di project Vercel yang dipakai — kemungkinan butuh permission/plan tertentu yang
belum aktif di akun ini. Daripada terus bergantung pada fitur experimental yang
ternyata tidak bisa diakses, backend ini dijadikan project Vercel terpisah yang
dipanggil lewat HTTPS biasa dari frontend. Ini pendekatan yang lebih umum dan
pasti didukung di semua plan Vercel.

## Cara Deploy

1. Push folder ini (`pdf-to-word-backend/`) sebagai repo GitHub terpisah
   (atau subfolder repo, dengan root directory project Vercel diarahkan ke sini).
2. Di Vercel, klik **Add New Project**, hubungkan ke repo ini.
3. Vercel akan otomatis mendeteksi ini sebagai project Python (dari `requirements.txt`
   dan `main.py` yang berisi `app = FastAPI()`).
4. **Set environment variable** di Project Settings → Environment Variables:
   - `FRONTEND_ORIGIN` = URL frontend winpdf kamu, contoh: `https://winpdf.vercel.app`
     (tanpa trailing slash). Ini WAJIB diset dengan benar, kalau tidak, request
     dari frontend akan diblokir browser karena CORS.
5. Deploy. Setelah selesai, catat URL deployment-nya, contoh: `https://pdf-to-word-backend.vercel.app`

## Menghubungkan ke Frontend (winpdf)

Di project Vercel **winpdf** (Next.js), buka Settings → Environment Variables, tambahkan:

- `NEXT_PUBLIC_BACKEND_URL` = URL backend dari langkah deploy di atas,
  contoh: `https://pdf-to-word-backend.vercel.app` (tanpa trailing slash)

Setelah env var ini diset, **redeploy project winpdf** (env var baru tidak otomatis
berlaku ke deployment yang sudah ada, perlu trigger build baru — bisa lewat
"Redeploy" di dashboard, atau push commit baru).

## Endpoint

- `GET /health` — cek server hidup
- `POST /pdf-to-word` — body `multipart/form-data` dengan field:
  - `file`: file PDF
  - `ocr_mode`: `auto` (default) | `force` | `off`

## Test Lokal

```bash
pip install -r requirements.txt --break-system-packages
pip install uvicorn --break-system-packages
uvicorn main:app --reload --port 8000
```

Lalu jalankan project Next.js seperti biasa (`npm run dev`) — secara default,
frontend akan mengarah ke `http://localhost:8000` saat development lokal
(lihat komentar `BACKEND_URL` di `app/pdf-to-word/page.tsx` pada project Next.js).

## Catatan Ukuran & Limit

- Total dependency (`pdf2docx`, `PyMuPDF`, `rapidocr`, `docxcompose`, dll) sekitar
  ~301MB — di bawah limit 500MB Vercel Python function, tapi marginnya tidak besar.
  Kalau menambah dependency baru, cek ulang total ukurannya.
- `maxDuration` diset 120 detik di `vercel.json` — dokumen dengan banyak halaman
  hasil scan (perlu OCR) bisa makan waktu lama; sesuaikan kalau perlu lebih,
  tapi perhatikan limit plan Vercel kamu (Hobby plan punya limit durasi function
  yang lebih rendah dari Pro).
- Model OCR (RapidOCR) di-download otomatis saat pertama kali dipanggil (disimpan
  di `/tmp`, yang writable di Vercel tapi tidak persisten antar cold start) —
  artinya request pertama setelah idle lama akan sedikit lebih lambat.
