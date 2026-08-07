"""Bake app/assets/icons/app_icon.ico from the source artwork in
app/assets/icons/app_logo.png. Run once (or whenever the design changes):

    .venv\\Scripts\\python.exe generate_app_icon.py

Qt's ICO writer only ever keeps the last image passed to it (no
multi-resolution support), so this hand-assembles a real multi-size ICO
container (PNG-compressed frames, valid since Windows Vista) instead.

An icon must be SHAPED, not a picture: Windows composites it onto the title
bar, the taskbar, Alt+Tab and the Start tile, all of which can be light. The
artwork is exported flat on a near-black ground, and shipping that as-is put
a hard black square in every one of those places (the same bitmap also backs
the top-bar `LogoRoundel`, where it read as a cold black tile on the warm
panel). So the ground is keyed out to alpha here, and the result is trimmed
and re-centred: transparency alone would have left the mark floating in the
export's generous padding, i.e. a correct but conspicuously small icon.

Both steps are IDEMPOTENT — a source that already carries alpha is passed
through untouched — and when the source did need keying the keyed, trimmed
version is written back over app_logo.png, so the runtime asset and the .ico
never disagree about the shape. The flat original stays in git history.
"""
import os
import struct

from PySide6.QtCore import QBuffer, QIODevice, QRect, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QApplication

SIZES = (16, 24, 32, 48, 64, 128, 256)
SRC_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "app", "assets", "icons", "app_logo.png")
OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "app", "assets", "icons", "app_icon.ico")

# Luminance ramp that separates the export's ground from the mark. Measured,
# not guessed: every pixel of the artwork's ground lands at or below 4.51
# (the ground is a near-black radial vignette), the band up to 9 is EMPTY,
# and the darkest thing actually drawn — the terminal body inside the
# hexagon — starts around 10. Keying in that gap therefore cuts nothing that
# was drawn, and the ramp, rather than a hard threshold, is what keeps the
# mark's antialiased edges smooth instead of jagged. A plain global threshold
# is safe here for the same reason a flood fill would be: the only dark
# region INSIDE the mark sits above the gap, while the dark gaps between the
# honeycomb cells are ground showing through and are meant to be cut. Keep
# _KEY_LO clear of the ground's real maximum: at 4.0 the vignette's brightest
# pixels survived with alpha 1-26 and, being spread corner to corner, they
# handed `mark_bounds` the whole canvas and defeated the trim.
_KEY_LO, _KEY_HI = 5.0, 9.0
# Alpha a pixel needs before it counts as part of the mark for trimming. Not
# `> 0`, for the reason above — a bounding box must not be decidable by a
# stray pixel nobody can see.
_MARK_ALPHA = 24
# How much of the square canvas the mark's longest side fills. Icons need a
# little air at the edges; 92% is the usual amount.
_MARK_FILL = 0.92


def _luminance(rgb: int) -> float:
    return (0.2126 * ((rgb >> 16) & 0xFF) + 0.7152 * ((rgb >> 8) & 0xFF)
            + 0.0722 * (rgb & 0xFF))


def is_keyed(img: QImage) -> bool:
    """Does this artwork already carry a cut-out background? Read from the
    CORNERS, not `hasAlphaChannel` — that reports the format, and a flat
    export loaded into an ARGB format is opaque in every pixel."""
    w, h = img.width(), img.height()
    corners = ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1))
    return all(img.pixelColor(x, y).alpha() == 0 for x, y in corners)


def key_background(img: QImage) -> QImage:
    """Turn the near-black ground into transparency (see `_KEY_LO/_KEY_HI`).
    Works on the raw scanlines: per-pixel QImage access would be ~1.5M Python
    calls for this artwork."""
    img = img.convertToFormat(QImage.Format.Format_ARGB32)
    span = _KEY_HI - _KEY_LO
    for y in range(img.height()):
        line = img.scanLine(y)
        row = memoryview(line).cast("I")     # 0xAARRGGBB, host order
        for x in range(img.width()):
            lum = _luminance(row[x])
            if lum <= _KEY_LO:
                row[x] = 0                   # ground: fully cut, RGB zeroed
            elif lum < _KEY_HI:              # antialiased edge: partial alpha
                a = int(255 * (lum - _KEY_LO) / span)
                row[x] = (a << 24) | (row[x] & 0x00FFFFFF)
    return img


def mark_bounds(img: QImage) -> QRect:
    """The bounding box of everything visibly opaque (see `_MARK_ALPHA`)."""
    left, top = img.width(), img.height()
    right = bottom = -1
    for y in range(img.height()):
        row = memoryview(img.scanLine(y)).cast("I")
        for x in range(img.width()):
            if (row[x] >> 24) >= _MARK_ALPHA:
                if x < left:
                    left = x
                if x > right:
                    right = x
                if y < top:
                    top = y
                if y > bottom:
                    bottom = y
    if right < 0:
        return QRect(0, 0, img.width(), img.height())
    return QRect(left, top, right - left + 1, bottom - top + 1)


def trim_to_square(img: QImage) -> QImage:
    """Crop to the mark and re-centre it on a SQUARE transparent canvas.

    Square matters beyond looks: `build_ico` declares each frame's dimensions
    in the directory entry, and `render` scales KeepAspectRatio, so a
    non-square source would ship frames whose real size disagreed with what
    the header claims."""
    box = mark_bounds(img)
    side = round(max(box.width(), box.height()) / _MARK_FILL)
    out = QImage(side, side, QImage.Format.Format_ARGB32)
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.drawImage((side - box.width()) // 2, (side - box.height()) // 2,
                img, box.x(), box.y(), box.width(), box.height())
    p.end()
    return out


def render(size: int, source: QImage) -> QImage:
    return source.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)


def png_bytes(img: QImage) -> bytes:
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    return bytes(buf.data())


def build_ico(sizes, source: QImage, out_path: str) -> None:
    frames = [png_bytes(render(s, source)) for s in sizes]
    count = len(frames)
    header = struct.pack("<HHH", 0, 1, count)
    entries = b""
    offset = 6 + 16 * count
    for size, data in zip(sizes, frames):
        dim = size if size < 256 else 0  # 0 means 256 in ICO format
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32,
                                len(data), offset)
        offset += len(data)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "wb") as f:
        f.write(header)
        f.write(entries)
        for data in frames:
            f.write(data)


if __name__ == "__main__":
    app = QApplication.instance() or QApplication([])
    source = QImage(SRC_PATH)
    if source.isNull():
        raise SystemExit(f"could not load source artwork: {SRC_PATH}")
    if is_keyed(source):
        source = source.convertToFormat(QImage.Format.Format_ARGB32)
        print(f"source already has a cut-out background: {SRC_PATH}")
    else:
        source = trim_to_square(key_background(source))
        if not source.save(SRC_PATH, "PNG"):
            raise SystemExit(f"could not write keyed artwork: {SRC_PATH}")
        print(f"keyed + trimmed the flat export back into {SRC_PATH} "
              f"({source.width()}x{source.height()})")
    build_ico(SIZES, source, OUT_PATH)
    print(f"wrote {OUT_PATH}")
