#!/usr/bin/env python3
"""make_qr.py - portable QR code generator (Cowork + Copilot Studio).

Required: Pillow. QR encoder: ReportLab (preferred) -> qrcode -> segno, whichever is installed.
PDF: ReportLab vector PDF, or a Pillow raster PDF when ReportLab is missing. SVG needs nothing extra.
Optional: OpenCV / zxing-cpp / pyzbar - if present, the output is decoded to verify it really scans.
Run `python make_qr.py --check` first to see what this environment supports.

Examples:
  python make_qr.py --type url --url https://contoso.com --format png --out out/site.png
  python make_qr.py --type wifi --ssid MyNet --password s3cret --auth WPA --format pdf --out out/wifi.pdf
  python make_qr.py --type url --url https://contoso.com --logo logo.png --style rounded --fg "#0B3D91" --format png,svg,pdf --out out/brand
  python make_qr.py --batch people.csv --data-column url --name-column name --format png --outdir out/batch
"""
import argparse, re, base64, csv, io, json, os, sys
from urllib.parse import quote
from urllib.parse import urlsplit

class QRError(Exception):
    """A user-fixable problem. Printed as JSON {"error": code, "message": ...} on stdout, exit code 2."""
    def __init__(self, code, message):
        super().__init__(message); self.code = code; self.message = message

try:
    from PIL import Image, ImageDraw, ImageColor, ImageFont
except ImportError:  # Pillow is the one hard requirement
    print(json.dumps({"error": "missing_pillow", "message": "Pillow (PIL) is not installed in this environment, so QR images cannot be drawn."}))
    sys.exit(2)

ENCODER = None
try:
    from reportlab.graphics.barcode import qrencoder
    ENCODER = "reportlab"
except ImportError:
    try:
        import qrcode as _qrcode
        ENCODER = "qrcode"
    except ImportError:
        try:
            import segno as _segno
            ENCODER = "segno"
        except ImportError:
            ENCODER = None
try:
    import reportlab  # noqa: F401  (vector PDF)
    HAS_REPORTLAB = True
except ImportError:
    HAS_REPORTLAB = False

EC_LEVELS = ["L", "M", "Q", "H"]
RASTER = {"png", "jpg", "jpeg", "webp"}
VECTOR = {"svg", "pdf"}

# ---------- payload builders ----------
def _esc(s):  # escaping for WIFI / MECARD style fields
    return "".join("\\" + c if c in '\\;,:"' else c for c in (s or ""))

def build_payload(a):
    t = a.type
    if t == "url":
        return normalize_url(a.url)
    if t == "text":
        return a.text
    if t == "wifi":
        auth = (a.auth or "WPA").upper()
        if auth in ("NONE", "OPEN", "NOPASS"):
            return f"WIFI:T:nopass;S:{_esc(a.ssid)};{'H:true;' if a.hidden else ''};"
        return f"WIFI:T:{auth};S:{_esc(a.ssid)};P:{_esc(a.password)};{'H:true;' if a.hidden else ''};"
    if t == "vcard":
        parts = (a.name or "").split(" ", 1)
        first, last = parts[0], (parts[1] if len(parts) > 1 else "")
        lines = ["BEGIN:VCARD", "VERSION:3.0", f"N:{last};{first};;;", f"FN:{a.name}"]
        if a.org: lines.append(f"ORG:{a.org}")
        if a.title: lines.append(f"TITLE:{a.title}")
        if a.phone: lines.append(f"TEL;TYPE=CELL:{a.phone}")
        if a.email: lines.append(f"EMAIL:{a.email}")
        if a.url: lines.append(f"URL:{a.url}")
        if a.address: lines.append(f"ADR:;;{a.address};;;;")
        lines.append("END:VCARD")
        return "\n".join(lines)
    if t == "email":
        q = []
        if a.subject: q.append("subject=" + quote(a.subject))
        if a.body: q.append("body=" + quote(a.body))
        return f"mailto:{a.email}" + ("?" + "&".join(q) if q else "")
    if t == "phone":
        return f"tel:{a.phone}"
    if t == "sms":
        return f"SMSTO:{a.phone}:{a.body or ''}"
    if t == "geo":
        return f"geo:{a.lat},{a.lon}"
    if t == "event":
        def dt(s): return s.replace("-", "").replace(":", "")
        lines = ["BEGIN:VEVENT", f"SUMMARY:{a.summary}", f"DTSTART:{dt(a.start)}", f"DTEND:{dt(a.end)}"]
        if a.location: lines.append(f"LOCATION:{a.location}")
        lines.append("END:VEVENT")
        return "\n".join(lines)
    raise SystemExit(f"Unknown type {t}")

