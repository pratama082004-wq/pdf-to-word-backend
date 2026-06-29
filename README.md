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

## Struktur Folder

Semua kode Python ada di dalam folder `api/` — ini bukan pilihan gaya, tapi
syarat wajib dari Vercel: <cite>untuk semua runtime resmi yang didukung,
satu-satunya syarat adalah membuat direktori `api` di root project, lalu
meletakkan Vercel function di dalamnya</cite>. Versi awal project ini sempat
meletakkan `main.py` di root (bukan di `api/`), yang menyebabkan build gagal
dengan error: `The pattern "main.py" defined in functions doesn't match any
Serverless Functions inside the api directory`. Kalau menambah file Python
baru, taruh di dalam `api/` juga.

```
pdf-to-word-backend/
├── api/
│   ├── main.py          ← entrypoint, harus berisi variabel `app`
│   ├── pdf_detect.py
│   ├── pdf_ocr.py
│   └── pdf_to_word.py
├── requirements.txt
├── vercel.json
└── README.md
```

## PENTING: maxDuration diatur lewat Dashboard, BUKAN vercel.json

`vercel.json` di project ini sengaja dikosongkan (`{}`). Sempat dicoba isi
`functions.api/main.py.maxDuration` dan variasinya (`api/*.py`, `api/**`),
SEMUANYA gagal build dengan error: `The pattern "..." defined in functions
doesn't match any Serverless Functions inside the api directory` — padahal
struktur foldernya sudah benar (dikonfirmasi langsung di GitHub). Begitu
`vercel.json` dikosongkan total, build langsung berhasil.

Ini ternyata bukan kesalahan konfigurasi di project ini — ada laporan
pengguna lain dengan masalah identik di forum komunitas Vercel, dan
dokumentasi resmi Vercel sendiri menyebutkan cara yang benar untuk set
`maxDuration` tanpa lewat `vercel.json` sama sekali:

1. Buka dashboard Vercel → pilih project ini
2. Settings → **Functions** (di sidebar kiri)
3. Cari bagian **Function Max Duration**
4. Ubah **Default Max Duration** ke nilai yang dibutuhkan (misal 120 detik
   untuk dokumen dengan banyak halaman OCR)
5. Save

Catatan plan: Hobby plan defaultnya 10 detik, Pro/Enterprise 15 detik — kedua
itu kemungkinan TIDAK CUKUP untuk dokumen multi-halaman dengan OCR. Naikkan
ke setidaknya 60-120 detik lewat dashboard seperti di atas. Kalau perlu lebih
dari 800 detik, baru itu butuh konfigurasi per-function eksplisit (bisa balik
butuh `vercel.json`, tapi itu kasus ekstrem yang seharusnya tidak terjadi
untuk ukuran dokumen wajar).

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
cd api && uvicorn main:app --reload --port 8000
```

Lalu jalankan project Next.js seperti biasa (`npm run dev`) — secara default,
frontend akan mengarah ke `http://localhost:8000` saat development lokal
(lihat komentar `BACKEND_URL` di `app/pdf-to-word/page.tsx` pada project Next.js).

## Catatan Ukuran & Limit

- **Bundle size sempat melebihi limit 500MB Vercel** (526.61MB) karena `rapidocr`
  meminta paket `opencv_python` (versi penuh, dengan GUI bindings) sementara
  `pdf2docx`/dependency lain menarik `opencv-python-headless` juga — pip
  menginstall KEDUANYA karena dianggap dua paket berbeda, padahal isinya
  konflik modul `cv2` yang sama. Fix-nya ada di `pyproject.toml`, bagian
  `[tool.uv] override-dependencies` — memberi tahu resolver `uv` (yang dipakai
  Vercel untuk build Python) untuk menganggap `opencv_python` "terpenuhi"
  tanpa pernah menginstallnya, sehingga hanya `opencv-python-headless` yang
  benar-benar terpasang. Ini mengurangi total dependency dari ~587MB ke ~473MB
  (diukur di sandbox testing; rasio efisiensi Vercel yang sebenarnya
  cenderung sedikit lebih baik dari sandbox ini berdasarkan observasi
  sebelumnya, tapi jangan asumsikan otomatis lebih kecil — selalu cek log
  build aktual setelah deploy).
- **PENTING kalau menambah/upgrade dependency baru**: jangan pin
  `opencv-python-headless` ke versi lama untuk hemat ukuran tanpa testing
  penuh — versi `opencv-python-headless==4.8.0.76` misalnya dikompilasi untuk
  NumPy 1.x dan akan gagal total (`ImportError: numpy.core.multiarray failed
  to import`) begitu `numpy>=2.0` ikut terinstall. Selalu jalankan
  `convert_pdf_to_docx()` end-to-end (bukan cuma cek `import` berhasil)
  setelah mengubah versi dependency manapun di sini.
- `excludeFiles` di `vercel.json` membuang folder `tests/`, file `.pyi`
  (type stubs), `*.dist-info` (metadata pip), dan `examples/` dari SELURUH
  dependency yang ter-bundle, bukan cuma kode kita sendiri — dampaknya kecil
  (~1MB) tapi tidak ada downside yang ditemukan, sudah ditest end-to-end
  dengan folder-folder ini dihapus secara manual dan hasil konversi tetap
  identik.
- `maxDuration` diset 120 detik di `vercel.json` — dokumen dengan banyak
  halaman hasil scan (perlu OCR) bisa makan waktu lama; sesuaikan kalau perlu
  lebih, tapi perhatikan limit plan Vercel kamu (Hobby plan punya limit durasi
  function yang lebih rendah dari Pro).
- Model OCR (RapidOCR varian default/"small") sudah ikut ter-bundle di dalam
  package `rapidocr` itu sendiri (~31MB, lihat `rapidocr/models/*.onnx`),
  TIDAK di-download saat runtime — beda dari asumsi awal project ini. Model
  varian lain (`medium`/`server`) baru di-download on-demand dari
  modelscope.cn saat pertama dipanggil, dan domain itu mungkin diblokir di
  beberapa environment sandbox/network terbatas.
