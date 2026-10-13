"""QR code helpers: read a QR code from a camera photo, and make QR images / printable ID cards."""
import io

import cv2
import numpy as np
import qrcode
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas


def decode_qr(image_bytes):
    """Return the text inside the first QR code found in the photo, or None."""
    arr = np.frombuffer(image_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return None
    det = cv2.QRCodeDetector()
    for candidate in (img, cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)):
        text, _, _ = det.detectAndDecode(candidate)
        if text:
            return text.strip()
    # try a sharpened / upscaled version for small or blurry codes
    big = cv2.resize(img, None, fx=1.6, fy=1.6, interpolation=cv2.INTER_CUBIC)
    text, _, _ = det.detectAndDecode(big)
    return text.strip() if text else None


def qr_png(text, box=8):
    img = qrcode.make(text, box_size=box, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _trips(s):
    from db import TRIPS
    t = [f"T{no}{'↑' if p == 'PICKUP' else '↓'}" for no, tr in TRIPS.items() for b, g, p in tr["movements"]
         if b == s.get("batch") and g == s.get("grp")]
    return "Trips: " + ", ".join(f"{x[:-1]} {'pickup' if x[-1] == '↑' else 'drop-off'}" for x in t)


def id_cards_pdf(students, school_name):
    """A4 sheet of student ID cards (2 x 5 per page) with QR codes, ready to print and laminate."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    W, H = A4
    cw, ch = 90 * mm, 52 * mm
    mx, my = (W - 2 * cw) / 3, 12 * mm
    gap_y = (H - 2 * my - 5 * ch) / 4
    for i, s in enumerate(students):
        k = i % 10
        if i and k == 0:
            c.showPage()
        col, row = k % 2, k // 2
        x = mx + col * (cw + mx)
        y = H - my - (row + 1) * ch - row * gap_y
        c.setStrokeColorRGB(0.75, 0.75, 0.75)
        c.roundRect(x, y, cw, ch, 4 * mm)
        c.setFillColorRGB(0.11, 0.33, 0.55)
        c.roundRect(x, y + ch - 10 * mm, cw, 10 * mm, 4 * mm, fill=1, stroke=0)
        c.rect(x, y + ch - 10 * mm, cw, 5 * mm, fill=1, stroke=0)
        c.setFillColorRGB(1, 1, 1)
        c.setFont("Helvetica-Bold", 8.5)
        c.drawString(x + 4 * mm, y + ch - 6.5 * mm, school_name[:48])
        c.setFillColorRGB(0, 0, 0)
        c.setFont("Helvetica-Bold", 10.5)
        c.drawString(x + 4 * mm, y + ch - 17 * mm, s["name"][:28])
        c.setFont("Helvetica", 8.5)
        lines = [f"ID: {s['code']}   {s.get('class_name') or ''}{s.get('section') or ''}",
                 f"Batch {s.get('batch')} {s.get('grp') or ''} · Bus {s.get('bus_no') or ''}", f"Stop: {(s.get('stop_name') or '')[:30]}",
                 _trips(s)]
        for j, line in enumerate(lines):
            c.drawString(x + 4 * mm, y + ch - (23 + 5 * j) * mm, line)
        c.drawImage(ImageReader(io.BytesIO(qr_png(s["badge"], box=6))), x + cw - 36 * mm, y + 4 * mm, 32 * mm, 32 * mm)
        c.setFont("Helvetica", 6.5)
        c.setFillColorRGB(0.35, 0.35, 0.35)
        c.drawString(x + 4 * mm, y + 3 * mm, f"Badge {s['badge'][:20]} · scan at every check-in / check-out")
        c.setFillColorRGB(0, 0, 0)
    c.save()
    return buf.getvalue()