# ---------- encoding (ReportLab) ----------
def encode(data, ec="M"):
    too_long = QRError("too_long", f"Content is too long for one QR code at error-correction {ec} "
                       f"({len(data.encode('utf-8'))} bytes). Shorten it (e.g. use a short link) or remove the logo.")
    if ENCODER is None:
        raise QRError("missing_encoder", "No QR encoder is installed (need ReportLab, qrcode or segno).")
    if ENCODER == "reportlab":
        lv = {"L": qrencoder.QRErrorCorrectLevel.L, "M": qrencoder.QRErrorCorrectLevel.M,
              "Q": qrencoder.QRErrorCorrectLevel.Q, "H": qrencoder.QRErrorCorrectLevel.H}[ec]
        q = qrencoder.QRCode(None, lv); q.addData(data)
        try:
            q.make()
        except Exception:
            raise too_long
        n = q.getModuleCount()
        return [[bool(q.isDark(r, c)) for c in range(n)] for r in range(n)], q.version
    if ENCODER == "qrcode":
        lv = {"L": _qrcode.constants.ERROR_CORRECT_L, "M": _qrcode.constants.ERROR_CORRECT_M,
              "Q": _qrcode.constants.ERROR_CORRECT_Q, "H": _qrcode.constants.ERROR_CORRECT_H}[ec]
        q = _qrcode.QRCode(version=None, error_correction=lv, border=0); q.add_data(data)
        try:
            q.make(fit=True)
        except Exception:
            raise too_long
        return [[bool(v) for v in row] for row in q.get_matrix()], q.version
    try:
        q = _segno.make(data, error=ec.lower(), boost_error=False, micro=False)
    except Exception:
        raise too_long
    return [[bool(v) for v in row] for row in q.matrix], q.version

def is_finder(r, c, n):
    return (r < 7 and c < 7) or (r < 7 and c >= n - 7) or (r >= n - 7 and c < 7)

# ---------- raster (Pillow) ----------
def _rgba(color):
    if color in (None, "transparent", "none"):
        return (255, 255, 255, 0)
    return ImageColor.getcolor(color, "RGBA")

# ---------- raster styling (Pillow) ----------
BODY_SHAPES = ["square", "rounded", "dots", "diamond", "fluid", "vertical-bars", "horizontal-bars", "small-squares"]
EYE_FRAMES = ["square", "rounded", "extra-rounded", "circle", "leaf"]
EYE_CENTERS = ["square", "rounded", "circle", "diamond", "leaf"]
FRAMES = ["none", "box", "rounded-box", "banner"]
SS = 4  # supersampling factor for smooth curves

def _finder_origins(n):
    return [(0, 0), (0, n - 7), (n - 7, 0)]

def _leaf_corners(idx):
    # (top_left, top_right, bottom_right, bottom_left); leaf points away from the code centre
    return [(True, False, True, False), (False, True, False, True), (False, True, False, True)][idx]

def _shape(d, box, kind, fill, idx=0):
    x0, y0, x1, y1 = box
    w = x1 - x0
    if kind == "square":
        d.rectangle(box, fill=fill)
    elif kind == "rounded":
        d.rounded_rectangle(box, radius=w * 0.25, fill=fill)
    elif kind == "extra-rounded":
        d.rounded_rectangle(box, radius=w * 0.42, fill=fill)
    elif kind == "circle":
        d.ellipse(box, fill=fill)
    elif kind == "leaf":
        try:
            d.rounded_rectangle(box, radius=w * 0.45, fill=fill, corners=_leaf_corners(idx))
        except TypeError:  # older Pillow without per-corner rounding
            d.rounded_rectangle(box, radius=w * 0.3, fill=fill)
    elif kind == "diamond":
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        d.polygon([(cx, y0), (x1, cy), (cx, y1), (x0, cy)], fill=fill)
    else:
        raise SystemExit(f"Unknown shape {kind}")

