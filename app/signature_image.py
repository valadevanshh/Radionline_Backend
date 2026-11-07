"""Remove paper / photo background from doctor signature uploads.

Users typically photograph a pen signature (sometimes with printed name/degree
under it) on paper. We keep the ink strokes and printed text, make the paper
transparent, then crop tightly. Output is always PNG with an alpha channel.
"""
from __future__ import annotations

import base64
import io
import logging
import re
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

_DATA_URL_RE = re.compile(
    r"^data:(?P<mime>[\w/+\-.]+);base64,(?P<data>.+)$",
    re.DOTALL,
)


def _lum(r: float, g: float, b: float) -> float:
    return 0.299 * r + 0.587 * g + 0.114 * b


def _median(values: list) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return float(ordered[len(ordered) // 2])


def _crop_ink(img, pixels, alpha_floor: int = 48):
    """Crop to opaque-enough ink. Returns None when there is not enough ink."""
    w, h = img.size
    minx, miny, maxx, maxy = w, h, 0, 0
    count = 0
    for i, (_r, _g, _b, a) in enumerate(pixels):
        if a < alpha_floor:
            continue
        count += 1
        x, y = i % w, i // w
        minx = min(minx, x)
        miny = min(miny, y)
        maxx = max(maxx, x)
        maxy = max(maxy, y)
    if count < 30 or maxx < minx or maxy < miny:
        return None
    pad = 12
    box = (max(0, minx - pad), max(0, miny - pad), min(w, maxx + 1 + pad), min(h, maxy + 1 + pad))
    out = img.copy()
    out.putdata(pixels)
    return out.crop(box)


def _png_bytes(img) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _keep_components(alpha, w, h, threshold: int = 36, min_size: int = 25):
    mask = [1 if a >= threshold else 0 for a in alpha]
    visited = [False] * (w * h)
    keep = [False] * (w * h)
    for i in range(w * h):
        if not mask[i] or visited[i]:
            continue
        stack = [i]
        visited[i] = True
        comp = []
        while stack:
            cur = stack.pop()
            comp.append(cur)
            x, y = cur % w, cur // w
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    if dx == 0 and dy == 0:
                        continue
                    nx, ny = x + dx, y + dy
                    if 0 <= nx < w and 0 <= ny < h:
                        ni = ny * w + nx
                        if mask[ni] and not visited[ni]:
                            visited[ni] = True
                            stack.append(ni)
        if len(comp) >= min_size:
            for c in comp:
                keep[c] = True
    return keep


def _finish(raw: bytes, img, pixels) -> bytes:
    cropped = _crop_ink(img, pixels)
    if cropped is None:
        logger.warning("Signature background removal found little ink; keeping original")
        return _png_bytes(img)
    return _png_bytes(cropped)


def remove_signature_background(raw: bytes) -> bytes:
    """Return a cropped transparent PNG. Raises ValueError if image cannot be read.

    Dark ink on light paper stays dark. Light ink on a grey or dark field is kept
    as the same strokes, recoloured dark enough to read on a white report, and the
    field becomes transparent. An image that is already transparent is not repainted
    unless the strokes themselves are light. Too little ink leaves the pixels unchanged.
    """
    try:
        from PIL import Image
    except ImportError as e:
        raise RuntimeError("Pillow is required for signature background removal") from e

    img = Image.open(io.BytesIO(raw)).convert("RGBA")
    max_side = 1600
    if max(img.size) > max_side:
        img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)

    w, h = img.size
    px = list(img.getdata())
    if not px:
        raise ValueError("empty signature image")

    transparent = sum(1 for _r, _g, _b, a in px if a < 24)
    if transparent >= 0.35 * w * h:
        ink = [(r, g, b, a) for r, g, b, a in px if a >= 40]
        if len(ink) >= 30:
            mean = sum(_lum(r, g, b) for r, g, b, _a in ink) / len(ink)
            out_px = []
            for r, g, b, a in px:
                if a < 24:
                    out_px.append((0, 0, 0, 0))
                elif mean >= 170:
                    out_px.append((18, 18, 18, a))
                else:
                    out_px.append((r, g, b, a))
            return _finish(raw, img, out_px)

    samples = []
    for x0, y0, x1, y1 in (
        (0, 0, min(80, w), min(80, h)),
        (max(0, w - 80), 0, w, min(80, h)),
        (0, max(0, h - 80), min(80, w), h),
        (max(0, w - 80), max(0, h - 80), w, h),
    ):
        for y in range(y0, y1, 2):
            for x in range(x0, x1, 2):
                r, g, b, _a = px[y * w + x]
                samples.append(_lum(r, g, b))
    if not samples:
        raise ValueError("empty signature image")
    bg = _median(samples)

    if bg >= 165:
        # Dark ink on light paper. Sample the brightest corner pixels as the paper colour.
        colour_samples = []
        for x0, y0, x1, y1 in (
            (0, 0, min(80, w), min(80, h)),
            (max(0, w - 80), 0, w, min(80, h)),
            (0, max(0, h - 80), min(80, w), h),
            (max(0, w - 80), max(0, h - 80), w, h),
        ):
            for y in range(y0, y1, 2):
                for x in range(x0, x1, 2):
                    colour_samples.append(px[y * w + x][:3])
        colour_samples.sort(key=lambda t: _lum(*t), reverse=True)
        cut = max(1, int(len(colour_samples) * 0.5))
        pr = sum(s[0] for s in colour_samples[:cut]) / cut
        pg = sum(s[1] for s in colour_samples[:cut]) / cut
        pb = sum(s[2] for s in colour_samples[:cut]) / cut
        paper_lum = _lum(pr, pg, pb)
        paper_blue_bias = max(0.0, pb - max(pr, pg))
        alpha = [0] * (w * h)
        rgb = [(0, 0, 0)] * (w * h)
        for i, (r, g, b, _a) in enumerate(px):
            darkness = paper_lum - _lum(r, g, b)
            dist = ((r - pr) ** 2 + (g - pg) ** 2 + (b - pb) ** 2) ** 0.5
            blue_bias = max(0.0, b - max(r, g) - paper_blue_bias)
            ink = 0.0
            if darkness > 14:
                ink = max(ink, darkness)
            if darkness > 12 and dist > 28:
                ink = max(ink, dist * 0.55)
            if darkness > 18 and blue_bias > 8:
                ink = max(ink, blue_bias * 1.6 + darkness * 0.3)
            if ink <= 18:
                a_out = 0
            elif ink >= 70:
                a_out = 255
            else:
                a_out = int(round((ink - 18) / (70 - 18) * 255))
            if a_out < 18:
                a_out = 0
            if a_out:
                factor = 0.78 if darkness < 80 else 0.72
                rgb[i] = (max(0, int(r * factor)), max(0, int(g * factor)), max(0, int(b * factor)))
                alpha[i] = a_out
    else:
        # Light handwriting on a grey or dark field. Keep the stroke mask, drop the field.
        alpha = [0] * (w * h)
        rgb = [(18, 18, 18)] * (w * h)
        for i, (r, g, b, _a) in enumerate(px):
            lightness = _lum(r, g, b) - bg
            if lightness <= 18:
                continue
            a_out = 255 if lightness >= 70 else int(round((lightness - 18) / (70 - 18) * 255))
            if a_out < 18:
                continue
            alpha[i] = a_out

    keep = _keep_components(alpha, w, h)
    out_px = []
    for i in range(w * h):
        if keep[i] and alpha[i]:
            r, g, b = rgb[i]
            out_px.append((r, g, b, alpha[i]))
        else:
            out_px.append((0, 0, 0, 0))
    return _finish(raw, img, out_px)


def clean_signature_data_url(data_url: str) -> Optional[str]:
    """If `data_url` is an image data URL, return a cleaned PNG data URL; else None."""
    m = _DATA_URL_RE.match((data_url or "").strip())
    if not m:
        return None
    try:
        raw = base64.b64decode(m.group("data"), validate=False)
    except Exception:
        return None
    try:
        cleaned = remove_signature_background(raw)
    except Exception as e:
        logger.warning("Signature background removal failed: %s", e)
        return None
    b64 = base64.b64encode(cleaned).decode("ascii")
    return f"data:image/png;base64,{b64}"


def clean_signature_bytes(raw: bytes) -> Tuple[bytes, str]:
    """Return (png_bytes, 'image/png'). Falls back to original wrapped as PNG on failure."""
    try:
        return remove_signature_background(raw), "image/png"
    except Exception as e:
        logger.warning("Signature background removal failed: %s", e)
        try:
            from PIL import Image

            buf = io.BytesIO()
            Image.open(io.BytesIO(raw)).convert("RGBA").save(buf, format="PNG")
            return buf.getvalue(), "image/png"
        except Exception:
            return raw, "image/png"
