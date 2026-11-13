#!/usr/bin/env python3
"""Cutest picture entry -- make.fun competition cutest-picture-01.

Draws a chubby baby kitten with the Python standard library only:
zlib + struct encode the PNG by hand, math does the geometry. No Pillow,
no network, no randomness, no clock reads -- running the program twice
writes byte-identical PNG files.

The kitten: big bright eyes, pink blush, a tiny pink bow on one ear,
a slight head tilt and clumsy front paws, drawn as clean flat vector-style
shapes on a soft pink-to-cream background. One clear subject, nothing
crowding it, so it still reads at ~300 px across.

Usage:
    mkdir -p out && MAKEFUN_OUTPUT_DIR=out python3 main.py
Writes <out>/entry.png (512x512, well under the 1 MiB limit).
Without MAKEFUN_OUTPUT_DIR it writes /output/entry.png, as the grader does.
"""

import math
import os
import struct
import zlib

SIZE = 512          # final image is SIZE x SIZE
SS = 2              # supersampling per axis (4 samples per final pixel)

# ---------------------------------------------------------------- palette
BG_TOP = (255, 217, 232)      # soft pink
BG_BOTTOM = (255, 243, 220)   # warm cream
FUR = (247, 178, 103)         # ginger
FUR_DEEP = (224, 138, 60)     # darker ginger stripes
OUTLINE = (214, 128, 55)      # warm outline for the fur
CREAM = (255, 246, 230)       # belly, muzzle, paws
INNER_EAR = (255, 159, 178)   # pink inner ears
NOSE = (232, 106, 122)        # pink nose
EYE = (59, 42, 38)            # dark brown eyes
MOUTH = (122, 74, 53)         # mouth / outline brown
BLUSH = (255, 158, 187)       # cheek blush
BOW = (255, 127, 168)         # pink bow
BOW_DARK = (232, 93, 138)     # bow knot / outline
WHISKER = (201, 143, 95)      # whiskers
SHADOW = (120, 70, 60)        # ground shadow
PAW_LINE = (216, 160, 106)    # toe lines
PAW_OUTLINE = (222, 190, 150) # paw outline
WHITE = (255, 255, 255)


# ---------------------------------------------------------------- canvas
class Canvas:
    """Final-resolution RGB buffer with alpha blending."""

    def __init__(self, size):
        self.size = size
        self.px = bytearray(size * size * 3)

    def blend(self, x, y, color, alpha):
        """Blend color (r, g, b) onto pixel (x, y); alpha 0..255."""
        if alpha <= 0:
            return
        i = (y * self.size + x) * 3
        if alpha >= 255:
            self.px[i], self.px[i + 1], self.px[i + 2] = color
        else:
            inv = 255 - alpha
            self.px[i] = (color[0] * alpha + self.px[i] * inv) // 255
            self.px[i + 1] = (color[1] * alpha + self.px[i + 1] * inv) // 255
            self.px[i + 2] = (color[2] * alpha + self.px[i + 2] * inv) // 255


class Rot:
    """Rotation of a local frame (origin at the head centre) about a pivot.

    Shapes are drawn in local coords (offsets from the head centre);
    fwd maps a local point to canvas coords, inv maps a canvas point
    back to local coords for the shape test.
    """

    def __init__(self, cx, cy, deg):
        t = math.radians(deg)
        self.c = math.cos(t)
        self.s = math.sin(t)
        self.cx = cx
        self.cy = cy

    def fwd(self, lx, ly):
        return (self.cx + self.c * lx - self.s * ly,
                self.cy + self.s * lx + self.c * ly)

    def inv(self, x, y):
        dx, dy = x - self.cx, y - self.cy
        return (self.c * dx + self.s * dy,
                -self.s * dx + self.c * dy)


def clamp01(v):
    return 0.0 if v < 0.0 else (1.0 if v > 1.0 else v)


# ------------------------------------------------------------ shape tests
# Each test(lx, ly) returns coverage 0..1 in local coords. Circles and
# ellipses use an analytic edge so curves stay smooth; polygons are binary
# and get their antialiasing from the supersampling in paint().

def circle_test(cx, cy, r):
    def test(x, y):
        return clamp01(r + 0.5 - math.hypot(x - cx, y - cy))
    return test