def render_code(m, size, border, fg, bg, body, eye_frame, eye_center, eye_color, logo, logo_scale):
    n = len(m); total = n + 2 * border
    box = max(1, size // total); b = box * SS; px = b * total
    bgc = _rgba(bg); fgc = _rgba(fg); eyec = _rgba(eye_color or fg)
    img = Image.new("RGBA", (px, px), bgc)
    d = ImageDraw.Draw(img)
    def in_f(r, c): return is_finder(r, c, n)
    def dark(r, c): return 0 <= r < n and 0 <= c < n and m[r][c] and not in_f(r, c)
    for r in range(n):
        for c in range(n):
            if not dark(r, c):
                continue
            x0, y0 = (c + border) * b, (r + border) * b
            x1, y1 = x0 + b - 1, y0 + b - 1
            if body == "square":
                d.rectangle([x0, y0, x1, y1], fill=fgc)
            elif body == "rounded":
                i = b * 0.04; d.rounded_rectangle([x0 + i, y0 + i, x1 - i, y1 - i], radius=b * 0.3, fill=fgc)
            elif body == "dots":
                i = b * 0.06; d.ellipse([x0 + i, y0 + i, x1 - i, y1 - i], fill=fgc)
            elif body == "diamond":
                cx, cy = x0 + b / 2, y0 + b / 2; h = b * 0.56
                d.polygon([(cx, cy - h), (cx + h, cy), (cx, cy + h), (cx - h, cy)], fill=fgc)
            elif body == "small-squares":
                i = b * 0.12; d.rectangle([x0 + i, y0 + i, x1 - i, y1 - i], fill=fgc)
            elif body == "fluid":
                d.ellipse([x0, y0, x1, y1], fill=fgc)
                if dark(r, c + 1): d.rectangle([x0 + b / 2, y0, x1 + b / 2, y1], fill=fgc)
                if dark(r + 1, c): d.rectangle([x0, y0 + b / 2, x1, y1 + b / 2], fill=fgc)
            elif body == "vertical-bars":
                i = b * 0.1; d.ellipse([x0 + i, y0 + i, x1 - i, y1 - i], fill=fgc)
                if dark(r + 1, c): d.rectangle([x0 + i, y0 + b / 2, x1 - i, y1 + b / 2], fill=fgc)
            elif body == "horizontal-bars":
                i = b * 0.1; d.ellipse([x0 + i, y0 + i, x1 - i, y1 - i], fill=fgc)
                if dark(r, c + 1): d.rectangle([x0 + b / 2, y0 + i, x1 + b / 2, y1 - i], fill=fgc)
            else:
                raise SystemExit(f"Unknown body shape {body}")
    hole = bgc if bgc[3] else (0, 0, 0, 0)
    for idx, (fr, fc) in enumerate(_finder_origins(n)):
        x0, y0 = (fc + border) * b, (fr + border) * b
        _shape(d, [x0, y0, x0 + 7 * b - 1, y0 + 7 * b - 1], eye_frame, eyec, idx)
        _shape(d, [x0 + b, y0 + b, x0 + 6 * b - 1, y0 + 6 * b - 1], eye_frame, hole, idx)
        _shape(d, [x0 + 2 * b, y0 + 2 * b, x0 + 5 * b - 1, y0 + 5 * b - 1], eye_center, eyec, idx)
    if logo:
        lg = Image.open(logo).convert("RGBA")
        target = int(n * b * logo_scale)
        k = target / max(lg.width, lg.height)  # scale up OR down to the requested size
        lg = lg.resize((max(1, int(lg.width * k)), max(1, int(lg.height * k))), Image.LANCZOS)
        pad = max(4 * SS, b)
        cx = cy = px // 2
        plate = [cx - lg.width // 2 - pad, cy - lg.height // 2 - pad, cx + lg.width // 2 + pad, cy + lg.height // 2 + pad]
        d.rounded_rectangle(plate, radius=pad, fill=bgc if bgc[3] else (255, 255, 255, 255))
        img.alpha_composite(lg, (cx - lg.width // 2, cy - lg.height // 2))
    return img.resize((box * total, box * total), Image.LANCZOS)

FONT_CANDIDATES = ("DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                   "LiberationSans-Bold.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
                   "Arial Bold.ttf", "arialbd.ttf", "C:/Windows/Fonts/arialbd.ttf")

def _font(sz):
    for f in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(f, sz)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=sz)  # Pillow >= 10.1
    except TypeError:
        return ImageFont.load_default()

def decorate(code, fg, bg, frame, frame_text, caption, frame_color=None):
    """Optional outer frame around the code, plus optional caption."""
    W = code.width; fgc = _rgba(fg); bgc = _rgba(bg); fc = _rgba(frame_color or fg)
    solid_bg = bgc if bgc[3] else (255, 255, 255, 255)
    img = code
    if frame != "none":
        t = max(6, W // 45); pad = t * 2
        banner_h = int(W * 0.16) if frame == "banner" else 0
        out = Image.new("RGBA", (W + 2 * (pad + t), W + 2 * (pad + t) + banner_h), bgc)
        d = ImageDraw.Draw(out)
        rect = [0, 0, out.width - 1, out.height - 1]
        r = 0 if frame == "box" else W // 12
        d.rounded_rectangle(rect, radius=r, fill=fc)
        inner_bottom = (W + 2 * pad + t - 1) if banner_h else (out.height - 1 - t)
        d.rounded_rectangle([t, t, out.width - 1 - t, inner_bottom], radius=max(0, r - t), fill=solid_bg)
        out.alpha_composite(code, (t + pad, t + pad))
        if banner_h:
            txt = (frame_text or "SCAN ME").upper(); fs = int(banner_h * 0.5); f = _font(fs)
            while d.textlength(txt, font=f) > out.width * 0.85 and fs > 10:
                fs -= 2; f = _font(fs)
            tw = d.textlength(txt, font=f)
            d.text(((out.width - tw) / 2, W + 2 * pad + t + (banner_h - fs) / 2 - fs * 0.1), txt, fill=solid_bg, font=f)
        img = out
    if caption:
        f = _font(max(12, W // 18)); cap_h = int(f.size * 1.8)
        canvas = Image.new("RGBA", (img.width, img.height + cap_h), bgc)
        canvas.alpha_composite(img, (0, 0))
        dd = ImageDraw.Draw(canvas); w = dd.textlength(caption, font=f)
        dd.text(((img.width - w) / 2, img.height + cap_h * 0.15), caption, fill=fgc, font=f)
        img = canvas
    return img

LOGO_SIZES = {"small": 0.15, "medium": 0.22, "large": 0.28}

def color_from_logo(path):
    """Pick the most common DARK colour in the logo (ignoring transparent/near-white/near-grey pixels)
    so the code matches the brand but keeps strong contrast on white."""
    im = Image.open(path).convert("RGBA"); im.thumbnail((200, 200))
    q = im.convert("RGB").quantize(colors=12, method=getattr(getattr(Image, "Quantize", None), "MEDIANCUT", 0))
    pal = q.getpalette(); alpha = im.split()[3].load(); qp = q.load(); counts = {}
    for y in range(im.height):
        for x in range(im.width):
            if alpha[x, y] < 128: continue
            i = qp[x, y]; counts[i] = counts.get(i, 0) + 1
    best = None
    for i, cnt in sorted(counts.items(), key=lambda kv: -kv[1]):
        r, g, b = pal[3 * i:3 * i + 3]
        lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
        if lum < 110:  # dark enough to scan on white
            best = (r, g, b); break
    if best is None:  # logo has no dark colour: darken its main colour
        i = max(counts, key=counts.get); r, g, b = pal[3 * i:3 * i + 3]
        f = 90 / max(1, 0.2126 * r + 0.7152 * g + 0.0722 * b); best = tuple(int(v * f) for v in (r, g, b))
    return "#%02X%02X%02X" % best

EC_CAPACITY = {"L": 7, "M": 15, "Q": 25, "H": 30}

def readback_check(code, m, size, border, ec):
    """Sample the rendered code at every data-module centre (finder eyes excluded - their shape is
    styled on purpose) and compare with the intended matrix. An ESTIMATE, not a real scanner."""
    n = len(m); total = n + 2 * border; box = max(1, size // total)
    g = Image.new("RGB", code.size, (255, 255, 255)); g.paste(code, mask=code.split()[3]); g = g.convert("L")
    px = g.load(); bad = 0; cnt = 0
    for r in range(n):
        for c in range(n):
            if is_finder(r, c, n): continue
            cnt += 1
            x = (c + border) * box + box // 2; y = (r + border) * box + box // 2
            if (px[x, y] < 128) != m[r][c]: bad += 1
    pct = 100.0 * bad / cnt; cap = EC_CAPACITY[ec]
    verdict = "good" if pct <= cap * 0.5 else ("risky" if pct <= cap * 0.8 else "likely to fail")
    return {"modules_obscured_pct": round(pct, 1), "error_correction_budget_pct": cap, "verdict": verdict}

def save_raster(img, path, fmt, bg):
    if fmt in ("jpg", "jpeg"):
        base = Image.new("RGB", img.size, _rgba(bg)[:3] if _rgba(bg)[3] else (255, 255, 255))
        base.paste(img, mask=img.split()[3])
        base.save(path, "JPEG", quality=95)
    elif fmt == "webp":
        img.save(path, "WEBP", lossless=True)
    else:
        img.save(path, "PNG")

# ---------- vector (ReportLab PDF, hand-built SVG) ----------
def render_svg(m, border, fg, bg, logo, logo_scale, path, size):
    n = len(m); total = n + 2 * border
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {total} {total}" width="{size}" height="{size}" shape-rendering="crispEdges">']
    if bg not in (None, "transparent", "none"):
        out.append(f'<rect width="{total}" height="{total}" fill="{bg}"/>')
    d = "".join(f"M{c+border},{r+border}h1v1h-1z" for r in range(n) for c in range(n) if m[r][c])
    out.append(f'<path d="{d}" fill="{fg}"/>')
    if logo:
        buf = io.BytesIO(); lg = Image.open(logo).convert("RGBA"); lg.save(buf, "PNG")
        lw = n * logo_scale; ratio = lg.height / lg.width
        w, h = lw, lw * ratio; x, y = total / 2 - w / 2, total / 2 - h / 2
        plate = "white" if bg in (None, "transparent", "none") else bg
        out.append(f'<rect x="{x-0.6:.3f}" y="{y-0.6:.3f}" width="{w+1.2:.3f}" height="{h+1.2:.3f}" rx="0.6" fill="{plate}"/>')
        out.append(f'<image x="{x:.3f}" y="{y:.3f}" width="{w:.3f}" height="{h:.3f}" href="data:image/png;base64,{base64.b64encode(buf.getvalue()).decode()}"/>')
    out.append("</svg>")
    open(path, "w").write("".join(out))

def render_pdf(m, border, fg, bg, logo, logo_scale, path, size_pts, caption):
    from reportlab.pdfgen import canvas
    from reportlab.lib.colors import HexColor, white
    from reportlab.lib.utils import ImageReader
    n = len(m); total = n + 2 * border; u = size_pts / total
    cap_h = 28 if caption else 0
    c = canvas.Canvas(path, pagesize=(size_pts, size_pts + cap_h))
    if bg not in (None, "transparent", "none"):
        c.setFillColor(HexColor(bg) if bg.startswith("#") else bg); c.rect(0, 0, size_pts, size_pts + cap_h, stroke=0, fill=1)
    c.setFillColor(HexColor(fg) if fg.startswith("#") else fg)
    for r in range(n):
        for col in range(n):
            if m[r][col]:
                c.rect((col + border) * u, cap_h + (total - border - r - 1) * u, u, u, stroke=0, fill=1)
    if logo:
        lg = Image.open(logo).convert("RGBA"); ratio = lg.height / lg.width
        w = n * u * logo_scale; h = w * ratio
        x, y = size_pts / 2 - w / 2, cap_h + size_pts / 2 - h / 2
        c.setFillColor(white if bg in (None, "transparent", "none") else HexColor(bg))
        c.roundRect(x - u, y - u, w + 2 * u, h + 2 * u, u, stroke=0, fill=1)
        c.drawImage(ImageReader(lg), x, y, w, h, mask="auto")
    if caption:
        c.setFillColor(HexColor(fg) if fg.startswith("#") else fg); c.setFont("Helvetica-Bold", 14)
        c.drawCentredString(size_pts / 2, 10, caption)
    c.save()

def render_pdf_pillow(m, border, fg, bg, logo, logo_scale, path, size_pts, caption):
    """PDF without ReportLab: a 300-dpi raster of the plain square code placed on a PDF page."""
    px = int(size_pts / 72 * 300)
    code = render_code(m, px, border, fg, bg if bg not in ("transparent", "none") else "#FFFFFF",
                       "square", "square", "square", None, logo, logo_scale)
    img = decorate(code, fg, bg if bg not in ("transparent", "none") else "#FFFFFF", "none", None, caption)
    img.convert("RGB").save(path, "PDF", resolution=300.0)

# ---------- verification (optional real decode) ----------
def decode_check(path, expected):
    """Decode the rendered image with any QR reader that happens to be installed. None if no reader."""
    try:
        import zxingcpp
        res = zxingcpp.read_barcodes(Image.open(path).convert("RGB"))
        got = res[0].text if res else None
        return {"engine": "zxing-cpp", "decoded": got is not None, "matches_content": got == expected}
    except ImportError:
        pass
    except Exception as e:
        return {"engine": "zxing-cpp", "decoded": False, "matches_content": False, "note": str(e)[:120]}
    try:
        import cv2, numpy as np
        img = np.array(Image.open(path).convert("RGB"))[:, :, ::-1]
        got, _, _ = cv2.QRCodeDetector().detectAndDecode(img)
        return {"engine": "opencv", "decoded": bool(got), "matches_content": got == expected}
    except ImportError:
        pass
    except Exception as e:
        return {"engine": "opencv", "decoded": False, "matches_content": False, "note": str(e)[:120]}
    try:
        from pyzbar.pyzbar import decode as zbar
        res = zbar(Image.open(path)); got = res[0].data.decode("utf-8") if res else None
        return {"engine": "pyzbar", "decoded": got is not None, "matches_content": got == expected}
    except ImportError:
        return None
    except Exception as e:
        return {"engine": "pyzbar", "decoded": False, "matches_content": False, "note": str(e)[:120]}

def environment_report():
    def has(mod):
        try:
            __import__(mod); return True
        except Exception:
            return False
    import PIL
    readers = [n for n, mod in (("zxing-cpp", "zxingcpp"), ("opencv", "cv2"), ("pyzbar", "pyzbar")) if has(mod)]
    font_ok = any(_try_font(f) for f in FONT_CANDIDATES)
    return {"ok": ENCODER is not None, "encoder": ENCODER, "pillow": PIL.__version__,
            "vector_pdf": HAS_REPORTLAB, "pdf": "vector" if HAS_REPORTLAB else "raster (Pillow)",
            "scan_verifier": readers[0] if readers else None, "truetype_font": font_ok,
            "styles": {"body": BODY_SHAPES, "eye_frame": EYE_FRAMES, "eye_center": EYE_CENTERS, "frame": FRAMES}}

def _try_font(f):
    try:
        ImageFont.truetype(f, 12); return True
    except OSError:
        return False

# ---------- input validation ----------
def normalize_url(raw):
    u = (raw or "").strip()
    if not u:
        raise QRError("missing_url", "A web address is required.")
    if any(ch.isspace() for ch in u):
        raise QRError("invalid_url", f"The web address \"{u}\" contains a space, so the code would open a broken link. Check the address and try again.")
    if not u.lower().startswith(("http://", "https://")):
        u = "https://" + u
    host = urlsplit(u).hostname or ""
    if "." not in host and host != "localhost":
        raise QRError("invalid_url", f"\"{raw}\" doesn't look like a web address (expected something like contoso.com).")
    return u

def check_color(name, value):
    if value in (None, "transparent", "none") or str(value).lower() == "logo":
        return
    try:
        ImageColor.getcolor(value, "RGBA")
    except ValueError:
        raise QRError("bad_color", f"\"{value}\" isn't a color I recognize for {name}. Use a hex code like #5C3A21 or a basic name like navy.")

def check_logo(path):
    if not os.path.isfile(path):
        raise QRError("logo_not_found", f"I couldn't find the logo file \"{path}\".")
    try:
        with Image.open(path) as im:
            im.verify()
    except Exception:
        if path.lower().endswith(".svg"):
            try:
                import cairosvg
                png = os.path.splitext(path)[0] + "_converted.png"
                cairosvg.svg2png(url=path, write_to=png, output_width=800)
                return png
            except ImportError:
                raise QRError("logo_svg", "SVG logos can't be read here. Please attach the logo as a PNG (preferably with a transparent background) or JPG.")
        raise QRError("logo_unreadable", f"The logo \"{os.path.basename(path)}\" couldn't be opened as an image. Please attach a PNG or JPG.")
    return path

UNSUPPORTED_GLYPH_START = 0x2E80  # CJK and later blocks are missing from the bundled fonts

def glyph_warning(label, text):
    if text and any(ord(ch) >= UNSUPPORTED_GLYPH_START for ch in text):
        return [f"The {label} contains characters (e.g. Chinese/Japanese/Korean or emoji) that may show as empty boxes - consider Latin text."]
    return []

def pad_to_size(img, size, bg):
    """Squares must be whole pixels, so the drawn code can be a few px under the requested size.
    Add the difference as extra quiet-zone margin (never stretch - that would blur the squares)."""
    if img.width >= size:
        return img
    out = Image.new("RGBA", (size, size), _rgba(bg))
    off = (size - img.width) // 2
    out.alpha_composite(img, (off, off))
    return out

def mask_secrets(payload):
    """Hide a Wi-Fi password in the JSON report (the QR code itself keeps the real password)."""
    if payload.upper().startswith("WIFI:"):
        return re.sub(r"(P:)((?:\\.|[^;])*)", lambda m_: m_.group(1) + ("********" if m_.group(2) else ""), payload)
    return payload

# ---------- driver ----------
def contrast_warning(fg, bg, label="code"):
    def lum(col):
        r, g, b, a = _rgba(col)
        if a == 0: return 1.0
        f = lambda v: (v / 255) / 12.92 if v / 255 <= 0.03928 else (((v / 255) + 0.055) / 1.055) ** 2.4
        return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)
    lf, lb = lum(fg), lum(bg)
    warn = []
    if lf > lb: warn.append(f"The {label} color is lighter than the background (inverted) - many scanners fail on inverted codes.")
    ratio = (max(lf, lb) + 0.05) / (min(lf, lb) + 0.05)
    if ratio < 4: warn.append(f"Low contrast for the {label} ({ratio:.1f}:1) - aim for 4:1 or higher.")
    return warn

def make_one(data, a, out_base):
    ec = a.ec
    warnings = []
    for nm, val in (("the code", a.fg), ("the background", a.bg), ("the corner squares", a.eye_color), ("the frame", a.frame_color)):
        check_color(nm, val)
    if a.logo:
        a.logo = check_logo(a.logo)
    if a.logo_size:
        a.logo_scale = LOGO_SIZES[a.logo_size]
    for attr in ("fg", "eye_color", "frame_color"):
        if (getattr(a, attr) or "").lower() == "logo":
            if not a.logo: raise QRError("no_logo", f"--{attr.replace('_','-')} logo needs --logo (a logo file to take the color from).")
            setattr(a, attr, color_from_logo(a.logo)); warnings.append(f"{attr} taken from logo: {getattr(a, attr)}")
    if a.logo:
        if ec != "H": warnings.append(f"Logo present: error correction raised from {ec} to H.")
        ec = "H"
        if a.logo_scale > 0.30:
            warnings.append("Logo scale capped at 0.30 of the code width to keep it scannable."); a.logo_scale = 0.30
    m, version = encode(data, ec)
    warnings += contrast_warning(a.fg, a.bg)
    if a.eye_color: warnings += contrast_warning(a.eye_color, a.bg, "corner squares")
    if a.frame != "none" and a.frame_color: warnings += contrast_warning(a.frame_color, a.bg, "frame")
    if a.border < 2:
        warnings.append(f"The white margin is only {a.border} square(s) wide - scanners need about 4. Use --border 4 unless the code will sit on a large plain white area.")
    elif a.border < 4:
        warnings.append(f"The white margin is {a.border} squares wide; 4 is the standard. Keep plenty of white space around the code when you place it.")
    warnings += glyph_warning("caption", a.caption) + (glyph_warning("banner text", a.frame_text) if a.frame == "banner" else [])
    fmts = [f.strip().lower() for f in a.format.split(",") if f.strip()]
    bad = [f for f in fmts if f not in RASTER | VECTOR]
    if bad or not fmts:
        raise QRError("bad_format", f"Unsupported format(s): {', '.join(bad) or '(none)'}. Choose from png, jpg, webp, svg, pdf.")
    module_px = a.size // (len(m) + 2 * a.border)
    if any(f in RASTER for f in fmts) and module_px < 4:
        warnings.append(f"Each square is only {module_px}px - the image is too small to scan reliably. Use --size 600 or more.")
    root, ext = os.path.splitext(out_base)
    if ext.lower().lstrip(".") not in RASTER | VECTOR:
        root = out_base  # keep dots that are part of the name (e.g. contoso.com-qr)
    os.makedirs(os.path.dirname(root) or ".", exist_ok=True)
    files = []
    raster_img = None
    for f in fmts:
        path = f"{root}.{f}"
        if f in RASTER:
            if raster_img is None:
                code_img = render_code(m, a.size, a.border, a.fg, a.bg, a.style, a.eye_frame, a.eye_center, a.eye_color, a.logo, a.logo_scale)
                readback = readback_check(code_img, m, a.size, a.border, ec)
                code_img = pad_to_size(code_img, a.size, a.bg)
                raster_img = decorate(code_img, a.fg, a.bg, a.frame, a.frame_text, a.caption, a.frame_color)
            if f in ("jpg", "jpeg") and a.bg in ("transparent", "none"):
                warnings.append("JPG cannot be transparent - used white background.")
            save_raster(raster_img, path, f, a.bg)
        elif f == "svg":
            if (a.style, a.eye_frame, a.eye_center, a.frame) != ("square", "square", "square", "none"): warnings.append("SVG is always plain square with no frame - shapes, eye styles and frames apply to PNG/JPG/WEBP.")
            render_svg(m, a.border, a.fg, a.bg, a.logo, a.logo_scale, path, a.size)
        elif f == "pdf":
            if (a.style, a.eye_frame, a.eye_center, a.frame) != ("square", "square", "square", "none"): warnings.append("PDF is always plain square with no frame - shapes, eye styles and frames apply to PNG/JPG/WEBP.")
            if HAS_REPORTLAB:
                render_pdf(m, a.border, a.fg, a.bg, a.logo, a.logo_scale, path, a.pdf_size, a.caption)
            else:
                render_pdf_pillow(m, a.border, a.fg, a.bg, a.logo, a.logo_scale, path, a.pdf_size, a.caption)
                warnings.append("ReportLab isn't installed here, so the PDF holds a high-resolution (300 dpi) image of the code rather than vector shapes - still fine for printing.")
        files.append(path)
    rb = locals().get("readback")
    dc = None
    first_raster = next((p for p in files if p.rsplit(".", 1)[-1] in RASTER), None)
    if first_raster:
        dc = decode_check(first_raster, data)
        if dc and not dc.get("matches_content"):
            warnings.append(f"A real scan test with {dc['engine']} could NOT read this code back correctly - simplify the styling, shrink the logo or increase contrast.")
    if rb and rb["verdict"] != "good":
        warnings.append(f"Readback check {rb['verdict']}: {rb['modules_obscured_pct']}% of modules obscured vs {rb['error_correction_budget_pct']}% budget - shrink the logo or simplify styling.")
    return {"payload": mask_secrets(data), "version": version, "readback_check": rb, "scan_verified": dc,
            "modules": len(m), "error_correction": ec, "encoder": ENCODER,
            "files": files, "warnings": list(dict.fromkeys(warnings))}

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--check", action="store_true", help="report what this environment supports, then exit")
    p.add_argument("--type", default="url", choices=["url", "text", "wifi", "vcard", "email", "phone", "sms", "geo", "event"])
    for k in ["url", "text", "ssid", "password", "auth", "name", "org", "title", "phone", "email", "address",
              "subject", "body", "lat", "lon", "summary", "start", "end", "location"]:
        p.add_argument(f"--{k}")
    p.add_argument("--hidden", action="store_true")
    p.add_argument("--format", default="png", help="comma list: png,jpg,webp,svg,pdf")
    p.add_argument("--out", default="qr")
    p.add_argument("--size", type=int, default=1000, help="raster pixel width / svg width")
    p.add_argument("--pdf-size", type=float, default=216, help="PDF code width in points (72 = 1 inch)")
    p.add_argument("--border", type=int, default=4)
    p.add_argument("--ec", default="M", choices=EC_LEVELS)
    p.add_argument("--fg", default="#000000", help='hex colour, or "logo" to match the logo\'s main dark colour'); p.add_argument("--bg", default="#FFFFFF")
    p.add_argument("--style", default="square", choices=BODY_SHAPES, help="data-module (body) shape")
    p.add_argument("--eye-frame", default="square", choices=EYE_FRAMES, help="outer ring of the 3 corner eyes")
    p.add_argument("--eye-center", default="square", choices=EYE_CENTERS, help="inner dot of the 3 corner eyes")
    p.add_argument("--eye-color", help="optional separate color for the corner eyes")
    p.add_argument("--frame", default="none", choices=FRAMES, help="optional outer frame around the code")
    p.add_argument("--frame-text", default="SCAN ME", help="text in the banner frame")
    p.add_argument("--frame-color", help="frame color (defaults to --fg)")
    p.add_argument("--logo"); p.add_argument("--logo-scale", type=float, default=0.22)
    p.add_argument("--logo-size", choices=list(LOGO_SIZES), help="small=0.15 (subtle), medium=0.22, large=0.28; overrides --logo-scale")
    p.add_argument("--caption")
    p.add_argument("--batch"); p.add_argument("--data-column", default="data"); p.add_argument("--name-column")
    p.add_argument("--outdir", default="qr_batch")
    a = p.parse_args()
    if a.check:
        print(json.dumps(environment_report(), indent=2)); return
    try:
        run(a)
    except QRError as e:
        print(json.dumps({"error": e.code, "message": e.message}, indent=2)); sys.exit(2)

def run(a):
    if a.batch:
        results = []
        with open(a.batch, newline="", encoding="utf-8-sig") as fh:
            for i, row in enumerate(csv.DictReader(fh), 1):
                data = (row.get(a.data_column) or "").strip()
                if not data: continue
                name = (row.get(a.name_column) if a.name_column else None) or f"qr_{i:03d}"
                safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name)[:60]
                try:
                    if a.type == "url" and not data.lower().startswith(("http://", "https://", "mailto:", "tel:", "wifi:", "smsto:", "geo:", "begin:")):
                        data = normalize_url(data)
                    results.append(make_one(data, a, os.path.join(a.outdir, safe)))
                except QRError as e:
                    results.append({"row": i, "name": name, "error": e.code, "message": e.message})
        ok = sum(1 for r in results if "error" not in r)
        print(json.dumps({"count": ok, "failed": len(results) - ok, "results": results}, indent=2))
    else:
        print(json.dumps(make_one(build_payload(a), a, a.out), indent=2))

if __name__ == "__main__":
    main()
