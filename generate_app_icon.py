"""Bake app/assets/icons/app_icon.ico from the hexagon glyph drawn in
main.py's _app_icon(). Run once (or whenever the design changes):

    .venv\\Scripts\\python.exe generate_app_icon.py

Qt's ICO writer only ever keeps the last image passed to it (no
multi-resolution support), so this hand-assembles a real multi-size ICO
container (PNG-compressed frames, valid since Windows Vista) instead.
"""
import os
import struct

from PySide6.QtCore import QBuffer, QIODevice, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter
from PySide6.QtWidgets import QApplication

from app.ui_theme import Palette

SIZES = (16, 24, 32, 48, 64, 128, 256)
OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "app", "assets", "icons", "app_icon.ico")


def render(size: int) -> QImage:
    img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(0)
    painter = QPainter(img)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    scale = size / 64
    painter.setBrush(QColor(Palette.BG_PANEL))
    painter.setPen(QColor(Palette.BORDER))
    inset = max(1.0, 1 * scale)
    painter.drawRoundedRect(QRectF(inset, inset, size - 2 * inset,
                                    size - 2 * inset), 12 * scale, 12 * scale)
    painter.setPen(QColor(Palette.ACCENT_ORANGE))
    font = QFont("Segoe UI Symbol", max(6, round(34 * scale)), 700)
    painter.setFont(font)
    painter.drawText(img.rect(), Qt.AlignmentFlag.AlignCenter, "⬡")
    painter.end()
    return img


def png_bytes(img: QImage) -> bytes:
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    return bytes(buf.data())


def build_ico(sizes, out_path: str) -> None:
    frames = [png_bytes(render(s)) for s in sizes]
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
    build_ico(SIZES, OUT_PATH)
    print(f"wrote {OUT_PATH}")
