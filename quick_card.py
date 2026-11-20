import io
import os
from typing import Optional
from PIL import Image, ImageDraw, ImageFont

W, H = 1524, 1310
SS = 2

BG = (17, 17, 20)
CARD = (29, 29, 33)
ROW = (40, 40, 46)
BTN = (44, 44, 50)
WHITE = (247, 248, 250)
GRAY = (150, 154, 163)
YELLOW = (255, 214, 24)
YELLOW_SOFT = (255, 226, 92)
BLACK = (18, 18, 20)

_BOLD_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
]
_REG_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
]
_FONT_CACHE = {}


def _font(size: int, bold: bool = False):
    key = (size, bold)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    paths = _BOLD_PATHS if bold else _REG_PATHS
    for p in paths:
        if os.path.exists(p):
            try:
                res = (ImageFont.truetype(p, size), 0)
                _FONT_CACHE[key] = res
                return res
            except Exception:
                pass
    try:
        f = ImageFont.load_default(size=size)
        res = (f, max(1, size // 28) if bold else 0)
    except TypeError:
        res = (ImageFont.load_default(), 0)
    _FONT_CACHE[key] = res
    return res


def _text(d, x, y, txt, size, fill, bold=False, align="l"):
    if txt is None or txt == "":
        return 0
    font, stroke = _font(int(size * SS), bold)
    bb = d.textbbox((0, 0), str(txt), font=font, stroke_width=stroke)
    w = bb[2] - bb[0]
    px = x * SS
    if align == "r":
        px -= w
    elif align == "c":
        px -= w / 2
    d.text((px - bb[0], y * SS - bb[1]), str(txt), font=font, fill=fill, stroke_width=stroke, stroke_fill=fill)
    return w / SS


def _rr(d, box, r, fill):
    x1, y1, x2, y2 = box
    d.rounded_rectangle([x1 * SS, y1 * SS, x2 * SS, y2 * SS], radius=r * SS, fill=fill)


def _poly(d, pts, fill):
    d.polygon([(x * SS, y * SS) for x, y in pts], fill=fill)


def _diamond(d, cx, cy, w, h, fill):
    top = cy - h / 2
    _poly(d, [(cx - w / 2, top + h * 0.22), (cx - w * 0.28, top), (cx + w * 0.28, top), (cx + w / 2, top + h * 0.22),
              (cx, cy + h / 2)], fill)


def _cover(src: Image.Image, w: int, h: int) -> Image.Image:
    src = src.convert("RGB")
    sw, sh = src.size
    scale = max(w / sw, h / sh)
    nw, nh = max(w, int(round(sw * scale))), max(h, int(round(sh * scale)))
    im = src.resize((nw, nh), Image.Resampling.LANCZOS)
    l, t = (nw - w) // 2, (nh - h) // 2
    return im.crop((l, t, l + w, t + h))


def _paste_art(canvas, art, box, radius, corners=None):
    x1, y1, x2, y2 = [int(v * SS) for v in box]
    w, h = x2 - x1, y2 - y1
    if art is None:
        return
    im = _cover(art, w, h)
    mask = Image.new("L", (w, h), 0)
    md = ImageDraw.Draw(mask)
    if corners is None:
        md.rounded_rectangle([0, 0, w - 1, h - 1], radius=radius * SS, fill=255)
    else:
        md.rounded_rectangle([0, 0, w - 1, h - 1], radius=radius * SS, fill=255, corners=corners)
    canvas.paste(im, (x1, y1), mask)


def _fmt(val) -> str:
    if val is None:
        return ""
    try:
        return f"{round(float(val), 2):g}"
    except Exception:
        return str(val)


def _cart(d, cx, cy, fill):
    pts = [(cx - 18, cy - 14), (cx - 11, cy - 14), (cx - 6, cy + 6), (cx + 13, cy + 6)]
    d.line([(x * SS, y * SS) for x, y in pts], fill=fill, width=int(3 * SS), joint="curve")
    d.line([((cx - 9) * SS, (cy - 7) * SS), ((cx + 16) * SS, (cy - 7) * SS), ((cx + 13) * SS, (cy + 5) * SS)],
           fill=fill, width=int(2.5 * SS))
    for ex in (cx - 4, cx + 11):
        d.ellipse([(ex - 3.5) * SS, (cy + 10) * SS, (ex + 3.5) * SS, (cy + 17) * SS], fill=fill)


def _clipboard(d, cx, cy):
    _rr(d, (cx - 46, cy - 52, cx + 46, cy + 66), 16, BLACK)
    _rr(d, (cx - 22, cy - 66, cx + 22, cy - 38), 10, BLACK)
    for i, yy in enumerate((cy - 22, cy + 8)):
        _rr(d, (cx - 28, yy, cx - 14, yy + 14), 5, YELLOW)
        _rr(d, (cx - 4, yy + 3, cx + 30, yy + 11), 4, YELLOW)


def _small_card(d, canvas, box, item, art, default_title):
    x1, y1, x2, y2 = box
    _rr(d, box, 36, CARD)
    art_h = x2 - x1
    if item is None:
        return
    _paste_art(canvas, art, (x1, y1, x2, y1 + art_h), 36, corners=(True, True, False, False))
    title = str(item.get("title") or default_title or "")[:18]
    num = f"#{item['number']}" if item.get("number") else ""
    _text(d, x1 + 22, y1 + art_h + 18, title, 36, WHITE, bold=True)
    _text(d, x1 + 22, y1 + art_h + 64, num, 28, GRAY)
    py1, py2 = y2 - 78, y2 - 12
    _rr(d, (x1 + 18, py1, x2 - 104, py2), 16, BTN)
    price = _fmt(item.get("price"))
    _diamond(d, x1 + 62, (py1 + py2) / 2, 34, 28, GRAY)
    _text(d, x1 + 98, py1 + 13, price, 38, WHITE, bold=True)
    _rr(d, (x2 - 92, py1, x2 - 18, py2), 16, BTN)
    _cart(d, x2 - 55, (py1 + py2) / 2, GRAY)


def build_card_png(data: dict, main_art: Optional[Image.Image], sub_arts: list) -> bytes:
    canvas = Image.new("RGB", (W * SS, H * SS), BG)
    d = ImageDraw.Draw(canvas)

    _rr(d, (28, 28, 628, 1236), 36, CARD)
    _paste_art(canvas, main_art, (52, 52, 604, 604), 30)
    if main_art is not None:
        _rr(d, (490, 52, 590, 108), 18, YELLOW)
        _text(d, 540, 66, "MRKT", 28, BLACK, bold=True, align="c")

    title = str(data.get("title") or data.get("slug") or "")[:20]
    _text(d, 56, 626, title, 58, WHITE, bold=True)
    if data.get("number"):
        _text(d, 58, 700, f"#{data['number']}", 38, GRAY, bold=True)

    rows = [("Model", data.get("model"), data.get("model_rarity")),
            ("Backdrop", data.get("backdrop"), data.get("backdrop_rarity")),
            ("Symbol", data.get("symbol"), data.get("symbol_rarity"))]
    y = 766
    for label, val, rar in rows:
        _rr(d, (52, y, 604, y + 92), 16, ROW)
        _text(d, 74, y + 12, label, 23, GRAY)
        _text(d, 74, y + 42, str(val or "—")[:20], 34, WHITE, bold=True)
        if rar:
            _text(d, 582, y + 46, str(rar), 29, YELLOW, bold=True, align="r")
        y += 104

    _rr(d, (52, 1088, 116, 1152), 16, ROW)
    for i, (xa, xb) in enumerate(((68, 100), (74, 94), (80, 88))):
        yy = 1104 + i * 16
        d.line([(xa * SS, yy * SS), (xb * SS, yy * SS)], fill=WHITE, width=int(3 * SS))
    if data.get("model"):
        _rr(d, (128, 1088, 400, 1152), 16, ROW)
        chip_w = _text(d, 152, 1104, str(data["model"])[:16], 28, YELLOW, bold=True)
        cx0 = 152 + chip_w + 22
        d.line([(cx0 * SS, 1112 * SS), ((cx0 + 14) * SS, 1126 * SS)], fill=YELLOW, width=int(3 * SS))
        d.line([((cx0 + 14) * SS, 1112 * SS), (cx0 * SS, 1126 * SS)], fill=YELLOW, width=int(3 * SS))

    _rr(d, (652, 28, 1062, 620), 36, CARD)
    _rr(d, (652, 28, 1062, 438), 36, YELLOW)
    d.rectangle([652 * SS, 400 * SS, 1062 * SS, 438 * SS], fill=YELLOW)
    _rr(d, (988, 52, 1040, 104), 14, YELLOW_SOFT)
    d.ellipse([1002 * SS, 66 * SS, 1026 * SS, 90 * SS], fill=BLACK)
    _text(d, 1014, 67, "!", 22, YELLOW, bold=True, align="c")
    _clipboard(d, 857, 172)

    quick = data.get("quick_ton")
    if quick is not None:
        qs = _fmt(quick)
        font, stroke = _font(int(88 * SS), True)
        bb = d.textbbox((0, 0), qs, font=font, stroke_width=stroke)
        tw = (bb[2] - bb[0]) / SS
        total = tw + 70
        left = 857 - total / 2
        _diamond(d, left + 24, 338, 48, 40, BLACK)
        _text(d, left + 70, 288, qs, 88, BLACK, bold=True)
    else:
        _text(d, 857, 296, "—", 88, BLACK, bold=True, align="c")
    _text(d, 857, 372, "Any gift of this collection", 27, (46, 40, 10), align="c")

    _rr(d, (670, 460, 1044, 520), 16, BTN)
    _text(d, 857, 474, "All Orders", 30, WHITE, align="c")
    _rr(d, (670, 534, 1044, 600), 16, YELLOW)
    _text(d, 857, 550, "Quick sell", 32, BLACK, bold=True, align="c")

    cheap = data.get("cheapest_three") or []
    boxes = [(1086, 28, 1496, 620), (652, 644, 1062, 1236), (1086, 644, 1496, 1236)]
    for i, box in enumerate(boxes):
        item = cheap[i] if i < len(cheap) else None
        art = sub_arts[i] if i < len(sub_arts) else None
        _small_card(d, canvas, box, item, art, title)

    _text(d, 34, 1256, "MRKT · Quick sell", 27, GRAY)
    _text(d, 1490, 1256, "Giftpulse", 27, GRAY, bold=True, align="r")

    out = canvas.resize((W, H), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    out.save(buf, format="JPEG", quality=92, optimize=True)
    return buf.getvalue()
