"""Image reliefs (model kind "relief"): a lithophane or an embossed plate from a picture.

The picture is kept in the recipe as a small grayscale PNG (base64), so the model can be rebuilt,
adjusted and copied without any file. One grid vertex per pixel:
  lithophane: thickness from min_thickness (light) to max_thickness (dark) - hold it against a
              light; print it standing up, 100 % infill
  relief:     base + brightness * height (bright = high; invert flips it), e.g. a logo or a map
A border of full thickness frames the picture (lithophanes default to 3 mm).
"""
from __future__ import annotations

import base64
import io

import numpy as np

import fnshapes

MAX_PIXELS = 600            # longest side of the stored picture
MAX_IMAGE_BYTES = 1_500_000  # of the stored PNG (base64 of it in the recipe)
DEFAULTS = {
    "lithophane": {"width": 100.0, "min_thickness": 0.8, "max_thickness": 3.0, "border": 3.0},
    "relief": {"width": 80.0, "base": 1.5, "height": 2.0, "border": 0.0},
}


def prepare(data: bytes, pixels: int = 300) -> tuple[str, list[int]]:
    """An uploaded picture as the stored base64 PNG: grayscale, longest side `pixels`, the
    alpha channel flattened onto white. Runs on the server, before any build."""
    from PIL import Image, ImageOps
    if not 16 <= pixels <= MAX_PIXELS:
        raise ValueError(f"pixels must be 16-{MAX_PIXELS}")
    try:
        im = Image.open(io.BytesIO(data))
        if im.width * im.height > 50_000_000:
            raise ValueError("the picture is too large (over 50 megapixels)")
        im = ImageOps.exif_transpose(im)
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
            im = Image.alpha_composite(bg, im)
        im = im.convert("L")
    except ValueError:
        raise
    except Exception as e:  # noqa: BLE001 – Pillow's many decode errors
        raise ValueError(f"could not read the picture ({type(e).__name__}): use PNG or JPEG") from None
    im.thumbnail((pixels, pixels), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    if buf.tell() > MAX_IMAGE_BYTES:
        raise ValueError("the picture is too detailed - use fewer pixels")
    return base64.b64encode(buf.getvalue()).decode(), [im.width, im.height]


def _gray(params) -> np.ndarray:
    from PIL import Image
    try:
        raw = base64.b64decode(params["image"], validate=True)
    except (KeyError, ValueError, TypeError):
        raise ValueError("relief: 'image' must be the stored picture (base64 PNG)") from None
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError("relief: the stored picture is too large")
    im = Image.open(io.BytesIO(raw))
    if max(im.size) > MAX_PIXELS:
        raise ValueError(f"relief: the stored picture is over {MAX_PIXELS} pixels")
    return np.asarray(im.convert("L"), np.float64)[::-1] / 255.0    # row 0 = bottom (y up)


def _num(p, key, lo, hi):
    v = p.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
        raise ValueError(f"relief: '{key}' must be a number from {lo} to {hi}")
    return float(v)


def build(params: dict):
    mode = params.get("mode", "lithophane")
    if mode not in DEFAULTS:
        raise ValueError("relief: mode must be 'lithophane' or 'relief'")
    p = {**DEFAULTS[mode], **params}
    g = _gray(p)
    if p.get("invert"):
        g = 1 - g
    width = _num(p, "width", 5, 1000)
    border = _num(p, "border", 0, 100)
    if mode == "lithophane":
        lo, hi = _num(p, "min_thickness", 0.2, 50), _num(p, "max_thickness", 0.3, 50)
        if hi <= lo:
            raise ValueError("relief: max_thickness must be larger than min_thickness")
        Z = lo + (1 - g) * (hi - lo)            # dark = thick
        full = hi
    else:
        base, height = _num(p, "base", 0.2, 100), _num(p, "height", -100, 100)
        Z = base + g * height
        if Z.min() < 0.2:
            raise ValueError("relief: base + height must stay at least 0.2 mm everywhere")
        full = base + max(height, 0)
    rows, cols = g.shape
    step = width / max(cols - 1, 1)
    if border > 0:                              # a frame of full thickness around the picture
        b = max(1, int(round(border / step)))
        Z = np.pad(Z, b, constant_values=full)
        rows, cols = Z.shape
    fnshapes._check_tris("relief", 2 * rows * cols + 4 * (rows + cols))
    Y, X = np.meshgrid(np.arange(rows) * step, np.arange(cols) * step, indexing="ij")
    return {"model": fnshapes.height_solid("relief", X, Y, Z)}