def ellipse_test(cx, cy, rx, ry):
    m = min(rx, ry)

    def test(x, y):
        n = math.sqrt(((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2)
        return clamp01((1.0 - n) * m + 0.5)
    return test


def polygon_test(points):
    def test(x, y):
        inside = False
        n = len(points)
        j = n - 1
        for i in range(n):
            xi, yi = points[i]
            xj, yj = points[j]
            if (yi > y) != (yj > y):
                if x < xi + (y - yi) * (xj - xi) / (yj - yi):
                    inside = not inside
            j = i
        return 1.0 if inside else 0.0
    return test


def _seg_dist(px, py, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1
    l2 = dx * dx + dy * dy
    if l2 == 0.0:
        return math.hypot(px - x1, py - y1)
    t = ((px - x1) * dx + (py - y1) * dy) / l2
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def polyline_test(points, width):
    hw = width / 2.0
    segs = list(zip(points, points[1:]))

    def test(x, y):
        best = 0.0
        for (x1, y1), (x2, y2) in segs:
            c = clamp01(hw + 0.5 - _seg_dist(x, y, x1, y1, x2, y2))
            if c > best:
                best = c
                if best >= 1.0:
                    break
        return best
    return test


# -------------------------------------------------------------- painters
def paint(cv, test, bbox, color, alpha=255, tr=None):
    """Paint a shape over bbox (x0, y0, x1, y1) in canvas coords.

    Each final pixel samples SS x SS subpixels; coverage is their mean.
    tr is an optional Rot applied to the shape's local coords.
    """
    x0, y0, x1, y1 = bbox
    x0 = max(0, x0)
    y0 = max(0, y0)
    x1 = min(cv.size, x1)
    y1 = min(cv.size, y1)
    step = 1.0 / SS
    for y in range(y0, y1):
        for x in range(x0, x1):
            cov = 0.0
            for sy in range(SS):
                py = y + (sy + 0.5) * step
                for sx in range(SS):
                    px = x + (sx + 0.5) * step
                    if tr is not None:
                        px, py = tr.inv(px, py)
                    cov += test(px, py)
            cov /= SS * SS
            if cov > 0.0:
                cv.blend(x, y, color, int(cov * alpha))


def tbbox(x0, y0, x1, y1, tr=None, pad=2):
    """Canvas bbox for a local rect, through an optional rotation."""
    corners = [(x0, y0), (x1, y0), (x0, y1), (x1, y1)]
    if tr is not None:
        corners = [tr.fwd(x, y) for (x, y) in corners]
    xs = [c[0] for c in corners]
    ys = [c[1] for c in corners]
    return (int(min(xs)) - pad, int(min(ys)) - pad,
            int(max(xs)) + pad + 1, int(max(ys)) + pad + 1)


def circle(cv, cx, cy, r, color, alpha=255, tr=None):
    # The test stays in local coords; only the bbox is transformed.
    paint(cv, circle_test(cx, cy, r),
          tbbox(cx - r, cy - r, cx + r, cy + r, tr),
          color, alpha, tr)


def ellipse(cv, cx, cy, rx, ry, color, alpha=255, tr=None):
    # The test stays in local coords; only the bbox is transformed.
    paint(cv, ellipse_test(cx, cy, rx, ry),
          tbbox(cx - rx, cy - ry, cx + rx, cy + ry, tr),
          color, alpha, tr)


def polygon(cv, points, color, alpha=255, tr=None):
    paint(cv, polygon_test(points), tbbox(
        min(p[0] for p in points), min(p[1] for p in points),
        max(p[0] for p in points), max(p[1] for p in points), tr),
        color, alpha, tr)


def polyline(cv, points, width, color, alpha=255, tr=None):
    paint(cv, polyline_test(points, width), tbbox(
        min(p[0] for p in points), min(p[1] for p in points),
        max(p[0] for p in points), max(p[1] for p in points), tr,
        pad=int(width) + 2),
        color, alpha, tr)


# -------------------------------------------------------------- geometry
def quad_bezier(p0, p1, p2, n=12):
    pts = []
    for i in range(n + 1):
        t = i / n
        u = 1.0 - t
        pts.append((u * u * p0[0] + 2 * u * t * p1[0] + t * t * p2[0],
                    u * u * p0[1] + 2 * u * t * p1[1] + t * t * p2[1]))
    return pts


def star_points(cx, cy, r_out, r_in, points=4):
    pts = []
    for i in range(points * 2):
        ang = math.pi * i / points - math.pi / 2
        r = r_out if i % 2 == 0 else r_in
        pts.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
    return pts


def rect_points(cx, cy, w, h, deg):
    t = math.radians(deg)
    c, s = math.cos(t), math.sin(t)
    hw, hh = w / 2.0, h / 2.0
    pts = []
    for (lx, ly) in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)):
        pts.append((cx + lx * c - ly * s, cy + lx * s + ly * c))
    return pts


def scale_about(points, cx, cy, k, dy=0.0):
    return [(cx + (x - cx) * k, cy + (y - cy) * k + dy) for (x, y) in points]


def fill_vertical_gradient(cv, top, bottom):
    for y in range(cv.size):
        t = y / (cv.size - 1)
        r = int(top[0] + (bottom[0] - top[0]) * t)
        g = int(top[1] + (bottom[1] - top[1]) * t)
        b = int(top[2] + (bottom[2] - top[2]) * t)
        base = y * cv.size * 3
        for x in range(cv.size):
            i = base + x * 3
            cv.px[i] = r
            cv.px[i + 1] = g
            cv.px[i + 2] = b


def radial_glow(cv, cx, cy, r, color, max_alpha):
    x0, y0 = int(cx - r), int(cy - r)
    x1, y1 = int(cx + r) + 1, int(cy + r) + 1
    for y in range(max(0, y0), min(cv.size, y1)):
        for x in range(max(0, x0), min(cv.size, x1)):
            d = math.hypot(x + 0.5 - cx, y + 0.5 - cy) / r
            if d < 1.0:
                a = int(max_alpha * (1.0 - d * d))
                if a > 0:
                    cv.blend(x, y, color, a)


def heart(cv, cx, cy, s, color, alpha=255):
    """Small heart: two circles plus a triangle, same color."""
    circle(cv, cx - 0.42 * s, cy - 0.18 * s, 0.45 * s, color, alpha)
    circle(cv, cx + 0.42 * s, cy - 0.18 * s, 0.45 * s, color, alpha)
    polygon(cv, [(cx - 0.85 * s, cy + 0.05 * s),
                 (cx + 0.85 * s, cy + 0.05 * s),
                 (cx, cy + 0.95 * s)], color, alpha)


# ---------------------------------------------------------------- drawing
def draw():
    cv = Canvas(SIZE)

    # Background: soft pink-to-cream gradient, white glow behind the kitten,
    # a few tiny stars and one small heart -- nothing crowding the subject.
    fill_vertical_gradient(cv, BG_TOP, BG_BOTTOM)
    radial_glow(cv, 256, 270, 215, WHITE, 55)
    for (x, y, ro, ri) in ((72, 84, 13, 5), (438, 66, 10, 4), (452, 178, 8, 3.5)):
        polygon(cv, star_points(x, y, ro, ri), WHITE, 200)
    heart(cv, 428, 128, 16, INNER_EAR, 220)

    # Ground shadow.
    ellipse(cv, 256, 466, 130, 18, SHADOW, 45)

    # Tail: tapered stroke curling up behind the body, cream tip.
    tail = [(340, 408), (374, 402), (396, 376), (390, 346), (370, 330)]
    for i in range(24):
        t = i / 23.0
        seg = int(t * (len(tail) - 1))
        u = t * (len(tail) - 1) - seg
        seg = min(seg, len(tail) - 2)
        x = tail[seg][0] + (tail[seg + 1][0] - tail[seg][0]) * u
        y = tail[seg][1] + (tail[seg + 1][1] - tail[seg][1]) * u
        r = 15.0 - 5.0 * t
        circle(cv, x, y, r + 3, OUTLINE)
    for i in range(24):
        t = i / 23.0
        seg = int(t * (len(tail) - 1))
        u = t * (len(tail) - 1) - seg
        seg = min(seg, len(tail) - 2)
        x = tail[seg][0] + (tail[seg + 1][0] - tail[seg][0]) * u
        y = tail[seg][1] + (tail[seg + 1][1] - tail[seg][1]) * u
        r = 15.0 - 5.0 * t
        circle(cv, x, y, r, FUR)
    circle(cv, 370, 330, 10, CREAM)

    # Body: outline, fill, cream belly, two side stripes.
    ellipse(cv, 256, 392, 120, 96, OUTLINE)
    ellipse(cv, 256, 392, 116, 92, FUR)
    ellipse(cv, 256, 414, 72, 58, CREAM)
    polygon(cv, rect_points(158, 396, 12, 26, 25), FUR_DEEP)
    polygon(cv, rect_points(354, 396, 12, 26, -25), FUR_DEEP)

    # Clumsy front paws, right one a touch higher than the left.
    for (px, py) in ((208, 456), (304, 452)):
        ellipse(cv, px, py, 35, 25, PAW_OUTLINE)
        ellipse(cv, px, py, 32, 22, CREAM)
        polyline(cv, [(px - 9, py - 2), (px - 9, py + 12)], 3, PAW_LINE)
        polyline(cv, [(px + 9, py - 2), (px + 9, py + 12)], 3, PAW_LINE)

    # Head group, tilted 10 degrees: ears, head, stripes, muzzle, eyes,
    # nose, mouth, blush, whiskers, bow. Local coords, origin at the
    # head centre (256, 228) in canvas space.
    head = Rot(256, 228, 10)

    # Ears: outline, fill, softened tip, pink inner ear.
    for ear in ([(-102, -48), (-80, -108), (-12, -80)],
                [(102, -48), (80, -108), (12, -80)]):
        cx = sum(p[0] for p in ear) / 3.0
        cy = sum(p[1] for p in ear) / 3.0
        polygon(cv, scale_about(ear, cx, cy, 1.08), OUTLINE, tr=head)
        polygon(cv, ear, FUR, tr=head)
        tip = max(ear, key=lambda p: -p[1])
        circle(cv, tip[0], tip[1], 6, FUR, tr=head)
        polygon(cv, scale_about(ear, cx, cy, 0.6, dy=10), INNER_EAR, tr=head)

    # Head: outline then fill.
    circle(cv, 0, 0, 112, OUTLINE, tr=head)
    circle(cv, 0, 0, 108, FUR, tr=head)

    # Forehead stripes.
    for (sx, sy, deg) in ((-32, -84, -14), (0, -90, 0), (32, -84, 14)):
        polygon(cv, rect_points(sx, sy, 12, 26, deg), FUR_DEEP, tr=head)

    # Cream muzzle.
    ellipse(cv, 0, 32, 52, 28, CREAM, tr=head)

    # Big bright eyes: dark discs with two white highlights each.
    for ex in (-46, 46):
        circle(cv, ex, -6, 27, EYE, tr=head)
        circle(cv, ex - 8, -15, 9, WHITE, tr=head)
        circle(cv, ex + 9, 3, 4.5, WHITE, tr=head)

    # Pink nose: rounded triangle.
    nose = [(-12, 20), (12, 20), (0, 34)]
    polygon(cv, nose, NOSE, tr=head)
    for (nx, ny) in nose:
        circle(cv, nx, ny, 4, NOSE, tr=head)

    # Little "w" mouth under the nose.
    polyline(cv, quad_bezier((0, 36), (-9, 49), (-20, 47)), 4, MOUTH, tr=head)
    polyline(cv, quad_bezier((0, 36), (9, 49), (20, 47)), 4, MOUTH, tr=head)

    # Blush.
    ellipse(cv, -76, 34, 22, 12, BLUSH, 150, tr=head)
    ellipse(cv, 76, 34, 22, 12, BLUSH, 150, tr=head)

    # Whiskers, two a side, kept short of the blush.
    for sgn in (-1, 1):
        for (y1, y2) in ((30, 20), (48, 60)):
            polyline(cv, [(sgn * 64, y1), (sgn * 100, y2)], 3,
                     WHISKER, 190, tr=head)

    # Tiny pink bow on the right ear.
    bow_cx, bow_cy = 70, -72
    for wing in ([(bow_cx, bow_cy), (48, -86), (48, -58)],
                 [(bow_cx, bow_cy), (92, -86), (92, -58)]):
        polygon(cv, scale_about(wing, bow_cx, bow_cy, 1.12), BOW_DARK, tr=head)
        polygon(cv, wing, BOW, tr=head)
    circle(cv, bow_cx, bow_cy, 7, BOW_DARK, tr=head)

    return cv


# ------------------------------------------------------------ PNG encoding
def encode_png(rgb, width, height):
    """Encode RGB bytes as a PNG (color type 2, filter 0 per row)."""
    raw = bytearray()
    stride = width * 3
    for y in range(height):
        raw.append(0)
        raw += rgb[y * stride:(y + 1) * stride]
    comp = zlib.compress(bytes(raw), 9)

    def chunk(tag, data):
        out = struct.pack(">I", len(data)) + tag + data
        out += struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        return out

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", comp) + chunk(b"IEND", b""))


def main():
    out_dir = os.environ.get("MAKEFUN_OUTPUT_DIR", "/output")
    os.makedirs(out_dir, exist_ok=True)
    cv = draw()
    data = encode_png(cv.px, SIZE, SIZE)
    path = os.path.join(out_dir, "entry.png")
    with open(path, "wb") as f:
        f.write(data)
    print("wrote %s (%d bytes, %dx%d)" % (path, len(data), SIZE, SIZE))


if __name__ == "__main__":
    main()
