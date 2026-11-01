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


def remove_signature_background(raw: bytes) -> bytes:
    """Return a cropped transparent PNG. Raises ValueError if image cannot be read."""
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

    # Paper colour: brightest samples from corners (handles tinted indoor lighting)
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
                samples.append((r, g, b))
    if not samples:
        raise ValueError("empty signature image")
    samples.sort(key=lambda t: _lum(*t), reverse=True)
    cut = max(1, int(len(samples) * 0.5))
    pr = sum(s[0] for s in samples[:cut]) / cut
    pg = sum(s[1] for s in samples[:cut]) / cut
    pb = sum(s[2] for s in samples[:cut]) / cut
    paper_lum = _lum(pr, pg, pb)
    # Indoor photos often have a blue/purple cast on the paper — subtract it
    # so we do not treat the whole page as "blue ink".
    paper_blue_bias = max(0.0, pb - max(pr, pg))

    alpha = [0] * (w * h)
    rgb = [(0, 0, 0)] * (w * h)
    for i, (r, g, b, _a) in enumerate(px):
        darkness = paper_lum - _lum(r, g, b)
        dist = ((r - pr) ** 2 + (g - pg) ** 2 + (b - pb) ** 2) ** 0.5
        blue_bias = max(0.0, b - max(r, g) - paper_blue_bias)

        # Ink / printed text is darker or chromatically different from paper.
        # Require real darkness for chroma shortcuts so tinted paper stays transparent.
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
            # Slight darken for clear print on white letterheads
            factor = 0.78 if darkness < 80 else 0.72
            rgb[i] = (max(0, int(r * factor)), max(0, int(g * factor)), max(0, int(b * factor)))
            alpha[i] = a_out

    # Drop speckles / show-through that are not part of a large ink component
    mask = [1 if a >= 36 else 0 for a in alpha]
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
        # Printed letters + signature strokes are large; dust is small
        if len(comp) >= 25:
            for c in comp:
                keep[c] = True

    out_px = []
    minx, miny, maxx, maxy = w, h, 0, 0
    ink_count = 0
    for i in range(w * h):
        if keep[i] and alpha[i]:
            r, g, b = rgb[i]
            out_px.append((r, g, b, alpha[i]))
            ink_count += 1
            # Crop from solid ink only — soft haze must not inflate the box
            if alpha[i] >= 48:
                x, y = i % w, i // w
                minx = min(minx, x)
                miny = min(miny, y)
                maxx = max(maxx, x)
                maxy = max(maxy, y)
        else:
            out_px.append((0, 0, 0, 0))

    if ink_count < 30:
        logger.warning("Signature background removal found little ink; keeping original")
        buf = io.BytesIO()
        Image.open(io.BytesIO(raw)).convert("RGBA").save(buf, format="PNG", optimize=True)
        return buf.getvalue()

    out = Image.new("RGBA", (w, h))
    out.putdata(out_px)
    pad = 12
    box = (max(0, minx - pad), max(0, miny - pad), min(w, maxx + 1 + pad), min(h, maxy + 1 + pad))
    out = out.crop(box)

    buf = io.BytesIO()
    out.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


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
