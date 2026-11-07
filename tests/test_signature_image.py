"""Fixture signatures only. No stored doctor files are rewritten."""
import io
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from PIL import Image, ImageDraw
from app.signature_image import remove_signature_background

results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (("  -> " + str(extra)[:200]) if not cond else ""))


def png(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def load(raw):
    return Image.open(io.BytesIO(raw)).convert("RGBA")


def stroke(bg, ink, width=5):
    img = Image.new("RGBA", (140, 90), bg)
    draw = ImageDraw.Draw(img)
    draw.line((18, 50, 120, 28), fill=ink, width=width)
    draw.line((30, 62, 100, 70), fill=ink, width=width)
    return img


def measure(img):
    px = list(img.getdata())
    return {
        "transparent": sum(1 for p in px if p[3] < 24),
        "dark": sum(1 for p in px if p[3] > 80 and p[0] < 60),
        "grey_field": sum(1 for p in px if p[3] > 200 and 90 < p[0] < 180),
        "white_ink": sum(1 for p in px if p[3] > 200 and p[0] > 220),
        "n": len(px),
    }


dark = load(remove_signature_background(png(stroke((255, 255, 255, 255), (12, 12, 12, 255)))))
m = measure(dark)
check("dark ink on white keeps dark strokes", m["dark"] > 40, m)
check("dark ink paper is transparent", m["transparent"] > m["n"] * 0.7, m)
check("dark ink does not leave a grey field", m["grey_field"] == 0, m)

light = load(remove_signature_background(png(stroke((136, 136, 136, 255), (248, 248, 248, 255)))))
m = measure(light)
check("white on grey becomes dark strokes", m["dark"] > 40, m)
check("grey field is removed", m["grey_field"] == 0 and m["white_ink"] == 0, m)
check("white on grey background is transparent", m["transparent"] > m["n"] * 0.7, m)

clear = load(remove_signature_background(png(stroke((0, 0, 0, 0), (8, 8, 8, 255)))))
m = measure(clear)
check("transparent dark ink stays dark", m["dark"] > 40 and m["white_ink"] == 0, m)

white_clear = load(remove_signature_background(png(stroke((0, 0, 0, 0), (255, 255, 255, 255), 6))))
m = measure(white_clear)
check("transparent white ink is darkened, not dropped", m["dark"] > 40 and m["white_ink"] == 0, m)

blank = Image.new("RGBA", (80, 40), (255, 255, 255, 255))
kept = load(remove_signature_background(png(blank)))
m = measure(kept)
check("blank page is not given invented strokes", m["dark"] < 10, m)

failed = [name for name, ok in results if not ok]
print(f"\n{len(results) - len(failed)} passed, {len(failed)} failed")
if failed:
    sys.exit(1)
